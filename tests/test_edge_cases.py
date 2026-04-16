"""Edge-case and regression tests.

Modeled on Hopper's ``test_trust.py``: narrow, adversarial tests that check
behavior when inputs are malformed, filesystems are weird, or concurrency
happens. Each class covers one category of hazard.
"""

import json
import os
import threading

import pytest

from gate.cleanup import _trim_jsonl
from gate.io import atomic_write, atomic_write_bytes
from gate.logger import log_review, read_recent_decisions
from gate.quota import _read_cache, _write_cache
from gate.state import (
    get_fix_attempts,
    get_pr_state_dir,
    load_prior_review,
    record_fix_attempt,
)

# ── Atomic write helpers ─────────────────────────────────────


class TestAtomicWrite:
    def test_writes_content(self, tmp_path):
        target = tmp_path / "out.txt"
        atomic_write(target, "hello")
        assert target.read_text() == "hello"

    def test_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "a" / "b" / "c" / "out.txt"
        atomic_write(target, "nested")
        assert target.read_text() == "nested"

    def test_overwrites_existing(self, tmp_path):
        target = tmp_path / "out.txt"
        target.write_text("old")
        atomic_write(target, "new")
        assert target.read_text() == "new"

    def test_no_tmp_file_on_success(self, tmp_path):
        target = tmp_path / "out.txt"
        atomic_write(target, "x")
        tmp = target.with_suffix(".txt.tmp")
        assert not tmp.exists()

    def test_cleans_tmp_on_failure(self, tmp_path, monkeypatch):
        target = tmp_path / "out.txt"

        def boom(*a, **k):
            raise RuntimeError("rename failed")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(RuntimeError):
            atomic_write(target, "x")
        assert not target.with_suffix(".txt.tmp").exists()

    def test_bytes_variant(self, tmp_path):
        target = tmp_path / "out.bin"
        atomic_write_bytes(target, b"\x00\x01\x02")
        assert target.read_bytes() == b"\x00\x01\x02"

    def test_unicode_content(self, tmp_path):
        target = tmp_path / "out.txt"
        atomic_write(target, "hello -> worl\u00f0 emoji\U0001f600")
        assert "worl\u00f0" in target.read_text()


# ── JSONL corruption tolerance ───────────────────────────────


class TestCorruptJsonl:
    def test_read_recent_decisions_skips_bad_lines(self, tmp_path):

        jsonl = tmp_path / "reviews.jsonl"
        jsonl.write_text(
            '{"decision": "approve"}\n'
            "{not json at all}\n"
            '{"decision": "error"}\n'
            "\n"
            '{"decision": "approve"}\n'
        )
        from unittest.mock import patch

        with patch("gate.logger.reviews_jsonl", lambda: jsonl):
            recent = read_recent_decisions(count=10)
        assert recent == ["approve", "error", "approve"]

    def test_partial_last_line_does_not_crash(self, tmp_path):
        from unittest.mock import patch

        jsonl = tmp_path / "reviews.jsonl"
        jsonl.write_text('{"decision": "approve"}\n{"decision": "app')  # truncated
        with patch("gate.logger.reviews_jsonl", lambda: jsonl):
            recent = read_recent_decisions(count=5)
        assert "approve" in recent

    def test_trim_jsonl_tolerates_bad_lines(self, tmp_path):
        jsonl = tmp_path / "reviews.jsonl"
        lines = [json.dumps({"i": i}) for i in range(10)]
        lines[3] = "{bad"
        jsonl.write_text("\n".join(lines) + "\n")
        _trim_jsonl(jsonl, max_lines=5)
        result_lines = jsonl.read_text().strip().split("\n")
        assert len(result_lines) == 5


class TestCorruptQuotaCache:
    def test_read_cache_handles_corrupt_json(self, tmp_path, monkeypatch):
        cache = tmp_path / "quota-cache.json"
        cache.write_text("{not json")
        monkeypatch.setattr("gate.quota.quota_cache_path", lambda: cache)
        assert _read_cache() is None

    def test_read_cache_handles_missing_keys(self, tmp_path, monkeypatch):
        cache = tmp_path / "quota-cache.json"
        cache.write_text('{"cached_at": "2024-01-01T00:00:00+00:00"}')
        monkeypatch.setattr("gate.quota.quota_cache_path", lambda: cache)
        # Missing five_hour / seven_day keys — should still return usable dict
        result = _read_cache()
        # Expired (2024 is old) returns None
        assert result is None or isinstance(result, dict)

    def test_write_then_read_roundtrip(self, tmp_path, monkeypatch):
        cache = tmp_path / "quota-cache.json"
        monkeypatch.setattr("gate.quota.quota_cache_path", lambda: cache)
        _write_cache({"five_hour": {"utilization": 50}, "seven_day": {"utilization": 30}})
        result = _read_cache()
        assert result is not None
        assert result["five_hour"]["utilization"] == 50


# ── State edge cases ─────────────────────────────────────────


class TestPartialState:
    def test_prior_review_with_only_verdict(self, tmp_path, tmp_workspace, monkeypatch):
        monkeypatch.setattr("gate.state.state_dir", lambda: tmp_path)
        pr_dir = tmp_path / "pr42"
        pr_dir.mkdir(parents=True)
        (pr_dir / "verdict.json").write_text(
            json.dumps({"decision": "approve", "findings": []})
        )
        result = load_prior_review(42, tmp_workspace)
        assert result["has_prior"] is True
        assert result["prior_sha"] == ""  # missing last_sha.txt
        assert result["review_count"] == 0

    def test_prior_review_with_corrupt_verdict(self, tmp_path, tmp_workspace, monkeypatch):
        monkeypatch.setattr("gate.state.state_dir", lambda: tmp_path)
        pr_dir = tmp_path / "pr42"
        pr_dir.mkdir(parents=True)
        (pr_dir / "verdict.json").write_text("{garbage")
        result = load_prior_review(42, tmp_workspace)
        assert result["has_prior"] is False

    def test_counters_with_non_numeric_content(self, tmp_path, monkeypatch):
        monkeypatch.setattr("gate.state.state_dir", lambda: tmp_path)
        pr_dir = tmp_path / "pr42"
        pr_dir.mkdir(parents=True)
        (pr_dir / "fix_attempts.txt").write_text("not a number\n")
        result = get_fix_attempts(42)
        assert result["soft"] == 0


class TestSequentialFixAttempts:
    """Per-PR counter correctness under sequential bumps.

    Real concurrency across reviews for the same PR is impossible because
    ``ReviewQueue`` serialises per PR; we verify the sequential correctness
    contract (matches what the queue guarantees in production).
    """

    def test_monotonic_increment(self, tmp_path, monkeypatch):
        monkeypatch.setattr("gate.state.state_dir", lambda: tmp_path)
        for _ in range(10):
            record_fix_attempt(42)
        result = get_fix_attempts(42)
        assert result["soft"] == 10
        assert result["total"] == 10

    def test_threaded_serialized_writes(self, tmp_path, monkeypatch):
        """Queue-like serialisation: each thread holds a lock while writing."""
        monkeypatch.setattr("gate.state.state_dir", lambda: tmp_path)
        lock = threading.Lock()

        def bump():
            with lock:
                record_fix_attempt(42)

        threads = [threading.Thread(target=bump) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        result = get_fix_attempts(42)
        assert result["soft"] == 20


class TestMultiRepoIsolation:
    def test_same_pr_different_repos(self, tmp_path, monkeypatch):
        monkeypatch.setattr("gate.state.state_dir", lambda: tmp_path)
        d1 = get_pr_state_dir(42, repo="org/repo-a")
        d2 = get_pr_state_dir(42, repo="org/repo-b")
        assert d1 != d2
        record_fix_attempt(42, repo="org/repo-a")
        a = get_fix_attempts(42, repo="org/repo-a")
        b = get_fix_attempts(42, repo="org/repo-b")
        assert a["soft"] == 1
        assert b["soft"] == 0


# ── Unicode handling ─────────────────────────────────────────


class TestUnicode:
    def test_log_review_with_unicode_repo(self, tmp_path, monkeypatch):
        monkeypatch.setattr("gate.logger.logs_dir", lambda: tmp_path)
        verdict = {
            "decision": "approve",
            "confidence": "high",
            "findings": [],
            "stats": {},
        }
        log_review(42, verdict, None, 60, repo="\u00f4rg/r\u00e9po")
        jsonl = tmp_path / "reviews.jsonl"
        entry = json.loads(jsonl.read_text().strip())
        assert entry["repo"] == "\u00f4rg/r\u00e9po"

    def test_atomic_write_with_control_chars(self, tmp_path):
        content = "line1\nline2\tindented\n\x00null-byte\n"
        atomic_write(tmp_path / "out.txt", content)
        assert (tmp_path / "out.txt").read_text() == content


# ── Symlinks ─────────────────────────────────────────────────


class TestSymlinks:
    def test_atomic_write_through_symlinked_dir(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        target = link / "out.txt"
        atomic_write(target, "hello")
        assert (real / "out.txt").read_text() == "hello"

    def test_atomic_write_replaces_symlink_target(self, tmp_path):
        """Writing to a path that is itself a symlink replaces the link with a file.

        Documents the actual behavior: since ``os.replace`` renames the tmp
        sibling over the path, the symlink is unlinked and replaced. Callers
        that want to preserve symlinks must resolve the target first.
        """
        real = tmp_path / "real.txt"
        real.write_text("original")
        link = tmp_path / "link.txt"
        link.symlink_to(real)
        atomic_write(link, "new")
        assert link.read_text() == "new"
        assert real.read_text() == "original"
        assert not link.is_symlink()


# ── Config edge cases ────────────────────────────────────────


class TestConfigRobustness:
    def test_unknown_keys_ignored(self, tmp_path):
        from gate import config as cfg

        toml = tmp_path / "gate.toml"
        toml.write_text(
            '[repo]\nname = "x/y"\nfrobnicate = "hello"\n'
            "[future_section]\nkey = 1\n"
        )
        cfg.GATE_DIR = tmp_path.parent  # won't be used
        # Parse directly, since load_config looks at gate_dir()/config/gate.toml
        import tomllib

        parsed = tomllib.loads(toml.read_text())
        assert parsed["repo"]["name"] == "x/y"
        assert parsed["repo"]["frobnicate"] == "hello"  # unknown key preserved
        assert parsed["future_section"]["key"] == 1

    def test_missing_gate_toml_returns_empty(self):
        # conftest copied the real toml in; remove it to simulate missing
        import gate.config as cfg
        from gate.config import load_config
        toml_path = cfg.gate_dir() / "config" / "gate.toml"
        if toml_path.exists():
            toml_path.unlink()
        assert load_config() == {}


# ── Empty / boundary inputs ──────────────────────────────────


class TestBoundaryInputs:
    def test_log_review_empty_findings(self, tmp_path, monkeypatch):
        monkeypatch.setattr("gate.logger.logs_dir", lambda: tmp_path)
        verdict = {"decision": "approve", "findings": [], "stats": {}}
        log_review(0, verdict, None, 0, repo="")
        jsonl = tmp_path / "reviews.jsonl"
        assert jsonl.exists()
        entry = json.loads(jsonl.read_text().strip())
        assert entry["findings"] == 0
        assert entry["review_time_seconds"] == 0

    def test_read_recent_decisions_empty_file(self, tmp_path, monkeypatch):
        jsonl = tmp_path / "reviews.jsonl"
        jsonl.write_text("")
        monkeypatch.setattr("gate.logger.reviews_jsonl", lambda: jsonl)
        assert read_recent_decisions() == []

    def test_trim_jsonl_fewer_than_max(self, tmp_path):
        jsonl = tmp_path / "reviews.jsonl"
        lines = [json.dumps({"i": i}) for i in range(3)]
        jsonl.write_text("\n".join(lines) + "\n")
        _trim_jsonl(jsonl, max_lines=10)
        # Unchanged
        assert len(jsonl.read_text().strip().split("\n")) == 3


# ── data_dir isolation verification ──────────────────────────


class TestDataDirIsolation:
    """Paranoid checks that the conftest isolation actually works."""

    def test_data_dir_is_redirected(self):
        from gate.config import data_dir

        resolved = data_dir().resolve()
        # Must NOT be under the real home (we'd be polluting real user data)
        assert "Library/Application Support/gate" not in str(resolved)
        assert ".local/share/gate" not in str(resolved)
        # And must not contain real_home unless it's a tmp subpath
        assert "pytest" in str(resolved) or "tmp" in str(resolved).lower()

    def test_state_dir_inside_data_dir(self):
        from gate.config import data_dir, state_dir

        assert data_dir() in state_dir().parents or data_dir() == state_dir().parent

    def test_logs_dir_inside_data_dir(self):
        from gate.config import data_dir, logs_dir

        assert data_dir() == logs_dir().parent

    def test_socket_path_inside_data_dir(self):
        from gate.config import data_dir, socket_path

        assert data_dir() == socket_path().parent

    def test_gate_dir_is_install_not_data(self):
        from gate.config import data_dir, gate_dir

        assert gate_dir() != data_dir()

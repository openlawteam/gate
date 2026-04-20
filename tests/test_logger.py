"""Tests for gate.logger module."""

import json
from unittest.mock import patch

from gate.logger import (
    log_fix_result,
    log_review,
    read_recent_decisions,
    write_live_log,
    write_sidecar_meta,
)


def _patch_logs(logs_dir):
    """Return a patch context that redirects logger paths to logs_dir."""
    return patch("gate.logger.logs_dir", lambda: logs_dir)


def _patch_live(live_dir_path):
    return patch("gate.logger.live_dir", lambda: live_dir_path)


def _patch_reviews(jsonl):
    return patch("gate.logger.reviews_jsonl", lambda: jsonl)


class TestLogReview:
    def test_appends_to_jsonl(self, tmp_path):
        logs_dir = tmp_path / "logs"
        jsonl = logs_dir / "reviews.jsonl"

        with _patch_logs(logs_dir):
            verdict = {
                "decision": "approve",
                "confidence": "high",
                "findings": [],
                "stats": {"total_findings": 0, "stages_run": 4},
            }
            log_review(42, verdict, None, 120)

            assert jsonl.exists()
            entry = json.loads(jsonl.read_text().strip())
            assert entry["pr"] == 42
            assert entry["decision"] == "approve"
            assert entry["review_time_seconds"] == 120
            assert entry["mode"] == "enforcement"

    def test_multiple_entries(self, tmp_path):
        logs_dir = tmp_path / "logs"
        jsonl = logs_dir / "reviews.jsonl"

        with _patch_logs(logs_dir):
            for i in range(3):
                log_review(
                    i, {"decision": "approve", "findings": [], "stats": {}}, None, 60
                )
            lines = jsonl.read_text().strip().split("\n")
            assert len(lines) == 3

    def test_includes_repo_field(self, tmp_path):
        logs_dir = tmp_path / "logs"
        jsonl = logs_dir / "reviews.jsonl"

        with _patch_logs(logs_dir):
            verdict = {"decision": "approve", "findings": [], "stats": {}}
            log_review(42, verdict, None, 60, repo="org/repo")
            entry = json.loads(jsonl.read_text().strip())
            assert entry["repo"] == "org/repo"


class TestReadRecentDecisions:
    def test_empty_file(self, tmp_path):
        jsonl = tmp_path / "reviews.jsonl"
        with _patch_reviews(jsonl):
            assert read_recent_decisions() == []

    def test_reads_last_n(self, tmp_path):
        jsonl = tmp_path / "reviews.jsonl"
        entries = [
            json.dumps({"decision": "approve"}),
            json.dumps({"decision": "error"}),
            json.dumps({"decision": "error"}),
            json.dumps({"decision": "error"}),
        ]
        jsonl.write_text("\n".join(entries) + "\n")

        with _patch_reviews(jsonl):
            recent = read_recent_decisions(3)
            assert recent == ["error", "error", "error"]


class TestWriteLiveLog:
    def test_creates_log_file(self, tmp_path):
        live = tmp_path / "live"
        with _patch_live(live):
            write_live_log(42, "Triage starting", "stage")
            log_file = live / "pr42.log"
            assert log_file.exists()
            content = log_file.read_text()
            assert "Triage starting" in content
            assert "[stage]" in content

    def test_repo_namespaced_log(self, tmp_path):
        live = tmp_path / "live"
        with _patch_live(live):
            write_live_log(42, "Starting review", "stage", repo="org/repo")
            log_file = live / "org-repo" / "pr42.log"
            assert log_file.exists()
            assert "Starting review" in log_file.read_text()

    def test_no_repo_uses_flat(self, tmp_path):
        live = tmp_path / "live"
        with _patch_live(live):
            write_live_log(42, "Starting review", "stage")
            log_file = live / "pr42.log"
            assert log_file.exists()


class TestLogFixResult:
    def test_fix_result_includes_elapsed(self, tmp_path):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        jsonl = logs_dir / "reviews.jsonl"

        with _patch_logs(logs_dir):
            log_fix_result(
                42, True, "Fixed 3 issues", "request_changes",
                repo="org/repo", fix_elapsed_seconds=120,
            )
            entry = json.loads(jsonl.read_text().strip())
            assert entry["review_time_seconds"] == 120
            assert entry["decision"] == "fix_succeeded"
            assert entry["is_fix_followup"] is True
            assert entry["fix_summary"] == "Fixed 3 issues"

    def test_fix_result_default_elapsed(self, tmp_path):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        jsonl = logs_dir / "reviews.jsonl"

        with _patch_logs(logs_dir):
            log_fix_result(42, False, "Build failed", "request_changes")
            entry = json.loads(jsonl.read_text().strip())
            assert entry["review_time_seconds"] == 0
            assert entry["decision"] == "fix_failed"

    def test_fix_result_serialises_hopper_kwargs(self, tmp_path):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        jsonl = logs_dir / "reviews.jsonl"

        with _patch_logs(logs_dir):
            log_fix_result(
                17, True, "hopper ok", "approve_with_notes",
                pipeline_mode="hopper",
                sub_scope_total=3, sub_scope_committed=2,
                sub_scope_reverted=1, sub_scope_empty=0,
                wall_clock_seconds=900,
                runaway_guard_hit=False,
                fixed_count=5, not_fixed_count=2,
            )
            entry = json.loads(jsonl.read_text().strip())
            assert entry["pipeline_mode"] == "hopper"
            assert entry["sub_scope_total"] == 3
            assert entry["sub_scope_committed"] == 2
            assert entry["sub_scope_reverted"] == 1
            assert entry["sub_scope_empty"] == 0
            assert entry["wall_clock_seconds"] == 900
            assert entry["fixed_count"] == 5
            assert entry["not_fixed_count"] == 2
            # runaway_guard_hit is False → should be serialised when explicitly
            # passed so we can diff "ran to completion" from "never populated".
            assert entry["runaway_guard_hit"] is False

    def test_fix_result_omits_hopper_kwargs_when_not_set(self, tmp_path):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        jsonl = logs_dir / "reviews.jsonl"

        with _patch_logs(logs_dir):
            log_fix_result(99, True, "ok", "approve_with_notes")
            entry = json.loads(jsonl.read_text().strip())
            for key in (
                "pipeline_mode", "sub_scope_total", "sub_scope_committed",
                "sub_scope_reverted", "sub_scope_empty", "wall_clock_seconds",
                "runaway_guard_hit", "fixed_count", "not_fixed_count",
            ):
                assert key not in entry


class TestWriteSidecarMeta:
    def test_writes_meta_file(self, tmp_path):
        meta = {"stage": "triage", "elapsed_seconds": 30, "model": "sonnet"}
        write_sidecar_meta(tmp_path, "triage", meta)
        path = tmp_path / "triage_meta.json"
        assert path.exists()
        loaded = json.loads(path.read_text())
        assert loaded["stage"] == "triage"


# ── write_sidecar_meta error handling (review warning regression) ──


class TestWriteSidecarMetaErrorHandling:
    """Regression: write_sidecar_meta must not propagate filesystem
    errors to callers (it's a diagnostic-only helper).
    """

    def test_oserror_is_swallowed(self, tmp_path, monkeypatch, caplog):
        import logging
        bad_path = tmp_path / "nonexistent-dir" / "does" / "not" / "exist"
        bad_path.mkdir(parents=True)  # but we'll make the file write fail by

        # Monkeypatch write_text to raise OSError
        from pathlib import Path as _Path

        real_write = _Path.write_text

        def fail(self, *args, **kwargs):
            if self.name.endswith("_meta.json"):
                raise OSError("disk full")
            return real_write(self, *args, **kwargs)

        monkeypatch.setattr(_Path, "write_text", fail)

        with caplog.at_level(logging.WARNING, logger="gate.logger"):
            # Must not raise
            write_sidecar_meta(bad_path, "triage", {"k": "v"})
        assert any("write_sidecar_meta failed" in rec.message for rec in caplog.records)

"""Unit tests for gate.audit (PR B.1 + B.2)."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

from gate.audit import list_contradictions, retro_scan


def _write_archive(
    pr_dir,
    timestamp: str,
    sha: str,
    suffix: str,
    verdict: dict,
    build: dict | None = None,
):
    reviews_dir = pr_dir / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    archive = reviews_dir / f"{timestamp}-{sha}-{suffix}"
    archive.mkdir()
    (archive / "verdict.json").write_text(json.dumps(verdict))
    if build is not None:
        (archive / "build.json").write_text(json.dumps(build))
    return archive


class TestRetroScan:
    def test_no_hits_on_clean_archives(self, tmp_path):
        repo_dir = tmp_path / "org-repo" / "pr10"
        _write_archive(
            repo_dir, "20260422T120000Z", "abc123ab", "pre-fix",
            verdict={"decision": "approve", "findings": []},
            build={
                "lint": {"pass": True},
                "tests": {"pass": True},
                "typecheck": {"pass": True},
            },
        )
        with patch("gate.audit.state_dir", lambda: tmp_path):
            hits = retro_scan()
        assert hits == []

    def test_flags_silent_approval_on_lint_failure(self, tmp_path):
        repo_dir = tmp_path / "org-repo" / "pr11"
        _write_archive(
            repo_dir, "20260422T121500Z", "def456cd", "pre-fix",
            verdict={"decision": "approve", "findings": []},
            build={
                "lint": {"pass": False, "exit_code": 1},
                "tests": {"pass": True},
            },
        )
        with patch("gate.audit.state_dir", lambda: tmp_path):
            hits = retro_scan()
        assert len(hits) == 1
        hit = hits[0]
        assert hit["decision"] == "approve"
        assert any("lint.pass=false" in r for r in hit["reasons"])

    def test_flags_parse_failure(self, tmp_path):
        repo_dir = tmp_path / "org-repo" / "pr12"
        _write_archive(
            repo_dir, "20260422T122200Z", "ab11ab11", "post-fix",
            verdict={"decision": "approve_with_notes", "findings": []},
            build={"lint": {"pass": True, "parse_failure": True}},
        )
        with patch("gate.audit.state_dir", lambda: tmp_path):
            hits = retro_scan()
        assert len(hits) == 1
        assert any("parse_failure" in r for r in hits[0]["reasons"])

    def test_skips_request_changes_verdicts(self, tmp_path):
        repo_dir = tmp_path / "org-repo" / "pr13"
        _write_archive(
            repo_dir, "20260422T122500Z", "12345678", "pre-fix",
            verdict={"decision": "request_changes", "findings": []},
            build={"lint": {"pass": False}},
        )
        with patch("gate.audit.state_dir", lambda: tmp_path):
            hits = retro_scan()
        assert hits == []

    def test_skips_archives_with_missing_build(self, tmp_path):
        repo_dir = tmp_path / "org-repo" / "pr14"
        _write_archive(
            repo_dir, "20260422T122700Z", "aaaabbbb", "pre-fix",
            verdict={"decision": "approve", "findings": []},
            build=None,
        )
        with patch("gate.audit.state_dir", lambda: tmp_path):
            hits = retro_scan()
        assert hits == []


class TestListContradictions:
    def _write_contradiction(self, tmp_path, pr: int, when: float, name: str) -> None:
        c_dir = tmp_path / f"pr{pr}" / "contradictions"
        c_dir.mkdir(parents=True, exist_ok=True)
        fname = c_dir / f"20260422T120000Z-{name}.json"
        fname.write_text(json.dumps({
            "pr": pr,
            "check": {"name": name, "conclusion": "failure"},
        }))
        import os as _os
        _os.utime(fname, (when, when))

    def test_empty_dir(self, tmp_path):
        with patch("gate.audit.state_dir", lambda: tmp_path):
            assert list_contradictions() == []

    def test_returns_newest_first(self, tmp_path):
        now = time.time()
        self._write_contradiction(tmp_path, 1, when=now - 3600, name="alpha")
        self._write_contradiction(tmp_path, 1, when=now - 60, name="beta")
        with patch("gate.audit.state_dir", lambda: tmp_path):
            hits = list_contradictions()
        assert [h["name"].split("-", 1)[1] for h in hits] == ["beta", "alpha"]

    def test_since_seconds_filter(self, tmp_path):
        now = time.time()
        self._write_contradiction(tmp_path, 2, when=now - 10 * 86400, name="old")
        self._write_contradiction(tmp_path, 2, when=now - 60, name="recent")
        with patch("gate.audit.state_dir", lambda: tmp_path):
            hits = list_contradictions(since_seconds=3600)
        assert len(hits) == 1
        assert "recent" in hits[0]["name"]

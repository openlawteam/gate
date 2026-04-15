"""Tests for gate.cleanup module."""

import json
import time
from unittest.mock import patch

from gate.cleanup import _compress_file, _trim_jsonl, cleanup_logs, cleanup_state


class TestTrimJsonl:
    def test_trims_long_file(self, tmp_path):
        path = tmp_path / "test.jsonl"
        lines = [json.dumps({"n": i}) for i in range(100)]
        path.write_text("\n".join(lines) + "\n")

        _trim_jsonl(path, 10)
        result_lines = path.read_text().strip().split("\n")
        assert len(result_lines) == 10
        assert json.loads(result_lines[0])["n"] == 90

    def test_short_file_unchanged(self, tmp_path):
        path = tmp_path / "test.jsonl"
        lines = [json.dumps({"n": i}) for i in range(5)]
        path.write_text("\n".join(lines) + "\n")

        _trim_jsonl(path, 10)
        result_lines = path.read_text().strip().split("\n")
        assert len(result_lines) == 5


class TestCompressFile:
    def test_creates_gz(self, tmp_path):
        path = tmp_path / "test.log"
        path.write_text("log content" * 100)

        _compress_file(path)
        assert not path.exists()
        assert (tmp_path / "test.log.gz").exists()


class TestCleanupLogs:
    def test_runs_without_error(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        jsonl = logs / "reviews.jsonl"
        jsonl.write_text("")

        with (
            patch("gate.cleanup.LOGS_DIR", logs),
            patch("gate.cleanup.REVIEWS_JSONL", jsonl),
        ):
            cleanup_logs()


class TestCleanupState:
    def test_removes_old_dirs(self, tmp_path):
        import os

        state_dir = tmp_path / "state"
        old = state_dir / "pr1"
        old.mkdir(parents=True)
        old_time = time.time() - 90 * 86400
        os.utime(old, (old_time, old_time))

        recent = state_dir / "pr2"
        recent.mkdir(parents=True)

        with patch("gate.cleanup.gate_dir", return_value=tmp_path):
            cleanup_state(max_age_days=30)

        assert not old.exists()
        assert recent.exists()

    def test_preserves_active_reviews(self, tmp_path):
        import os

        state_dir = tmp_path / "state"
        active = state_dir / "pr3"
        active.mkdir(parents=True)
        (active / "active_review.json").write_text("{}")
        old_time = time.time() - 90 * 86400
        os.utime(active, (old_time, old_time))

        with patch("gate.cleanup.gate_dir", return_value=tmp_path):
            cleanup_state(max_age_days=30)

        assert active.exists()

    def test_two_level_scan_removes_old_repo_dirs(self, tmp_path):
        import os

        state_dir = tmp_path / "state"
        old = state_dir / "org-repo" / "pr1"
        old.mkdir(parents=True)
        old_time = time.time() - 90 * 86400
        os.utime(old, (old_time, old_time))

        recent = state_dir / "org-repo" / "pr2"
        recent.mkdir(parents=True)

        with patch("gate.cleanup.gate_dir", return_value=tmp_path):
            cleanup_state(max_age_days=30)

        assert not old.exists()
        assert recent.exists()

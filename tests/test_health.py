"""Tests for gate.health module."""

import json
import time
from unittest.mock import MagicMock, patch

from gate.health import (
    _is_pid_alive,
    check_circuit_breaker,
    check_disk_usage,
    check_github_api,
    check_orphaned_check_runs,
    check_quota_freshness,
    check_recent_errors,
    check_stuck_reviews,
)


class TestCheckCircuitBreaker:
    @patch("gate.health.read_recent_decisions")
    def test_not_tripped(self, mock_recent):
        mock_recent.return_value = ["approve", "approve", "error"]
        result = check_circuit_breaker()
        assert result["ok"] is True

    @patch("gate.health.read_recent_decisions")
    def test_tripped(self, mock_recent):
        mock_recent.return_value = ["error", "error", "error"]
        result = check_circuit_breaker()
        assert result["ok"] is False
        assert "tripped" in result["detail"]


class TestCheckRecentErrors:
    @patch("gate.health.read_recent_decisions")
    def test_few_errors(self, mock_recent):
        mock_recent.return_value = ["approve", "error", "approve", "approve", "approve"]
        result = check_recent_errors()
        assert result["ok"] is True

    @patch("gate.health.read_recent_decisions")
    def test_many_errors(self, mock_recent):
        mock_recent.return_value = ["error", "error", "error", "approve", "error"]
        result = check_recent_errors()
        assert result["ok"] is False


class TestCheckGithubApi:
    @patch("gate.health.subprocess.run")
    def test_reachable(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="Keep it logically awesome.")
        result = check_github_api()
        assert result["ok"] is True

    @patch("gate.health.subprocess.run")
    def test_unreachable(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = check_github_api()
        assert result["ok"] is False


class TestCheckDiskUsage:
    @patch("gate.health.subprocess.run")
    def test_low_usage(self, mock_run):
        df_out = (
            "Filesystem  Size  Used Avail Use% Mounted\n"
            "/dev/sda1  100G  50G  50G  50% /\n"
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=df_out)
        result = check_disk_usage()
        assert result["ok"] is True


class TestCheckStuckReviews:
    def test_no_state(self, tmp_path):
        with patch("gate.health.gate_dir", return_value=tmp_path):
            result = check_stuck_reviews()
            assert result["ok"] is True

    def test_stuck_review(self, tmp_path):
        state_dir = tmp_path / "state" / "pr42"
        state_dir.mkdir(parents=True)
        marker = state_dir / "active_review.json"
        marker.write_text(json.dumps({
            "check_run_id": 123,
            "started_at": time.time() - 9999,
            "pid": 99999,
        }))

        with (
            patch("gate.health.gate_dir", return_value=tmp_path),
            patch("gate.health.load_config", return_value={"timeouts": {"hard_timeout_s": 1200}}),
        ):
            result = check_stuck_reviews()
            assert result["ok"] is False


class TestCheckOrphanedCheckRuns:
    def test_no_state(self, tmp_path):
        with patch("gate.health.gate_dir", return_value=tmp_path):
            result = check_orphaned_check_runs()
            assert result["ok"] is True

    def test_orphaned_with_dead_pid(self, tmp_path):
        state_dir = tmp_path / "state" / "pr42"
        state_dir.mkdir(parents=True)
        marker = state_dir / "active_review.json"
        marker.write_text(json.dumps({
            "check_run_id": 123,
            "started_at": time.time() - 300,
            "pid": 99999999,
            "head_sha": "abc123",
        }))

        with (
            patch("gate.health.gate_dir", return_value=tmp_path),
            patch("gate.health.load_config", return_value={
                "timeouts": {"hard_timeout_s": 1200},
                "repo": {"name": "test/repo"},
            }),
            patch("gate.health.github.complete_check_run"),
            patch("gate.health.github.approve_pr"),
            patch("gate.health.notify.review_failed"),
        ):
            result = check_orphaned_check_runs()
            assert result["ok"] is False
            assert not marker.exists()


class TestCheckQuotaFreshness:
    def test_no_cache(self, tmp_path):
        with patch("gate.health.gate_dir", return_value=tmp_path):
            result = check_quota_freshness()
            assert result["ok"] is True

    def test_fresh_cache(self, tmp_path):
        cache = tmp_path / "state" / "quota-cache.json"
        cache.parent.mkdir(parents=True)
        cache.write_text("{}")

        with patch("gate.health.gate_dir", return_value=tmp_path):
            result = check_quota_freshness()
            assert result["ok"] is True

    def test_stale_cache(self, tmp_path):
        import os

        cache = tmp_path / "state" / "quota-cache.json"
        cache.parent.mkdir(parents=True)
        cache.write_text("{}")
        old_time = time.time() - 7200
        os.utime(cache, (old_time, old_time))

        with patch("gate.health.gate_dir", return_value=tmp_path):
            result = check_quota_freshness()
            assert result["ok"] is False


class TestIsPidAlive:
    def test_current_process(self):
        import os

        assert _is_pid_alive(os.getpid()) is True

    def test_nonexistent_pid(self):
        assert _is_pid_alive(99999999) is False

    def test_none_pid(self):
        assert _is_pid_alive(None) is False

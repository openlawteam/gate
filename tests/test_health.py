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
        with patch("gate.health.state_dir", lambda: tmp_path / "nonexistent"):
            result = check_stuck_reviews()
            assert result["ok"] is True

    def test_stuck_review(self, tmp_path):
        state_root = tmp_path / "state"
        pr_dir = state_root / "pr42"
        pr_dir.mkdir(parents=True)
        marker = pr_dir / "active_review.json"
        marker.write_text(json.dumps({
            "check_run_id": 123,
            "started_at": time.time() - 9999,
            "pid": 99999,
        }))

        with (
            patch("gate.health.state_dir", lambda: state_root),
            patch("gate.health.load_config", return_value={"timeouts": {"hard_timeout_s": 1200}}),
        ):
            result = check_stuck_reviews()
            assert result["ok"] is False


class TestCheckOrphanedCheckRuns:
    def test_no_state(self, tmp_path):
        with patch("gate.health.state_dir", lambda: tmp_path / "nonexistent"):
            result = check_orphaned_check_runs()
            assert result["ok"] is True

    def test_orphaned_with_dead_pid(self, tmp_path):
        state_root = tmp_path / "state"
        pr_dir = state_root / "pr42"
        pr_dir.mkdir(parents=True)
        marker = pr_dir / "active_review.json"
        marker.write_text(json.dumps({
            "check_run_id": 123,
            "started_at": time.time() - 300,
            "pid": 99999999,
            "head_sha": "abc123",
        }))

        with (
            patch("gate.health.state_dir", lambda: state_root),
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
        with patch("gate.health.quota_cache_path", lambda: tmp_path / "nonexistent.json"):
            result = check_quota_freshness()
            assert result["ok"] is True

    def test_fresh_cache(self, tmp_path):
        cache = tmp_path / "quota-cache.json"
        cache.write_text("{}")

        with patch("gate.health.quota_cache_path", lambda: cache):
            result = check_quota_freshness()
            assert result["ok"] is True

    def test_stale_cache(self, tmp_path):
        import os

        cache = tmp_path / "quota-cache.json"
        cache.write_text("{}")
        old_time = time.time() - 7200
        os.utime(cache, (old_time, old_time))

        with patch("gate.health.quota_cache_path", lambda: cache):
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


# ── Stale activity detection (new in fix-resume-tty-deadlock) ─


class TestCheckStaleActivity:
    from unittest.mock import patch

    def _make_active_marker(self, pr_dir, started_at):
        import json
        pr_dir.mkdir(parents=True, exist_ok=True)
        (pr_dir / "active_review.json").write_text(json.dumps({
            "check_run_id": 123,
            "review_id": pr_dir.name,
            "started_at": started_at,
            "head_sha": "abc",
            "pid": 1,
            "repo": "a/b",
        }))

    def test_no_state(self, tmp_path):
        from unittest.mock import patch

        from gate.health import check_stale_activity
        with patch("gate.health.state_dir", lambda: tmp_path / "nonexistent" / "state"), \
             patch("gate.health.logs_dir", lambda: tmp_path / "nonexistent" / "logs"):
            assert check_stale_activity()["ok"] is True

    def test_fresh_review_never_flags(self, tmp_path):
        """A review that started recently is never stale, regardless of log state."""
        import time
        from unittest.mock import patch

        from gate.health import check_stale_activity

        (tmp_path / "logs" / "live").mkdir(parents=True)
        pr_dir = tmp_path / "state" / "pr1"
        self._make_active_marker(pr_dir, started_at=time.time() - 30)

        with patch("gate.health.state_dir", lambda: tmp_path / "state"), \
             patch("gate.health.logs_dir", lambda: tmp_path / "logs"):
            result = check_stale_activity()
        assert result["ok"] is True

    def test_old_review_with_recent_log_is_healthy(self, tmp_path):
        import os
        import time
        from unittest.mock import patch

        from gate.health import check_stale_activity

        pr_dir = tmp_path / "state" / "pr1"
        # Started 30 min ago
        self._make_active_marker(pr_dir, started_at=time.time() - 1800)

        # Live log touched recently
        live_dir = tmp_path / "logs" / "live"
        live_dir.mkdir(parents=True)
        log = live_dir / "pr1.log"
        log.write_text("recent activity\n")
        os.utime(log, (time.time() - 30, time.time() - 30))

        with patch("gate.health.state_dir", lambda: tmp_path / "state"), \
             patch("gate.health.logs_dir", lambda: tmp_path / "logs"):
            result = check_stale_activity()
        assert result["ok"] is True, result

    def test_old_review_with_stale_log_is_flagged(self, tmp_path):
        import os
        import time
        from unittest.mock import patch

        from gate.health import check_stale_activity

        pr_dir = tmp_path / "state" / "pr1"
        # Started 30 min ago
        self._make_active_marker(pr_dir, started_at=time.time() - 1800)

        # Live log last touched 15 min ago (> 10 min threshold)
        live_dir = tmp_path / "logs" / "live"
        live_dir.mkdir(parents=True)
        log = live_dir / "pr1.log"
        log.write_text("stale\n")
        stale_time = time.time() - 900
        os.utime(log, (stale_time, stale_time))

        with patch("gate.health.state_dir", lambda: tmp_path / "state"), \
             patch("gate.health.logs_dir", lambda: tmp_path / "logs"):
            result = check_stale_activity()
        assert result["ok"] is False
        assert "stale" in result["detail"]
        assert "pr1" in result["detail"]

    def test_multi_repo_namespaced_log(self, tmp_path):
        import os
        import time
        from unittest.mock import patch

        from gate.health import check_stale_activity

        pr_dir = tmp_path / "state" / "org-repo" / "pr7"
        self._make_active_marker(pr_dir, started_at=time.time() - 1800)

        live_dir = tmp_path / "logs" / "live" / "org-repo"
        live_dir.mkdir(parents=True)
        log = live_dir / "pr7.log"
        log.write_text("old\n")
        stale_time = time.time() - 900
        os.utime(log, (stale_time, stale_time))

        with patch("gate.health.state_dir", lambda: tmp_path / "state"), \
             patch("gate.health.logs_dir", lambda: tmp_path / "logs"):
            result = check_stale_activity()
        assert result["ok"] is False
        assert "org-repo/pr7" in result["detail"]

    def test_missing_live_log_skips(self, tmp_path):
        """If there's no live log, we can't tell, so don't flag."""
        import time
        from unittest.mock import patch

        from gate.health import check_stale_activity

        pr_dir = tmp_path / "state" / "pr1"
        self._make_active_marker(pr_dir, started_at=time.time() - 1800)
        (tmp_path / "logs" / "live").mkdir(parents=True)

        with patch("gate.health.state_dir", lambda: tmp_path / "state"), \
             patch("gate.health.logs_dir", lambda: tmp_path / "logs"):
            result = check_stale_activity()
        assert result["ok"] is True

    def test_corrupt_marker_skips(self, tmp_path):
        from unittest.mock import patch

        from gate.health import check_stale_activity

        pr_dir = tmp_path / "state" / "pr1"
        pr_dir.mkdir(parents=True)
        (pr_dir / "active_review.json").write_text("{not json")
        (tmp_path / "logs" / "live").mkdir(parents=True)

        with patch("gate.health.state_dir", lambda: tmp_path / "state"), \
             patch("gate.health.logs_dir", lambda: tmp_path / "logs"):
            result = check_stale_activity()
        assert result["ok"] is True

    def test_included_in_run_health_check(self):
        from unittest.mock import patch

        from gate.health import run_health_check

        with (
            patch("gate.health.check_sleep_disabled", return_value={"ok": True}),
            patch("gate.health.check_runner", return_value={"ok": True}),
            patch("gate.health.check_github_api", return_value={"ok": True}),
            patch("gate.health.check_tailscale", return_value={"ok": True}),
            patch("gate.health.check_disk_usage", return_value={"ok": True}),
            patch("gate.health.check_tmux_session", return_value={"ok": True}),
            patch("gate.health.check_gate_server", return_value={"ok": True}),
            patch("gate.health.check_stuck_reviews", return_value={"ok": True}),
            patch("gate.health.check_stale_activity", return_value={"ok": True}) as m,
            patch("gate.health.check_orphaned_check_runs", return_value={"ok": True}),
            patch("gate.health.check_orphaned_tmux_windows", return_value={"ok": True}),
            patch("gate.health.check_circuit_breaker", return_value={"ok": True}),
            patch("gate.health.check_quota_freshness", return_value={"ok": True}),
            patch("gate.health.check_recent_errors", return_value={"ok": True}),
        ):
            results = run_health_check()
        assert "stale_activity" in results
        m.assert_called_once()


# ── Config threading (review warning regression) ────────────────


class TestHealthConfigThreading:
    """Regression: check_disk_usage / _cleanup_old_worktrees must accept
    a caller-provided config so run_health_check doesn't cause per-cycle
    TOML reloads through the cleanup path.
    """

    def test_check_disk_usage_accepts_config_kw(self):
        from unittest.mock import patch

        from gate.health import check_disk_usage
        # Simulate disk full so the cleanup branch fires
        with patch("gate.health.subprocess.run") as mr, \
             patch("gate.health._cleanup_old_worktrees") as cleanup, \
             patch("gate.health.load_config") as load:
            mr.return_value.stdout = "Filesystem\n/dev/x  100 90 10 95% /\n"
            mr.return_value.returncode = 0
            cfg = {"repos": [{"worktree_base": "/tmp/x"}]}
            result = check_disk_usage(config=cfg)
            # Cleanup was called with the caller's config
            cleanup.assert_called_once_with(cfg)
            # No load_config reload happened
            load.assert_not_called()
            assert result["ok"] is False

    def test_check_disk_usage_no_config_falls_back_to_load(self):
        from unittest.mock import patch

        from gate.health import check_disk_usage
        with patch("gate.health.subprocess.run") as mr, \
             patch("gate.health._cleanup_old_worktrees") as cleanup, \
             patch("gate.health.load_config", return_value={"fallback": True}) as load:
            mr.return_value.stdout = "Filesystem\n/dev/x  100 90 10 95% /\n"
            mr.return_value.returncode = 0
            check_disk_usage()
            load.assert_called_once()
            cleanup.assert_called_once_with({"fallback": True})

    def test_cleanup_accepts_explicit_config(self):
        from unittest.mock import patch

        from gate.health import _cleanup_old_worktrees
        cfg = {"repos": [{"worktree_base": "/tmp/custom"}]}
        with patch("gate.health.subprocess.run") as mr, \
             patch("gate.health.load_config") as load:
            _cleanup_old_worktrees(cfg)
            load.assert_not_called()
            assert mr.called
            # The find target should be the caller's worktree_base
            cmd = mr.call_args.args[0]
            assert "/tmp/custom" in cmd

    def test_run_health_check_loads_config_once(self):
        from unittest.mock import patch

        from gate.health import run_health_check
        call_count = {"n": 0}

        def count_load():
            call_count["n"] += 1
            return {"repos": []}

        stub = {"ok": True, "detail": "stub"}
        with patch("gate.health.load_config", side_effect=count_load), \
             patch("gate.health.check_sleep_disabled", return_value=stub), \
             patch("gate.health.check_runner", return_value=stub), \
             patch("gate.health.check_github_api", return_value=stub), \
             patch("gate.health.check_tailscale", return_value=stub), \
             patch("gate.health.check_disk_usage", return_value=stub) as disk, \
             patch("gate.health.check_tmux_session", return_value=stub), \
             patch("gate.health.check_gate_server", return_value=stub), \
             patch("gate.health.check_stuck_reviews", return_value=stub), \
             patch("gate.health.check_stale_activity", return_value=stub), \
             patch("gate.health.check_orphaned_check_runs", return_value=stub), \
             patch("gate.health.check_orphaned_tmux_windows", return_value=stub), \
             patch("gate.health.check_circuit_breaker", return_value=stub), \
             patch("gate.health.check_quota_freshness", return_value=stub), \
             patch("gate.health.check_recent_errors", return_value=stub):
            run_health_check()
        # Exactly one load_config per cycle; disk check got it via kwarg
        assert call_count["n"] == 1
        assert "config" in disk.call_args.kwargs

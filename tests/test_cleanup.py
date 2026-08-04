"""Tests for gate.cleanup module."""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from gate.cleanup import (
    _compress_file,
    _trim_jsonl,
    cleanup_logs,
    cleanup_orphans,
    cleanup_pr_worktrees,
    cleanup_state,
    cleanup_worktrees,
    daily_digest,
    run_cleanup,
)


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
            patch("gate.cleanup.logs_dir", lambda: logs),
            patch("gate.cleanup.reviews_jsonl", lambda: jsonl),
        ):
            cleanup_logs()

    def test_rotates_trims_and_expires_all_log_classes(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        activity = logs / "activity.log"
        activity.write_text("rotate me")
        reviews = logs / "reviews.jsonl"
        reviews.write_text("one\ntwo\nthree\n")
        disputes = logs / "disputes.jsonl"
        disputes.write_text("one\ntwo\nthree\n")

        old_time = time.time() - 40 * 86400
        live = logs / "live"
        nested = live / "org-repo"
        nested.mkdir(parents=True)
        old_live = live / "old.log"
        old_nested = nested / "old.log"
        old_live.write_text("old")
        old_nested.write_text("old")
        os.utime(old_live, (old_time, old_time))
        os.utime(old_nested, (old_time, old_time))

        historical = logs / "historical.log"
        historical.write_text("history")
        os.utime(historical, (old_time, old_time))

        runners = logs / "runners"
        runners.mkdir()
        expired = runners / "expired.log"
        compressible = runners / "compress.log"
        current = runners / "current.log"
        for path in (expired, compressible, current):
            path.write_text(path.name)
        os.utime(expired, (old_time, old_time))
        eight_days = time.time() - 8 * 86400
        os.utime(compressible, (eight_days, eight_days))

        with (
            patch("gate.cleanup.logs_dir", lambda: logs),
            patch("gate.cleanup.reviews_jsonl", lambda: reviews),
        ):
            cleanup_logs(max_log_size_mb=0, max_jsonl_lines=2)

        assert not activity.exists()
        assert list(logs.glob("activity.log.*.gz"))
        assert reviews.read_text().splitlines() == ["two", "three"]
        assert disputes.read_text().splitlines() == ["two", "three"]
        assert not old_live.exists()
        assert not nested.exists()
        assert not historical.exists()
        assert (logs / "historical.log.gz").exists()
        assert not expired.exists()
        assert not compressible.exists()
        assert (runners / "compress.log.gz").exists()
        assert current.exists()


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

        with patch("gate.cleanup.state_dir", lambda: state_dir):
            cleanup_state(max_age_days=30)

        assert not old.exists()
        assert recent.exists()


def test_cleanup_worktrees_deduplicates_bases_and_prunes_clones(tmp_path):
    worktrees = tmp_path / "worktrees"
    old = worktrees / "old"
    recent = worktrees / "recent"
    old.mkdir(parents=True)
    recent.mkdir()
    old_time = time.time() - 48 * 3600
    os.utime(old, (old_time, old_time))
    config = {
        "repos": [
            {
                "name": "org/one",
                "worktree_base": str(worktrees),
                "clone_path": str(tmp_path / "one"),
            },
            {
                "name": "org/two",
                "worktree_base": str(worktrees),
                "clone_path": str(tmp_path / "two"),
            },
        ]
    }
    with (
        patch("gate.cleanup.load_config", return_value=config),
        patch("gate.cleanup.prune_worktrees") as prune,
    ):
        cleanup_worktrees(max_age_hours=24)
    assert not old.exists()
    assert recent.exists()
    assert prune.call_count == 2


def test_cleanup_pr_worktrees_uses_repo_prefix_and_prunes_clone(tmp_path):
    worktrees = tmp_path / "worktrees"
    target = worktrees / "org-repo-pr7-deadbeef"
    other = worktrees / "org-repo-pr8-cafebabe"
    target.mkdir(parents=True)
    other.mkdir()
    config = {
        "repos": [
            {
                "name": "org/repo",
                "worktree_base": str(worktrees),
                "clone_path": str(tmp_path / "clone"),
            }
        ]
    }
    with (
        patch("gate.cleanup.load_config", return_value=config),
        patch("gate.cleanup.prune_worktrees") as prune,
    ):
        cleanup_pr_worktrees(7, "org/repo")
    assert not target.exists()
    assert other.exists()
    prune.assert_called_once_with(str(tmp_path / "clone"))


def test_cleanup_orphans_removes_dead_marker_and_matching_worktree(tmp_path):
    state_root = tmp_path / "state"
    dead_pr = state_root / "org-repo" / "pr3"
    live_pr = state_root / "org-repo" / "pr4"
    dead_pr.mkdir(parents=True)
    live_pr.mkdir()
    (dead_pr / "active_review.json").write_text(json.dumps({"pid": 1003}))
    (live_pr / "active_review.json").write_text(json.dumps({"pid": 1004}))

    worktrees = tmp_path / "worktrees"
    dead_worktree = worktrees / "org-repo-pr3-deadbeef"
    live_worktree = worktrees / "org-repo-pr4-cafebabe"
    dead_worktree.mkdir(parents=True)
    live_worktree.mkdir()
    config = {
        "repos": [
            {
                "name": "org/repo",
                "worktree_base": str(worktrees),
                "clone_path": str(tmp_path / "clone"),
            }
        ]
    }

    def check_pid(pid, _signal):
        if pid == 1003:
            raise ProcessLookupError

    with (
        patch("gate.cleanup.state_dir", lambda: state_root),
        patch("gate.cleanup.load_config", return_value=config),
        patch("gate.cleanup.os.kill", side_effect=check_pid),
        patch("gate.cleanup.prune_worktrees") as prune,
    ):
        cleanup_orphans()

    assert not (dead_pr / "active_review.json").exists()
    assert (live_pr / "active_review.json").exists()
    assert not dead_worktree.exists()
    assert live_worktree.exists()
    prune.assert_called_once_with(str(tmp_path / "clone"))


def test_run_cleanup_invokes_all_retention_steps():
    with (
        patch("gate.cleanup.cleanup_logs") as logs,
        patch("gate.cleanup.cleanup_worktrees") as worktrees,
        patch("gate.cleanup.cleanup_state") as state,
    ):
        run_cleanup()
    logs.assert_called_once_with()
    worktrees.assert_called_once_with()
    state.assert_called_once_with()


def test_daily_digest_summarizes_recent_reviews_by_repo(tmp_path):
    now = datetime.now(timezone.utc)
    rows = [
        {
            "timestamp": now.isoformat(),
            "repo": "org/one",
            "decision": "approve",
            "review_time_seconds": 12,
        },
        {
            "timestamp": now.isoformat(),
            "repo": "org/two",
            "decision": "request_changes",
            "review_time_seconds": 20,
        },
        {
            "timestamp": (now - timedelta(days=2)).isoformat(),
            "repo": "org/old",
            "decision": "error",
            "review_time_seconds": 100,
        },
    ]
    path = tmp_path / "reviews.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\nmalformed\n")
    with (
        patch("gate.cleanup.reviews_jsonl", lambda: path),
        patch("gate.cleanup.notify.notify") as notify,
        patch("gate.cleanup.notify.notify_discord") as discord,
    ):
        daily_digest()
    summary = notify.call_args.args[1]
    assert "Reviews: 2" in summary
    assert "Approved: 1" in summary
    assert "org/one: 1 (1 ok)" in summary
    assert "org/two: 1 (0 ok)" in summary
    discord.assert_called_once()


def test_cleanup_state_preserves_active_reviews(tmp_path):
    state_dir = tmp_path / "state"
    active = state_dir / "pr3"
    active.mkdir(parents=True)
    (active / "active_review.json").write_text("{}")
    old_time = time.time() - 90 * 86400
    os.utime(active, (old_time, old_time))

    with patch("gate.cleanup.state_dir", lambda: state_dir):
        cleanup_state(max_age_days=30)

    assert active.exists()


def test_cleanup_state_two_level_scan_removes_old_repo_dirs(tmp_path):
    state_dir = tmp_path / "state"
    old = state_dir / "org-repo" / "pr1"
    old.mkdir(parents=True)
    old_time = time.time() - 90 * 86400
    os.utime(old, (old_time, old_time))

    recent = state_dir / "org-repo" / "pr2"
    recent.mkdir(parents=True)

    with patch("gate.cleanup.state_dir", lambda: state_dir):
        cleanup_state(max_age_days=30)

    assert not old.exists()
    assert recent.exists()

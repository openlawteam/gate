"""Tests for gate.state module."""

import json
from unittest.mock import patch

from gate.state import (
    check_fix_limits,
    cleanup_pr_state,
    get_fix_attempts,
    get_pr_state_dir,
    load_prior_review,
    persist_review_state,
    record_fix_attempt,
)


class TestGetPrStateDir:
    def test_creates_directory(self, tmp_path):
        with patch("gate.state.state_dir", lambda: tmp_path):
            d = get_pr_state_dir(42)
            assert d.exists()
            assert d.name == "pr42"

    def test_repo_namespaced(self, tmp_path):
        with patch("gate.state.state_dir", lambda: tmp_path):
            d = get_pr_state_dir(42, repo="org/repo")
            assert d.exists()
            assert d.parent.name == "org-repo"
            assert d.name == "pr42"

    def test_different_repos_different_dirs(self, tmp_path):
        with patch("gate.state.state_dir", lambda: tmp_path):
            d1 = get_pr_state_dir(42, repo="org/repo-a")
            d2 = get_pr_state_dir(42, repo="org/repo-b")
            assert d1 != d2


class TestLoadPriorReview:
    def test_no_prior_state(self, tmp_path, tmp_workspace):
        with patch("gate.state.state_dir", lambda: tmp_path):
            result = load_prior_review(99, tmp_workspace)
            assert result["has_prior"] is False
            assert result["review_count"] == 0

    def test_with_prior_verdict(self, tmp_path, tmp_workspace):
        state_dir = tmp_path / "pr42"
        state_dir.mkdir(parents=True)
        verdict = {
            "decision": "request_changes",
            "confidence": "high",
            "findings": [
                {"severity": "error", "file": "x.ts", "message": "bug", "source_stage": "logic"},
            ],
            "stats": {"total_findings": 1},
        }
        (state_dir / "verdict.json").write_text(json.dumps(verdict))
        (state_dir / "last_sha.txt").write_text("abc123")
        (state_dir / "review_count.txt").write_text("2")

        with patch("gate.state.state_dir", lambda: tmp_path):
            result = load_prior_review(42, tmp_workspace)
            assert result["has_prior"] is True
            assert result["prior_decision"] == "request_changes"
            assert len(result["prior_findings"]) == 1
            assert result["review_count"] == 2

            # Check it wrote to workspace
            output = json.loads((tmp_workspace / "prior-review.json").read_text())
            assert output["has_prior"] is True


class TestPersistReviewState:
    def test_saves_verdict(self, tmp_path, tmp_workspace):
        verdict = {"decision": "approve", "findings": []}
        (tmp_workspace / "verdict.json").write_text(json.dumps(verdict))

        with patch("gate.state.state_dir", lambda: tmp_path):
            persist_review_state(42, "sha123", tmp_workspace)
            state_dir = tmp_path / "pr42"
            assert (state_dir / "verdict.json").exists()
            assert (state_dir / "last_sha.txt").read_text() == "sha123"
            assert (state_dir / "review_count.txt").read_text() == "1"

    def test_increments_count(self, tmp_path, tmp_workspace):
        verdict = {"decision": "approve", "findings": []}
        (tmp_workspace / "verdict.json").write_text(json.dumps(verdict))

        state_dir = tmp_path / "pr42"
        state_dir.mkdir(parents=True)
        (state_dir / "review_count.txt").write_text("3")
        (state_dir / "last_sha.txt").write_text("sha123")

        with patch("gate.state.state_dir", lambda: tmp_path):
            persist_review_state(42, "sha123", tmp_workspace)
            assert (state_dir / "review_count.txt").read_text() == "4"


class TestCleanupPrState:
    def test_removes_directory(self, tmp_path):
        state_dir = tmp_path / "pr42"
        state_dir.mkdir()
        (state_dir / "verdict.json").write_text("{}")

        with patch("gate.state.state_dir", lambda: tmp_path):
            cleanup_pr_state(42)
            assert not state_dir.exists()

    def test_no_error_on_missing(self, tmp_path):
        with patch("gate.state.state_dir", lambda: tmp_path):
            cleanup_pr_state(999)


class TestFixAttempts:
    def test_initial_state(self, tmp_path):
        with patch("gate.state.state_dir", lambda: tmp_path):
            attempts = get_fix_attempts(42)
            assert attempts["soft"] == 0
            assert attempts["total"] == 0

    def test_record_and_read(self, tmp_path):
        with patch("gate.state.state_dir", lambda: tmp_path):
            record_fix_attempt(42)
            record_fix_attempt(42)
            attempts = get_fix_attempts(42)
            assert attempts["soft"] == 2
            assert attempts["total"] == 2
            assert attempts["last_fix_at"] > 0

    def test_check_limits_allowed(self, tmp_path):
        limits = {"max_fix_attempts_soft": 3, "max_fix_attempts_total": 6, "fix_cooldown_s": 0}
        config = {"limits": limits}
        with patch("gate.state.state_dir", lambda: tmp_path):
            allowed, reason = check_fix_limits(42, config)
            assert allowed is True

    def test_check_limits_soft_exceeded(self, tmp_path):
        limits = {"max_fix_attempts_soft": 2, "max_fix_attempts_total": 6, "fix_cooldown_s": 0}
        config = {"limits": limits}
        with patch("gate.state.state_dir", lambda: tmp_path):
            record_fix_attempt(42)
            record_fix_attempt(42)
            allowed, reason = check_fix_limits(42, config)
            assert allowed is False
            assert "Soft fix limit" in reason

    def test_fix_attempts_isolated_per_repo(self, tmp_path):
        with patch("gate.state.state_dir", lambda: tmp_path):
            record_fix_attempt(42, repo="org/repo-a")
            record_fix_attempt(42, repo="org/repo-a")
            record_fix_attempt(42, repo="org/repo-b")
            a = get_fix_attempts(42, repo="org/repo-a")
            b = get_fix_attempts(42, repo="org/repo-b")
            assert a["soft"] == 2
            assert b["soft"] == 1

    def test_no_op_resets_soft_counter_and_skips_total(self, tmp_path):
        """Graceful no-op events must not consume the fix budget (Audit A9)."""
        with patch("gate.state.state_dir", lambda: tmp_path):
            record_fix_attempt(42)
            record_fix_attempt(42)
            before = get_fix_attempts(42)
            assert before["soft"] == 2
            record_fix_attempt(42, no_op=True)
            after = get_fix_attempts(42)
            assert after["soft"] == 0, "soft counter must reset on no-op"
            assert after["total"] == before["total"], "total must NOT increment on no-op"


# ── persist_review_state robustness (review warning regression) ─────


_patch = patch  # alias so later code keeps working after the import move


class TestPersistReviewStateRobustness:
    """Regression for review warning: persist_review_state must not leak
    OSError from the merge-base subprocess call (e.g. git missing from
    PATH, clone_path pruned). Previously only CalledProcessError/
    TimeoutExpired were caught, so FileNotFoundError would escape AFTER
    sha_path was already written — leaving counters inconsistent.
    """

    def _setup(self, tmp_path, tmp_workspace):
        # Put a prior SHA in state so the force-push / merge-base branch runs
        state_dir = tmp_path / "pr42"
        state_dir.mkdir(parents=True)
        (state_dir / "last_sha.txt").write_text("oldsha1234567")
        verdict_json = {"decision": "approve", "findings": [], "stats": {}}
        (tmp_workspace / "verdict.json").write_text(
            __import__("json").dumps(verdict_json)
        )
        return state_dir

    def test_git_missing_does_not_escape(self, tmp_path, tmp_workspace):
        self._setup(tmp_path, tmp_workspace)
        with _patch("gate.state.state_dir", lambda: tmp_path), \
             _patch("gate.state.subprocess.run",
                    side_effect=FileNotFoundError(2, "No git", "git")):
            # Must not raise
            persist_review_state(42, "newsha7654321", tmp_workspace)
        # The force-push path should have fired (is_ancestor stays False)
        assert (tmp_path / "pr42" / "review_count.txt").read_text() == "1"
        assert (tmp_path / "pr42" / "fix_attempts.txt").read_text() == "0"

    def test_clone_path_pruned_does_not_escape(self, tmp_path, tmp_workspace):
        self._setup(tmp_path, tmp_workspace)
        with _patch("gate.state.state_dir", lambda: tmp_path), \
             _patch("gate.state.subprocess.run",
                    side_effect=NotADirectoryError(20, "Not a directory", "x")):
            persist_review_state(42, "newsha7654321", tmp_workspace,
                                 clone_path="/pruned/path")
        # Force-push branch fires because is_ancestor stays False
        assert (tmp_path / "pr42" / "review_count.txt").read_text() == "1"

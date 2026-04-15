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
        with patch("gate.state.STATE_DIR", tmp_path):
            d = get_pr_state_dir(42)
            assert d.exists()
            assert d.name == "pr42"

    def test_repo_namespaced(self, tmp_path):
        with patch("gate.state.STATE_DIR", tmp_path):
            d = get_pr_state_dir(42, repo="org/repo")
            assert d.exists()
            assert d.parent.name == "org-repo"
            assert d.name == "pr42"

    def test_different_repos_different_dirs(self, tmp_path):
        with patch("gate.state.STATE_DIR", tmp_path):
            d1 = get_pr_state_dir(42, repo="org/repo-a")
            d2 = get_pr_state_dir(42, repo="org/repo-b")
            assert d1 != d2


class TestLoadPriorReview:
    def test_no_prior_state(self, tmp_path, tmp_workspace):
        with patch("gate.state.STATE_DIR", tmp_path):
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

        with patch("gate.state.STATE_DIR", tmp_path):
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

        with patch("gate.state.STATE_DIR", tmp_path):
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

        with patch("gate.state.STATE_DIR", tmp_path):
            persist_review_state(42, "sha123", tmp_workspace)
            assert (state_dir / "review_count.txt").read_text() == "4"


class TestCleanupPrState:
    def test_removes_directory(self, tmp_path):
        state_dir = tmp_path / "pr42"
        state_dir.mkdir()
        (state_dir / "verdict.json").write_text("{}")

        with patch("gate.state.STATE_DIR", tmp_path):
            cleanup_pr_state(42)
            assert not state_dir.exists()

    def test_no_error_on_missing(self, tmp_path):
        with patch("gate.state.STATE_DIR", tmp_path):
            cleanup_pr_state(999)


class TestFixAttempts:
    def test_initial_state(self, tmp_path):
        with patch("gate.state.STATE_DIR", tmp_path):
            attempts = get_fix_attempts(42)
            assert attempts["soft"] == 0
            assert attempts["total"] == 0

    def test_record_and_read(self, tmp_path):
        with patch("gate.state.STATE_DIR", tmp_path):
            record_fix_attempt(42)
            record_fix_attempt(42)
            attempts = get_fix_attempts(42)
            assert attempts["soft"] == 2
            assert attempts["total"] == 2
            assert attempts["last_fix_at"] > 0

    def test_check_limits_allowed(self, tmp_path):
        limits = {"max_fix_attempts_soft": 3, "max_fix_attempts_total": 6, "fix_cooldown_s": 0}
        config = {"limits": limits}
        with patch("gate.state.STATE_DIR", tmp_path):
            allowed, reason = check_fix_limits(42, config)
            assert allowed is True

    def test_check_limits_soft_exceeded(self, tmp_path):
        limits = {"max_fix_attempts_soft": 2, "max_fix_attempts_total": 6, "fix_cooldown_s": 0}
        config = {"limits": limits}
        with patch("gate.state.STATE_DIR", tmp_path):
            record_fix_attempt(42)
            record_fix_attempt(42)
            allowed, reason = check_fix_limits(42, config)
            assert allowed is False
            assert "Soft fix limit" in reason

    def test_fix_attempts_isolated_per_repo(self, tmp_path):
        with patch("gate.state.STATE_DIR", tmp_path):
            record_fix_attempt(42, repo="org/repo-a")
            record_fix_attempt(42, repo="org/repo-a")
            record_fix_attempt(42, repo="org/repo-b")
            a = get_fix_attempts(42, repo="org/repo-a")
            b = get_fix_attempts(42, repo="org/repo-b")
            assert a["soft"] == 2
            assert b["soft"] == 1

"""Tests for gate.orchestrator module."""

import threading
import time
from unittest.mock import patch

import pytest

from gate.orchestrator import ReviewOrchestrator


@pytest.fixture
def orchestrator(sample_config, tmp_path):
    """Create a ReviewOrchestrator with mocked dependencies."""
    with patch("gate.orchestrator.workspace_mod"), \
         patch("gate.orchestrator.github"), \
         patch("gate.orchestrator.notify"), \
         patch("gate.orchestrator.state"), \
         patch("gate.orchestrator.builder"), \
         patch("gate.orchestrator.prompt"), \
         patch("gate.orchestrator.spawn_review_stage"), \
         patch("gate.orchestrator.write_live_log"), \
         patch("gate.orchestrator.log_review"), \
         patch("gate.orchestrator.read_recent_decisions", return_value=[]):
        orch = ReviewOrchestrator(
            pr_number=42,
            repo="test-org/test-repo",
            head_sha="abc1234567890",
            event="synchronize",
            branch="feature-branch",
            labels=[],
            config=sample_config,
            socket_path=None,
        )
        orch.workspace = tmp_path / "workspace"
        orch.workspace.mkdir()
        yield orch


class TestOrchestratorInit:
    def test_initial_state(self, orchestrator):
        assert orchestrator.pr_number == 42
        assert orchestrator.head_sha == "abc1234567890"
        assert orchestrator.branch == "feature-branch"
        assert not orchestrator._cancelled.is_set()
        assert orchestrator._active_panes == {}

    def test_has_panes_lock(self, orchestrator):
        assert hasattr(orchestrator, "_panes_lock")
        assert isinstance(orchestrator._panes_lock, type(threading.Lock()))


class TestCancel:
    @patch("gate.orchestrator.kill_window")
    @patch("gate.orchestrator.github")
    def test_cancel_sets_event_and_kills_panes(self, mock_github, mock_kill, orchestrator):
        with orchestrator._panes_lock:
            orchestrator._active_panes["architecture"] = "%1"
            orchestrator._active_panes["security"] = "%2"
        orchestrator.cancel()
        assert orchestrator._cancelled.is_set()
        assert mock_kill.call_count == 2

    @patch("gate.orchestrator.kill_window")
    @patch("gate.orchestrator.github")
    def test_cancel_empty_panes(self, mock_github, mock_kill, orchestrator):
        orchestrator.cancel()
        assert orchestrator._cancelled.is_set()
        mock_kill.assert_not_called()


class TestLabelGates:
    @patch("gate.orchestrator.write_live_log")
    @patch("gate.orchestrator.github")
    def test_skip_label_approves(self, mock_github, mock_log, sample_config, tmp_path):
        with patch("gate.orchestrator.workspace_mod"), \
             patch("gate.orchestrator.state"), \
             patch("gate.orchestrator.notify"), \
             patch("gate.orchestrator.builder"), \
             patch("gate.orchestrator.prompt"), \
             patch("gate.orchestrator.spawn_review_stage"), \
             patch("gate.orchestrator.log_review"), \
             patch("gate.orchestrator.read_recent_decisions", return_value=[]):
            orch = ReviewOrchestrator(
                pr_number=10,
                repo="test-org/test-repo",
                head_sha="abc123",
                event="synchronize",
                branch="main",
                labels=["gate-skip"],
                config=sample_config,
            )
            mock_github._wait_for_connectivity.return_value = True
            mock_github.create_check_run.return_value = "cr1"
            orch.run()
            mock_github.approve_pr.assert_called_once()
            mock_github.complete_check_run.assert_called_once()

    @patch("gate.orchestrator.write_live_log")
    @patch("gate.orchestrator.github")
    def test_emergency_bypass_label(self, mock_github, mock_log, sample_config, tmp_path):
        with patch("gate.orchestrator.workspace_mod"), \
             patch("gate.orchestrator.state"), \
             patch("gate.orchestrator.notify"), \
             patch("gate.orchestrator.builder"), \
             patch("gate.orchestrator.prompt"), \
             patch("gate.orchestrator.spawn_review_stage"), \
             patch("gate.orchestrator.log_review"), \
             patch("gate.orchestrator.read_recent_decisions", return_value=[]):
            orch = ReviewOrchestrator(
                pr_number=11,
                repo="test-org/test-repo",
                head_sha="def456",
                event="synchronize",
                branch="hotfix",
                labels=["gate-emergency-bypass"],
                config=sample_config,
            )
            mock_github._wait_for_connectivity.return_value = True
            mock_github.create_check_run.return_value = "cr2"
            orch.run()
            mock_github.approve_pr.assert_called_once()
            approve_msg = mock_github.approve_pr.call_args[0][2]
            assert "bypass" in approve_msg.lower()


class TestFailOpen:
    @patch("gate.orchestrator.write_live_log")
    @patch("gate.orchestrator.github")
    def test_exception_approves_fail_open(self, mock_github, mock_log, sample_config):
         with patch("gate.orchestrator.workspace_mod"), \
             patch("gate.orchestrator.state"), \
             patch("gate.orchestrator.notify"), \
             patch("gate.orchestrator.builder"), \
             patch("gate.orchestrator.prompt"), \
             patch("gate.orchestrator.spawn_review_stage"), \
             patch("gate.orchestrator.log_review"), \
             patch("gate.orchestrator.read_recent_decisions", return_value=[]):
            mock_github._wait_for_connectivity.return_value = True
            mock_github.create_check_run.return_value = "cr3"
            mock_github.get_pr_info.side_effect = RuntimeError("API exploded")
            orch = ReviewOrchestrator(
                pr_number=99,
                repo="test-org/test-repo",
                head_sha="fff999",
                event="synchronize",
                branch="boom",
                labels=[],
                config=sample_config,
            )
            orch.run()
            mock_github.approve_pr.assert_called_once()
            approve_msg = mock_github.approve_pr.call_args[0][2]
            msg = approve_msg.lower()
            assert "error" in msg or "fail" in msg or "exception" in msg


class TestDetectFixRerun:
    def test_no_prior(self, orchestrator):
        prior = {"has_prior": False, "fix_attempts": 0}
        assert not orchestrator._detect_fix_rerun(prior)

    def test_request_changes_is_fix_rerun(self, orchestrator):
        prior = {"has_prior": True, "prior_decision": "request_changes", "fix_attempts": 1}
        assert orchestrator._detect_fix_rerun(prior)

    def test_approve_with_notes_is_fix_rerun(self, orchestrator):
        prior = {"has_prior": True, "prior_decision": "approve_with_notes", "fix_attempts": 1}
        assert orchestrator._detect_fix_rerun(prior)

    def test_approve_not_fix_rerun(self, orchestrator):
        prior = {"has_prior": True, "prior_decision": "approve", "fix_attempts": 0}
        assert not orchestrator._detect_fix_rerun(prior)


class TestReviewId:
    def test_composite_review_id(self, orchestrator):
        rid = orchestrator._review_id()
        assert rid == "test-org-test-repo-pr42"

    def test_review_id_no_slash(self, sample_config, tmp_path):
        with patch("gate.orchestrator.workspace_mod"), \
             patch("gate.orchestrator.github"), \
             patch("gate.orchestrator.notify"), \
             patch("gate.orchestrator.state"), \
             patch("gate.orchestrator.builder"), \
             patch("gate.orchestrator.prompt"), \
             patch("gate.orchestrator.spawn_review_stage"), \
             patch("gate.orchestrator.write_live_log"), \
             patch("gate.orchestrator.log_review"), \
             patch("gate.orchestrator.read_recent_decisions", return_value=[]):
            orch = ReviewOrchestrator(
                pr_number=7,
                repo="myorg/myrepo",
                head_sha="abc123",
                event="synchronize",
                branch="feat",
                labels=[],
                config=sample_config,
                socket_path=None,
            )
            assert "/" not in orch._review_id()
            assert orch._review_id() == "myorg-myrepo-pr7"


class TestPanesLockConcurrency:
    def test_concurrent_pane_access(self, orchestrator):
        """Verify _panes_lock prevents concurrent modification issues."""

        def writer():
            for i in range(50):
                with orchestrator._panes_lock:
                    orchestrator._active_panes[f"stage-{i}"] = f"%{i}"
                time.sleep(0.001)

        def reader():
            for _ in range(50):
                with orchestrator._panes_lock:
                    _ = dict(orchestrator._active_panes)
                time.sleep(0.001)

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=reader)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert not t1.is_alive()
        assert not t2.is_alive()

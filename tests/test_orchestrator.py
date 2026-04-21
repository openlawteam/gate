"""Tests for gate.orchestrator module.

The orchestrator is the central state machine of Gate. These tests cover:
- Initialization and review ID composition
- Cancellation (sync + pane-kill)
- All five gates (label skip, label bypass, circuit breaker, fix-rerun, quota,
  cycle limit)
- Fail-open exception handling
- _detect_fix_rerun logic
- Pane lock concurrency
- Active-marker write/remove lifecycle

Stage execution is heavy on external calls (tmux, Claude, GitHub), so we
mock at module boundaries and focus on orchestration logic rather than the
stages themselves (which have their own tests).
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from gate.orchestrator import ReviewOrchestrator


def _mocks():
    """Return a stack of patches to stub the orchestrator's dependencies."""
    return [
        patch("gate.orchestrator.workspace_mod"),
        patch("gate.orchestrator.github"),
        patch("gate.orchestrator.notify"),
        patch("gate.orchestrator.state"),
        patch("gate.orchestrator.builder"),
        patch("gate.orchestrator.prompt"),
        patch("gate.orchestrator.spawn_review_stage"),
        patch("gate.orchestrator.write_live_log"),
        patch("gate.orchestrator.log_review"),
        patch("gate.orchestrator.read_recent_decisions", return_value=[]),
        patch("gate.orchestrator.quota_mod"),
    ]


def _enter_all(patches):
    started = []
    values = []
    for p in patches:
        mock = p.start()
        started.append(p)
        values.append(mock)
    return started, values


def _stop_all(started):
    for p in started:
        p.stop()


@pytest.fixture
def mocks():
    """Yield a dict of mocked orchestrator dependencies."""
    patches = _mocks()
    started, values = _enter_all(patches)
    (
        workspace_mod, github, notify, state, builder, prompt,
        spawn_review_stage, write_live_log, log_review, read_recent_decisions,
        quota_mod,
    ) = values
    # Defaults that let run() get through gates
    github._wait_for_connectivity.return_value = True
    github.create_check_run.return_value = 1
    github.get_pr_info.return_value = {
        "title": "test", "body": "body", "user": {"login": "author"}
    }
    state.load_prior_review.return_value = {
        "has_prior": False, "review_count": 0, "fix_attempts": 0
    }
    quota_mod.check_quota.return_value = {
        "quota_ok": True, "five_hour_pct": 10, "seven_day_pct": 10,
    }
    try:
        yield {
            "workspace_mod": workspace_mod,
            "github": github,
            "notify": notify,
            "state": state,
            "builder": builder,
            "prompt": prompt,
            "spawn_review_stage": spawn_review_stage,
            "write_live_log": write_live_log,
            "log_review": log_review,
            "read_recent_decisions": read_recent_decisions,
            "quota_mod": quota_mod,
        }
    finally:
        _stop_all(started)


@pytest.fixture
def orchestrator(sample_config, tmp_path, mocks):
    """Create a ReviewOrchestrator with every external dependency mocked."""
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


# ── Initialization ───────────────────────────────────────────


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

    def test_init_without_socket_has_no_connection(self, orchestrator):
        assert orchestrator._connection is None

    def test_init_with_socket_starts_connection(self, sample_config, tmp_path):
        with patch("gate.client.GateConnection") as conn_cls:
            conn = MagicMock()
            conn_cls.return_value = conn
            o = ReviewOrchestrator(
                pr_number=1, repo="a/b", head_sha="x", event="s",
                branch="m", labels=[], config=sample_config,
                socket_path=tmp_path / "sock",
            )
            conn_cls.assert_called_once()
            conn.start.assert_called_once()
            assert o._connection is conn


# ── Review ID ────────────────────────────────────────────────


class TestReviewId:
    def test_composite_review_id(self, orchestrator):
        assert orchestrator._review_id() == "test-org-test-repo-pr42"

    def test_review_id_no_slash(self, sample_config):
        o = ReviewOrchestrator(
            pr_number=7, repo="myorg/myrepo", head_sha="abc", event="s",
            branch="feat", labels=[], config=sample_config,
        )
        assert "/" not in o._review_id()
        assert o._review_id() == "myorg-myrepo-pr7"


# ── Clone path ───────────────────────────────────────────────


class TestClonePath:
    def test_clone_path_expands_tilde(self, orchestrator):
        result = orchestrator._clone_path()
        assert not str(result).startswith("~")

    def test_missing_clone_path_raises(self, sample_config):
        cfg = {**sample_config, "repo": {**sample_config["repo"], "clone_path": ""}}
        o = ReviewOrchestrator(
            pr_number=1, repo="a/b", head_sha="x", event="s",
            branch="m", labels=[], config=cfg,
        )
        with pytest.raises(RuntimeError, match="clone_path"):
            o._clone_path()


# ── Cancel ───────────────────────────────────────────────────


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

    @patch("gate.orchestrator.kill_window")
    @patch("gate.orchestrator.github")
    def test_cancel_completes_check_run(self, mock_github, mock_kill, orchestrator):
        orchestrator.check_run_id = 42
        orchestrator.cancel()
        mock_github.complete_check_run.assert_called_once()
        call = mock_github.complete_check_run.call_args
        assert call[1]["conclusion"] == "cancelled"

    @patch("gate.orchestrator.workspace_mod")
    @patch("gate.orchestrator.kill_window")
    @patch("gate.orchestrator.github")
    def test_cancel_does_not_remove_worktree(
        self, mock_github, mock_kill, mock_ws, orchestrator, tmp_path,
    ):
        """Group 2B: cancel() must not call remove_worktree — that is
        owned by run()'s finally block so stages see a live worktree
        until they yield on the cancellation flag."""
        orchestrator.workspace = tmp_path
        orchestrator.cancel()
        mock_ws.remove_worktree.assert_not_called()


class TestCancelReasons:
    """Issue #17: cancel() routes the GitHub payload by reason so the
    PR/issue author doesn't see ``Superseded by newer push`` on a manual
    cancel."""

    @patch("gate.orchestrator.kill_window")
    @patch("gate.orchestrator.github")
    def test_superseded_default_reports_cancelled(
        self, mock_github, _mock_kill, orchestrator,
    ):
        orchestrator.check_run_id = 42
        orchestrator.cancel()  # default reason = "superseded"
        call = mock_github.complete_check_run.call_args
        assert call[1]["conclusion"] == "cancelled"
        assert call[1]["output_title"] == "Superseded by newer push"
        assert "newer commit" in call[1]["output_summary"].lower()

    @patch("gate.orchestrator.kill_window")
    @patch("gate.orchestrator.github")
    def test_manual_reports_neutral(
        self, mock_github, _mock_kill, orchestrator,
    ):
        orchestrator.check_run_id = 42
        orchestrator.cancel(reason="manual")
        call = mock_github.complete_check_run.call_args
        # The whole point of Issue #17: operator cancel is NOT a failure.
        assert call[1]["conclusion"] == "neutral"
        assert call[1]["output_title"] == "Review cancelled by operator"
        assert "gate cancel" in call[1]["output_summary"]

    @patch("gate.orchestrator.kill_window")
    @patch("gate.orchestrator.github")
    def test_timeout_reports_cancelled(
        self, mock_github, _mock_kill, orchestrator,
    ):
        orchestrator.check_run_id = 42
        orchestrator.cancel(reason="timeout")
        call = mock_github.complete_check_run.call_args
        assert call[1]["conclusion"] == "cancelled"
        assert call[1]["output_title"] == "Review timed out"

    @patch("gate.orchestrator.kill_window")
    @patch("gate.orchestrator.github")
    def test_unknown_reason_raises_keyerror(
        self, _mock_github, _mock_kill, orchestrator,
    ):
        """Typos in reason strings should surface loudly, not silently
        route to a default payload (which is how we ended up with
        Issue #17 in the first place)."""
        orchestrator.check_run_id = 42
        with pytest.raises(KeyError):
            orchestrator.cancel(reason="definitely-not-a-reason")
        # The guard still set _cancelled before the lookup — that's fine;
        # the second cancel with a valid reason would now no-op, which
        # is the safe failure mode. But complete_check_run must not have
        # been invoked with a partial/default payload.

    @patch("gate.orchestrator.kill_window")
    @patch("gate.orchestrator.github")
    def test_cancel_is_idempotent_on_concurrent_calls(
        self, mock_github, _mock_kill, orchestrator,
    ):
        """Server.py echoes the orchestrator's own ``review_cancelled``
        event back into ``queue.cancel_pr``. Without the idempotency
        guard, the second cancel would overwrite the first payload —
        e.g. a manual cancel followed by the echoed re-entry would flip
        the status from ``neutral`` back to ``cancelled``."""
        orchestrator.check_run_id = 42

        orchestrator.cancel(reason="manual")
        orchestrator.cancel(reason="superseded")  # would clobber — must no-op
        orchestrator.cancel(reason="timeout")

        assert mock_github.complete_check_run.call_count == 1
        call = mock_github.complete_check_run.call_args
        # First writer wins, so the neutral "manual" payload survives.
        assert call[1]["conclusion"] == "neutral"
        assert call[1]["output_title"] == "Review cancelled by operator"

    @patch("gate.orchestrator.kill_window")
    @patch("gate.orchestrator.github")
    def test_cancel_concurrent_threads_single_writer(
        self, mock_github, _mock_kill, orchestrator,
    ):
        """Race the lock: N threads all call ``cancel()`` simultaneously.
        Exactly one must win the check-then-set and invoke
        ``complete_check_run`` — the rest must observe ``_cancelled``
        already set and return."""
        orchestrator.check_run_id = 42
        barrier = threading.Barrier(8)

        def racer(reason: str) -> None:
            barrier.wait()
            orchestrator.cancel(reason=reason)

        threads = [
            threading.Thread(target=racer, args=("manual" if i % 2 else "superseded",))
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert mock_github.complete_check_run.call_count == 1


class TestSaveStageResult:
    """Group 2A: _save_stage_result must be a no-op when the review was
    cancelled or the workspace vanished."""

    def test_noop_when_cancelled(self, orchestrator, tmp_path):
        from gate.schemas import StageResult

        orchestrator.workspace = tmp_path
        orchestrator._cancelled.set()
        result = StageResult(stage="triage", success=True, data={"ok": True})
        orchestrator._save_stage_result("triage", result)
        assert not (tmp_path / "triage.json").exists()

    def test_noop_when_workspace_missing(self, orchestrator, tmp_path):
        from gate.schemas import StageResult

        orchestrator.workspace = tmp_path / "ghost"
        orchestrator._cancelled.set()
        result = StageResult(stage="triage", success=True, data={"ok": True})
        orchestrator._save_stage_result("triage", result)

    def test_swallows_file_not_found_after_cancel(self, orchestrator, tmp_path):
        from unittest.mock import patch

        from gate.schemas import StageResult

        orchestrator.workspace = tmp_path
        orchestrator._cancelled.set()
        result = StageResult(stage="triage", success=True, data={"ok": True})
        # Workspace exists at dispatch, but write_text raises (e.g. a
        # concurrent cleanup races between the exists() check and the
        # actual write).
        with patch("pathlib.Path.write_text", side_effect=FileNotFoundError(2, "gone")):
            orchestrator._save_stage_result("triage", result)

    def test_writes_when_healthy(self, orchestrator, tmp_path):
        from gate.schemas import StageResult

        orchestrator.workspace = tmp_path
        result = StageResult(stage="triage", success=True, data={"ok": True})
        orchestrator._save_stage_result("triage", result)
        assert (tmp_path / "triage.json").exists()


# ── Gate 1: Labels ───────────────────────────────────────────


class TestLabelGates:
    def test_skip_label_approves(self, sample_config, mocks):
        orch = ReviewOrchestrator(
            pr_number=10, repo="test-org/test-repo", head_sha="abc123",
            event="synchronize", branch="main", labels=["gate-skip"],
            config=sample_config,
        )
        orch.run()
        mocks["github"].approve_pr.assert_called_once()
        mocks["github"].complete_check_run.assert_called_once()

    def test_skip_label_short_circuits_before_stages(self, sample_config, mocks):
        orch = ReviewOrchestrator(
            pr_number=10, repo="test-org/test-repo", head_sha="abc123",
            event="synchronize", branch="main", labels=["gate-skip"],
            config=sample_config,
        )
        orch.run()
        mocks["workspace_mod"].create_worktree.assert_not_called()
        mocks["builder"].run_build.assert_not_called()

    def test_emergency_bypass_label(self, sample_config, mocks):
        orch = ReviewOrchestrator(
            pr_number=11, repo="test-org/test-repo", head_sha="def456",
            event="synchronize", branch="hotfix",
            labels=["gate-emergency-bypass"], config=sample_config,
        )
        orch.run()
        mocks["github"].approve_pr.assert_called_once()
        msg = mocks["github"].approve_pr.call_args[0][2]
        assert "bypass" in msg.lower()

    def test_gate_rerun_label_removed_on_labeled_event(self, sample_config, mocks):
        orch = ReviewOrchestrator(
            pr_number=12, repo="test-org/test-repo", head_sha="xyz",
            event="labeled", branch="feature",
            labels=["gate-rerun"], config=sample_config,
        )
        # Pre-emptively force a non-fix-rerun path by having no prior review
        with patch("gate.orchestrator.builder.run_build",
                   return_value={"overall_pass": True}):
            try:
                orch.run()
            except Exception:
                pass
        # remove_label should be called with gate-rerun
        removed = any(
            call.args[2] == "gate-rerun"
            for call in mocks["github"].remove_label.call_args_list
        )
        assert removed


# ── Gate 2: Circuit breaker ──────────────────────────────────


class TestCircuitBreakerGate:
    def test_three_errors_trips_breaker(self, sample_config, mocks):
        mocks["read_recent_decisions"].return_value = ["error", "error", "error"]
        orch = ReviewOrchestrator(
            pr_number=20, repo="test-org/test-repo", head_sha="x",
            event="synchronize", branch="m", labels=[], config=sample_config,
        )
        orch.run()
        mocks["github"].approve_pr.assert_called_once()
        msg = mocks["github"].approve_pr.call_args[0][2]
        assert "circuit breaker" in msg.lower()
        mocks["notify"].circuit_breaker.assert_called_once()

    def test_mixed_decisions_does_not_trip(self, sample_config, mocks):
        mocks["read_recent_decisions"].return_value = ["approve", "error", "error"]
        orch = ReviewOrchestrator(
            pr_number=21, repo="test-org/test-repo", head_sha="x",
            event="synchronize", branch="m", labels=[], config=sample_config,
        )
        # Just make sure we progress past the breaker; mock rest to keep it simple
        with patch("gate.orchestrator.builder.run_build", return_value={"overall_pass": True}):
            try:
                orch.run()
            except Exception:
                pass
        # The notify.circuit_breaker is only called when breaker trips
        mocks["notify"].circuit_breaker.assert_not_called()


# ── Gate 4: Quota ────────────────────────────────────────────


class TestQuotaGate:
    def test_quota_not_ok_approves_and_defers(self, sample_config, mocks):
        mocks["quota_mod"].check_quota.return_value = {
            "quota_ok": False, "five_hour_pct": 95, "seven_day_pct": 90,
            "reason": "quota low",
        }
        orch = ReviewOrchestrator(
            pr_number=30, repo="test-org/test-repo", head_sha="x",
            event="synchronize", branch="m", labels=[], config=sample_config,
        )
        orch.run()
        # Should approve and comment, no stages run
        mocks["github"].comment_pr.assert_called()
        mocks["github"].approve_pr.assert_called_once()
        msg = mocks["github"].approve_pr.call_args[0][2]
        assert "quota" in msg.lower()
        mocks["builder"].run_build.assert_not_called()


# ── Gate 5: Cycle limit ──────────────────────────────────────


class TestCycleLimitGate:
    def test_cycle_limit_reached_approves(self, sample_config, mocks):
        mocks["state"].load_prior_review.return_value = {
            "has_prior": True, "prior_decision": "approve",
            "review_count": 5, "fix_attempts": 0,
        }
        orch = ReviewOrchestrator(
            pr_number=40, repo="test-org/test-repo", head_sha="x",
            event="synchronize", branch="m", labels=[], config=sample_config,
        )
        orch.run()
        mocks["github"].approve_pr.assert_called_once()
        msg = mocks["github"].approve_pr.call_args[0][2]
        assert "cycle limit" in msg.lower()
        mocks["builder"].run_build.assert_not_called()

    def test_under_limit_proceeds(self, sample_config, mocks):
        """Under the cycle limit, the cycle gate does NOT short-circuit.

        We don't assert that `run_build` was called because later stages may
        crash first; we only assert the gate-specific approve message is not
        the one emitted.
        """
        mocks["state"].load_prior_review.return_value = {
            "has_prior": True, "prior_decision": "approve",
            "review_count": 2, "fix_attempts": 0,
        }
        orch = ReviewOrchestrator(
            pr_number=41, repo="test-org/test-repo", head_sha="x",
            event="synchronize", branch="m", labels=[], config=sample_config,
        )
        try:
            orch.run()
        except Exception:
            pass
        # If cycle gate tripped it would have emitted an approve with "cycle limit"
        calls = mocks["github"].approve_pr.call_args_list
        cycle_msgs = [c for c in calls if "cycle limit" in c[0][2].lower()]
        assert cycle_msgs == []


# ── Fail-open ────────────────────────────────────────────────


class TestFailOpen:
    def test_exception_approves_fail_open(self, sample_config, mocks):
        mocks["github"].get_pr_info.side_effect = RuntimeError("API exploded")
        orch = ReviewOrchestrator(
            pr_number=99, repo="test-org/test-repo", head_sha="fff999",
            event="synchronize", branch="boom", labels=[], config=sample_config,
        )
        orch.run()
        mocks["github"].approve_pr.assert_called_once()
        msg = mocks["github"].approve_pr.call_args[0][2]
        assert "error" in msg.lower() or "fail" in msg.lower()
        mocks["notify"].review_failed.assert_called_once()

    def test_exception_completes_check_run_cancelled(self, sample_config, mocks):
        mocks["github"].get_pr_info.side_effect = RuntimeError("boom")
        orch = ReviewOrchestrator(
            pr_number=99, repo="test-org/test-repo", head_sha="f",
            event="synchronize", branch="b", labels=[], config=sample_config,
        )
        orch.run()
        # check run should be completed with cancelled
        calls = mocks["github"].complete_check_run.call_args_list
        assert any(
            call.kwargs.get("conclusion") == "cancelled" for call in calls
        )

    def test_exception_removes_workspace(self, sample_config, mocks):
        mocks["github"].get_pr_info.side_effect = RuntimeError("boom")
        ws = MagicMock()
        mocks["workspace_mod"].create_worktree.return_value = ws
        orch = ReviewOrchestrator(
            pr_number=99, repo="test-org/test-repo", head_sha="f",
            event="synchronize", branch="b", labels=[], config=sample_config,
        )
        orch.run()
        # The exception happens before create_worktree, so no remove needed.
        # But: workspace_mod.remove_worktree is in the finally path when
        # workspace is set. Ensure the flow doesn't crash.
        assert orch.workspace is None


# ── Fix rerun detection ──────────────────────────────────────


class TestLoadCachedPostconditions:
    """Phase 3: fix-reruns must reuse the postconditions from the initial
    review so Logic sees the same contract on every iteration."""

    def test_returns_none_when_no_cache(self, orchestrator, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with patch("gate.orchestrator.state") as mock_state:
            mock_state.get_pr_state_dir.return_value = state_dir
            result = orchestrator._load_cached_postconditions()
        assert result is None

    def test_loads_cached_json(self, orchestrator, tmp_path):
        import json as _json

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        pc = {"postconditions": [{"function_path": "x:y", "prose": "p", "confidence": "high"}]}
        (state_dir / "postconditions.json").write_text(_json.dumps(pc))
        with patch("gate.orchestrator.state") as mock_state:
            mock_state.get_pr_state_dir.return_value = state_dir
            result = orchestrator._load_cached_postconditions()
        assert result is not None
        assert result.data == pc
        # Should also copy to workspace for prompt vars
        ws_copy = orchestrator.workspace / "postconditions.json"
        assert ws_copy.exists()

    def test_returns_none_on_corrupt_cache(self, orchestrator, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "postconditions.json").write_text("not json")
        with patch("gate.orchestrator.state") as mock_state:
            mock_state.get_pr_state_dir.return_value = state_dir
            result = orchestrator._load_cached_postconditions()
        assert result is None


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

    def test_no_prior_decision_is_not_fix_rerun(self, orchestrator):
        prior = {"has_prior": True, "prior_decision": "", "fix_attempts": 0}
        assert not orchestrator._detect_fix_rerun(prior)


# ── Concurrency ──────────────────────────────────────────────


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


# ── Active marker lifecycle ──────────────────────────────────


class TestActiveMarker:
    def test_marker_written_and_removed_on_label_skip(self, sample_config, mocks, tmp_path):
        """Skip path writes and removes the active marker."""
        mocks["state"].get_pr_state_dir.return_value = tmp_path
        orch = ReviewOrchestrator(
            pr_number=50, repo="test-org/test-repo", head_sha="x",
            event="synchronize", branch="m",
            labels=["gate-skip"], config=sample_config,
        )
        orch.run()
        # Marker was written and then removed (state dir empty of marker)
        assert not (tmp_path / "active_review.json").exists()


# ── Emit helper ──────────────────────────────────────────────


class TestEmit:
    def test_emit_without_connection_noop(self, orchestrator):
        # No exception should be raised
        orchestrator._emit("review_started", review={})

    def test_emit_with_connection_calls_emit(self, sample_config, tmp_path):
        with patch("gate.client.GateConnection") as conn_cls:
            conn = MagicMock()
            conn_cls.return_value = conn
            o = ReviewOrchestrator(
                pr_number=1, repo="a/b", head_sha="x", event="s",
                branch="m", labels=[], config=sample_config,
                socket_path=tmp_path / "sock",
            )
            o._emit("review_started", review={"id": "x"})
            conn.emit.assert_called_once_with(
                "review_started", review={"id": "x"}, head_sha="x"
            )

    def test_emit_stamps_head_sha_for_race_disambiguation(
        self, sample_config, tmp_path
    ):
        """Every lifecycle event is stamped with this orchestrator's
        ``head_sha`` so the server can drop late events from a superseded
        orchestrator on the same PR (both share ``review_id``)."""
        with patch("gate.client.GateConnection") as conn_cls:
            conn = MagicMock()
            conn_cls.return_value = conn
            o = ReviewOrchestrator(
                pr_number=1, repo="a/b", head_sha="deadbeef", event="s",
                branch="m", labels=[], config=sample_config,
                socket_path=tmp_path / "sock",
            )
            o._emit("review_cancelled", review_id="a-b-pr1")
            conn.emit.assert_called_once_with(
                "review_cancelled", review_id="a-b-pr1", head_sha="deadbeef"
            )

    def test_emit_preserves_explicit_head_sha_if_caller_provides(
        self, sample_config, tmp_path
    ):
        with patch("gate.client.GateConnection") as conn_cls:
            conn = MagicMock()
            conn_cls.return_value = conn
            o = ReviewOrchestrator(
                pr_number=1, repo="a/b", head_sha="deadbeef", event="s",
                branch="m", labels=[], config=sample_config,
                socket_path=tmp_path / "sock",
            )
            o._emit("review_cancelled", review_id="a-b-pr1", head_sha="override")
            conn.emit.assert_called_once_with(
                "review_cancelled", review_id="a-b-pr1", head_sha="override"
            )

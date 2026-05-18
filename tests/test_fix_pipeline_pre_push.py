"""Integration tests for the strict pre-push gate in ``FixPipeline.run``.

Motivated by PR #399 — the fix passed re-review under
``build_verify``'s pre-existing-failure tolerance, then was rejected
at ``git push`` by the Husky pre-push hook. The strict gate inserts a
pass/fail check between re-review and ``_commit_and_finish`` that
mirrors what the remote hook will face.

Coverage:

- Strict pass → short-circuits to ``_commit_and_finish``
- Regression → resume session + retry inside the same iteration
- Pre-existing failures → fail fast without burning iterations
- ``pre_push_disable`` → preserves legacy behavior
- Iteration budget defaults to 3
- No revert on regression (worktree state preserved across iterations)
"""

from unittest.mock import MagicMock, patch

import pytest

from gate.fixer import FixPipeline


def _make_pipeline(sample_config, tmp_path, repo_overrides=None):
    """Build a FixPipeline ready for ``run()`` with realistic state.

    The verdict has one finding so the iteration loop is actually
    entered (the polish-loop branch only triggers when polish mode is
    on, which sample_config does not enable). build={} is the empty
    baseline — original_build for pre_push_verify will be None, which
    routes uncertain failures to "regression" (safer side).
    """
    findings = [
        {
            "severity": "warning", "message": "test finding",
            "file": "a.ts", "line": 1, "rule": "test/rule",
        }
    ]
    verdict = {"decision": "request_changes", "findings": findings}
    (tmp_path / "verdict.json").write_text("{}")
    (tmp_path / "triage.json").write_text("{}")
    if repo_overrides:
        sample_config = dict(sample_config)
        sample_config["repo"] = {**sample_config["repo"], **repo_overrides}
    return FixPipeline(
        pr_number=399, repo="openlawteam/adin-chat", workspace=tmp_path,
        verdict=verdict, build={}, config=sample_config,
    )


@pytest.fixture
def patched_pipeline_env(sample_config, tmp_path):
    """All the I/O the fix pipeline reaches for, mocked at the right
    seams. Returns the (pipe, mocks) tuple; tests then override the
    specific seams they care about (pre_push_verify, _run_rereview)."""
    with (
        patch("gate.fixer.notify"),
        patch("gate.fixer.state.check_fix_limits", return_value=(True, "")),
        patch("gate.fixer.state.record_fix_attempt"),
        patch("gate.fixer.codex_health_check", return_value=(True, "0.128.0")),
        patch("gate.fixer.bootstrap_codex", return_value=(0, "thread-abc", "")),
        patch("gate.fixer.github"),
        patch("gate.fixer.enforce_blocklist"),
        patch("gate.fixer.cleanup_gate_tests"),
        patch("gate.fixer.write_live_log"),
    ):
        pipe = _make_pipeline(sample_config, tmp_path)
        pipe._run_fix_session = MagicMock(
            return_value={"has_changes": True, "fix_json": {"fixed": [], "not_fixed": []}}
        )
        pipe._resume_fix_session = MagicMock(
            return_value={"has_changes": True, "fix_json": None}
        )
        pipe._is_graceful_noop_case = MagicMock(return_value=False)
        pipe._write_baseline_diff = MagicMock()
        pipe._emit_fix_stage = MagicMock()
        pipe._revert_to_baseline = MagicMock()
        yield pipe


class TestPrePushGateHappyPath:
    """Strict pass → no retry, straight to commit."""

    @patch("gate.fixer.pre_push_verify")
    @patch("gate.fixer.build_verify")
    def test_strict_pass_short_circuits_to_commit(
        self, mock_bv, mock_pp, patched_pipeline_env,
    ):
        pipe = patched_pipeline_env
        mock_bv.return_value = {
            "pass": True, "strict_pass": True, "build_result": None,
            "typecheck_errors": 0, "lint_errors": 0, "test_failures": 0,
            "typecheck_log": "", "lint_log": "", "typecheck_tool": "",
        }
        mock_pp.return_value = {"pass": True, "kind": "pass"}
        pipe._run_rereview = MagicMock(return_value=True)
        commit_mock = MagicMock(return_value=MagicMock(success=True, summary="ok"))
        pipe._commit_and_finish = commit_mock

        result = pipe.run()

        assert result.success is True
        assert mock_pp.call_count == 1, "pre_push_verify called exactly once"
        commit_mock.assert_called_once()
        pipe._resume_fix_session.assert_not_called(), "no retry on happy path"


class TestPrePushGateRegression:
    """Regression → same-iteration retry."""

    @patch("gate.fixer.pre_push_verify")
    @patch("gate.fixer.build_verify")
    def test_regression_retry_succeeds_then_commits(
        self, mock_bv, mock_pp, patched_pipeline_env,
    ):
        """First pre_push fails (regression), retry passes, commit fires."""
        pipe = patched_pipeline_env
        mock_bv.return_value = {
            "pass": True, "strict_pass": False,
            "build_result": {"tests": {"failed": 2}},
            "typecheck_errors": 0, "lint_errors": 0, "test_failures": 2,
            "typecheck_log": "", "lint_log": "", "typecheck_tool": "",
        }
        mock_pp.side_effect = [
            {"pass": False, "kind": "regression", "test_failures": 2,
             "baseline_failures": 0, "failed_cmd": "", "logs": ""},
            {"pass": True, "kind": "pass"},
        ]
        pipe._run_rereview = MagicMock(return_value=True)
        commit_mock = MagicMock(return_value=MagicMock(success=True, summary="ok"))
        pipe._commit_and_finish = commit_mock

        result = pipe.run()

        assert result.success is True
        assert mock_pp.call_count == 2, "called twice — initial + retry"
        pipe._resume_fix_session.assert_called_once(), "regression triggers resume"
        commit_mock.assert_called_once()
        pipe._revert_to_baseline.assert_not_called(), (
            "must NOT revert on regression — keep partial fix for retry"
        )

    @patch("gate.fixer.pre_push_verify")
    @patch("gate.fixer.build_verify")
    def test_regression_retry_fails_continues_no_revert(
        self, mock_bv, mock_pp, patched_pipeline_env,
    ):
        """Both pre_push attempts fail → continues to next iteration
        without reverting (worktree state preserved for iter N+1)."""
        pipe = patched_pipeline_env
        mock_bv.return_value = {
            "pass": True, "strict_pass": False,
            "build_result": {"tests": {"failed": 2}},
            "typecheck_errors": 0, "lint_errors": 0, "test_failures": 2,
            "typecheck_log": "", "lint_log": "", "typecheck_tool": "",
        }
        # Every pre_push call: regression. With max_iter=3 we expect
        # 2 calls per iteration (initial + retry) × 3 iterations = 6.
        mock_pp.return_value = {
            "pass": False, "kind": "regression", "test_failures": 2,
            "baseline_failures": 0, "failed_cmd": "", "logs": "",
        }
        pipe._run_rereview = MagicMock(return_value=True)
        pipe._commit_and_finish = MagicMock()

        result = pipe.run()

        assert result.success is False
        assert "Pre-push test regressions" in result.summary
        # 2 calls per iter × 3 iters
        assert mock_pp.call_count == 6
        pipe._commit_and_finish.assert_not_called()
        pipe._revert_to_baseline.assert_not_called(), (
            "no revert across iterations — partial fix preserved"
        )


class TestPrePushGatePreExisting:
    """Pre-existing baseline failures → fail fast."""

    @patch("gate.fixer.pre_push_verify")
    @patch("gate.fixer.build_verify")
    def test_pre_existing_fails_fast_no_retry(
        self, mock_bv, mock_pp, patched_pipeline_env,
    ):
        pipe = patched_pipeline_env
        mock_bv.return_value = {
            "pass": True, "strict_pass": False,
            "build_result": {"tests": {"failed": 3}},
            "typecheck_errors": 0, "lint_errors": 0, "test_failures": 3,
            "typecheck_log": "", "lint_log": "", "typecheck_tool": "",
        }
        mock_pp.return_value = {
            "pass": False, "kind": "pre_existing",
            "test_failures": 3, "baseline_failures": 3,
            "failed_cmd": "", "logs": "",
        }
        pipe._run_rereview = MagicMock(return_value=True)
        commit_mock = MagicMock()
        pipe._commit_and_finish = commit_mock

        result = pipe.run()

        assert result.success is False
        assert "pre-existing" in result.summary.lower()
        assert "baseline" in result.summary.lower()
        # Should call pre_push_verify exactly ONCE — no retry, no continue.
        assert mock_pp.call_count == 1, "pre-existing fails fast, no retry"
        pipe._resume_fix_session.assert_not_called()
        commit_mock.assert_not_called()


class TestPrePushGateDisabled:
    """``pre_push_disable = true`` → legacy behavior (commit on rereview pass)."""

    @patch("gate.fixer.build_verify")
    def test_disable_skips_gate(self, mock_bv, sample_config, tmp_path):
        repo_overrides = {"build": {"pre_push_disable": True}}
        with (
            patch("gate.fixer.notify"),
            patch("gate.fixer.state.check_fix_limits", return_value=(True, "")),
            patch("gate.fixer.state.record_fix_attempt"),
            patch("gate.fixer.codex_health_check", return_value=(True, "0.128.0")),
            patch("gate.fixer.bootstrap_codex", return_value=(0, "thread-abc", "")),
            patch("gate.fixer.github"),
            patch("gate.fixer.enforce_blocklist"),
            patch("gate.fixer.cleanup_gate_tests"),
            patch("gate.fixer.write_live_log"),
        ):
            pipe = _make_pipeline(sample_config, tmp_path, repo_overrides)
            # Even strict failure here should not block — disable bypasses.
            mock_bv.return_value = {
                "pass": True, "strict_pass": False,
                "build_result": {"tests": {"failed": 5}},
                "typecheck_errors": 0, "lint_errors": 0, "test_failures": 5,
                "typecheck_log": "", "lint_log": "", "typecheck_tool": "",
            }
            pipe._run_fix_session = MagicMock(
                return_value={"has_changes": True, "fix_json": {"fixed": [], "not_fixed": []}}
            )
            pipe._is_graceful_noop_case = MagicMock(return_value=False)
            pipe._write_baseline_diff = MagicMock()
            pipe._emit_fix_stage = MagicMock()
            pipe._revert_to_baseline = MagicMock()
            pipe._run_rereview = MagicMock(return_value=True)
            commit_mock = MagicMock(return_value=MagicMock(success=True, summary="ok"))
            pipe._commit_and_finish = commit_mock

            result = pipe.run()

            assert result.success is True
            commit_mock.assert_called_once()


class TestIterationBudgetIs3:
    """The outer fix loop now defaults to 3 iterations (was 2)."""

    @patch("gate.fixer.pre_push_verify")
    @patch("gate.fixer.build_verify")
    def test_default_budget_runs_3_iterations(
        self, mock_bv, mock_pp, patched_pipeline_env,
    ):
        """Forcing re-review rejection on every iter: should attempt
        iter 1, 2, 3 (and bail after the third)."""
        pipe = patched_pipeline_env
        mock_bv.return_value = {
            "pass": True, "strict_pass": True, "build_result": None,
            "typecheck_errors": 0, "lint_errors": 0, "test_failures": 0,
            "typecheck_log": "", "lint_log": "", "typecheck_tool": "",
        }
        # Force re-review fail every time so the pre-push gate never
        # runs — we're checking iter budget alone.
        mock_pp.return_value = {"pass": True, "kind": "pass"}
        pipe._run_rereview = MagicMock(return_value=False)
        pipe._commit_and_finish = MagicMock()

        result = pipe.run()

        assert result.success is False
        assert "Re-review rejected after 3 iterations" in result.summary
        assert pipe._run_rereview.call_count == 3

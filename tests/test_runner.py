"""Tests for gate.runner module.

Tests command building for each stage type, mock tmux execution,
StructuredRunner parsing, and run_with_retry logic.
"""

import json
from unittest.mock import patch

from gate.runner import (
    ReviewRunner,
    StructuredRunner,
    extract_error_message,
    run_with_retry,
)
from gate.schemas import StageResult


class TestExtractErrorMessage:
    def test_extracts_last_lines(self):
        stderr = b"line1\nline2\nline3\nline4\nline5\nline6\nline7\n"
        result = extract_error_message(stderr)
        assert "line7" in result
        assert "line3" in result

    def test_empty_stderr(self):
        assert extract_error_message(b"") is None
        assert extract_error_message(None) is None

    def test_single_line(self):
        result = extract_error_message(b"error message\n")
        assert result == "error message"


class TestReviewRunnerBuildCommand:
    def test_agent_stage_command(self, tmp_workspace, sample_config):
        runner = ReviewRunner(
            review_id="test-org-test-repo-pr42",
            stage="architecture",
            workspace=tmp_workspace,
            config=sample_config,
        )
        runner._prompt_text = "Review this code"
        runner._session_id = "test-session-id"

        cmd, cwd = runner._build_command()

        assert cmd[0] == "claude"
        assert "--dangerously-skip-permissions" in cmd
        assert "--session-id" in cmd
        assert "test-session-id" in cmd
        assert "--model" in cmd
        assert "sonnet" in cmd
        assert cmd[-1] == "Review this code"
        assert cwd == str(tmp_workspace)

    def test_security_stage_uses_opus(self, tmp_workspace, sample_config):
        runner = ReviewRunner(
            review_id="test-org-test-repo-pr42",
            stage="security",
            workspace=tmp_workspace,
            config=sample_config,
        )
        runner._prompt_text = "Check security"
        runner._session_id = "test-id"

        cmd, _ = runner._build_command()
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "opus"

    def test_fix_senior_has_effort_max(self, tmp_workspace, sample_config):
        runner = ReviewRunner(
            review_id="test-org-test-repo-pr42",
            stage="fix-senior",
            workspace=tmp_workspace,
            config=sample_config,
        )
        runner._prompt_text = "Fix these issues"
        runner._session_id = "test-id"

        cmd, _ = runner._build_command()
        assert "--effort" in cmd
        effort_idx = cmd.index("--effort")
        assert cmd[effort_idx + 1] == "max"

    def test_context_file_included(self, tmp_workspace, sample_config):
        context = tmp_workspace / "architecture-context.md"
        context.write_text("Extra context here")

        runner = ReviewRunner(
            review_id="test-org-test-repo-pr42",
            stage="architecture",
            workspace=tmp_workspace,
            config=sample_config,
        )
        runner._prompt_text = "Review"
        runner._session_id = "test-id"

        cmd, _ = runner._build_command()
        assert "--append-system-prompt-file" in cmd

    def test_no_context_file(self, tmp_workspace, sample_config):
        runner = ReviewRunner(
            review_id="test-org-test-repo-pr42",
            stage="architecture",
            workspace=tmp_workspace,
            config=sample_config,
        )
        runner._prompt_text = "Review"
        runner._session_id = "test-id"

        cmd, _ = runner._build_command()
        assert "--append-system-prompt-file" not in cmd


class TestReviewRunnerExtractResult:
    def test_reads_findings_file(self, tmp_workspace, sample_config):
        findings = {"findings": [{"message": "test"}], "pass": True}
        (tmp_workspace / "architecture-findings.json").write_text(json.dumps(findings))

        runner = ReviewRunner(
            review_id="test-org-test-repo-pr42",
            stage="architecture",
            workspace=tmp_workspace,
            config=sample_config,
        )
        result = runner._extract_and_write_result()
        assert result is not None
        assert result["findings"][0]["message"] == "test"

        envelope = json.loads((tmp_workspace / "architecture-result.json").read_text())
        assert envelope["success"] is True

    def test_falls_back_to_transcript(self, tmp_workspace, sample_config):
        transcript = json.dumps([
            {
                "role": "assistant",
                "content": '{"findings": [{"message": "from transcript"}], "pass": true}',
            }
        ])
        (tmp_workspace / "architecture-raw.json").write_text(transcript)

        runner = ReviewRunner(
            review_id="test-org-test-repo-pr42",
            stage="architecture",
            workspace=tmp_workspace,
            config=sample_config,
        )
        result = runner._extract_and_write_result()
        assert result is not None
        assert result["findings"][0]["message"] == "from transcript"

    def test_writes_fallback_on_no_result(self, tmp_workspace, sample_config):
        runner = ReviewRunner(
            review_id="test-org-test-repo-pr42",
            stage="architecture",
            workspace=tmp_workspace,
            config=sample_config,
        )
        result = runner._extract_and_write_result()
        assert result is None

        envelope = json.loads((tmp_workspace / "architecture-result.json").read_text())
        assert envelope["success"] is False
        assert envelope["data"]["error"] == "stage_failed"


class TestStructuredRunner:
    def test_parse_output_json_schema_envelope(self):
        runner = StructuredRunner()
        stdout = json.dumps({"structured_output": {"decision": "approve", "confidence": "high"}})
        result = runner._parse_output(stdout, "verdict")
        assert result["decision"] == "approve"

    def test_parse_output_raw_json(self):
        runner = StructuredRunner()
        stdout = '{"change_type": "bugfix", "risk_level": "low", "summary": "test"}'
        result = runner._parse_output(stdout, "triage")
        assert result["change_type"] == "bugfix"

    def test_parse_output_empty(self):
        runner = StructuredRunner()
        assert runner._parse_output("", "triage") is None
        assert runner._parse_output("   ", "triage") is None

    @patch("gate.runner.subprocess.run")
    def test_run_passes_prompt_via_stdin_not_argv(self, mock_run, tmp_path):
        """Regression test for ARG_MAX overflow on PRs with huge diffs.

        Before this fix, the assembled prompt was appended to argv with
        ``cmd.append(prompt_text)`` and any prompt larger than macOS
        ARG_MAX (~1 MB) raised
        ``OSError: [Errno 7] Argument list too long: 'claude'`` inside
        ``_execute_child`` before claude even started — silently failing
        the structured stage with no actionable error.

        adin-chat PR #261 (554 files, 5.5 MB diff) was the original repro.
        """
        mock_run.return_value = type(
            "P", (), {"returncode": 0, "stdout": '{"change_type":"x"}', "stderr": ""}
        )()
        runner = StructuredRunner()
        prompt = "x" * 5_500_000  # 5.5 MB — same scale as PR #261
        runner.run("triage", prompt, tmp_path, {"models": {}, "timeouts": {}})
        call = mock_run.call_args
        cmd = call.args[0]
        assert prompt not in cmd, (
            "prompt must NOT be appended to argv (would hit ARG_MAX on big PRs); "
            "pass it via input= instead"
        )
        assert call.kwargs.get("input") == prompt, "prompt must be piped via stdin"
        assert "--print" in cmd, "claude must run with --print so it reads stdin"

    @patch("gate.runner.subprocess.run")
    def test_run_does_not_set_stdin_kwarg(self, mock_run, tmp_path):
        """``input=`` and ``stdin=`` are mutually exclusive in subprocess.run.

        Setting both raises ValueError at runtime. Pin the contract so
        nobody re-adds an explicit ``stdin=DEVNULL``.
        """
        mock_run.return_value = type(
            "P", (), {"returncode": 0, "stdout": "{}", "stderr": ""}
        )()
        runner = StructuredRunner()
        runner.run("triage", "tiny prompt", tmp_path, {"models": {}, "timeouts": {}})
        assert "stdin" not in mock_run.call_args.kwargs


class TestRunWithRetry:
    def test_success_on_first_try(self, sample_config):
        success = StageResult(stage="triage", success=True, data={"key": "val"})
        result = run_with_retry(lambda: success, "triage", sample_config)
        assert result.success is True

    def test_retries_on_rate_limit(self, sample_config):
        call_count = [0]

        def run_fn():
            call_count[0] += 1
            if call_count[0] < 3:
                return StageResult(stage="triage", success=False, is_rate_limited=True)
            return StageResult(stage="triage", success=True, data={"ok": True})

        with patch("gate.runner.time.sleep"):
            result = run_with_retry(run_fn, "triage", sample_config)
        assert result.success is True
        assert call_count[0] == 3

    def test_retries_on_transient_error(self, sample_config):
        call_count = [0]

        def run_fn():
            call_count[0] += 1
            if call_count[0] < 2:
                return StageResult(stage="triage", success=False, is_transient=True)
            return StageResult(stage="triage", success=True, data={"ok": True})

        with patch("gate.runner.time.sleep"):
            result = run_with_retry(run_fn, "triage", sample_config)
        assert result.success is True
        assert call_count[0] == 2

    def test_returns_fallback_after_max_retries(self, sample_config):
        fail = StageResult(stage="triage", success=False, is_rate_limited=True)

        with patch("gate.runner.time.sleep"):
            result = run_with_retry(lambda: fail, "triage", sample_config)
        assert result.success is True  # fallback is fail-open
        assert result.data["flags"] == ["triage_fallback"]

    def test_stops_on_non_retryable_error(self, sample_config):
        call_count = [0]

        def run_fn():
            call_count[0] += 1
            return StageResult(stage="triage", success=False)

        result = run_with_retry(run_fn, "triage", sample_config)
        assert call_count[0] == 1  # no retries
        assert result.success is True  # fallback

    def test_cancelled_result_not_retried(self, sample_config):
        cancelled = StageResult(stage="triage", success=False, cancelled=True)
        result = run_with_retry(lambda: cancelled, "triage", sample_config)
        assert result.cancelled is True


class TestReviewRunnerHandleSignalSweep:
    """Fix 2d: the runner's shutdown handler must ``pkill -TERM -P``
    its own children before exiting, so a SIGHUP on the tmux pane (or
    a SIGTERM from the orchestrator) cannot leave codex orphaned even
    if some link in the senior→gate-code→codex signal chain is broken.
    """

    def _make_runner(self, tmp_workspace, sample_config):
        return ReviewRunner(
            review_id="test-org-test-repo-pr42",
            stage="architecture",
            workspace=tmp_workspace,
            config=sample_config,
        )

    def test_sigterm_invokes_pkill_with_own_pid(
        self, tmp_workspace, sample_config
    ):
        import signal as _signal

        runner = self._make_runner(tmp_workspace, sample_config)
        with patch("gate.runner.subprocess.run") as mock_run, \
             patch("gate.runner.os.getpid", return_value=4242), \
             patch("gate.runner.sys.exit") as mock_exit:
            runner._handle_signal(_signal.SIGTERM, None)

        assert mock_run.called, "pkill sweep must run before exit"
        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert cmd[:3] == ["pkill", "-TERM", "-P"]
        assert cmd[3] == "4242"
        assert kwargs.get("timeout") == 2
        assert kwargs.get("check") is False
        mock_exit.assert_called_once_with(128 + _signal.SIGTERM)

    def test_sigint_still_raises_after_sweep(
        self, tmp_workspace, sample_config
    ):
        """SIGINT must still surface as KeyboardInterrupt (preserving
        the pre-Fix-2d contract) — but only after we attempt to reap
        direct children."""
        import signal as _signal

        runner = self._make_runner(tmp_workspace, sample_config)
        import pytest

        with patch("gate.runner.subprocess.run") as mock_run, \
             pytest.raises(KeyboardInterrupt):
            runner._handle_signal(_signal.SIGINT, None)

        assert mock_run.called

    def test_pkill_failures_are_swallowed(
        self, tmp_workspace, sample_config
    ):
        """Missing pkill binary, timeout, or OSError must not prevent
        the handler from exiting — this is best-effort cleanup."""
        import signal as _signal
        import subprocess as _subprocess

        runner = self._make_runner(tmp_workspace, sample_config)
        for exc in (
            FileNotFoundError(),
            _subprocess.TimeoutExpired(cmd="pkill", timeout=2),
            OSError(),
        ):
            with patch("gate.runner.subprocess.run", side_effect=exc), \
                 patch("gate.runner.sys.exit") as mock_exit:
                runner._handle_signal(_signal.SIGTERM, None)
                mock_exit.assert_called_once_with(128 + _signal.SIGTERM)

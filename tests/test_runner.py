"""Tests for gate.runner module.

Tests command building for each stage type, mock tmux execution,
StructuredRunner parsing, and run_with_retry logic.
"""

import json
import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

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

    @patch("gate.runner.subprocess.Popen")
    def test_run_passes_prompt_via_stdin_not_argv(self, mock_popen, tmp_path):
        """Regression test for ARG_MAX overflow on PRs with huge diffs.

        Before the original ARG_MAX fix, the assembled prompt was
        appended to argv with ``cmd.append(prompt_text)`` and any
        prompt larger than macOS ARG_MAX (~1 MB) raised
        ``OSError: [Errno 7] Argument list too long: 'claude'`` inside
        ``_execute_child`` before claude even started — silently failing
        the structured stage with no actionable error.

        adin-chat PR #261 (554 files, 5.5 MB diff) was the original
        repro. After the May 2026 cancellation refactor (audit P2.1)
        we run claude via ``Popen`` + ``communicate(input=...)`` so a
        watchdog thread can ``terminate()`` mid-flight on cancel;
        the prompt-via-stdin contract is preserved.
        """
        proc = MagicMock()
        proc.communicate.return_value = ('{"change_type":"x"}', "")
        proc.returncode = 0
        mock_popen.return_value = proc

        runner = StructuredRunner()
        prompt = "x" * 5_500_000  # 5.5 MB — same scale as PR #261
        runner.run("triage", prompt, tmp_path, {"models": {}, "timeouts": {}})

        popen_call = mock_popen.call_args
        cmd = popen_call.args[0]
        assert prompt not in cmd, (
            "prompt must NOT be appended to argv (would hit ARG_MAX on big PRs); "
            "pass it via communicate(input=...) instead"
        )
        # The prompt is delivered through ``communicate(input=prompt)``,
        # which requires ``stdin=PIPE`` on the Popen call so the child
        # has a pipe to read from. Verify both halves of that contract.
        assert popen_call.kwargs.get("stdin") == subprocess.PIPE, (
            "Popen must set stdin=PIPE so communicate(input=...) has somewhere to write"
        )
        proc.communicate.assert_called_once()
        comm_kwargs = proc.communicate.call_args.kwargs
        assert comm_kwargs.get("input") == prompt, "prompt must be piped via stdin"
        assert "--print" in cmd, "claude must run with --print so it reads stdin"

    @patch("gate.runner.subprocess.Popen")
    def test_run_sets_stdin_pipe_for_communicate(self, mock_popen, tmp_path):
        """``Popen(stdin=PIPE)`` is required for ``communicate(input=...)``.

        Pins the contract that the prompt-via-stdin path uses a pipe
        (not DEVNULL, not file). Replaces the prior ``test_run_does_not_
        set_stdin_kwarg`` regression which only made sense with
        ``subprocess.run(input=...)``.
        """
        proc = MagicMock()
        proc.communicate.return_value = ("{}", "")
        proc.returncode = 0
        mock_popen.return_value = proc

        runner = StructuredRunner()
        runner.run("triage", "tiny prompt", tmp_path, {"models": {}, "timeouts": {}})

        assert mock_popen.call_args.kwargs.get("stdin") == subprocess.PIPE
        assert mock_popen.call_args.kwargs.get("stdout") == subprocess.PIPE
        assert mock_popen.call_args.kwargs.get("stderr") == subprocess.PIPE


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

    # ── Cancellation event plumbing (audit P2.1) ─────────────

    def test_cancelled_event_short_circuits_before_first_attempt(
        self, sample_config
    ):
        """Pre-set cancel event causes early return before run_fn runs.

        Mirrors the orchestrator behavior where a queue supersede can
        flip ``_cancelled`` between stage selection and the stage's
        first attempt. Without this gate the orchestrator burns one
        full stage attempt's worth of time after the cancel.
        """
        cancelled = threading.Event()
        cancelled.set()
        called = [0]

        def run_fn():
            called[0] += 1
            return StageResult(stage="triage", success=True, data={"key": "val"})

        result = run_with_retry(
            run_fn, "triage", sample_config, cancelled=cancelled
        )
        assert called[0] == 0
        assert result.cancelled is True
        assert result.success is False

    def test_cancelled_event_breaks_rate_limit_retry_sleep(self, sample_config):
        """Setting the event mid retry-sleep wakes the wait immediately.

        Without ``Event.wait``, the rate-limit back-off uses
        ``time.sleep(60+)`` which blocks supersede latency by minutes.
        With the event, ``wait(delay)`` returns True the moment the
        event is set from another thread.
        """
        # Force a short base delay so the test can complete fast.
        config = {"retry": {"max_retries": 2, "base_delay_s": 5}}
        cancelled = threading.Event()
        rate_limited = StageResult(
            stage="triage", success=False, is_rate_limited=True
        )

        # Fire the cancel ~50ms into the rate-limit sleep.
        def _cancel_soon():
            time.sleep(0.05)
            cancelled.set()

        threading.Thread(target=_cancel_soon, daemon=True).start()

        start = time.monotonic()
        result = run_with_retry(
            lambda: rate_limited, "triage", config, cancelled=cancelled
        )
        elapsed = time.monotonic() - start

        assert result.cancelled is True
        assert elapsed < 1.0, (
            f"cancel during rate-limit sleep should wake in <1s, took {elapsed:.2f}s"
        )

    def test_cancelled_event_breaks_transient_retry_sleep(self, sample_config):
        """Same shape as the rate-limit test but for the transient branch."""
        config = {
            "retry": {"max_retries": 2, "base_delay_s": 60, "transient_base_delay_s": 5}
        }
        cancelled = threading.Event()
        transient = StageResult(stage="triage", success=False, is_transient=True)

        def _cancel_soon():
            time.sleep(0.05)
            cancelled.set()

        threading.Thread(target=_cancel_soon, daemon=True).start()

        start = time.monotonic()
        result = run_with_retry(
            lambda: transient, "triage", config, cancelled=cancelled
        )
        elapsed = time.monotonic() - start

        assert result.cancelled is True
        assert elapsed < 1.0, (
            f"cancel during transient sleep should wake in <1s, took {elapsed:.2f}s"
        )

    def test_cancelled_none_preserves_legacy_behavior(self, sample_config):
        """Back-compat: ``cancelled=None`` (the default) uses time.sleep.

        Legacy callers that haven't been updated must see byte-identical
        behavior. Verified by patching ``time.sleep`` and asserting it
        was invoked (the Event.wait path would skip the patch).
        """
        rate_limited = StageResult(
            stage="triage", success=False, is_rate_limited=True
        )

        with patch("gate.runner.time.sleep") as mock_sleep:
            run_with_retry(lambda: rate_limited, "triage", sample_config)
            assert mock_sleep.called, "legacy callers must still use time.sleep"


class TestStructuredRunnerCancellation:
    """Watchdog-based cancellation for the structured stage subprocess.

    Audit P2.1: ``StructuredRunner.run`` previously blocked up to
    ``structured_stage_s`` (600s in production config) on a single
    attempt. With the watchdog wired in, cancellation arriving mid-call
    terminates the Claude subprocess within ~0.5s.
    """

    @patch("gate.runner.subprocess.Popen")
    def test_cancelled_event_terminates_subprocess(self, mock_popen, tmp_path):
        cancelled = threading.Event()

        proc = MagicMock()
        # Simulate Claude taking forever: communicate() blocks until cancel
        # is set, then unblocks (mimicking proc.terminate()).
        comm_done = threading.Event()

        def _communicate(input=None, timeout=None):
            # Wait for the watchdog to call terminate(), then return.
            comm_done.wait(timeout=3.0)
            return ("", "killed by cancel")

        def _terminate():
            comm_done.set()

        proc.communicate.side_effect = _communicate
        proc.terminate.side_effect = _terminate
        proc.kill = MagicMock()
        proc.poll.return_value = None
        proc.returncode = -15  # SIGTERM
        proc.wait.return_value = -15
        mock_popen.return_value = proc

        # Fire cancel ~100ms after run() starts.
        def _cancel_soon():
            time.sleep(0.1)
            cancelled.set()

        threading.Thread(target=_cancel_soon, daemon=True).start()

        runner = StructuredRunner()
        result = runner.run(
            "triage", "prompt", tmp_path,
            {"models": {}, "timeouts": {}},
            cancelled=cancelled,
        )

        assert result.cancelled is True
        assert proc.terminate.called, "watchdog must call terminate() on cancel"

    @patch("gate.runner.subprocess.Popen")
    def test_cancelled_after_completion_returns_normal_result(
        self, mock_popen, tmp_path
    ):
        """If cancel arrives after communicate() returns cleanly, the
        normal parse path runs — we don't retroactively mark success as
        cancelled.
        """
        cancelled = threading.Event()

        proc = MagicMock()
        proc.communicate.return_value = ('{"change_type":"refactor"}', "")
        proc.returncode = 0
        proc.poll.return_value = 0
        mock_popen.return_value = proc

        runner = StructuredRunner()
        result = runner.run(
            "triage", "prompt", tmp_path,
            {"models": {}, "timeouts": {}},
            cancelled=cancelled,
        )

        # Cancel set AFTER run completes — should not affect result.
        cancelled.set()

        assert result.cancelled is False
        assert result.success is True

    @patch("gate.runner.subprocess.Popen")
    def test_no_watchdog_when_cancelled_is_none(self, mock_popen, tmp_path):
        """Legacy callers (``cancelled=None``) get the cheaper code path —
        no watchdog thread, no terminate() machinery.
        """
        proc = MagicMock()
        proc.communicate.return_value = ('{"x": 1}', "")
        proc.returncode = 0
        mock_popen.return_value = proc

        runner = StructuredRunner()
        # We can't directly assert "no thread spawned" without
        # introspection, but we can assert terminate is not called.
        result = runner.run(
            "triage", "prompt", tmp_path, {"models": {}, "timeouts": {}}
        )
        assert proc.terminate.called is False
        assert result.success is True


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

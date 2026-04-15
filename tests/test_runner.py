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

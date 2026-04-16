"""Tests for gate.fixer module — fix helpers and pipeline logic."""

import subprocess
import threading
from unittest.mock import MagicMock, patch

from gate.fixer import (
    FixPipeline,
    _build_build_error_prompt,
    _build_rereview_feedback_prompt,
    _match_glob,
    build_verify,
    cleanup_artifacts,
    cleanup_gate_tests,
    enforce_blocklist,
    sort_findings_by_severity,
    write_diff,
)


class TestBuildVerifySkip:
    def test_build_verify_skips_without_commands(self, tmp_path):
        result = build_verify(tmp_path)
        assert result["pass"] is True
        assert result["typecheck_errors"] == 0
        assert result["lint_errors"] == 0
        assert result["test_failures"] == 0
        assert result["typecheck_log"] == ""
        assert result["lint_log"] == ""

    def test_build_verify_with_python_config(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        config = {"repo": {"project_type": "python"}}
        result = build_verify(tmp_path, config=config)
        assert "typecheck_errors" in result
        assert "lint_errors" in result


class TestMatchGlob:
    def test_exact_match(self):
        assert _match_glob("package.json", "package.json") is True
        assert _match_glob("package.json", "other.json") is False

    def test_star_wildcard(self):
        assert _match_glob("foo.lock", "*.lock") is True
        assert _match_glob("bar.lock", "*.lock") is True
        assert _match_glob("foo.txt", "*.lock") is False

    def test_directory_glob(self):
        assert _match_glob(".github/workflows/ci.yml", ".github/**") is True
        assert _match_glob(".github/gate/test.js", ".github/**") is True
        assert _match_glob("src/foo.ts", ".github/**") is False

    def test_exact_dir(self):
        assert _match_glob(".github", ".github/**") is True


class TestEnforceBlocklist:
    @patch("gate.fixer._revert_file")
    @patch("gate.fixer._get_changed_files")
    def test_reverts_blocklisted_files(self, mock_changed, mock_revert, tmp_path):
        blocklist = tmp_path / "config" / "fix-blocklist.txt"
        blocklist.parent.mkdir(parents=True)
        blocklist.write_text("package-lock.json\n*.lock\n# comment\n\n")

        mock_changed.return_value = ["src/foo.ts", "package-lock.json", "yarn.lock"]

        with patch("gate.fixer.gate_dir", return_value=tmp_path):
            violations = enforce_blocklist(tmp_path)

        assert "package-lock.json" in violations
        assert "yarn.lock" in violations
        assert "src/foo.ts" not in violations
        assert mock_revert.call_count == 2

    @patch("gate.fixer._get_changed_files")
    def test_no_blocklist_file(self, mock_changed, tmp_path):
        with patch("gate.fixer.gate_dir", return_value=tmp_path):
            violations = enforce_blocklist(tmp_path)
        assert violations == []


class TestCleanupGateTests:
    def test_removes_gate_test_dir(self, tmp_path):
        gate_dir = tmp_path / "tests" / "gate"
        gate_dir.mkdir(parents=True)
        (gate_dir / "test.ts").write_text("test")

        with patch("gate.fixer.subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            cleanup_gate_tests(tmp_path)

        assert not gate_dir.exists()

    def test_removes_gate_test_files(self, tmp_path):
        (tmp_path / "__gate_test_foo.ts").write_text("test")
        (tmp_path / "__gate_fix_test_bar.ts").write_text("test")

        with patch("gate.fixer.subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            cleanup_gate_tests(tmp_path)

        assert not (tmp_path / "__gate_test_foo.ts").exists()
        assert not (tmp_path / "__gate_fix_test_bar.ts").exists()


class TestSortFindingsBySeverity:
    def test_sorts_critical_first(self):
        findings = [
            {"severity": "info", "message": "note"},
            {"severity": "critical", "message": "critical"},
            {"severity": "warning", "message": "warn"},
            {"severity": "error", "message": "err"},
        ]
        result = sort_findings_by_severity(findings)
        assert result[0]["severity"] == "critical"
        assert result[1]["severity"] == "error"
        assert result[2]["severity"] == "warning"
        assert result[3]["severity"] == "info"

    def test_empty_list(self):
        assert sort_findings_by_severity([]) == []


class TestWriteDiff:
    @patch("gate.fixer._run_silent")
    def test_writes_diff_file(self, mock_run, tmp_path):
        mock_run.return_value = ("diff --git a/foo.ts\n+line\n", 0)
        write_diff(tmp_path)
        assert (tmp_path / "fix-diff.txt").exists()
        assert "diff" in (tmp_path / "fix-diff.txt").read_text()

    @patch("gate.fixer._run_silent")
    def test_no_changes(self, mock_run, tmp_path):
        mock_run.return_value = ("", 0)
        write_diff(tmp_path)
        assert (tmp_path / "fix-diff.txt").read_text() == "(no changes)"


class TestBuildErrorPrompt:
    def test_includes_typecheck_errors(self):
        result = _build_build_error_prompt({
            "typecheck_errors": 3,
            "lint_errors": 0,
            "typecheck_log": "error TS2322: Type mismatch",
            "typecheck_tool": "npx",
        })
        assert "npx Errors (3)" in result
        assert "TS2322" in result

    def test_includes_lint_errors(self):
        result = _build_build_error_prompt({
            "typecheck_errors": 0,
            "lint_errors": 2,
            "lint_log": "no-unused-vars",
        })
        assert "Lint Errors (2)" in result

    def test_no_errors(self):
        result = _build_build_error_prompt({"typecheck_errors": 0, "lint_errors": 0})
        assert "Build Errors After Fix" in result

    def test_default_tool_name(self):
        result = _build_build_error_prompt({
            "typecheck_errors": 1,
            "lint_errors": 0,
            "typecheck_log": "some error",
        })
        assert "Type Check Errors (1)" in result


class TestRereviewFeedbackPrompt:
    def test_includes_json(self):
        rereview = {"pass": False, "issues": [{"message": "regression"}]}
        result = _build_rereview_feedback_prompt(rereview)
        assert "Re-Review Feedback" in result
        assert "regression" in result
        assert "```json" in result


class TestCleanupArtifacts:
    def test_removes_known_files(self, tmp_path):
        (tmp_path / "diff.txt").write_text("d")
        (tmp_path / "verdict.json").write_text("{}")
        (tmp_path / "fix-build.json").write_text("{}")

        with patch("gate.fixer.subprocess.run"):
            removed = cleanup_artifacts(tmp_path)

        assert not (tmp_path / "diff.txt").exists()
        assert not (tmp_path / "verdict.json").exists()
        assert not (tmp_path / "fix-build.json").exists()
        assert "diff.txt" in removed
        assert "verdict.json" in removed

    def test_removes_glob_matched_files(self, tmp_path):
        (tmp_path / "architecture-findings.json").write_text("{}")
        (tmp_path / "logic-result.json").write_text("{}")
        (tmp_path / "fix-senior-session-id.txt").write_text("abc")
        (tmp_path / "implement.in.md").write_text("prompt")
        (tmp_path / "implement.out.md").write_text("output")
        (tmp_path / "implement_1.in.md").write_text("prompt2")

        with patch("gate.fixer.subprocess.run"):
            removed = cleanup_artifacts(tmp_path)

        assert not (tmp_path / "architecture-findings.json").exists()
        assert not (tmp_path / "logic-result.json").exists()
        assert not (tmp_path / "fix-senior-session-id.txt").exists()
        assert not (tmp_path / "implement.in.md").exists()
        assert not (tmp_path / "implement_1.in.md").exists()
        assert len(removed) == 6

    def test_removes_fix_build_directory(self, tmp_path):
        build_dir = tmp_path / "fix-build"
        build_dir.mkdir()
        (build_dir / "tsc.log").write_text("log")
        (build_dir / "lint.log").write_text("log")

        with patch("gate.fixer.subprocess.run"):
            removed = cleanup_artifacts(tmp_path)

        assert not build_dir.exists()
        assert "fix-build/" in removed

    def test_does_not_touch_source_files(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "README.md").write_text("hi")
        src = tmp_path / "src"
        src.mkdir()
        (src / "app.ts").write_text("code")
        (tmp_path / "diff.txt").write_text("artifact")

        with patch("gate.fixer.subprocess.run"):
            cleanup_artifacts(tmp_path)

        assert (tmp_path / "package.json").exists()
        assert (tmp_path / "README.md").exists()
        assert (src / "app.ts").exists()
        assert not (tmp_path / "diff.txt").exists()

    def test_idempotent_when_no_artifacts(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")

        with patch("gate.fixer.subprocess.run"):
            removed = cleanup_artifacts(tmp_path)

        assert removed == []
        assert (tmp_path / "package.json").exists()

    def test_globs_do_not_recurse_into_subdirs(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "notes-findings.json").write_text("should stay")

        with patch("gate.fixer.subprocess.run"):
            removed = cleanup_artifacts(tmp_path)

        assert (src / "notes-findings.json").exists()
        assert removed == []


class TestRevertFile:
    def _init_repo(self, tmp_path):
        """Set up a minimal git repo for _revert_file tests."""
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path), capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "test"],
            cwd=str(tmp_path), capture_output=True,
        )
        tracked = tmp_path / "tracked.txt"
        tracked.write_text("original")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(tmp_path), capture_output=True,
        )
        return tracked

    def test_tracked_file_restored_not_deleted(self, tmp_path):
        from gate.fixer import _revert_file

        tracked = self._init_repo(tmp_path)
        tracked.write_text("modified")

        _revert_file(tmp_path, "tracked.txt")

        assert tracked.exists()
        assert tracked.read_text() == "original"

    def test_untracked_file_removed(self, tmp_path):
        from gate.fixer import _revert_file

        self._init_repo(tmp_path)
        untracked = tmp_path / "new-artifact.json"
        untracked.write_text("{}")

        _revert_file(tmp_path, "new-artifact.json")

        assert not untracked.exists()


# ── FixPipeline ──────────────────────────────────────────────


class TestFixPipelineCancellation:
    def test_cancelled_before_start_returns_failure(self, sample_config, tmp_path):
        verdict = {"decision": "request_changes", "findings": []}
        cancelled = threading.Event()
        cancelled.set()
        pipe = FixPipeline(
            pr_number=1, repo="a/b", workspace=tmp_path,
            verdict=verdict, build={}, config=sample_config,
            cancelled=cancelled,
        )
        result = pipe.run()
        assert result.success is False
        assert "cancelled" in result.summary.lower()


class TestFixPipelineLimits:
    @patch("gate.fixer.state.check_fix_limits")
    @patch("gate.fixer.github")
    def test_limit_exceeded_comments_and_fails(
        self, mock_github, mock_limits, sample_config, tmp_path
    ):
        mock_limits.return_value = (False, "lifetime limit reached")
        verdict = {"decision": "request_changes", "findings": []}
        pipe = FixPipeline(
            pr_number=1, repo="a/b", workspace=tmp_path,
            verdict=verdict, build={}, config=sample_config,
        )
        result = pipe.run()
        assert result.success is False
        assert "limit" in result.reason.lower()
        mock_github.comment_pr.assert_called_once()

    def test_limit_check_called_with_pr_and_repo(self, sample_config, tmp_path):
        """Verify check_fix_limits is invoked with the correct PR and repo.

        We mock state.check_fix_limits to a blocking failure so we can
        assert on the call arguments without running the full pipeline.
        """
        verdict = {"decision": "request_changes", "findings": []}
        with patch("gate.fixer.state.check_fix_limits") as mock_limits, \
             patch("gate.fixer.github"):
            mock_limits.return_value = (False, "blocked")
            pipe = FixPipeline(
                pr_number=42, repo="owner/repo", workspace=tmp_path,
                verdict=verdict, build={}, config=sample_config,
            )
            pipe.run()
            assert mock_limits.call_args[0][0] == 42
            assert mock_limits.call_args.kwargs.get("repo") == "owner/repo"


class TestFixPipelinePolishFlag:
    def test_polish_on_approve_with_notes(self, sample_config, tmp_path):
        verdict = {"decision": "approve_with_notes", "findings": []}
        pipe = FixPipeline(
            pr_number=1, repo="a/b", workspace=tmp_path,
            verdict=verdict, build={}, config=sample_config,
        )
        assert pipe.is_polish is True

    def test_not_polish_on_request_changes(self, sample_config, tmp_path):
        verdict = {"decision": "request_changes", "findings": []}
        pipe = FixPipeline(
            pr_number=1, repo="a/b", workspace=tmp_path,
            verdict=verdict, build={}, config=sample_config,
        )
        assert pipe.is_polish is False


class TestSortFindingsBySeverityEdgeCases:
    def test_unknown_severity_goes_last(self):
        findings = [
            {"severity": "weird", "message": "x"},
            {"severity": "critical", "message": "y"},
        ]
        result = sort_findings_by_severity(findings)
        assert result[0]["severity"] == "critical"
        assert result[-1]["severity"] == "weird"

    def test_missing_severity_defaults_low(self):
        findings = [
            {"message": "no severity"},
            {"severity": "error", "message": "err"},
        ]
        result = sort_findings_by_severity(findings)
        assert result[0]["severity"] == "error"


class TestRunSilentShellTrue:
    """Regression guard: _run_silent uses shell=True by design.

    This is a known engineering trade-off (audit flagged it). The test pins
    the current behavior so any change has to be deliberate and explicit.
    """

    @patch("gate.fixer.subprocess.run")
    def test_passes_shell_true(self, mock_run):
        from gate.fixer import _run_silent
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        _run_silent("echo hi", cwd="/tmp")
        assert mock_run.call_args.kwargs["shell"] is True

    @patch("gate.fixer.subprocess.run")
    def test_timeout_returns_empty(self, mock_run):
        from gate.fixer import _run_silent
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=1)
        result = _run_silent("sleep 100")
        assert result == ("", 1)


class TestWriteDiffEdgeCases:
    @patch("gate.fixer._run_silent")
    def test_nonzero_exit_still_writes(self, mock_run, tmp_path):
        mock_run.return_value = ("partial output", 128)
        write_diff(tmp_path)
        assert (tmp_path / "fix-diff.txt").exists()


class TestEnforceBlocklistEdgeCases:
    @patch("gate.fixer._get_changed_files")
    def test_empty_blocklist_file(self, mock_changed, tmp_path):
        blocklist = tmp_path / "config" / "fix-blocklist.txt"
        blocklist.parent.mkdir(parents=True)
        blocklist.write_text("\n# only comments\n\n")
        mock_changed.return_value = ["src/foo.ts"]
        with patch("gate.fixer.gate_dir", return_value=tmp_path):
            violations = enforce_blocklist(tmp_path)
        assert violations == []

    @patch("gate.fixer._get_changed_files")
    def test_comments_ignored(self, mock_changed, tmp_path):
        blocklist = tmp_path / "config" / "fix-blocklist.txt"
        blocklist.parent.mkdir(parents=True)
        blocklist.write_text("# foo.ts is fine\nbar.ts\n")
        mock_changed.return_value = ["foo.ts", "bar.ts"]
        with patch("gate.fixer.gate_dir", return_value=tmp_path), \
             patch("gate.fixer._revert_file"):
            violations = enforce_blocklist(tmp_path)
        assert violations == ["bar.ts"]


# ── Resume session TTY-safety (regression test) ──────────────


class TestResumeFixSession:
    """``_resume_fix_session`` must never inherit the parent process's stdio.

    Regression test for the TTY-deadlock bug where running the orchestrator
    under ``gate up`` (TUI mode) caused the resumed claude subprocess to
    fight the Textual app for ``/dev/tty``, hanging both indefinitely.

    The fix is: detach stdio (``stdin=DEVNULL`` + file-backed stdout/stderr)
    and pass ``--print`` so claude runs non-interactively.
    """

    def _pipeline(self, tmp_path, sample_config):
        from gate.fixer import FixPipeline
        verdict = {"decision": "request_changes", "findings": []}
        return FixPipeline(
            pr_number=1, repo="a/b", workspace=tmp_path,
            verdict=verdict, build={}, config=sample_config,
        )

    def _find_claude_call(self, mock_run):
        """Return the subprocess.run call that invoked ``claude``.

        ``_resume_fix_session`` also calls subprocess.run indirectly via
        ``_get_changed_files`` (which shells out to git) after the claude
        session ends, so we must filter by command name.
        """
        for call in mock_run.call_args_list:
            cmd = call.args[0] if call.args else call.kwargs.get("args", [])
            if cmd and isinstance(cmd, list) and cmd[0] == "claude":
                return call
        raise AssertionError("subprocess.run was never called with claude")

    @patch("gate.fixer.subprocess.run")
    def test_resume_detaches_stdin(self, mock_run, sample_config, tmp_path):
        pipe = self._pipeline(tmp_path, sample_config)
        pipe.session_id = "sess-abc"
        pipe._resume_fix_session("go fix it")
        claude_call = self._find_claude_call(mock_run)
        assert claude_call.kwargs["stdin"] is subprocess.DEVNULL, \
            "stdin must be DEVNULL to avoid TTY deadlock with parent process"

    @patch("gate.fixer.subprocess.run")
    def test_resume_redirects_stdout_and_stderr(self, mock_run, sample_config, tmp_path):
        pipe = self._pipeline(tmp_path, sample_config)
        pipe.session_id = "sess-abc"
        pipe._resume_fix_session("go fix it")
        claude_call = self._find_claude_call(mock_run)
        # Both should be file handles, not None (which would inherit parent stdio)
        assert claude_call.kwargs["stdout"] is not None, "stdout must be redirected"
        assert claude_call.kwargs["stderr"] is not None, "stderr must be redirected"
        # Log files should be written to the workspace
        assert (tmp_path / "resume-stdout.log").exists()
        assert (tmp_path / "resume-stderr.log").exists()

    @patch("gate.fixer.subprocess.run")
    def test_resume_uses_print_mode(self, mock_run, sample_config, tmp_path):
        pipe = self._pipeline(tmp_path, sample_config)
        pipe.session_id = "sess-abc"
        pipe._resume_fix_session("go fix it")
        cmd = self._find_claude_call(mock_run).args[0]
        assert "--print" in cmd, "claude must run with --print for non-interactive mode"
        assert "--resume" in cmd
        assert "sess-abc" in cmd

    @patch("gate.fixer.subprocess.run")
    def test_resume_no_session_id_skips(self, mock_run, sample_config, tmp_path):
        pipe = self._pipeline(tmp_path, sample_config)
        pipe.session_id = None
        result = pipe._resume_fix_session("go fix it")
        mock_run.assert_not_called()
        assert result == {"fix_json": None, "has_changes": False}

    @patch("gate.fixer.subprocess.run")
    def test_resume_handles_timeout_gracefully(self, mock_run, sample_config, tmp_path):
        def side_effect(cmd, *a, **kw):
            if cmd and cmd[0] == "claude":
                raise subprocess.TimeoutExpired(cmd="claude", timeout=2400)
            # Let non-claude calls (the git probe) succeed with a no-op result
            return MagicMock(stdout="", returncode=0)
        mock_run.side_effect = side_effect
        pipe = self._pipeline(tmp_path, sample_config)
        pipe.session_id = "sess-abc"
        # Should NOT raise
        result = pipe._resume_fix_session("go fix it")
        assert "fix_json" in result
        assert "has_changes" in result

    @patch("gate.fixer.subprocess.run")
    def test_resume_writes_prompt_file(self, mock_run, sample_config, tmp_path):
        pipe = self._pipeline(tmp_path, sample_config)
        pipe.session_id = "sess-abc"
        pipe._resume_fix_session("go fix it")
        assert (tmp_path / "fix-resume-prompt.md").read_text() == "go fix it"

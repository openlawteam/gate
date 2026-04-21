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

    def test_removes_codex_stdout_logs(self, tmp_path):
        """Fix 1c: *.codex.log artifacts from gate-code stdout redirect
        must be cleaned up; never committed to the target repo."""
        (tmp_path / "implement.codex.log").write_text("codex stdout")
        (tmp_path / "audit_1.codex.log").write_text("codex stdout")

        with patch("gate.fixer.subprocess.run"):
            removed = cleanup_artifacts(tmp_path)

        assert not (tmp_path / "implement.codex.log").exists()
        assert not (tmp_path / "audit_1.codex.log").exists()
        assert "implement.codex.log" in removed
        assert "audit_1.codex.log" in removed

    def test_codex_log_is_in_artifact_globs(self):
        """Regression: *.codex.log must be in the declared glob list so
        future artifact-cleanup additions don't drop it by accident."""
        from gate.fixer import GATE_ARTIFACT_GLOBS
        assert "*.codex.log" in GATE_ARTIFACT_GLOBS

    def test_removes_gate_directions_md(self, tmp_path):
        """Fix 3d: gate-directions.md (senior's scratch file for
        directions passed to gate-code via file redirection) must be
        cleaned up; never committed."""
        (tmp_path / "gate-directions.md").write_text("directions text")

        with patch("gate.fixer.subprocess.run"):
            removed = cleanup_artifacts(tmp_path)

        assert not (tmp_path / "gate-directions.md").exists()
        assert "gate-directions.md" in removed

    def test_gate_directions_md_is_in_artifact_files(self):
        """Regression: gate-directions.md must be in the static
        artifact-files set so future cleanup additions don't drop it."""
        from gate.fixer import GATE_ARTIFACT_FILES
        assert "gate-directions.md" in GATE_ARTIFACT_FILES


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


class TestRunSilent:
    """Cover the hardened _run_silent: no shell, shlex.split for strings,
    list form passed through, combined stdout+stderr, safe error paths."""

    @patch("gate.fixer.subprocess.run")
    def test_string_cmd_uses_shlex_split(self, mock_run):
        from gate.fixer import _run_silent
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        _run_silent("echo hi arg2", cwd="/tmp")
        call = mock_run.call_args
        assert call.args[0] == ["echo", "hi", "arg2"]
        # shell should not be requested anywhere (default False).
        assert call.kwargs.get("shell", False) is False

    @patch("gate.fixer.subprocess.run")
    def test_list_cmd_passed_through(self, mock_run):
        from gate.fixer import _run_silent
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        _run_silent(["git", "diff", "HEAD"], cwd="/tmp")
        call = mock_run.call_args
        assert call.args[0] == ["git", "diff", "HEAD"]
        assert call.kwargs.get("shell", False) is False

    @patch("gate.fixer.subprocess.run")
    def test_returns_merged_output(self, mock_run):
        """stderr=subprocess.STDOUT merges both streams in the kernel;
        result.stdout contains the combined output."""
        from gate.fixer import _run_silent
        mock_run.return_value = MagicMock(stdout="out\nerr\n", returncode=0)
        result = _run_silent("noop")
        assert result == ("out\nerr\n", 0)
        call = mock_run.call_args
        assert call.kwargs["stderr"] == subprocess.STDOUT
        assert call.kwargs["stdout"] == subprocess.PIPE
        assert "capture_output" not in call.kwargs

    @patch("gate.fixer.subprocess.run")
    def test_timeout_returns_empty(self, mock_run):
        from gate.fixer import _run_silent
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=1)
        result = _run_silent("sleep 100")
        assert result == ("", 1)

    def test_invalid_shlex_returns_empty(self):
        """An unbalanced quote makes shlex.split raise ValueError; the
        helper must swallow it and return the safe ("", 1) sentinel."""
        from gate.fixer import _run_silent
        result = _run_silent("echo 'unterminated")
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


# ── Git subprocess timeouts (review warning regression) ──────


class TestGitSubprocessTimeouts:
    """Regression: every git subprocess.run in fixer.py needs a timeout=
    so a hung git (e.g. index.lock) can't wedge the fix pipeline past
    the outer orchestrator timeout.
    """

    @patch("gate.fixer.subprocess.run")
    def test_get_changed_files_has_timeout(self, mock_run, tmp_path):
        from gate.fixer import _get_changed_files
        mock_run.return_value = MagicMock(stdout="")
        _get_changed_files(tmp_path)
        assert "timeout" in mock_run.call_args.kwargs
        assert mock_run.call_args.kwargs["timeout"] > 0

    @patch("gate.fixer.subprocess.run")
    def test_get_changed_files_handles_timeout(self, mock_run, tmp_path):
        from gate.fixer import _get_changed_files
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="sh", timeout=60)
        result = _get_changed_files(tmp_path)
        assert result == []

    @patch("gate.fixer.subprocess.run")
    def test_get_changed_files_handles_missing_git(self, mock_run, tmp_path):
        from gate.fixer import _get_changed_files
        mock_run.side_effect = FileNotFoundError(2, "No such file", "sh")
        result = _get_changed_files(tmp_path)
        assert result == []

    @patch("gate.fixer.subprocess.run")
    def test_revert_all_all_calls_have_timeout(self, mock_run, tmp_path):
        from gate.fixer import _revert_all
        mock_run.return_value = MagicMock(returncode=0)
        _revert_all(tmp_path)
        # Two calls: git checkout, git clean
        assert mock_run.call_count == 2
        for call in mock_run.call_args_list:
            assert "timeout" in call.kwargs, f"missing timeout: {call}"

    @patch("gate.fixer.subprocess.run")
    def test_revert_all_tolerates_failures(self, mock_run, tmp_path):
        from gate.fixer import _revert_all
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=30)
        # Must not raise
        _revert_all(tmp_path)

    @patch("gate.fixer.subprocess.run")
    def test_revert_file_cat_file_has_timeout(self, mock_run, tmp_path):
        from gate.fixer import _revert_file
        mock_run.return_value = MagicMock(returncode=1)  # untracked
        _revert_file(tmp_path, "x.txt")
        assert "timeout" in mock_run.call_args_list[0].kwargs

    @patch("gate.fixer.subprocess.run")
    def test_revert_file_checkout_has_timeout(self, mock_run, tmp_path):
        from gate.fixer import _revert_file
        # First call (cat-file) returns 0 (tracked), then checkout is invoked
        mock_run.return_value = MagicMock(returncode=0)
        _revert_file(tmp_path, "x.txt")
        assert mock_run.call_count == 2
        assert "timeout" in mock_run.call_args_list[1].kwargs
        assert mock_run.call_args_list[1].args[0][:3] == ["git", "checkout", "HEAD"]

    @patch("gate.fixer.subprocess.run")
    def test_revert_file_tolerates_cat_file_failure(self, mock_run, tmp_path):
        from gate.fixer import _revert_file
        mock_run.side_effect = FileNotFoundError(2, "No git", "git")
        # Must not raise
        _revert_file(tmp_path, "x.txt")


# ── Finding-ID + fixability helpers (Audit A2, Group 1E) ─────


class TestComputeFindingId:
    def test_stable_for_same_inputs(self):
        from gate.fixer import compute_finding_id
        f = {"file": "a.ts", "line": 10, "source_stage": "logic", "message": "bad"}
        assert compute_finding_id(f) == compute_finding_id(dict(f))

    def test_differs_on_file_change(self):
        from gate.fixer import compute_finding_id
        a = {"file": "a.ts", "line": 10, "source_stage": "logic", "message": "x"}
        b = {"file": "b.ts", "line": 10, "source_stage": "logic", "message": "x"}
        assert compute_finding_id(a) != compute_finding_id(b)

    def test_handles_missing_fields(self):
        from gate.fixer import compute_finding_id
        assert len(compute_finding_id({})) == 10


class TestClassifyFixability:
    def test_respects_preset_fixability(self):
        from gate.fixer import classify_fixability
        assert classify_fixability({"fixability": "broad"}) == "broad"
        assert classify_fixability({"fixability": "TRIVIAL"}) == "trivial"

    def test_unknown_when_no_keywords(self):
        from gate.fixer import classify_fixability
        assert classify_fixability({"message": "something unusual"}) == "unknown"


class TestTagFindings:
    def test_adds_ids_and_fixability(self):
        from gate.fixer import tag_findings
        tagged = tag_findings([{"file": "x.ts", "line": 1, "message": "m"}])
        assert "finding_id" in tagged[0]
        assert "fixability" in tagged[0]

    def test_preserves_existing_id(self):
        from gate.fixer import tag_findings
        tagged = tag_findings([{"file": "x.ts", "finding_id": "preset"}])
        assert tagged[0]["finding_id"] == "preset"

    def test_does_not_mutate_input(self):
        from gate.fixer import tag_findings
        orig = {"file": "x.ts", "line": 1, "message": "m"}
        tag_findings([orig])
        assert "finding_id" not in orig

    def test_tag_findings_sets_ambiguity(self):
        """Phase 4: tag_findings must set `ambiguity` on every finding
        so both fix paths (polish + monolithic) observe the same tag
        without duplicated classification logic."""
        from gate.fixer import tag_findings
        tagged = tag_findings([
            {"file": "x.ts", "message": "it is unclear whether X should..."},
            {"file": "y.ts", "message": "boundary off by one",
             "suggestion": "change > to >="},
            {"file": "z.ts", "message": "raw SQL"},
        ])
        assert tagged[0]["ambiguity"] == "high"
        assert tagged[1]["ambiguity"] == "none"
        # No suggestion and no ambiguity cue -> "low"
        assert tagged[2]["ambiguity"] == "low"


class TestClassifyAmbiguity:
    """Phase 4: ambiguity classifier heuristic."""

    def test_respects_preset_ambiguity(self):
        from gate.fixer import classify_ambiguity
        assert classify_ambiguity({"ambiguity": "HIGH"}) == "high"
        assert classify_ambiguity({"ambiguity": "low"}) == "low"

    def test_high_keywords_set_high(self):
        from gate.fixer import classify_ambiguity
        assert classify_ambiguity(
            {"message": "It is unclear whether empty input returns 0 or raises."}
        ) == "high"
        assert classify_ambiguity(
            {"title": "Ambiguous behavior", "message": "..."}
        ) == "high"

    def test_concrete_suggestion_is_none(self):
        from gate.fixer import classify_ambiguity
        assert classify_ambiguity({
            "message": "missing null check",
            "suggestion": "add `if (x == null) return;`",
        }) == "none"

    def test_no_suggestion_is_low(self):
        from gate.fixer import classify_ambiguity
        assert classify_ambiguity({"message": "consider fixing this"}) == "low"


class TestDisambigDigest:
    """Phase 4: digest stability — same finding → same digest."""

    def test_digest_stable_across_calls(self):
        entry = {
            "finding_id": "abc123",
            "file": "src/foo.py",
            "line": 42,
            "finding_message": "it is unclear whether ...",
        }
        d1 = FixPipeline._disambig_digest(entry)
        d2 = FixPipeline._disambig_digest(entry)
        assert d1 == d2
        assert len(d1) == 16

    def test_digest_differs_on_message_change(self):
        base = {"finding_id": "x", "file": "a.py", "line": 1, "finding_message": "m1"}
        other = {**base, "finding_message": "m2"}
        assert FixPipeline._disambig_digest(base) != FixPipeline._disambig_digest(other)


class TestPostDisambigCommentIfNeeded:
    """Phase 4: _commit_and_finish delegates to this helper for the
    author-disambiguation comment. Covers dedup + cap + halt-disabled."""

    def _make_pipeline(self, tmp_path, halt=True, cap=2):
        pipe = FixPipeline.__new__(FixPipeline)
        pipe.pr_number = 42
        pipe.repo = "org/repo"
        pipe.config = {
            "repo": {
                "halt_on_ambiguity": halt,
                "max_disambig_questions_per_pr": cap,
            },
        }
        pipe._state_dir = tmp_path
        return pipe

    def test_posts_once_and_writes_dedup(self, tmp_path):
        pipe = self._make_pipeline(tmp_path)
        not_fixed = [
            {
                "finding_id": "a",
                "file": "x.py",
                "line": 1,
                "finding_message": "it is unclear whether...",
                "reason": "requires_author_disambiguation",
                "detail": "numbered interpretations here",
            }
        ]
        with patch("gate.fixer.github.comment_pr") as cm, \
             patch("gate.fixer.state") as mock_state:
            mock_state.get_pr_state_dir.return_value = tmp_path
            pipe._post_disambig_comment_if_needed(not_fixed)
        assert cm.call_count == 1
        asked = (tmp_path / "disambig_asked.txt").read_text().splitlines()
        assert len(asked) == 1
        assert len(asked[0]) == 16  # 16-char digest

    def test_second_call_same_digest_does_not_post(self, tmp_path):
        pipe = self._make_pipeline(tmp_path)
        not_fixed = [
            {
                "finding_id": "a",
                "file": "x.py",
                "line": 1,
                "finding_message": "it is unclear whether...",
                "reason": "requires_author_disambiguation",
                "detail": "d",
            }
        ]
        # First call — seed the dedup file
        with patch("gate.fixer.github.comment_pr"), \
             patch("gate.fixer.state") as mock_state:
            mock_state.get_pr_state_dir.return_value = tmp_path
            pipe._post_disambig_comment_if_needed(not_fixed)
        # Second call — should NOT post again
        with patch("gate.fixer.github.comment_pr") as cm, \
             patch("gate.fixer.state") as mock_state:
            mock_state.get_pr_state_dir.return_value = tmp_path
            pipe._post_disambig_comment_if_needed(not_fixed)
        assert cm.call_count == 0
        # And the stale counter must have been bumped
        stale_file = tmp_path / "disambig_stale_count.txt"
        assert stale_file.exists()
        assert int(stale_file.read_text().strip()) == 1

    def test_halt_disabled_does_not_post(self, tmp_path):
        pipe = self._make_pipeline(tmp_path, halt=False)
        not_fixed = [
            {
                "finding_id": "a",
                "file": "x.py",
                "line": 1,
                "finding_message": "…",
                "reason": "requires_author_disambiguation",
                "detail": "d",
            }
        ]
        with patch("gate.fixer.github.comment_pr") as cm, \
             patch("gate.fixer.state") as mock_state:
            mock_state.get_pr_state_dir.return_value = tmp_path
            pipe._post_disambig_comment_if_needed(not_fixed)
        assert cm.call_count == 0

    def test_respects_cap(self, tmp_path):
        pipe = self._make_pipeline(tmp_path, cap=2)
        not_fixed = [
            {
                "finding_id": f"id-{i}",
                "file": f"f{i}.py",
                "line": i,
                "finding_message": f"m{i}",
                "reason": "requires_author_disambiguation",
                "detail": "d",
            }
            for i in range(5)
        ]
        with patch("gate.fixer.github.comment_pr") as cm, \
             patch("gate.fixer.state") as mock_state:
            mock_state.get_pr_state_dir.return_value = tmp_path
            pipe._post_disambig_comment_if_needed(not_fixed)
        assert cm.call_count == 1
        asked = (tmp_path / "disambig_asked.txt").read_text().splitlines()
        assert len(asked) == 2


class TestPolishLoopAmbiguityHalt:
    """Phase 4: run_polish_loop must skip ambiguity=high findings BEFORE
    calling _attempt_finding, appending to not_fixed with the correct
    reason. This is the primary safety guarantee of Phase 4."""

    def _make_pipeline(self, halt_on_ambiguity=True, budget=1800):
        pipe = MagicMock()
        pipe.config = {
            "repo": {"halt_on_ambiguity": halt_on_ambiguity},
            "limits": {},
        }
        pipe._cancelled = threading.Event()
        pipe.pr_number = 1
        pipe.repo = "a/b"
        pipe._polish_context = []
        pipe._emit_fix_stage = MagicMock()
        return pipe

    def test_halts_ambiguous_finding_before_attempt(self, monkeypatch):
        from gate import fixer_polish

        pipe = self._make_pipeline()
        attempted = []

        def _fake_attempt(p, f, t):
            attempted.append(f.get("finding_id"))
            return {"fixed": True, "entry": {"finding_id": f["finding_id"]}}

        monkeypatch.setattr(fixer_polish, "_attempt_finding", _fake_attempt)
        monkeypatch.setattr(fixer_polish, "_run_fix_polish_audit", lambda _: None)
        monkeypatch.setattr(fixer_polish, "get_polish_timeouts", lambda _: {
            "trivial": 180, "scoped": 600, "broad": 0, "unknown": 180,
        })
        monkeypatch.setattr(fixer_polish, "get_polish_total_budget_s", lambda _: 1800)
        monkeypatch.setattr(fixer_polish, "write_live_log", lambda *a, **kw: None)

        findings = [
            {
                "finding_id": "amb-1",
                "file": "f.py",
                "line": 3,
                "message": "it is unclear whether ...",
                "fixability": "trivial",
                "ambiguity": "high",
            },
            {
                "finding_id": "ok-1",
                "file": "g.py",
                "line": 5,
                "message": "typo",
                "fixability": "trivial",
                "ambiguity": "none",
            },
        ]
        out = fixer_polish.run_polish_loop(pipe, findings)

        assert "amb-1" not in attempted
        assert "ok-1" in attempted
        nf_reasons = [e["reason"] for e in out.get("not_fixed", [])]
        assert "requires_author_disambiguation" in nf_reasons

    def test_halt_disabled_still_attempts(self, monkeypatch):
        from gate import fixer_polish

        pipe = self._make_pipeline(halt_on_ambiguity=False)
        attempted = []

        def _fake_attempt(p, f, t):
            attempted.append(f.get("finding_id"))
            return {"fixed": True, "entry": {"finding_id": f["finding_id"]}}

        monkeypatch.setattr(fixer_polish, "_attempt_finding", _fake_attempt)
        monkeypatch.setattr(fixer_polish, "_run_fix_polish_audit", lambda _: None)
        monkeypatch.setattr(fixer_polish, "get_polish_timeouts", lambda _: {
            "trivial": 180, "scoped": 600, "broad": 0, "unknown": 180,
        })
        monkeypatch.setattr(fixer_polish, "get_polish_total_budget_s", lambda _: 1800)
        monkeypatch.setattr(fixer_polish, "write_live_log", lambda *a, **kw: None)

        findings = [
            {
                "finding_id": "amb-1",
                "file": "f.py",
                "line": 3,
                "message": "it is unclear whether ...",
                "fixability": "trivial",
                "ambiguity": "high",
            },
        ]
        fixer_polish.run_polish_loop(pipe, findings)
        assert "amb-1" in attempted


class TestFixabilitySummary:
    def test_counts_each_bucket(self):
        from gate.fixer import fixability_summary
        summary = fixability_summary([
            {"fixability": "trivial"}, {"fixability": "trivial"},
            {"fixability": "scoped"}, {"fixability": "broad"},
        ])
        assert "2 trivial" in summary
        assert "1 scoped" in summary
        assert "1 broad" in summary
        assert "0 unknown" in summary


class TestValidateFixJson:
    def test_none_returns_warnings_and_empty_shape(self):
        from gate.fixer import _validate_fix_json
        warnings, norm = _validate_fix_json(None)
        assert warnings
        assert norm == {"fixed": [], "not_fixed": [], "stats": {}}

    def test_synthesizes_missing_fix_description(self):
        from gate.fixer import _validate_fix_json
        data = {"fixed": [{"file": "a.ts", "line": 1}], "not_fixed": []}
        warnings, norm = _validate_fix_json(data)
        entry = norm["fixed"][0]
        assert entry["_description_synthesized"] is True
        assert "fix-senior" in entry["fix_description"]
        assert warnings  # synthesis logs a warning

    def test_synthesizes_missing_not_fixed_detail(self):
        from gate.fixer import _validate_fix_json
        data = {"fixed": [], "not_fixed": [{"file": "a.ts", "line": 1}]}
        _, norm = _validate_fix_json(data)
        assert norm["not_fixed"][0]["_detail_synthesized"] is True
        assert norm["not_fixed"][0]["reason"] == "deferred"

    def test_resolves_missing_finding_id_from_findings(self):
        from gate.fixer import _validate_fix_json
        findings = [{"file": "a.ts", "line": 5, "finding_id": "xyz"}]
        data = {
            "fixed": [{"file": "a.ts", "line": 5, "fix_description": "ok"}],
            "not_fixed": [],
        }
        _, norm = _validate_fix_json(data, findings=findings)
        assert norm["fixed"][0]["finding_id"] == "xyz"

    def test_drops_non_dict_entries(self):
        from gate.fixer import _validate_fix_json
        data = {"fixed": ["oops"], "not_fixed": [42]}
        warnings, norm = _validate_fix_json(data)
        assert norm["fixed"] == []
        assert norm["not_fixed"] == []
        assert any("not a dict" in w for w in warnings)

    def test_hopper_sub_scope_log_normalized(self):
        from gate.fixer import _validate_fix_json
        data = {
            "fixed": [],
            "not_fixed": [],
            "sub_scope_log": [
                {
                    "name": "scope1",
                    "finding_ids": ["a", "b"],
                    "iterations": "2",
                    "outcome": "committed",
                    "checkpoint_sha": "abc12345",
                },
                {
                    "name": "scope2",
                    "finding_ids": ["c"],
                    "iterations": 3,
                    "outcome": "reverted",
                    "reason": "subscope_exhausted",
                },
            ],
            "final_commit_message": "  fix(gate): ok  ",
        }
        warnings, norm = _validate_fix_json(data)
        assert [s["name"] for s in norm["sub_scope_log"]] == ["scope1", "scope2"]
        assert norm["sub_scope_log"][0]["iterations"] == 2  # coerced
        assert norm["sub_scope_log"][0]["outcome"] == "committed"
        assert norm["final_commit_message"] == "fix(gate): ok"
        assert not any("sub_scope_log" in w for w in warnings)

    def test_hopper_invalid_outcome_warns_but_keeps(self):
        from gate.fixer import _validate_fix_json
        data = {
            "fixed": [],
            "not_fixed": [],
            "sub_scope_log": [
                {"name": "scope1", "outcome": "weird-value"},
            ],
        }
        warnings, norm = _validate_fix_json(data)
        assert any("outcome" in w for w in warnings)
        assert norm["sub_scope_log"][0]["outcome"] == "weird-value"

    def test_hopper_missing_name_drops_entry(self):
        from gate.fixer import _validate_fix_json
        data = {
            "fixed": [],
            "not_fixed": [],
            "sub_scope_log": [
                {"outcome": "committed"},
                {"name": "  ", "outcome": "committed"},
                {"name": "kept", "outcome": "committed"},
            ],
        }
        warnings, norm = _validate_fix_json(data)
        assert [s["name"] for s in norm["sub_scope_log"]] == ["kept"]
        assert sum("missing name" in w for w in warnings) == 2

    def test_hopper_non_list_sub_scope_log_warns(self):
        from gate.fixer import _validate_fix_json
        data = {"fixed": [], "not_fixed": [], "sub_scope_log": "nope"}
        warnings, norm = _validate_fix_json(data)
        assert any("sub_scope_log" in w for w in warnings)
        assert norm["sub_scope_log"] == []

    def test_hopper_final_commit_non_string_warns(self):
        from gate.fixer import _validate_fix_json
        data = {"fixed": [], "not_fixed": [], "final_commit_message": 42}
        warnings, norm = _validate_fix_json(data)
        assert any("final_commit_message" in w for w in warnings)
        assert norm["final_commit_message"] == ""


class TestReprompTrivialSkipsHopperGate:
    """``_reprompt_trivial_skips`` must no-op under hopper mode.

    Under hopper the senior plans the whole scope up-front and the
    fixability classes are informational. Re-prompting based on them
    reintroduces the PR #217 pattern that motivated the pipeline switch.
    """

    def _make_fixer(self, tmp_path, config):
        from gate.fixer import FixPipeline
        f = FixPipeline(
            pr_number=1, repo="org/repo", workspace=tmp_path,
            verdict={"findings": []}, build=None, config=config,
        )
        f.session_id = "stub-session"
        return f

    def test_hopper_mode_does_not_reprompt(self, tmp_path):
        f = self._make_fixer(tmp_path, {"fix_pipeline": {"mode": "hopper"}})
        result = f._reprompt_trivial_skips(
            {"not_fixed": [{"finding_id": "x"}]},
            [{"finding_id": "x", "fixability": "trivial"}],
        )
        assert result is False

    def test_hopper_watchdog_trips_and_sets_flag(self, tmp_path):
        """Watchdog cancels the run and sets ``_runaway_guard_hit``."""
        from gate.fixer import FixPipeline
        cfg = {
            "fix_pipeline": {
                "mode": "hopper",
                "max_wall_clock_s": 1,
                "senior_session_timeout_s": 1,
            }
        }
        f = FixPipeline(
            pr_number=1, repo="org/repo", workspace=tmp_path,
            verdict={"findings": []}, build=None, config=cfg,
        )
        import time as _time
        f._fix_start_monotonic = _time.monotonic()
        f._start_watchdog()
        # Give the daemon thread up to 5s to trip the 1s cap.
        deadline = _time.monotonic() + 5.0
        while _time.monotonic() < deadline and not f._cancelled.is_set():
            _time.sleep(0.1)
        assert f._cancelled.is_set()
        assert f._runaway_guard_hit is True

    def test_polish_legacy_still_reprompts(self, tmp_path, monkeypatch):
        from gate import fixer
        f = self._make_fixer(tmp_path, {"fix_pipeline": {"mode": "polish_legacy"}})
        # Stub out the heavyweight helpers this path would normally hit.
        monkeypatch.setattr(
            f, "_resume_fix_session", lambda *_a, **_k: {"fix_json": {}}
        )
        monkeypatch.setattr(fixer, "write_live_log", lambda *a, **k: None)
        result = f._reprompt_trivial_skips(
            {"not_fixed": [
                {"finding_id": "x", "reason": "skipped_broad_in_polish"}
            ]},
            [{"finding_id": "x", "fixability": "trivial"}],
        )
        assert result is True


class TestDedupFixed:
    def test_dedups_by_finding_id_keeping_latest(self):
        from gate.fixer import _dedup_fixed
        entries = [
            {"finding_id": "a", "iter": 1, "fix_description": "old"},
            {"finding_id": "b", "iter": 1},
            {"finding_id": "a", "iter": 2, "fix_description": "new"},
        ]
        out = _dedup_fixed(entries)
        assert len(out) == 2
        ids = [e["finding_id"] for e in out]
        assert ids == ["a", "b"]
        a = next(e for e in out if e["finding_id"] == "a")
        assert a["iter"] == 2

"""Tests for gate.github module."""

import subprocess
from unittest.mock import patch

from gate.github import (
    _build_comment,
    _format_build_section,
    _format_findings,
    _format_resolved,
    approve_pr,
    comment_pr,
    complete_check_run,
    create_check_run,
)


class TestFormatFindings:
    def test_empty_findings(self):
        assert _format_findings([]) == ""

    def test_error_findings(self):
        findings = [
            {"severity": "critical", "file": "foo.ts", "line": 10, "message": "SQL injection"},
        ]
        result = _format_findings(findings)
        assert "### Errors" in result
        assert "foo.ts:10" in result
        assert "SQL injection" in result

    def test_warning_with_suggestion(self):
        findings = [
            {
                "severity": "warning",
                "file": "bar.ts",
                "line": 5,
                "message": "Unused var",
                "suggestion": "Remove it",
            },
        ]
        result = _format_findings(findings)
        assert "### Warnings" in result
        assert "Fix: Remove it" in result

    def test_info_findings(self):
        findings = [{"severity": "info", "file": "readme.md", "message": "Consider updating"}]
        result = _format_findings(findings)
        assert "### Notes" in result

    def test_evidence_labels(self):
        findings = [
            {
                "severity": "error",
                "file": "x.ts",
                "message": "bug",
                "evidence_level": "test_confirmed",
            },
        ]
        result = _format_findings(findings)
        assert "test confirmed" in result

    def test_deduped_multi_location_finding_renders_also_at(self):
        findings = [
            {
                "severity": "warning",
                "file": "a.py",
                "line": 10,
                "message": "Multi-line comment block",
                "locations": [
                    {"file": "a.py", "line": 10},
                    {"file": "a.py", "line": 50},
                    {"file": "b.py", "line": 5},
                ],
            },
        ]
        result = _format_findings(findings)
        assert "a.py:10" in result
        assert "Also at:" in result
        assert "`a.py:50`" in result
        assert "`b.py:5`" in result

    def test_malformed_finding_surfaces_in_dedicated_section(self):
        # Missing `message` — must NOT be silently dropped. A
        # "Malformed findings" section flags emitter drift so the
        # reviewer sees something is wrong at comment render time.
        findings = [
            {"severity": "error", "file": "a.py"},
            {"severity": "warning", "file": "b.py", "message": "ok warning"},
        ]
        result = _format_findings(findings)
        assert "Malformed findings" in result
        assert "### Warnings" in result  # good one still rendered

    def test_missing_severity_is_malformed_not_dropped(self):
        findings = [{"file": "a.py", "message": "I forgot severity"}]
        result = _format_findings(findings)
        assert "Malformed findings" in result


class TestFormatResolved:
    def test_empty_resolved(self):
        assert _format_resolved([]) == ""

    def test_resolved_findings(self):
        resolved = [
            {"file": "old.ts", "message": "Old bug", "resolution": "fixed_by_author"},
        ]
        result = _format_resolved(resolved)
        assert "Resolved since last review" in result
        assert "old.ts" in result
        assert "fixed" in result


class TestFormatBuildSection:
    def test_none_build(self):
        assert _format_build_section(None) == ""

    def test_all_passing_with_tools(self):
        build = {
            "typecheck": {"pass": True, "error_count": 0, "tool": "npx"},
            "lint": {"pass": True, "warning_count": 0, "tool": "eslint"},
            "tests": {"pass": True, "passed": 42, "total": 42, "tool": "vitest"},
        }
        result = _format_build_section(build)
        assert "npx: ✅" in result
        assert "eslint: ✅" in result
        assert "vitest: ✅" in result

    def test_failures_with_tools(self):
        build = {
            "typecheck": {"pass": False, "error_count": 3, "tool": "npx"},
            "lint": {"pass": False, "error_count": 2, "warning_count": 5, "tool": "eslint"},
            "tests": {"pass": False, "failed": 1, "passed": 41, "total": 42, "tool": "vitest"},
        }
        result = _format_build_section(build)
        assert "npx: ❌" in result
        assert "eslint: ❌" in result
        assert "vitest: ❌" in result

    def test_no_tool_means_section_omitted(self):
        """Sections without a configured tool should not appear — avoids
        misleading '0 errors' lines for steps that never ran."""
        build = {
            "typecheck": {"pass": True, "error_count": 0, "tool": ""},
            "lint": {"pass": True, "warning_count": 0, "tool": ""},
            "tests": {"pass": True, "passed": 10, "total": 10, "tool": ""},
        }
        result = _format_build_section(build)
        # When no section has a configured tool, the whole block is dropped
        assert result == ""

    def test_mixed_configured_and_unconfigured(self):
        """Only configured tools appear; unconfigured ones are omitted."""
        build = {
            "typecheck": {"pass": True, "error_count": 0, "tool": ""},
            "lint": {"pass": True, "warning_count": 0, "tool": "ruff"},
            "tests": {"pass": True, "passed": 100, "total": 100, "tool": "pytest"},
        }
        result = _format_build_section(build)
        assert "Type check" not in result  # typecheck omitted
        assert "ruff: ✅" in result
        assert "pytest: ✅" in result

    def test_python_tool_display_maps_to_pytest(self):
        """A `python -m pytest` test_cmd yields tool='python'; the comment
        should display 'pytest' for clarity."""
        build = {
            "tests": {"pass": True, "passed": 100, "total": 100, "tool": "python"},
        }
        result = _format_build_section(build)
        assert "pytest: ✅ (100/100 passed)" in result
        assert "python:" not in result  # raw tool name should not leak

    def test_backward_compat_typescript_key_with_tool(self):
        """Legacy `typescript` key still works when a tool is set."""
        build = {
            "typescript": {"pass": True, "error_count": 0, "tool": "tsc"},
            "lint": {"pass": True, "warning_count": 0, "tool": "eslint"},
            "tests": {"pass": True, "passed": 42, "total": 42, "tool": "vitest"},
        }
        result = _format_build_section(build)
        assert "tsc: ✅" in result


class TestBuildComment:
    def test_approved(self):
        verdict = {
            "decision": "approve",
            "confidence": "high",
            "summary": "Looks good",
            "findings": [],
            "stats": {"stages_run": 4},
            "review_time_seconds": 120,
        }
        result = _build_comment(verdict, None)
        assert "Gate Review ✅" in result
        assert "Approved" in result
        assert "Looks good" in result

    def test_approve_with_notes(self):
        verdict = {
            "decision": "approve_with_notes",
            "confidence": "medium",
            "summary": "Minor issues",
            "findings": [{"severity": "info", "file": "x.ts", "message": "note"}],
            "stats": {"stages_run": 4},
        }
        result = _build_comment(verdict, None)
        assert "Approved with notes" in result

    def test_request_changes(self):
        verdict = {
            "decision": "request_changes",
            "confidence": "high",
            "summary": "Critical bugs",
            "findings": [{"severity": "error", "file": "x.ts", "message": "bug"}],
            "stats": {"stages_run": 4},
        }
        result = _build_comment(verdict, None)
        assert "Gate Review ❌" in result
        assert "Changes requested" in result

    def test_with_build(self):
        verdict = {
            "decision": "approve",
            "confidence": "high",
            "summary": "OK",
            "findings": [],
            "stats": {"stages_run": 4},
        }
        build = {"typecheck": {"pass": True, "error_count": 0, "tool": "tsc"}}
        result = _build_comment(verdict, build)
        assert "Build Results" in result


class TestFormatBuildSectionSkipped:
    def test_format_build_section_skipped(self):
        build = {
            "skipped": True,
            "skip_reason": "no build commands (project_type=none)",
            "overall_pass": True,
        }
        result = _format_build_section(build)
        assert "skipped" in result.lower()
        assert "no build commands" in result

    def test_format_build_section_skipped_default_reason(self):
        build = {"skipped": True, "overall_pass": True}
        result = _format_build_section(build)
        assert "no build commands configured" in result


# ── Integration-ish tests for the API wrappers ─────────────────
#
# These cover the argv that Gate actually hands to ``gh``, not just the
# markdown helpers. Every test patches ``gate.github._gh`` so no real
# network call is made. The goal is to catch regressions in the command
# shape (flags, ordering, `-f` key=value pairs) that our branch-protection
# contract depends on -- the context name has to stay ``gate-review``,
# statuses have to target ``repos/{repo}/statuses/{sha}``, etc.


class TestCreateCheckRun:
    """Ports to the Commit Statuses API (naming is historical)."""

    @patch("gate.github._gh")
    def test_posts_pending_status_with_default_context(self, mock_gh):
        mock_gh.return_value = ""
        result = create_check_run("owner/repo", "deadbeef")
        assert result == "gate-review"
        mock_gh.assert_called_once()
        args = mock_gh.call_args.args[0]
        assert args[0] == "api"
        assert args[1] == "repos/owner/repo/statuses/deadbeef"
        assert "-X" in args and "POST" in args
        assert "state=pending" in args
        assert "context=gate-review" in args

    @patch("gate.github._gh")
    def test_honors_custom_context_name(self, mock_gh):
        mock_gh.return_value = ""
        result = create_check_run("owner/repo", "abc", name="gate-fix")
        assert result == "gate-fix"
        args = mock_gh.call_args.args[0]
        assert "context=gate-fix" in args

    @patch("gate.github._gh")
    def test_returns_none_on_gh_failure(self, mock_gh):
        mock_gh.side_effect = subprocess.CalledProcessError(1, ["gh"])
        result = create_check_run("owner/repo", "abc")
        assert result is None


class TestCompleteCheckRun:
    @patch("gate.github._gh")
    def test_maps_success_to_success_state(self, mock_gh):
        mock_gh.return_value = ""
        complete_check_run(
            "owner/repo", "gate-review",
            conclusion="success",
            output_title="Approved",
            sha="deadbeef",
        )
        args = mock_gh.call_args.args[0]
        assert args[1] == "repos/owner/repo/statuses/deadbeef"
        assert "state=success" in args
        assert "context=gate-review" in args
        assert "description=Approved" in args

    @patch("gate.github._gh")
    def test_maps_cancelled_to_failure_state(self, mock_gh):
        mock_gh.return_value = ""
        complete_check_run(
            "owner/repo", "gate-review",
            conclusion="cancelled",
            output_title="Superseded",
            sha="abc1234",
        )
        args = mock_gh.call_args.args[0]
        assert "state=failure" in args

    @patch("gate.github._gh")
    def test_truncates_long_title_in_description(self, mock_gh):
        mock_gh.return_value = ""
        long_title = "x" * 200
        complete_check_run(
            "owner/repo", "gate-review",
            conclusion="failure",
            output_title=long_title,
            sha="abc",
        )
        args = mock_gh.call_args.args[0]
        # GitHub's status description hard cap is 140 chars; we enforce it
        # on the caller side so a verbose output_title never poisons the API.
        desc_arg = next(a for a in args if a.startswith("description="))
        assert len(desc_arg) - len("description=") <= 140

    @patch("gate.github._gh")
    def test_no_op_when_no_sha(self, mock_gh):
        complete_check_run("owner/repo", "gate-review", "success", sha="")
        mock_gh.assert_not_called()

    @patch("gate.github._gh")
    def test_no_op_when_no_check_run_id(self, mock_gh):
        complete_check_run("owner/repo", None, "success", sha="abc")
        mock_gh.assert_not_called()


class TestApprovePr:
    @patch("gate.github._gh")
    def test_calls_pr_review_approve(self, mock_gh):
        mock_gh.return_value = ""
        approve_pr("owner/repo", 42, "Looks good")
        mock_gh.assert_called_once_with(
            ["pr", "review", "42", "--repo", "owner/repo", "--approve", "--body", "Looks good"]
        )

    @patch("gate.github.comment_pr")
    @patch("gate.github._gh")
    def test_falls_back_to_comment_when_self_approval_blocked(
        self, mock_gh, mock_comment
    ):
        err = subprocess.CalledProcessError(1, ["gh"])
        err.stderr = "cannot approve your own pull request"
        mock_gh.side_effect = err
        approve_pr("owner/repo", 42, "Looks good")
        mock_comment.assert_called_once_with("owner/repo", 42, "Looks good")

    @patch("gate.github.comment_pr")
    @patch("gate.github._gh")
    def test_swallows_other_errors_without_commenting(
        self, mock_gh, mock_comment
    ):
        err = subprocess.CalledProcessError(1, ["gh"])
        err.stderr = "permission denied"
        mock_gh.side_effect = err
        approve_pr("owner/repo", 42, "Looks good")
        mock_comment.assert_not_called()


class TestCommentPr:
    @patch("gate.github._gh")
    def test_calls_pr_comment(self, mock_gh):
        mock_gh.return_value = ""
        comment_pr("owner/repo", 99, "hello")
        mock_gh.assert_called_once_with(
            ["pr", "comment", "99", "--repo", "owner/repo", "--body", "hello"]
        )

    @patch("gate.github._gh")
    def test_swallows_gh_failure(self, mock_gh):
        mock_gh.side_effect = subprocess.CalledProcessError(1, ["gh"])
        # Should not raise.
        comment_pr("owner/repo", 99, "hello")


class TestCommitAndPush:
    """Regression: commit_and_push must return a CommitResult that
    distinguishes pushed/no_diff/push_failed (Group 1A)."""

    @patch("gate.github.subprocess.run")
    @patch("gate.workspace._git_env", return_value={})
    def test_returns_no_diff_when_index_empty(self, _env, mock_run, tmp_path):
        from gate.github import commit_and_push

        def fake_run(cmd, **kwargs):
            if cmd[:4] == ["git", "diff", "--cached", "--quiet"]:
                return subprocess.CompletedProcess(cmd, 0)
            return subprocess.CompletedProcess(cmd, 0)
        mock_run.side_effect = fake_run
        result = commit_and_push(tmp_path, "msg", branch="feat")
        assert result.status == "no_diff"
        assert result.success is False

    @patch("gate.github.subprocess.run")
    @patch("gate.workspace._git_env", return_value={})
    def test_returns_push_failed_on_push_error(self, _env, mock_run, tmp_path):
        from gate.github import commit_and_push

        def fake_run(cmd, **kwargs):
            if cmd[:4] == ["git", "diff", "--cached", "--quiet"]:
                return subprocess.CompletedProcess(cmd, 1)
            if cmd[:2] == ["git", "push"]:
                e = subprocess.CalledProcessError(1, cmd)
                e.stderr = b"denied: network down\n"
                raise e
            return subprocess.CompletedProcess(cmd, 0)
        mock_run.side_effect = fake_run
        result = commit_and_push(tmp_path, "msg", branch="feat")
        assert result.status == "push_failed"
        assert "denied" in result.error

    @patch("gate.github.subprocess.run")
    @patch("gate.workspace._git_env", return_value={})
    def test_returns_pushed_with_sha_on_success(self, _env, mock_run, tmp_path):
        from gate.github import commit_and_push

        def fake_run(cmd, **kwargs):
            if cmd[:4] == ["git", "diff", "--cached", "--quiet"]:
                return subprocess.CompletedProcess(cmd, 1)
            if cmd[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="abcdef1234\n")
            return subprocess.CompletedProcess(cmd, 0)
        mock_run.side_effect = fake_run
        result = commit_and_push(tmp_path, "msg", branch="feat")
        assert result.status == "pushed"
        assert result.success is True
        assert result.sha.startswith("abcdef")

"""Tests for gate.github module."""

from gate.github import (
    _build_comment,
    _format_build_section,
    _format_findings,
    _format_resolved,
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

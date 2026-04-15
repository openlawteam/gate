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

    def test_all_passing(self):
        build = {
            "typescript": {"pass": True, "error_count": 0},
            "lint": {"pass": True, "warning_count": 0},
            "tests": {"pass": True, "passed": 42, "total": 42},
        }
        result = _format_build_section(build)
        assert "TypeScript: ✅" in result
        assert "Lint: ✅" in result
        assert "Tests: ✅" in result

    def test_failures(self):
        build = {
            "typescript": {"pass": False, "error_count": 3},
            "lint": {"pass": False, "error_count": 2, "warning_count": 5},
            "tests": {"pass": False, "failed": 1, "passed": 41, "total": 42},
        }
        result = _format_build_section(build)
        assert "TypeScript: ❌" in result
        assert "Lint: ❌" in result
        assert "Tests: ❌" in result


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
        build = {"typescript": {"pass": True, "error_count": 0}}
        result = _build_comment(verdict, build)
        assert "Build Results" in result

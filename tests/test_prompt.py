"""Tests for gate.prompt module."""

import json

from gate.prompt import (
    _read_file,
    _read_json_file,
    build_diff_or_summary,
    build_vars,
    safe_substitute,
    truncate,
)


class TestSafeSubstitute:
    def test_replaces_known_vars(self):
        result = safe_substitute("Hello $name!", {"name": "World"})
        assert result == "Hello World!"

    def test_leaves_unknown_vars(self):
        result = safe_substitute("Hello $name $unknown!", {"name": "World"})
        assert result == "Hello World $unknown!"

    def test_multiple_vars(self):
        template = "PR #$pr_number by $pr_author: $pr_title"
        vars = {"pr_number": "42", "pr_author": "alice", "pr_title": "Fix bug"}
        result = safe_substitute(template, vars)
        assert result == "PR #42 by alice: Fix bug"

    def test_var_pattern(self):
        result = safe_substitute("$foo_bar $Foo $123", {"foo_bar": "replaced"})
        assert result == "replaced $Foo $123"

    def test_empty_template(self):
        result = safe_substitute("", {"key": "val"})
        assert result == ""

    def test_no_vars_in_template(self):
        result = safe_substitute("No variables here.", {"key": "val"})
        assert result == "No variables here."


class TestTruncate:
    def test_short_text_unchanged(self):
        assert truncate("hello", 100, "Test") == "hello"

    def test_long_text_truncated(self):
        text = "x" * 200
        result = truncate(text, 100, "Test")
        assert len(result.encode("utf-8")) < 200
        assert "[Test truncated at" in result

    def test_truncation_notice_includes_label(self):
        text = "x" * 200
        result = truncate(text, 50, "My Label")
        assert "My Label" in result


class TestReadFile:
    def test_reads_existing_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("content")
        assert _read_file(f) == "content"

    def test_returns_empty_for_missing(self, tmp_path):
        assert _read_file(tmp_path / "missing.txt") == ""


class TestReadJsonFile:
    def test_reads_valid_json(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text('{"key": "value"}')
        assert _read_json_file(f) == {"key": "value"}

    def test_returns_none_for_missing(self, tmp_path):
        assert _read_json_file(tmp_path / "missing.json") is None

    def test_returns_none_for_invalid_json(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("not json")
        assert _read_json_file(f) is None


class TestBuildVars:
    def test_returns_dict(self, tmp_workspace):
        env_vars = {"pr_title": "Test PR", "pr_body": "Body", "pr_author": "alice"}
        vars = build_vars(tmp_workspace, "triage", env_vars)
        assert isinstance(vars, dict)

    def test_includes_pr_metadata(self, tmp_workspace):
        env_vars = {"pr_title": "Test PR", "pr_body": "Body", "pr_author": "alice"}
        vars = build_vars(tmp_workspace, "triage", env_vars)
        assert vars["pr_title"] == "Test PR"
        assert vars["pr_author"] == "alice"

    def test_includes_diff(self, tmp_workspace):
        env_vars = {}
        vars = build_vars(tmp_workspace, "architecture", env_vars)
        assert "diff" in vars["diff"]

    def test_includes_changed_files(self, tmp_workspace):
        env_vars = {}
        vars = build_vars(tmp_workspace, "architecture", env_vars)
        assert "foo.ts" in vars["changed_files"]

    def test_includes_file_count(self, tmp_workspace):
        env_vars = {}
        vars = build_vars(tmp_workspace, "architecture", env_vars)
        assert vars["file_count"] == "2"

    def test_triage_uses_diff_or_summary(self, tmp_workspace):
        env_vars = {}
        vars = build_vars(tmp_workspace, "triage", env_vars)
        assert "diff_or_summary" in vars

    def test_missing_stage_files_default_empty(self, tmp_workspace):
        env_vars = {}
        vars = build_vars(tmp_workspace, "verdict", env_vars)
        assert vars["triage_json"] == ""
        assert vars["architecture_json"] == ""

    def test_prior_review_default(self, tmp_workspace):
        env_vars = {}
        vars = build_vars(tmp_workspace, "verdict", env_vars)
        assert '"has_prior": false' in vars["prior_review_json"]

    def test_fixable_findings_from_verdict(self, tmp_workspace):
        verdict = {
            "findings": [
                {"severity": "warning", "message": "test", "introduced_by_pr": True},
                {"severity": "info", "message": "note", "introduced_by_pr": True},
            ]
        }
        (tmp_workspace / "verdict.json").write_text(json.dumps(verdict))
        env_vars = {}
        vars = build_vars(tmp_workspace, "fix-senior", env_vars)
        findings = json.loads(vars["findings_json"])
        assert len(findings) == 1
        assert findings[0]["severity"] == "warning"


class TestBuildDiffOrSummary:
    def test_returns_full_diff_if_small(self, tmp_workspace):
        result = build_diff_or_summary(tmp_workspace)
        assert "diff --git" in result

    def test_returns_summary_if_large(self, tmp_workspace):
        large_diff = "x" * 200_000
        (tmp_workspace / "diff.txt").write_text(large_diff)
        result = build_diff_or_summary(tmp_workspace, budget_bytes=1000)
        assert "Per-File Preview" in result or "exceeds" in result

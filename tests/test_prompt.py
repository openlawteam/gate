"""Tests for gate.prompt module."""

import json

from gate.prompt import (
    _read_file,
    _read_json_file,
    _stage_summary,
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

    def test_dict_value_is_coerced_not_raised(self):
        # Regression: PR #216 architecture.summary came back as a count
        # dict; re.sub's internal str.join crashed with "sequence item N:
        # expected str instance, dict found". safe_substitute must coerce.
        result = safe_substitute(
            "summary=$architecture_summary",
            {"architecture_summary": {"errors": 4, "warnings": 14, "info": 3}},
        )
        assert result.startswith("summary=")
        assert "errors" in result and "4" in result

    def test_list_value_is_coerced(self):
        result = safe_substitute("$xs", {"xs": [1, 2, 3]})
        assert "1" in result and "3" in result

    def test_int_value_is_coerced(self):
        result = safe_substitute("count=$n", {"n": 42})
        assert result == "count=42"


class TestStageSummary:
    def test_string_summary_passthrough(self):
        assert _stage_summary({"summary": "hello"}, "fallback") == "hello"

    def test_missing_data_falls_back(self):
        assert _stage_summary(None, "fallback") == "fallback"

    def test_missing_summary_key_falls_back(self):
        assert _stage_summary({"risk_level": "low"}, "fallback") == "fallback"

    def test_count_dict_rendered_as_kv_pairs(self):
        out = _stage_summary(
            {"summary": {"errors": 4, "warnings": 14, "info": 3}},
            "fallback",
        )
        assert "errors: 4" in out
        assert "warnings: 14" in out
        assert "info: 3" in out

    def test_complex_dict_falls_back_to_json(self):
        out = _stage_summary(
            {"summary": {"nested": {"a": 1}}},
            "fallback",
        )
        assert '"nested"' in out

    def test_list_summary_rendered_as_json(self):
        out = _stage_summary({"summary": ["a", "b"]}, "fallback")
        assert "a" in out and "b" in out


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

    def test_change_intent_defaults_to_empty_object(self, tmp_workspace):
        """When triage.json is missing (or predates the change_intent
        field), change_intent_json must default to `{}` — prompts can
        still substitute it without crashing."""
        env_vars = {}
        vars = build_vars(tmp_workspace, "logic", env_vars)
        assert "change_intent_json" in vars
        assert json.loads(vars["change_intent_json"]) == {}

    def test_change_intent_from_triage_json(self, tmp_workspace):
        triage = {
            "change_type": "feature",
            "risk_level": "medium",
            "summary": "...",
            "files_by_category": {},
            "fast_track_eligible": False,
            "change_intent": {
                "claimed_behavioral_delta": "adds foo() API",
                "claimed_bug_fixed": None,
                "claimed_tests_updated": ["foo.test.ts"],
                "claimed_no_behavior_change": False,
                "confidence": "high",
            },
        }
        (tmp_workspace / "triage.json").write_text(json.dumps(triage))
        env_vars = {}
        vars = build_vars(tmp_workspace, "logic", env_vars)
        parsed = json.loads(vars["change_intent_json"])
        assert parsed["confidence"] == "high"
        assert parsed["claimed_behavioral_delta"] == "adds foo() API"

    def test_change_intent_absent_from_old_triage(self, tmp_workspace):
        """Cached triage.json from a pre-change_intent Gate version must
        still produce a valid (empty) change_intent_json."""
        old_triage = {
            "change_type": "feature",
            "risk_level": "low",
            "summary": "...",
            "files_by_category": {},
            "fast_track_eligible": False,
        }
        (tmp_workspace / "triage.json").write_text(json.dumps(old_triage))
        env_vars = {}
        vars = build_vars(tmp_workspace, "logic", env_vars)
        assert json.loads(vars["change_intent_json"]) == {}

    def test_postconditions_defaults_to_empty_list(self, tmp_workspace):
        env_vars = {}
        vars = build_vars(tmp_workspace, "logic", env_vars)
        assert "postconditions_json" in vars
        assert json.loads(vars["postconditions_json"]) == []

    def test_postconditions_populated_from_file(self, tmp_workspace):
        pc = {
            "postconditions": [
                {
                    "function_path": "src/x.py:foo",
                    "signature": "foo(a: int) -> int",
                    "prose": "returns non-negative",
                    "assertion_snippet": "result >= 0",
                    "confidence": "high",
                    "rationale": "...",
                }
            ]
        }
        (tmp_workspace / "postconditions.json").write_text(json.dumps(pc))
        env_vars = {}
        vars = build_vars(tmp_workspace, "logic", env_vars)
        parsed = json.loads(vars["postconditions_json"])
        assert len(parsed) == 1
        assert parsed[0]["function_path"] == "src/x.py:foo"

    def test_postconditions_max_functions_surfaced(self, tmp_workspace):
        env_vars = {}
        vars = build_vars(
            tmp_workspace, "postconditions", env_vars,
            config={"limits": {"postconditions_max_functions": 7}},
        )
        assert vars["postconditions_max_functions"] == "7"

    def test_postconditions_stage_uses_diff_or_summary(self, tmp_workspace):
        """postconditions runs inline with --tools "" so it must receive
        the summarized diff when the raw diff is too large."""
        env_vars = {}
        vars = build_vars(tmp_workspace, "postconditions", env_vars)
        assert "diff_or_summary" in vars

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


class TestPromptAnchors:
    """Regression tests: assert key phrases remain present in shipped prompts.

    These protect against accidental deletion of prompt instructions that
    encode contract behavior (mutation check, intent tagging, etc.).
    """

    def test_logic_prompt_has_mutation_check(self, real_gate_dir):
        text = (real_gate_dir / "prompts" / "logic.md").read_text()
        assert "Mutation Check" in text
        assert "mutation_check" in text
        assert "one-point mutation" in text or "one observable value" in text

    def test_logic_prompt_tests_written_includes_intent_type(self, real_gate_dir):
        text = (real_gate_dir / "prompts" / "logic.md").read_text()
        assert "intent_type" in text
        assert "confirmed_correct" in text
        assert "confirmed_bug" in text
        assert "inconclusive" in text

    def test_verdict_prompt_enforces_mutation_check(self, real_gate_dir):
        text = (real_gate_dir / "prompts" / "verdict.md").read_text()
        assert "Mutation-check requirement" in text or "mutation_check" in text
        assert "test_confirmed" in text
        assert "pattern_match" in text

    def test_triage_prompt_defines_change_intent(self, real_gate_dir):
        text = (real_gate_dir / "prompts" / "triage.md").read_text()
        assert "Change Intent Extraction" in text
        assert "change_intent" in text
        assert "claimed_behavioral_delta" in text
        assert "claimed_no_behavior_change" in text
        assert "confidence" in text

    def test_logic_prompt_consumes_change_intent(self, real_gate_dir):
        text = (real_gate_dir / "prompts" / "logic.md").read_text()
        assert "change_intent_json" in text
        assert "Intent Verification" in text

    def test_verdict_prompt_consumes_change_intent(self, real_gate_dir):
        text = (real_gate_dir / "prompts" / "verdict.md").read_text()
        assert "change_intent_json" in text
        assert "Intent-Mismatch Rule" in text or "intent mismatch" in text.lower()

    def test_postconditions_prompt_exists_and_has_schema(self, real_gate_dir):
        text = (real_gate_dir / "prompts" / "postconditions.md").read_text()
        assert "Postcondition" in text
        assert "function_path" in text
        assert "assertion_snippet" in text
        assert "postconditions_max_functions" in text
        assert "diff_or_summary" in text

    def test_logic_prompt_consumes_postconditions(self, real_gate_dir):
        text = (real_gate_dir / "prompts" / "logic.md").read_text()
        assert "postconditions_json" in text
        assert "Postcondition Checking" in text

    def test_verdict_prompt_consumes_postconditions(self, real_gate_dir):
        text = (real_gate_dir / "prompts" / "verdict.md").read_text()
        assert "postconditions_json" in text

    def test_fix_senior_uses_file_redirection_not_heredoc(self, real_gate_dir):
        """Fix 3a regression: fix-senior.md must document the
        ``gate-code <stage> < gate-directions.md`` file-redirection
        pattern and must NOT show a dispatch example using the
        deprecated ``<<'EOF'`` heredoc pattern. Heredocs corrupt
        directions containing ``EOF`` or backticks and are a
        shell-injection vector when the terminator is user-chosen.

        Note: the prompt may still *mention* ``<<'EOF'`` in a warning
        block telling senior NOT to use it — this test therefore only
        forbids the old dispatch example (``gate-code ... <<'EOF'``),
        not every occurrence of the string."""
        text = (real_gate_dir / "prompts" / "fix-senior.md").read_text()
        assert "gate-code <stage> < gate-directions.md" in text
        # Forbid the deprecated dispatch example on any single line.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("gate-code") and "<<" in stripped:
                raise AssertionError(
                    f"fix-senior.md still shows a heredoc dispatch example: {stripped!r}"
                )
        # Senior must know to overwrite the file each call, not append.
        assert "overwrite" in text.lower()

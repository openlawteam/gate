"""Tests for gate.extract module.

Ported from extract-stage.test.js — JSON extraction, exploit scenario
enforcement, fix stage normalization.
"""

import json

from gate.extract import (
    build_extract_fallback,
    enforce_exploit_scenario,
    extract_from_transcript,
    extract_json_from_text,
    extract_stage_output,
    parse_diff_hunks,
    validate_introduced_by_pr,
    validate_stage_output,
)


class TestExtractJsonFromText:
    def test_plain_json(self):
        text = '{"findings": [], "pass": true}'
        result = extract_json_from_text(text)
        assert result["pass"] is True
        assert result["findings"] == []

    def test_json_in_fence(self):
        text = 'Some text\n```json\n{"key": "value"}\n```\nMore text'
        result = extract_json_from_text(text)
        assert result == {"key": "value"}

    def test_json_in_fence_no_lang(self):
        text = 'Text\n```\n{"key": "value"}\n```\nMore'
        result = extract_json_from_text(text)
        assert result == {"key": "value"}

    def test_json_with_surrounding_prose(self):
        text = 'Here is the result:\n{"findings": [{"message": "test"}]}\nDone.'
        result = extract_json_from_text(text)
        assert result["findings"][0]["message"] == "test"

    def test_no_json(self):
        assert extract_json_from_text("No JSON here") is None

    def test_empty_string(self):
        assert extract_json_from_text("") is None

    def test_none_input(self):
        assert extract_json_from_text(None) is None

    def test_nested_braces(self):
        text = '{"outer": {"inner": "value"}}'
        result = extract_json_from_text(text)
        assert result["outer"]["inner"] == "value"


class TestExtractFromTranscript:
    def test_json_array_transcript(self):
        transcript = json.dumps([
            {"role": "user", "content": "Review this"},
            {
                "role": "assistant",
                "content": '```json\n{"findings": [{"message": "bug"}], "pass": false}\n```',
            },
        ])
        result = extract_from_transcript(transcript)
        assert result is not None
        assert len(result["findings"]) == 1

    def test_transcript_with_content_blocks(self):
        transcript = json.dumps([
            {"role": "user", "content": "Review"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": '{"findings": [], "pass": true}'},
                ],
            },
        ])
        result = extract_from_transcript(transcript)
        assert result["pass"] is True

    def test_last_assistant_message_wins(self):
        transcript = json.dumps([
            {"role": "assistant", "content": '{"findings": [{"message": "old"}]}'},
            {"role": "user", "content": "Continue"},
            {"role": "assistant", "content": '{"findings": [{"message": "new"}], "pass": true}'},
        ])
        result = extract_from_transcript(transcript)
        assert result["findings"][0]["message"] == "new"

    def test_transcript_result_field(self):
        transcript = json.dumps({
            "result": '{"findings": [], "pass": true}',
        })
        result = extract_from_transcript(transcript)
        assert result["pass"] is True

    def test_plain_text_fallback(self):
        text = '{"findings": [], "pass": true}'
        result = extract_from_transcript(text)
        assert result["pass"] is True


class TestEnforceExploitScenario:
    def test_downgrades_critical_without_scenario(self):
        parsed = {
            "findings": [
                {"severity": "critical", "message": "SQL injection", "exploit_scenario": ""},
            ]
        }
        enforce_exploit_scenario(parsed)
        assert parsed["findings"][0]["severity"] == "medium"
        assert "_downgraded" in parsed["findings"][0]

    def test_downgrades_high_with_short_scenario(self):
        parsed = {
            "findings": [
                {"severity": "high", "message": "XSS", "exploit_scenario": "short"},
            ]
        }
        enforce_exploit_scenario(parsed)
        assert parsed["findings"][0]["severity"] == "medium"

    def test_keeps_critical_with_long_scenario(self):
        scenario = "An attacker can exploit this by crafting a malicious input that " * 3
        parsed = {
            "findings": [
                {"severity": "critical", "message": "RCE", "exploit_scenario": scenario},
            ]
        }
        enforce_exploit_scenario(parsed)
        assert parsed["findings"][0]["severity"] == "critical"
        assert "_downgraded" not in parsed["findings"][0]

    def test_keeps_medium_severity(self):
        parsed = {
            "findings": [
                {"severity": "medium", "message": "Minor issue", "exploit_scenario": ""},
            ]
        }
        enforce_exploit_scenario(parsed)
        assert parsed["findings"][0]["severity"] == "medium"
        assert "_downgraded" not in parsed["findings"][0]

    def test_no_findings(self):
        parsed = {"findings": []}
        enforce_exploit_scenario(parsed)
        assert parsed["findings"] == []

    def test_no_findings_key(self):
        parsed = {"summary": "no findings"}
        enforce_exploit_scenario(parsed)


class TestValidateStageOutput:
    def test_drops_invalid_findings(self):
        parsed = {
            "findings": [
                {"message": "valid"},
                "not a dict",
                {"no_message_key": True},
                None,
            ]
        }
        result = validate_stage_output(parsed, "architecture")
        assert len(result["findings"]) == 1
        assert result["findings"][0]["message"] == "valid"

    def test_resets_non_array_findings(self):
        parsed = {"findings": "not an array"}
        result = validate_stage_output(parsed, "security")
        assert result["findings"] == []
        assert "_validation_warning" in result

    def test_passes_valid_findings(self):
        parsed = {
            "findings": [
                {"message": "issue 1", "severity": "warning"},
                {"message": "issue 2", "severity": "error"},
            ]
        }
        result = validate_stage_output(parsed, "logic")
        assert len(result["findings"]) == 2


class TestExtractStageOutput:
    def test_extracts_from_raw_file(self, tmp_path):
        raw = json.dumps([
            {"role": "user", "content": "Review"},
            {"role": "assistant", "content": '{"findings": [{"message": "bug"}], "pass": false}'},
        ])
        raw_path = tmp_path / "architecture-raw.json"
        raw_path.write_text(raw)
        result = extract_stage_output(raw_path, "architecture")
        assert result is not None
        assert len(result["findings"]) == 1

    def test_returns_none_for_missing_file(self, tmp_path):
        result = extract_stage_output(tmp_path / "missing.json", "architecture")
        assert result is None

    def test_fix_stage_normalization(self, tmp_path):
        raw = json.dumps({
            "result": '{"fixed": [{"id": 1}], "not_fixed": []}',
        })
        raw_path = tmp_path / "fix-raw.json"
        raw_path.write_text(raw)
        result = extract_stage_output(raw_path, "fix")
        assert isinstance(result["fixed"], list)
        assert result["pass"] is True

    def test_security_applies_exploit_enforcement(self, tmp_path):
        raw = json.dumps([
            {
                "role": "assistant",
                "content": json.dumps({
                    "findings": [
                        {"message": "SQLi", "severity": "critical", "exploit_scenario": ""},
                    ],
                    "pass": False,
                }),
            },
        ])
        raw_path = tmp_path / "security-raw.json"
        raw_path.write_text(raw)
        result = extract_stage_output(raw_path, "security")
        assert result["findings"][0]["severity"] == "medium"


class TestBuildExtractFallback:
    def test_structure(self):
        fb = build_extract_fallback("architecture")
        assert fb["findings"] == []
        assert fb["pass"] is True
        assert fb["error"] == "parse_failed"
        assert "architecture" in fb["summary"]

    def test_truncates_raw_output(self):
        raw = "x" * 5000
        fb = build_extract_fallback("security", raw)
        assert len(fb["raw_output"]) == 2000


# ── introduced_by_pr classifier validation ───────────────────
#
# Regression guard for PR #19's architecture finding at
# ``gate/fixer.py:1367`` — the agent stamped ``introduced_by_pr: true``
# on a line that was not in the PR's diff. Prompt-only guidance
# insufficient; we enforce the invariant in code.


SIMPLE_DIFF = """\
diff --git a/gate/fixer.py b/gate/fixer.py
index abc..def 100644
--- a/gate/fixer.py
+++ b/gate/fixer.py
@@ -17,6 +17,7 @@ from gate import builder, github
 from gate.codex import bootstrap_codex
 from gate.config import build_claude_env, gate_dir
 from gate.finding_id import compute_finding_id
+from gate.io import atomic_write
 from gate.logger import write_live_log
 from gate.runner import StructuredRunner, run_with_retry
 from gate.schemas import FixResult
@@ -1014,7 +1015,7 @@ class FixPipeline:
         if self._pre_fix_sha:
             try:
                 gate_dir_marker = self.workspace / ".gate"
-                (gate_dir_marker / "pre-fix-sha").write_text(
+                atomic_write(
                     gate_dir_marker / "pre-fix-sha",
                     self._pre_fix_sha + "\\n",
                 )
"""


class TestParseDiffHunks:
    def test_parses_single_file_multiple_hunks(self):
        h = parse_diff_hunks(SIMPLE_DIFF)
        assert "gate/fixer.py" in h
        ranges = h["gate/fixer.py"]
        # Two hunks: one starting at +17 (7 lines) and one starting at
        # +1015 (7 lines).
        starts = sorted(r[0] for r in ranges)
        assert starts == [17, 1015]

    def test_ignores_dev_null_files(self):
        diff = (
            "diff --git a/x b/x\n"
            "--- a/x\n"
            "+++ /dev/null\n"
            "@@ -1,3 +0,0 @@\n"
            "-a\n-b\n-c\n"
        )
        assert parse_diff_hunks(diff) == {}

    def test_empty_diff_returns_empty(self):
        assert parse_diff_hunks("") == {}

    def test_malformed_hunk_header_ignored(self):
        diff = (
            "diff --git a/x b/x\n--- a/x\n+++ b/x\n"
            "@@ not a real header @@\n"
            "+content\n"
        )
        # Parser must not crash — and should report no hunks for x.
        assert parse_diff_hunks(diff) == {"x": []}

    def test_strips_b_prefix(self):
        diff = "+++ b/path/to/foo.py\n@@ -1 +10,2 @@\n+x\n+y\n"
        h = parse_diff_hunks(diff)
        assert "path/to/foo.py" in h


class TestValidateIntroducedByPr:
    def _write_diff(self, workspace, diff_text):
        (workspace / "diff.txt").write_text(diff_text)

    def test_keeps_true_when_line_inside_hunk(self, tmp_path):
        self._write_diff(tmp_path, SIMPLE_DIFF)
        findings = [
            {"file": "gate/fixer.py", "line": 1018, "introduced_by_pr": True,
             "message": "inside second hunk"},
        ]
        out = validate_introduced_by_pr(findings, tmp_path, "architecture")
        assert out[0]["introduced_by_pr"] is True
        assert "_classifier_downgraded" not in out[0]

    def test_downgrades_line_outside_hunk(self, tmp_path):
        """The exact PR #19 regression: line 1367 cited but only lines
        17 and 1015-1021 were actually changed."""
        self._write_diff(tmp_path, SIMPLE_DIFF)
        findings = [
            {"file": "gate/fixer.py", "line": 1367, "introduced_by_pr": True,
             "message": "write_baseline_diff should use atomic_write"},
        ]
        out = validate_introduced_by_pr(findings, tmp_path, "architecture")
        assert out[0]["introduced_by_pr"] is False
        assert out[0]["_classifier_downgraded"] == "line_not_in_diff"
        # Substance preserved — we only fix the classification flag.
        assert out[0]["message"].startswith("write_baseline_diff")

    def test_downgrades_when_file_not_in_diff(self, tmp_path):
        self._write_diff(tmp_path, SIMPLE_DIFF)
        findings = [
            {"file": "gate/other.py", "line": 5, "introduced_by_pr": True,
             "message": "untouched file"},
        ]
        out = validate_introduced_by_pr(findings, tmp_path, "architecture")
        assert out[0]["introduced_by_pr"] is False
        assert out[0]["_classifier_downgraded"] == "file_not_in_diff"

    def test_downgrades_when_no_line_number(self, tmp_path):
        self._write_diff(tmp_path, SIMPLE_DIFF)
        findings = [
            {"file": "gate/fixer.py", "line": None, "introduced_by_pr": True,
             "message": "whole-file claim"},
        ]
        out = validate_introduced_by_pr(findings, tmp_path, "architecture")
        assert out[0]["introduced_by_pr"] is False
        assert out[0]["_classifier_downgraded"] == "no_line_number"

    def test_downgrades_when_no_file(self, tmp_path):
        self._write_diff(tmp_path, SIMPLE_DIFF)
        findings = [
            {"file": "", "line": 5, "introduced_by_pr": True,
             "message": "no file"},
        ]
        out = validate_introduced_by_pr(findings, tmp_path, "architecture")
        assert out[0]["introduced_by_pr"] is False
        assert out[0]["_classifier_downgraded"] == "no_file"

    def test_leaves_false_findings_alone(self, tmp_path):
        """Findings already marked false (or missing the flag) are
        untouched — the validator only enforces the ``true → false``
        direction."""
        self._write_diff(tmp_path, SIMPLE_DIFF)
        findings = [
            {"file": "gate/other.py", "line": 5, "introduced_by_pr": False,
             "message": "legacy debt"},
            {"file": "gate/other.py", "line": 5,
             "message": "flag missing entirely"},
        ]
        out = validate_introduced_by_pr(findings, tmp_path, "architecture")
        assert "_classifier_downgraded" not in out[0]
        assert "_classifier_downgraded" not in out[1]

    def test_missing_diff_skips_validation(self, tmp_path):
        """If ``diff.txt`` is missing we leave findings as-is rather
        than mass-downgrade — a missing diff is a bigger problem than
        a classifier nit and should surface elsewhere."""
        findings = [
            {"file": "x.py", "line": 5, "introduced_by_pr": True, "message": "x"},
        ]
        out = validate_introduced_by_pr(findings, tmp_path, "architecture")
        assert out[0]["introduced_by_pr"] is True

    def test_empty_diff_skips_validation(self, tmp_path):
        self._write_diff(tmp_path, "")
        findings = [
            {"file": "x.py", "line": 5, "introduced_by_pr": True, "message": "x"},
        ]
        out = validate_introduced_by_pr(findings, tmp_path, "architecture")
        assert out[0]["introduced_by_pr"] is True

    def test_handles_non_dict_findings_gracefully(self, tmp_path):
        self._write_diff(tmp_path, SIMPLE_DIFF)
        findings = [
            "not a dict",
            None,
            {"file": "gate/fixer.py", "line": 17, "introduced_by_pr": True,
             "message": "ok"},
        ]
        out = validate_introduced_by_pr(findings, tmp_path, "architecture")
        assert out[0] == "not a dict"
        assert out[1] is None
        assert out[2]["introduced_by_pr"] is True

    def test_returns_input_when_not_a_list(self, tmp_path):
        self._write_diff(tmp_path, SIMPLE_DIFF)
        assert validate_introduced_by_pr({}, tmp_path, "architecture") == {}
        assert validate_introduced_by_pr(None, tmp_path, "architecture") is None

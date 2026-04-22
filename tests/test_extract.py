"""Tests for gate.extract module.

Ported from extract-stage.test.js — JSON extraction, exploit scenario
enforcement, fix stage normalization.
"""

import json

from gate.extract import (
    _dedupe_findings,
    _normalise_dedup_message,
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


class TestNormaliseDedupMessage:
    def test_strips_embedded_line_coords(self):
        a = _normalise_dedup_message("Use of X at line 10")
        b = _normalise_dedup_message("Use of X at line 22")
        assert a == b

    def test_preserves_meaningful_suffix(self):
        a = _normalise_dedup_message("Missing null check on user")
        b = _normalise_dedup_message("Missing null check on order")
        assert a != b

    def test_truncates_to_80_chars(self):
        s = "a" * 200
        assert len(_normalise_dedup_message(s)) == 80

    def test_empty_string_is_empty(self):
        assert _normalise_dedup_message("") == ""


class TestDedupeFindings:
    def _sample(self, **overrides):
        base = {
            "severity": "warning",
            "file": "a.py",
            "line": 10,
            "message": "Multi-line comment block",
            "rule_source": "style §8",
            "source_stage": "architecture",
        }
        base.update(overrides)
        return base

    def test_same_rule_same_message_different_files_collapses(self):
        f = [
            self._sample(file="a.py", line=10),
            self._sample(file="a.py", line=50),
            self._sample(file="b.py", line=5),
        ]
        out = _dedupe_findings(f)
        assert len(out) == 1
        merged = out[0]
        assert len(merged["locations"]) == 3
        # Sorted by (file, line): a.py:10, a.py:50, b.py:5
        assert merged["locations"][0] == {"file": "a.py", "line": 10}
        assert merged["locations"][1] == {"file": "a.py", "line": 50}
        assert merged["locations"][2] == {"file": "b.py", "line": 5}
        # Top-level file/line reflect primary (locations[0]).
        assert merged["file"] == "a.py"
        assert merged["line"] == 10
        assert merged["_deduped_from"] == 3

    def test_different_rule_same_message_stays_distinct(self):
        f = [
            self._sample(rule_source="rule-A"),
            self._sample(rule_source="rule-B"),
        ]
        out = _dedupe_findings(f)
        assert len(out) == 2

    def test_different_stage_same_message_stays_distinct(self):
        f = [
            self._sample(source_stage="architecture"),
            self._sample(source_stage="security"),
        ]
        out = _dedupe_findings(f)
        assert len(out) == 2

    def test_same_prefix_different_suffix_stays_distinct(self):
        f = [
            self._sample(message="Missing null check on user"),
            self._sample(message="Missing null check on order"),
        ]
        out = _dedupe_findings(f)
        assert len(out) == 2

    def test_coord_normalization_collapses(self):
        # Same message shape with different embedded line numbers.
        f = [
            self._sample(line=10, message="Bad thing at line 10"),
            self._sample(line=22, message="Bad thing at line 22"),
        ]
        out = _dedupe_findings(f)
        assert len(out) == 1
        assert len(out[0]["locations"]) == 2

    def test_duplicate_coords_deduped_within_group(self):
        f = [
            self._sample(file="a.py", line=10),
            self._sample(file="a.py", line=10),  # exact dup
            self._sample(file="a.py", line=10),
        ]
        out = _dedupe_findings(f)
        assert len(out) == 1
        assert len(out[0]["locations"]) == 1

    def test_worst_severity_wins(self):
        f = [
            self._sample(severity="warning"),
            self._sample(file="b.py", severity="error"),
            self._sample(file="c.py", severity="info"),
        ]
        out = _dedupe_findings(f)
        assert out[0]["severity"] == "error"

    def test_single_occurrence_gets_locations_array(self):
        f = [self._sample()]
        out = _dedupe_findings(f)
        assert len(out) == 1
        assert out[0]["locations"] == [{"file": "a.py", "line": 10}]

    def test_noop_on_empty_input(self):
        assert _dedupe_findings([]) == []
        assert _dedupe_findings(None) is None
        # Singletons are processed so they gain a `locations` array
        # for uniform downstream iteration (see
        # test_single_occurrence_gets_locations_array).

    def test_empty_message_passes_through_without_merging(self):
        # Findings with no message can't be keyed for dedup — they
        # must still be surfaced, not dropped.
        f = [
            {"severity": "info", "file": "a.py", "line": 1, "message": ""},
            {"severity": "info", "file": "a.py", "line": 2, "message": ""},
        ]
        out = _dedupe_findings(f)
        assert len(out) == 2

    def test_category_fallback_when_no_rule_source(self):
        # Same (stage, category, message) should collapse even when
        # rule_source is unset.
        f = [
            {"severity": "warning", "file": "a.py", "line": 1,
             "message": "x", "category": "style", "source_stage": "arch"},
            {"severity": "warning", "file": "a.py", "line": 2,
             "message": "x", "category": "style", "source_stage": "arch"},
        ]
        out = _dedupe_findings(f)
        assert len(out) == 1

    def test_preserves_finding_id_hash_when_stamped_after(self):
        # Dedup runs BEFORE finding_id stamping in the orchestrator —
        # verify that no stable hash gets blown away by a second dedup
        # pass on already-stamped findings.
        from gate.finding_id import compute_finding_id
        f = [
            self._sample(file="a.py", line=10, finding_id="abc123"),
            self._sample(file="a.py", line=50, finding_id="def456"),
        ]
        out = _dedupe_findings(f)
        # The merged finding keeps the first group entry's finding_id.
        # (The orchestrator only stamps ids AFTER dedup, so in practice
        # this field is absent when dedup runs. Pre-stamped inputs are
        # an edge case we preserve rather than rehash.)
        assert out[0].get("finding_id") == "abc123"
        # A fresh hash on the deduped finding would match the primary
        # location's original hash input.
        primary_hash = compute_finding_id({
            "file": out[0]["file"], "line": out[0]["line"],
            "source_stage": out[0]["source_stage"],
            "message": out[0]["message"],
        })
        first_original_hash = compute_finding_id({
            "file": f[0]["file"], "line": f[0]["line"],
            "source_stage": f[0]["source_stage"],
            "message": f[0]["message"],
        })
        # Identical — stable hash scheme across dedup.
        assert primary_hash == first_original_hash

    def test_non_dict_entries_passed_through(self):
        f = [
            self._sample(file="a.py"),
            None,
            "junk",
            self._sample(file="b.py"),
        ]
        out = _dedupe_findings(f)
        # Non-dict entries stay; dict entries collapsed.
        # The two dicts merge into one; the two non-dict pass through.
        assert sum(1 for x in out if isinstance(x, dict)) == 1
        assert None in out
        assert "junk" in out

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

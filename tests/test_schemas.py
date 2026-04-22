"""Tests for gate.schemas module."""

import json

import pytest

from gate.schemas import (
    AGENT_STAGES,
    ALLOWED_STAGES,
    FINDING_SEVERITIES,
    STAGE_EFFORT,
    STAGE_SCHEMAS,
    STRUCTURED_STAGES,
    Finding,
    FindingLocation,
    FixResult,
    StageResult,
    build_fallback,
)


class TestStageSchemas:
    def test_all_schemas_are_valid_json_schema(self):
        for name, schema in STAGE_SCHEMAS.items():
            assert schema["type"] == "object", f"{name} should be object type"
            assert "properties" in schema, f"{name} missing properties"
            assert "required" in schema, f"{name} missing required"

    def test_triage_schema(self):
        schema = STAGE_SCHEMAS["triage"]
        assert "change_type" in schema["properties"]
        assert "risk_level" in schema["properties"]
        assert "fast_track_eligible" in schema["properties"]
        assert "change_type" in schema["required"]

    def test_triage_schema_includes_change_intent_optional(self):
        """change_intent is an optional field — older cached triage.json
        (without it) must still validate."""
        schema = STAGE_SCHEMAS["triage"]
        assert "change_intent" in schema["properties"]
        change_intent = schema["properties"]["change_intent"]
        assert change_intent["type"] == "object"
        props = change_intent["properties"]
        for key in (
            "claimed_behavioral_delta",
            "claimed_bug_fixed",
            "claimed_tests_updated",
            "claimed_no_behavior_change",
            "confidence",
        ):
            assert key in props, f"change_intent missing {key}"
        # Must NOT be in required — older cached triage.json predates
        # this field and must still load cleanly.
        assert "change_intent" not in schema["required"]

    def test_verdict_schema(self):
        schema = STAGE_SCHEMAS["verdict"]
        assert "decision" in schema["properties"]
        assert "confidence" in schema["properties"]
        assert "findings" in schema["properties"]
        assert "decision" in schema["required"]

    def test_fix_rereview_schema(self):
        schema = STAGE_SCHEMAS["fix-rereview"]
        assert "pass" in schema["properties"]
        assert "issues" in schema["properties"]

    def test_postconditions_schema(self):
        schema = STAGE_SCHEMAS["postconditions"]
        assert schema["type"] == "object"
        assert "postconditions" in schema["properties"]
        items = schema["properties"]["postconditions"]["items"]
        assert items["type"] == "object"
        for key in ("function_path", "prose", "confidence"):
            assert key in items["required"], f"postcondition item missing required {key}"

    def test_postconditions_registered(self):
        assert "postconditions" in ALLOWED_STAGES
        assert "postconditions" in STRUCTURED_STAGES
        assert "postconditions" not in AGENT_STAGES


class TestStageEffort:
    def test_fix_stages_have_max_effort(self):
        assert STAGE_EFFORT["fix"] == "max"
        assert STAGE_EFFORT["fix-senior"] == "max"

    def test_review_stages_have_no_effort(self):
        assert "triage" not in STAGE_EFFORT
        assert "architecture" not in STAGE_EFFORT
        assert "verdict" not in STAGE_EFFORT


class TestStageCategories:
    def test_agent_stages(self):
        assert "architecture" in AGENT_STAGES
        assert "security" in AGENT_STAGES
        assert "logic" in AGENT_STAGES
        assert "fix-senior" in AGENT_STAGES

    def test_structured_stages(self):
        assert "triage" in STRUCTURED_STAGES
        assert "verdict" in STRUCTURED_STAGES
        assert "fix-rereview" in STRUCTURED_STAGES

    def test_no_overlap(self):
        assert AGENT_STAGES.isdisjoint(STRUCTURED_STAGES)

    def test_all_stages_listed(self):
        for stage in ALLOWED_STAGES:
            assert isinstance(stage, str)


class TestBuildFallback:
    def test_triage_fallback(self):
        fb = build_fallback("triage")
        assert fb["change_type"] == "mixed"
        assert fb["risk_level"] == "medium"
        assert fb["fast_track_eligible"] is False
        assert "triage_fallback" in fb["flags"]

    def test_verdict_fallback_approves(self):
        fb = build_fallback("verdict")
        assert fb["decision"] == "approve"
        assert fb["confidence"] == "low"
        assert fb["findings"] == []
        assert fb["error"] == "stage_failed"

    def test_fix_fallback(self):
        fb = build_fallback("fix")
        assert fb["fixed"] == []
        assert fb["not_fixed"] == []
        assert fb["pass"] is True

    def test_fix_plan_fallback(self):
        fb = build_fallback("fix-plan")
        assert fb["plan"] == []

    def test_fix_polish_fallback(self):
        fb = build_fallback("fix-polish")
        assert fb["clean"] is True

    def test_postconditions_fallback_is_empty(self):
        fb = build_fallback("postconditions")
        assert fb["postconditions"] == []
        assert fb["error"] == "stage_failed"

    def test_agent_stage_fallback(self):
        for stage in ["architecture", "security", "logic"]:
            fb = build_fallback(stage)
            assert fb["findings"] == []
            assert fb["pass"] is True
            assert fb["error"] == "stage_failed"

    def test_all_fallbacks_are_json_serializable(self):
        for stage in ALLOWED_STAGES:
            fb = build_fallback(stage)
            serialized = json.dumps(fb)
            assert isinstance(serialized, str)


class TestStageResult:
    def test_basic_creation(self):
        r = StageResult(stage="triage", success=True, data={"key": "val"})
        assert r.stage == "triage"
        assert r.success is True
        assert r.data == {"key": "val"}

    def test_fallback(self):
        r = StageResult.fallback("triage")
        assert r.success is True
        assert r.data["change_type"] == "mixed"

    def test_quota_exhausted(self):
        r = StageResult.quota_exhausted("architecture")
        assert r.success is False
        assert r.error == "API quota exhausted"

    def test_defaults(self):
        r = StageResult(stage="test", success=False)
        assert r.data == {}
        assert r.error is None
        assert r.is_rate_limited is False
        assert r.is_transient is False
        assert r.cancelled is False


class TestFixResult:
    def test_basic_creation(self):
        r = FixResult(success=True, pushed=True, summary="Fixed 3 findings")
        assert r.success is True
        assert r.pushed is True
        assert r.summary == "Fixed 3 findings"

    def test_defaults(self):
        r = FixResult(success=False)
        assert r.pushed is False
        assert r.reason == ""
        assert r.error is None


class TestFinding:
    def test_severities_ordering_is_ascending(self):
        assert FINDING_SEVERITIES == ("info", "warning", "error", "critical")

    def test_minimal_required_fields(self):
        f = Finding.from_dict({"severity": "error", "file": "a.py", "message": "boom"})
        assert f.severity == "error"
        assert f.file == "a.py"
        assert f.message == "boom"
        assert f.line is None
        assert f.title == ""
        assert f.locations == []

    def test_missing_required_raises(self):
        with pytest.raises(ValueError, match="message"):
            Finding.from_dict({"severity": "error", "file": "a.py"})

    def test_empty_required_raises(self):
        # Empty string for required field is not a valid finding.
        with pytest.raises(ValueError):
            Finding.from_dict({"severity": "error", "file": "", "message": "m"})

    def test_non_dict_raises(self):
        with pytest.raises(ValueError):
            Finding.from_dict("not a dict")  # type: ignore[arg-type]

    def test_title_is_not_aliased_to_message(self):
        # title and message are distinct fields; neither should
        # silently fall back to the other.
        f = Finding.from_dict({
            "severity": "info", "file": "x.py",
            "message": "the message", "title": "the title",
        })
        assert f.title == "the title"
        assert f.message == "the message"

    def test_unknown_keys_preserved_in_extra(self):
        f = Finding.from_dict({
            "severity": "warning", "file": "a.py", "message": "m",
            "ambiguity": "high", "fixability": "trivial",
        })
        assert f.extra == {"ambiguity": "high", "fixability": "trivial"}

    def test_stringy_line_coerced(self):
        f = Finding.from_dict({
            "severity": "error", "file": "a.py", "message": "m",
            "line": "42", "column": "7",
        })
        assert f.line == 42
        assert f.column == 7

    def test_invalid_line_becomes_none(self):
        f = Finding.from_dict({
            "severity": "error", "file": "a.py", "message": "m",
            "line": "not-a-number",
        })
        assert f.line is None

    def test_locations_parsed_when_present(self):
        f = Finding.from_dict({
            "severity": "warning", "file": "a.py", "message": "m",
            "locations": [
                {"file": "a.py", "line": 10},
                {"file": "b.py", "line": 20, "column": 4},
                {"no_file": "skip"},  # dropped
                "not a dict",  # dropped
            ],
        })
        assert len(f.locations) == 2
        assert f.locations[0] == FindingLocation(file="a.py", line=10)
        assert f.locations[1] == FindingLocation(file="b.py", line=20, column=4)

    def test_iter_locations_synthesises_for_pre_dedup(self):
        f = Finding.from_dict({
            "severity": "warning", "file": "a.py", "message": "m", "line": 5,
        })
        locs = f.iter_locations()
        assert len(locs) == 1
        assert locs[0].file == "a.py"
        assert locs[0].line == 5

    def test_primary_location_prefers_locations_array(self):
        f = Finding.from_dict({
            "severity": "warning", "file": "a.py", "message": "m", "line": 99,
            "locations": [{"file": "b.py", "line": 1}],
        })
        primary = f.primary_location()
        assert primary.file == "b.py"
        assert primary.line == 1

    def test_introduced_by_pr_coerced(self):
        f = Finding.from_dict({
            "severity": "error", "file": "a.py", "message": "m",
            "introduced_by_pr": True,
        })
        assert f.introduced_by_pr is True
        f2 = Finding.from_dict({
            "severity": "error", "file": "a.py", "message": "m",
            "introduced_by_pr": "true",  # non-bool — dropped
        })
        assert f2.introduced_by_pr is None

    def test_severity_rank(self):
        f_info = Finding.from_dict({"severity": "info", "file": "a", "message": "m"})
        f_err = Finding.from_dict({"severity": "error", "file": "a", "message": "m"})
        assert f_info.severity_rank() < f_err.severity_rank()

    def test_private_class_var_not_a_field(self):
        # _KNOWN_FIELDS is ClassVar — must not appear as an instance
        # attribute in repr or interfere with dataclass fields.
        f = Finding.from_dict({"severity": "info", "file": "a", "message": "m"})
        assert "_KNOWN_FIELDS" not in repr(f)

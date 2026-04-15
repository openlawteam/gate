"""Tests for gate.schemas module."""

import json

from gate.schemas import (
    AGENT_STAGES,
    ALLOWED_STAGES,
    STAGE_EFFORT,
    STAGE_SCHEMAS,
    STRUCTURED_STAGES,
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

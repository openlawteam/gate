"""JSON schemas for structured stages, fallback responses, and result types.

Ported from run-stage.js STAGE_SCHEMAS, STAGE_EFFORT, and buildFallback().
"""

from dataclasses import dataclass, field

STAGE_SCHEMAS: dict[str, dict] = {
    "triage": {
        "type": "object",
        "properties": {
            "change_type": {
                "type": "string",
                "enum": ["feature", "bugfix", "refactor", "config", "deps", "docs", "mixed"],
            },
            "risk_level": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            "summary": {"type": "string"},
            "files_by_category": {"type": "object"},
            "fast_track_eligible": {"type": "boolean"},
            "fast_track_reason": {"type": ["string", "null"]},
            "flags": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "change_type",
            "risk_level",
            "summary",
            "files_by_category",
            "fast_track_eligible",
        ],
    },
    "verdict": {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["approve", "approve_with_notes", "request_changes"],
            },
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "summary": {"type": "string"},
            "findings": {"type": "array"},
            "resolved_findings": {"type": "array"},
            "stats": {"type": "object"},
            "review_time_seconds": {"type": "number"},
        },
        "required": ["decision", "confidence", "summary", "findings", "stats"],
    },
    "fix-plan": {
        "type": "object",
        "properties": {
            "plan": {"type": "array"},
            "skipped": {"type": "array"},
            "execution_notes": {"type": "string"},
        },
        "required": ["plan"],
    },
    "fix-rereview": {
        "type": "object",
        "properties": {
            "pass": {"type": "boolean"},
            "issues": {"type": "array"},
        },
        "required": ["pass", "issues"],
    },
    "fix-polish": {
        "type": "object",
        "properties": {
            "corrections": {"type": "array"},
            "clean": {"type": "boolean"},
        },
        "required": ["corrections", "clean"],
    },
}

STAGE_EFFORT: dict[str, str] = {
    "fix": "max",
    "fix-senior": "max",
}

ALLOWED_STAGES = [
    "triage",
    "architecture",
    "security",
    "logic",
    "verdict",
    "fix",
    "fix-senior",
    "fix-rereview",
    "fix-build",
    "fix-prep",
    "fix-plan",
    "fix-plan-refine",
    "fix-polish",
]

AGENT_STAGES = {"architecture", "security", "logic", "fix-senior"}
STRUCTURED_STAGES = {"triage", "verdict", "fix-rereview", "fix-plan", "fix-polish"}


def build_fallback(stage: str) -> dict:
    """Return a safe fallback response when a stage fails.

    Each stage has a specific fallback that fails-open so the review can continue.
    Ported from buildFallback() in run-stage.js.
    """
    if stage == "triage":
        return {
            "change_type": "mixed",
            "risk_level": "medium",
            "summary": "Triage failed — defaulting to full review pipeline",
            "files_by_category": {},
            "stages_to_run": [2, 3, 4, 5, 6],
            "stage_config": {
                "architecture_depth": "standard",
                "security_depth": "standard",
                "logic_depth": "standard",
            },
            "fast_track_eligible": False,
            "fast_track_reason": None,
            "flags": ["triage_fallback"],
        }

    if stage == "verdict":
        return {
            "decision": "approve",
            "confidence": "low",
            "summary": "Verdict stage failed — auto-approving to avoid blocking.",
            "findings": [],
            "resolved_findings": [],
            "stats": {"stages_run": 0, "total_findings": 0},
            "review_time_seconds": 0,
            "error": "stage_failed",
        }

    if stage == "fix":
        return {
            "fixed": [],
            "not_fixed": [],
            "stats": {"total_findings": 0, "fixed": 0, "not_fixed": 0, "files_modified": 0},
            "pass": True,
            "error": "stage_failed",
        }

    if stage == "fix-prep":
        return {
            "context": [],
            "cross_file_dependencies": [],
            "error": "stage_failed",
        }

    if stage == "fix-plan":
        return {
            "plan": [],
            "skipped": [],
            "execution_notes": "",
            "error": "stage_failed",
        }

    if stage == "fix-polish":
        return {
            "corrections": [],
            "clean": True,
            "error": "stage_failed",
        }

    if stage == "fix-rereview":
        return {
            "pass": True,
            "issues": [],
            "summary": "Fallback: pass",
            "error": "stage_failed",
        }

    # Default fallback for agent stages (architecture, security, logic)
    return {
        "findings": [],
        "summary": f"{stage} stage failed — no findings (auto-pass to avoid blocking)",
        "pass": True,
        "error": "stage_failed",
    }


@dataclass
class StageResult:
    """Result from running a review stage (agent or structured)."""

    stage: str
    success: bool
    data: dict = field(default_factory=dict)
    error: str | None = None
    is_rate_limited: bool = False
    is_transient: bool = False
    cancelled: bool = False

    @classmethod
    def fallback(cls, stage: str) -> "StageResult":
        """Fail-open fallback: returns a safe default so the review can continue."""
        return cls(stage=stage, success=True, data=build_fallback(stage))

    @classmethod
    def quota_exhausted(cls, stage: str) -> "StageResult":
        return cls(
            stage=stage,
            success=False,
            data=build_fallback(stage),
            error="API quota exhausted",
        )


@dataclass
class FixResult:
    """Result from the fix pipeline."""

    success: bool
    pushed: bool = False
    reason: str = ""
    error: str | None = None
    summary: str = ""

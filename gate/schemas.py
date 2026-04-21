"""JSON schemas for structured stages, fallback responses, and result types.

Ported from run-stage.js STAGE_SCHEMAS, STAGE_EFFORT, and buildFallback().
"""

from dataclasses import dataclass, field
from typing import Literal


class WorkspaceVanishedError(Exception):
    """Raised when a worktree is deleted out from under us mid-operation.

    Signals the orchestrator to exit the review cleanly as ``cancelled``
    rather than log a traceback — the vanishing is almost always caused
    by a concurrent cancel/supersede of this review.
    """


@dataclass
class CommitResult:
    """Outcome of ``github.commit_and_push``.

    Splits the three cases previous callers could not distinguish:

    - ``pushed``  — a new commit was pushed, ``sha`` is the new HEAD
    - ``no_diff`` — ``git add`` found no changes, nothing committed
    - ``push_failed`` — commit or push raised ``CalledProcessError``,
      ``error`` contains the stderr tail
    """

    status: Literal["pushed", "no_diff", "push_failed"]
    sha: str = ""
    error: str = ""

    @property
    def success(self) -> bool:
        return self.status == "pushed"


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
            "change_intent": {
                "type": "object",
                "properties": {
                    "claimed_behavioral_delta": {"type": ["string", "null"]},
                    "claimed_bug_fixed": {"type": ["string", "null"]},
                    "claimed_tests_updated": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "claimed_no_behavior_change": {"type": "boolean"},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                },
            },
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
    "postconditions": {
        "type": "object",
        "properties": {
            "postconditions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "function_path": {"type": "string"},
                        "signature": {"type": "string"},
                        "prose": {"type": "string"},
                        "assertion_snippet": {"type": "string"},
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                        "rationale": {"type": "string"},
                    },
                    "required": ["function_path", "prose", "confidence"],
                },
            },
        },
        "required": ["postconditions"],
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
    "postconditions",
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
STRUCTURED_STAGES = {
    "triage",
    "postconditions",
    "verdict",
    "fix-rereview",
    "fix-plan",
    "fix-polish",
}


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

    if stage == "postconditions":
        # Fail-open: an empty list means Logic treats this PR as if
        # postconditions extraction was skipped. Safer than blocking
        # the review on a structured-stage error.
        return {
            "postconditions": [],
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
    """Result from the fix pipeline.

    The hopper-mode metrics (``pipeline_mode`` / ``sub_scope_*`` /
    ``wall_clock_seconds`` / ``runaway_guard_hit``) stay at their empty
    defaults for polish_legacy callers so nothing changes on that path.
    The orchestrator forwards any populated field to ``log_fix_result``.
    """

    success: bool
    pushed: bool = False
    reason: str = ""
    error: str | None = None
    summary: str = ""
    fixed_count: int = 0
    not_fixed_count: int = 0
    pipeline_mode: str = ""
    sub_scope_total: int = 0
    sub_scope_committed: int = 0
    sub_scope_reverted: int = 0
    sub_scope_empty: int = 0
    wall_clock_seconds: int = 0
    runaway_guard_hit: bool = False
    # Senior-authored commit message telemetry. ``"senior"`` when Gate
    # accepted the hopper-mode ``final_commit_message``, ``"synth"`` when
    # validation rejected it and Gate's template was used instead. Empty
    # string for polish_legacy / no-op / failed runs where no commit was
    # produced. ``commit_message_reject_reason`` is populated only when
    # source == "synth" AND a senior message was present but rejected
    # (empty when senior simply did not author one).
    commit_message_source: str = ""
    commit_message_reject_reason: str = ""

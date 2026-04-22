"""JSON schemas for structured stages, fallback responses, and result types.

Ported from run-stage.js STAGE_SCHEMAS, STAGE_EFFORT, and buildFallback().
"""

from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal


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


# ── Finding shape ────────────────────────────────────────────


# Canonical severity ordering — index in this tuple is the numeric rank
# (higher == worse). Used by dedup when merging locations into a single
# finding to pick the worst reported severity.
FINDING_SEVERITIES: tuple[str, ...] = ("info", "warning", "error", "critical")


def _severity_rank(sev: str) -> int:
    try:
        return FINDING_SEVERITIES.index(str(sev).lower())
    except ValueError:
        # Unknown severity — treat as warning so we neither over- nor
        # under-flag.  Agents occasionally emit ``medium``/``high``
        # which we don't formally model yet.
        return FINDING_SEVERITIES.index("warning")


@dataclass
class FindingLocation:
    """One (file, line, column) where a finding applies.

    PR A.2's dedup pass produces a ``locations`` array so a single
    style-rule violation that surfaces at multiple sites is rendered and
    dispatched as a single finding rather than three near-duplicates.
    Single-location findings emit ``locations=[self]`` so every consumer
    has a uniform shape.
    """

    file: str
    line: int | None = None
    column: int | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"file": self.file}
        if self.line is not None:
            out["line"] = self.line
        if self.column is not None:
            out["column"] = self.column
        return out


@dataclass
class Finding:
    """Canonical shape of a review finding after stage output + dedup.

    This is the source-of-truth contract every review stage is expected
    to produce (via ``Finding.from_dict``). Keeping the shape explicit
    lets humans reach for documented fields via ``gate inspect-pr``
    instead of guessing at reasonable-sounding names like ``title`` /
    ``description`` that happen not to be populated.

    Required: ``severity``, ``file``, ``message``.
    Optional: everything else. ``source_stage`` is stamped by the
    verdict aggregator (see prompts/verdict.md); ``finding_id`` is
    stamped by the orchestrator after verdict.
    """

    severity: str
    file: str
    message: str
    line: int | None = None
    column: int | None = None
    # Short human-readable label. Reserved for stages that choose to
    # emit one; review prompts today do NOT emit ``title``. Kept as a
    # separate field (not aliased to ``message``) so inspection tools
    # can render both when present without confusion.
    title: str = ""
    rule_source: str = ""
    suggestion: str = ""
    category: str = ""
    source_stage: str = ""
    introduced_by_pr: bool | None = None
    evidence_level: str = ""
    finding_id: str = ""
    # Populated by PR A.2 dedup. Single-location findings normalise to a
    # one-element list so consumers can iterate uniformly.
    locations: list[FindingLocation] = field(default_factory=list)
    # Unknown keys preserved so future stage additions (e.g. ``ambiguity``,
    # ``fixability``) round-trip without silently being dropped.
    extra: dict[str, Any] = field(default_factory=dict)

    # Whitelist of known fields so ``from_dict`` can route everything
    # else into ``extra``. Kept at class-scope rather than re-derived
    # per call. ``ClassVar`` so @dataclass skips it as an instance field.
    _KNOWN_FIELDS: ClassVar[tuple[str, ...]] = (
        "severity",
        "file",
        "message",
        "line",
        "column",
        "title",
        "rule_source",
        "suggestion",
        "category",
        "source_stage",
        "introduced_by_pr",
        "evidence_level",
        "finding_id",
        "locations",
    )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Finding":
        """Coerce a raw finding dict into the canonical shape.

        Raises ``ValueError`` when required fields are missing. Accepts
        string ``line``/``column`` that look numeric (agents occasionally
        emit them quoted) and coerces them.
        """
        if not isinstance(raw, dict):
            raise ValueError(f"Finding.from_dict: expected dict, got {type(raw).__name__}")

        def _req(key: str) -> str:
            v = raw.get(key)
            if v is None or (isinstance(v, str) and not v.strip()):
                raise ValueError(f"Finding missing required field: {key!r}")
            return str(v)

        def _opt_int(key: str) -> int | None:
            v = raw.get(key)
            if v is None or v == "":
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        def _opt_str(key: str, default: str = "") -> str:
            v = raw.get(key)
            if v is None:
                return default
            return str(v)

        severity = _req("severity")
        file = _req("file")
        message = _req("message")

        locations_raw = raw.get("locations")
        locations: list[FindingLocation] = []
        if isinstance(locations_raw, list):
            for loc in locations_raw:
                if not isinstance(loc, dict):
                    continue
                loc_file = loc.get("file")
                if not isinstance(loc_file, str) or not loc_file:
                    continue
                locations.append(
                    FindingLocation(
                        file=loc_file,
                        line=_opt_int_from(loc, "line"),
                        column=_opt_int_from(loc, "column"),
                    )
                )

        extra = {
            k: v for k, v in raw.items()
            if k not in cls._KNOWN_FIELDS
        }

        return cls(
            severity=severity,
            file=file,
            message=message,
            line=_opt_int("line"),
            column=_opt_int("column"),
            title=_opt_str("title"),
            rule_source=_opt_str("rule_source"),
            suggestion=_opt_str("suggestion"),
            category=_opt_str("category"),
            source_stage=_opt_str("source_stage"),
            introduced_by_pr=raw.get("introduced_by_pr")
            if isinstance(raw.get("introduced_by_pr"), bool)
            else None,
            evidence_level=_opt_str("evidence_level"),
            finding_id=_opt_str("finding_id"),
            locations=locations,
            extra=extra,
        )

    def primary_location(self) -> FindingLocation:
        """Canonical (file, line, column) for renderers that only render one site.

        Prefers ``locations[0]`` (populated by dedup), falling back to
        the top-level ``file``/``line``/``column`` so pre-dedup findings
        render correctly too.
        """
        if self.locations:
            return self.locations[0]
        return FindingLocation(file=self.file, line=self.line, column=self.column)

    def iter_locations(self) -> list[FindingLocation]:
        """Return at least one location so renderers can always iterate.

        Pre-dedup findings have no ``locations`` array; we synthesise a
        one-element list from top-level ``file``/``line``/``column`` so
        callers never need to branch on ``if self.locations``.
        """
        if self.locations:
            return list(self.locations)
        return [FindingLocation(file=self.file, line=self.line, column=self.column)]

    def severity_rank(self) -> int:
        return _severity_rank(self.severity)


def _opt_int_from(d: dict, key: str) -> int | None:
    v = d.get(key)
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

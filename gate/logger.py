"""Structured logging for Gate.

Ported from assemble-log-entry.js. Manages reviews.jsonl, live logs,
and sidecar metadata files.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from gate.config import logs_dir, repo_slug

_logger = logging.getLogger(__name__)


def reviews_jsonl() -> Path:
    """Return the path to the reviews.jsonl file."""
    return logs_dir() / "reviews.jsonl"


def live_dir() -> Path:
    """Return the directory for per-PR live logs."""
    return logs_dir() / "live"


def runners_dir() -> Path:
    """Return the directory for per-runner-process log files."""
    return logs_dir() / "runners"


_LOG_FORMATTER = logging.Formatter(
    "%(asctime)s.%(msecs)03d %(name)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def attach_gate_file_handler(
    log_path: Path, level: int = logging.INFO
) -> logging.FileHandler:
    """Attach a FileHandler to the top-level ``gate`` logger.

    Used by both the long-running server (``activity.log``) and the
    short-lived ``gate process`` runner subprocesses (per-stage runner log)
    so that ``logger.info(...)`` calls inside ``ReviewRunner`` survive after
    the tmux pane that hosted them exits.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path)
    handler.setLevel(level)
    handler.setFormatter(_LOG_FORMATTER)
    gate_logger = logging.getLogger("gate")
    gate_logger.setLevel(logging.DEBUG)
    gate_logger.addHandler(handler)
    return handler


def log_review(
    pr_number: int,
    verdict: dict,
    build: dict | None,
    elapsed_s: int,
    quota: dict | None = None,
    run_id: str = "",
    triage: dict | None = None,
    repo: str = "",
    is_post_fix_rereview: bool = False,
    prior_findings_count: int | None = None,
    new_findings_count: int | None = None,
    persisting_findings_count: int | None = None,
    resolved_since_prior_count: int | None = None,
) -> None:
    """Append a review entry to reviews.jsonl.

    Ported from assemble-log-entry.js buildEntry().

    Optional re-review ROI fields (Group 2D) are serialised only when
    provided — callers on the normal review path still pass no extra
    args so the JSONL schema stays backward-compatible.
    """
    findings = verdict.get("findings", [])
    stats = verdict.get("stats", {})

    findings_by_stage: dict[str, bool] = {}
    for f in findings:
        if f.get("introduced_by_pr") is not False and f.get("source_stage"):
            findings_by_stage[f["source_stage"]] = True

    triage = triage or {}
    resolved = verdict.get("resolved_findings", [])

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "repo": repo,
        "pr": pr_number,
        "mode": "enforcement",
        "run_id": run_id,
        "pipeline": "multi-stage",
        "quota_five_hour_pct": (quota or {}).get("five_hour_pct", -1),
        "quota_seven_day_pct": (quota or {}).get("seven_day_pct", -1),
        "decision": verdict.get("decision", "error"),
        "confidence": verdict.get("confidence", "unknown"),
        "risk_level": triage.get("risk_level", "unknown"),
        "change_type": triage.get("change_type", "unknown"),
        "review_time_seconds": elapsed_s,
        "findings": stats.get("total_findings", len(findings)),
        "findings_by_severity": {
            "critical": stats.get("critical", 0),
            "error": stats.get("errors", 0),
            "warning": stats.get("warnings", 0),
            "info": stats.get("info", 0),
        },
        "finding_categories": sorted(findings_by_stage.keys()),
        "resolved_count": len(resolved),
        "build_pass": (build or {}).get("overall_pass", True),
        "fast_track_eligible": triage.get("fast_track_eligible", False),
        "stages_run": stats.get("stages_run", 0),
    }

    # Re-review ROI fields (only serialised when populated).
    if is_post_fix_rereview:
        entry["is_post_fix_rereview"] = True
    for key, value in (
        ("prior_findings_count", prior_findings_count),
        ("new_findings_count", new_findings_count),
        ("persisting_findings_count", persisting_findings_count),
        ("resolved_since_prior_count", resolved_since_prior_count),
    ):
        if value is not None:
            entry[key] = value

    logs = logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / "reviews.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")

    _logger.info(
        f"Logged review PR #{pr_number}: {entry['decision']} "
        f"({entry['findings']} findings, {elapsed_s}s)"
    )


def log_fix_result(
    pr_number: int,
    fix_success: bool,
    fix_summary: str,
    original_decision: str,
    repo: str = "",
    fix_elapsed_seconds: int = 0,
    status: str | None = None,
    pipeline_mode: str | None = None,
    sub_scope_total: int | None = None,
    sub_scope_committed: int | None = None,
    sub_scope_reverted: int | None = None,
    sub_scope_empty: int | None = None,
    wall_clock_seconds: int | None = None,
    runaway_guard_hit: bool | None = None,
    fixed_count: int | None = None,
    not_fixed_count: int | None = None,
) -> None:
    """Append a fix result entry to reviews.jsonl.

    ``status`` may be one of ``"succeeded"``, ``"failed"``, or ``"no_op"``.
    When not provided it is derived from ``fix_success`` (legacy callers
    that have not been updated yet). ``no_op`` lets log consumers
    distinguish "the fix pipeline intentionally did nothing" (e.g. a
    graceful no-op on an approve_with_notes PR with no mechanical work)
    from "the fix pipeline landed commits" and from "the fix pipeline
    failed" (audit A10).

    Hopper-mode kwargs (``pipeline_mode``, ``sub_scope_*``,
    ``wall_clock_seconds``, ``runaway_guard_hit``, ``fixed_count``,
    ``not_fixed_count``) are serialised only when populated so legacy
    polish_legacy entries remain byte-identical.
    """
    if status is None:
        status = "succeeded" if fix_success else "failed"
    if status == "no_op":
        decision = "fix_no_op"
    elif status == "succeeded":
        decision = "fix_succeeded"
    else:
        decision = "fix_failed"
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "repo": repo,
        "pr": pr_number,
        "decision": decision,
        "original_decision": original_decision,
        "fix_summary": fix_summary,
        "is_fix_followup": True,
        "review_time_seconds": fix_elapsed_seconds,
    }
    for key, value in (
        ("pipeline_mode", pipeline_mode),
        ("sub_scope_total", sub_scope_total),
        ("sub_scope_committed", sub_scope_committed),
        ("sub_scope_reverted", sub_scope_reverted),
        ("sub_scope_empty", sub_scope_empty),
        ("wall_clock_seconds", wall_clock_seconds),
        ("runaway_guard_hit", runaway_guard_hit),
        ("fixed_count", fixed_count),
        ("not_fixed_count", not_fixed_count),
    ):
        if value is not None:
            entry[key] = value
    logs = logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / "reviews.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")

    _logger.info(f"Logged fix result PR #{pr_number}: {status}")


def write_live_log(pr_number: int, message: str, prefix: str = "", repo: str = "") -> None:
    """Write to the live log file for a PR (real-time monitoring)."""
    base = live_dir()
    if repo:
        log_dir = base / repo_slug(repo)
    else:
        log_dir = base
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"pr{pr_number}.log"
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}]"
    if prefix:
        line += f" [{prefix}]"
    line += f" {message}\n"
    with open(log_file, "a") as f:
        f.write(line)


def write_sidecar_meta(workspace: Path, stage: str, meta: dict) -> None:
    """Write {stage}_meta.json alongside stage output.

    Ported from writeSidecar() in run-stage.js. Sidecar metadata is
    diagnostic-only: if the write fails (permissions, full disk, missing
    parent dir) we log and swallow rather than propagating, matching the
    defensive posture of other non-critical writes in this module.
    """
    meta_path = workspace / f"{stage}_meta.json"
    try:
        meta_path.write_text(json.dumps(meta, indent=2))
    except OSError as e:
        _logger.warning(f"write_sidecar_meta failed for {meta_path}: {e}")


def read_recent_decisions(count: int = 3) -> list[str]:
    """Read the last N decisions from reviews.jsonl for circuit breaker.

    Returns list of decision strings, most recent first.
    """
    path = reviews_jsonl()
    if not path.exists():
        return []

    lines = path.read_text().strip().split("\n")
    decisions = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            decisions.append(entry.get("decision", "unknown"))
        except json.JSONDecodeError:
            continue
        if len(decisions) >= count:
            break

    return decisions

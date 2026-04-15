"""Structured logging for Gate.

Ported from assemble-log-entry.js. Manages reviews.jsonl, live logs,
and sidecar metadata files.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from gate.config import gate_dir, repo_slug

_logger = logging.getLogger(__name__)

LOGS_DIR = gate_dir() / "logs"
REVIEWS_JSONL = LOGS_DIR / "reviews.jsonl"
LIVE_DIR = LOGS_DIR / "live"


def log_review(
    pr_number: int,
    verdict: dict,
    build: dict | None,
    elapsed_s: int,
    quota: dict | None = None,
    run_id: str = "",
    triage: dict | None = None,
    repo: str = "",
) -> None:
    """Append a review entry to reviews.jsonl.

    Ported from assemble-log-entry.js buildEntry().
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

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(REVIEWS_JSONL, "a") as f:
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
) -> None:
    """Append a fix result entry to reviews.jsonl."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "repo": repo,
        "pr": pr_number,
        "decision": "fix_succeeded" if fix_success else "fix_failed",
        "original_decision": original_decision,
        "fix_summary": fix_summary,
        "is_fix_followup": True,
    }
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(REVIEWS_JSONL, "a") as f:
        f.write(json.dumps(entry) + "\n")

    _logger.info(
        f"Logged fix result PR #{pr_number}: "
        f"{'succeeded' if fix_success else 'failed'}"
    )


def write_live_log(pr_number: int, message: str, prefix: str = "", repo: str = "") -> None:
    """Write to ~/gate/logs/live/pr<N>.log for real-time monitoring."""
    if repo:
        log_dir = LIVE_DIR / repo_slug(repo)
    else:
        log_dir = LIVE_DIR
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

    Ported from writeSidecar() in run-stage.js.
    """
    meta_path = workspace / f"{stage}_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2))


def read_recent_decisions(count: int = 3) -> list[str]:
    """Read the last N decisions from reviews.jsonl for circuit breaker.

    Returns list of decision strings, most recent first.
    """
    if not REVIEWS_JSONL.exists():
        return []

    lines = REVIEWS_JSONL.read_text().strip().split("\n")
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

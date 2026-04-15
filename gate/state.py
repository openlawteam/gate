"""Per-PR state persistence.

Ported from persist-review-state.js + fetch-prior-findings.js.
Manages review count, fix attempts, prior findings, and per-PR state directories.
"""

import fcntl
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

from gate.config import gate_dir, repo_slug

logger = logging.getLogger(__name__)

STATE_DIR = gate_dir() / "state"


def get_pr_state_dir(pr_number: int, repo: str = "") -> Path:
    """Get (and create) the per-PR state directory."""
    if repo:
        d = STATE_DIR / repo_slug(repo) / f"pr{pr_number}"
    else:
        d = STATE_DIR / f"pr{pr_number}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_prior_review(pr_number: int, workspace: Path, repo: str = "") -> dict:
    """Load prior review findings and write prior-review.json to workspace.

    Ported from fetch-prior-findings.js. Reads saved verdict, SHA, counters
    from the state directory and writes a summary for downstream stages.
    """
    state_dir = get_pr_state_dir(pr_number, repo)
    verdict_path = state_dir / "verdict.json"
    sha_path = state_dir / "last_sha.txt"
    output_path = workspace / "prior-review.json"

    verdict = None
    prior_sha = ""
    review_count = 0
    fix_attempts = 0

    try:
        verdict = json.loads(verdict_path.read_text())
    except (OSError, json.JSONDecodeError):
        pass

    try:
        prior_sha = sha_path.read_text().strip()
    except OSError:
        pass

    try:
        review_count = int((state_dir / "review_count.txt").read_text().strip()) or 0
    except (OSError, ValueError):
        pass

    try:
        fix_attempts = int((state_dir / "fix_attempts.txt").read_text().strip()) or 0
    except (OSError, ValueError):
        pass

    if not verdict:
        result = {"has_prior": False, "review_count": review_count, "fix_attempts": fix_attempts}
        output_path.write_text(json.dumps(result, indent=2))
        return result

    prior_findings = [
        {
            "source_stage": f.get("source_stage", "unknown"),
            "severity": f.get("severity"),
            "file": f.get("file"),
            "line": f.get("line"),
            "message": f.get("message"),
            "evidence_level": f.get("evidence_level"),
            "introduced_by_pr": f.get("introduced_by_pr"),
        }
        for f in (verdict.get("findings") or [])
    ]

    result = {
        "has_prior": True,
        "prior_decision": verdict.get("decision", "unknown"),
        "prior_confidence": verdict.get("confidence", "unknown"),
        "prior_sha": prior_sha,
        "prior_findings": prior_findings,
        "prior_stats": verdict.get("stats"),
        "prior_resolved": verdict.get("resolved_findings", []),
        "review_count": review_count,
        "fix_attempts": fix_attempts,
    }

    output_path.write_text(json.dumps(result, indent=2))
    return result


def persist_review_state(
    pr_number: int,
    sha: str,
    workspace: Path,
    decision: str | None = None,
    clone_path: str | Path | None = None,
    repo: str = "",
) -> None:
    """Save review state after completion.

    Ported from persist-review-state.js. Copies verdict/build/triage
    to state dir and manages review_count with force-push detection.
    """
    state_dir = get_pr_state_dir(pr_number, repo)
    verdict_path = workspace / "verdict.json"

    if not verdict_path.exists():
        logger.warning(f"No verdict.json in workspace for PR #{pr_number}")
        return

    verdict = json.loads(verdict_path.read_text())

    # Copy verdict
    (state_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))

    # Copy ancillary files if they exist
    ancillary = (
        "build.json", "triage.json", "architecture.json",
        "security.json", "logic.json",
    )
    for filename in ancillary:
        src = workspace / filename
        if src.exists():
            try:
                (state_dir / filename).write_text(src.read_text())
            except OSError:
                pass

    # SHA tracking and force-push detection
    last_sha = ""
    sha_path = state_dir / "last_sha.txt"
    try:
        last_sha = sha_path.read_text().strip()
    except OSError:
        pass

    sha_path.write_text(sha)

    count_path = state_dir / "review_count.txt"
    fix_count_path = state_dir / "fix_attempts.txt"

    if last_sha and sha and sha != last_sha:
        is_ancestor = False
        merge_base_cwd = str(clone_path) if clone_path else None
        try:
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", last_sha, sha],
                capture_output=True, timeout=5,
                cwd=merge_base_cwd,
            ).check_returncode()
            is_ancestor = True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass

        if not is_ancestor:
            # Force push — reset review count and soft fix attempts
            count_path.write_text("1")
            fix_count_path.write_text("0")
            logger.info(f"PR #{pr_number}: force push detected, counters reset")
            return

    # Increment review count
    count = 0
    try:
        count = int(count_path.read_text().strip()) or 0
    except (OSError, ValueError):
        pass
    count_path.write_text(str(count + 1))

    logger.info(
        f"PR #{pr_number}: state persisted, review #{count + 1}, "
        f"decision={verdict.get('decision', '?')}"
    )


def cleanup_pr_state(pr_number: int, repo: str = "") -> None:
    """Clean up state for a closed PR."""
    if repo:
        state_dir = STATE_DIR / repo_slug(repo) / f"pr{pr_number}"
    else:
        state_dir = STATE_DIR / f"pr{pr_number}"
    if state_dir.exists():
        shutil.rmtree(state_dir, ignore_errors=True)
        logger.info(f"Cleaned up state for PR #{pr_number}")


# ── Fix Attempt Tracking ─────────────────────────────────────


def get_fix_attempts(pr_number: int, repo: str = "") -> dict:
    """Read fix attempt counters."""
    state_dir = get_pr_state_dir(pr_number, repo)
    soft = 0
    total = 0
    last_fix_at = 0.0

    try:
        soft = int((state_dir / "fix_attempts.txt").read_text().strip()) or 0
    except (OSError, ValueError):
        pass
    try:
        total = int((state_dir / "fix_attempts_total.txt").read_text().strip()) or 0
    except (OSError, ValueError):
        pass
    try:
        last_fix_at = float((state_dir / "last_fix_at.txt").read_text().strip())
    except (OSError, ValueError):
        pass

    return {"soft": soft, "total": total, "last_fix_at": last_fix_at}


def record_fix_attempt(pr_number: int, repo: str = "") -> None:
    """Increment fix attempt counters and update timestamp."""
    state_dir = get_pr_state_dir(pr_number, repo)
    lock_path = state_dir / ".lock"
    lock_path.touch(exist_ok=True)

    with open(lock_path) as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        soft = 0
        try:
            soft = int((state_dir / "fix_attempts.txt").read_text().strip()) or 0
        except (OSError, ValueError):
            pass
        (state_dir / "fix_attempts.txt").write_text(str(soft + 1))

        total = 0
        try:
            total = int((state_dir / "fix_attempts_total.txt").read_text().strip()) or 0
        except (OSError, ValueError):
            pass
        (state_dir / "fix_attempts_total.txt").write_text(str(total + 1))

        (state_dir / "last_fix_at.txt").write_text(str(time.time()))


def check_fix_limits(pr_number: int, config: dict, repo: str = "") -> tuple[bool, str]:
    """Check soft (3), lifetime (6), cooldown (600s) limits.

    Returns:
        (allowed, reason) — allowed=True if fix can proceed.
    """
    limits = config.get("limits", {})
    attempts = get_fix_attempts(pr_number, repo)

    max_soft = limits.get("max_fix_attempts_soft", 3)
    max_total = limits.get("max_fix_attempts_total", 6)
    cooldown = limits.get("fix_cooldown_s", 600)

    if attempts["total"] >= max_total:
        return False, f"Lifetime fix limit reached ({max_total})"

    if attempts["soft"] >= max_soft:
        return False, f"Soft fix limit reached ({max_soft} this review cycle)"

    if attempts["last_fix_at"] > 0:
        elapsed = time.time() - attempts["last_fix_at"]
        if elapsed < cooldown:
            remaining = int(cooldown - elapsed)
            return False, f"Fix cooldown active ({remaining}s remaining)"

    return True, ""

"""Per-PR state persistence.

Ported from persist-review-state.js + fetch-prior-findings.js.
Manages review count, fix attempts, prior findings, and per-PR state directories.

Concurrency model: per-PR state is only written from the orchestrator, which
runs inside ``ReviewQueue``'s ``ThreadPoolExecutor``. The queue serialises
work per PR, so a single PR's counters are never written concurrently. We
therefore use atomic tmp+replace writes (safe against crashes) without
file-locking coordination.
"""

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

from gate.config import repo_slug, state_dir
from gate.io import atomic_write

logger = logging.getLogger(__name__)


def get_pr_state_dir(pr_number: int, repo: str = "") -> Path:
    """Get (and create) the per-PR state directory."""
    root = state_dir()
    if repo:
        d = root / repo_slug(repo) / f"pr{pr_number}"
    else:
        d = root / f"pr{pr_number}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_prior_review(pr_number: int, workspace: Path, repo: str = "") -> dict:
    """Load prior review findings and write prior-review.json to workspace.

    Ported from fetch-prior-findings.js. Reads saved verdict, SHA, counters
    from the state directory and writes a summary for downstream stages.
    """
    pr_state_dir = get_pr_state_dir(pr_number, repo)
    verdict_path = pr_state_dir / "verdict.json"
    sha_path = pr_state_dir / "last_sha.txt"
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
        review_count = int((pr_state_dir / "review_count.txt").read_text().strip()) or 0
    except (OSError, ValueError):
        pass

    try:
        fix_attempts = int((pr_state_dir / "fix_attempts.txt").read_text().strip()) or 0
    except (OSError, ValueError):
        pass

    if not verdict:
        result = {"has_prior": False, "review_count": review_count, "fix_attempts": fix_attempts}
        atomic_write(output_path, json.dumps(result, indent=2))
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

    atomic_write(output_path, json.dumps(result, indent=2))
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
    pr_state_dir = get_pr_state_dir(pr_number, repo)
    verdict_path = workspace / "verdict.json"

    if not verdict_path.exists():
        logger.warning(f"No verdict.json in workspace for PR #{pr_number}")
        return

    verdict = json.loads(verdict_path.read_text())

    atomic_write(pr_state_dir / "verdict.json", json.dumps(verdict, indent=2))

    ancillary = (
        "build.json", "triage.json", "architecture.json",
        "security.json", "logic.json",
    )
    for filename in ancillary:
        src = workspace / filename
        if src.exists():
            try:
                atomic_write(pr_state_dir / filename, src.read_text())
            except OSError:
                pass

    last_sha = ""
    sha_path = pr_state_dir / "last_sha.txt"
    try:
        last_sha = sha_path.read_text().strip()
    except OSError:
        pass

    atomic_write(sha_path, sha)

    count_path = pr_state_dir / "review_count.txt"
    fix_count_path = pr_state_dir / "fix_attempts.txt"

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
        except (subprocess.SubprocessError, OSError):
            # SubprocessError: CalledProcessError + TimeoutExpired
            # OSError: git missing from PATH (FileNotFoundError) or clone_path
            # pruned (NotADirectoryError). Without this widening those escape
            # persist_review_state after the sha_path write and counter logic
            # already partially executed, leaving state inconsistent.
            pass

        if not is_ancestor:
            # Force push — reset review count and soft fix attempts
            atomic_write(count_path, "1")
            atomic_write(fix_count_path, "0")
            logger.info(f"PR #{pr_number}: force push detected, counters reset")
            return

    count = 0
    try:
        count = int(count_path.read_text().strip()) or 0
    except (OSError, ValueError):
        pass
    atomic_write(count_path, str(count + 1))

    logger.info(
        f"PR #{pr_number}: state persisted, review #{count + 1}, "
        f"decision={verdict.get('decision', '?')}"
    )


def cleanup_pr_state(pr_number: int, repo: str = "") -> None:
    """Clean up state for a closed PR."""
    root = state_dir()
    if repo:
        pr_dir = root / repo_slug(repo) / f"pr{pr_number}"
    else:
        pr_dir = root / f"pr{pr_number}"
    if pr_dir.exists():
        shutil.rmtree(pr_dir, ignore_errors=True)
        logger.info(f"Cleaned up state for PR #{pr_number}")


# ── Fix Attempt Tracking ─────────────────────────────────────


def get_fix_attempts(pr_number: int, repo: str = "") -> dict:
    """Read fix attempt counters."""
    pr_state_dir = get_pr_state_dir(pr_number, repo)
    soft = 0
    total = 0
    last_fix_at = 0.0

    try:
        soft = int((pr_state_dir / "fix_attempts.txt").read_text().strip()) or 0
    except (OSError, ValueError):
        pass
    try:
        total = int((pr_state_dir / "fix_attempts_total.txt").read_text().strip()) or 0
    except (OSError, ValueError):
        pass
    try:
        last_fix_at = float((pr_state_dir / "last_fix_at.txt").read_text().strip())
    except (OSError, ValueError):
        pass

    return {"soft": soft, "total": total, "last_fix_at": last_fix_at}


def record_fix_attempt(
    pr_number: int,
    repo: str = "",
    no_op: bool = False,
) -> None:
    """Increment fix attempt counters and update timestamp.

    When ``no_op`` is true (graceful no-op on ``approve_with_notes`` — see
    audit A9), the soft counter is **reset** to 0 and nothing is added
    to the total. The rationale: a no-op did not consume a real attempt,
    and otherwise a PR that sees N pushes with zero mechanical work
    would silently exhaust its limit and block future real fixes.

    Safe without file locks because the ``ReviewQueue`` serialises work
    per PR; concurrent incrementers for the same PR are not possible.
    """
    pr_state_dir = get_pr_state_dir(pr_number, repo)

    if no_op:
        # Clear the soft counter so a subsequent real finding still gets
        # a full attempt budget. Leave the total alone — it's a lifetime
        # counter we don't want no-ops resetting.
        atomic_write(pr_state_dir / "fix_attempts.txt", "0")
        atomic_write(pr_state_dir / "last_fix_no_op_at.txt", str(time.time()))
        return

    soft = 0
    try:
        soft = int((pr_state_dir / "fix_attempts.txt").read_text().strip()) or 0
    except (OSError, ValueError):
        pass
    atomic_write(pr_state_dir / "fix_attempts.txt", str(soft + 1))

    total = 0
    try:
        total = int((pr_state_dir / "fix_attempts_total.txt").read_text().strip()) or 0
    except (OSError, ValueError):
        pass
    atomic_write(pr_state_dir / "fix_attempts_total.txt", str(total + 1))

    atomic_write(pr_state_dir / "last_fix_at.txt", str(time.time()))


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

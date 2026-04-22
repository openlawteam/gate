"""Per-PR state persistence.

Ported from persist-review-state.js + fetch-prior-findings.js.
Manages review count, fix attempts, prior findings, and per-PR state directories.

Concurrency model: per-PR state is only written from the orchestrator, which
runs inside ``ReviewQueue``'s ``ThreadPoolExecutor``. The queue serialises
work per PR, so a single PR's counters are never written concurrently. We
therefore use atomic tmp+replace writes (safe against crashes) without
file-locking coordination.

Archive layout (PR B.1):

``persist_review_state`` keeps writing the current-state pointers
(``verdict.json``, ``build.json``, …) for backwards compatibility, and
additionally snapshots each review into ``reviews/<ISO>-<short_sha>-<suffix>/``
where ``suffix`` is ``pre-fix`` or ``post-fix``. This gives forensic
immutability — post-fix re-reviews no longer clobber the pre-fix
evidence. Consumers that need history iterate ``pr<N>/reviews/``;
consumers that just want "the latest" keep reading the pointers.
"""

import datetime as _dt
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from gate.config import repo_slug, state_dir
from gate.finding_id import compute_finding_id
from gate.io import atomic_write

logger = logging.getLogger(__name__)

# Stage files that get pointer-written + archived alongside verdict.json.
_ARCHIVED_STAGE_FILES: tuple[str, ...] = (
    "build.json",
    "triage.json",
    "postconditions.json",
    "architecture.json",
    "security.json",
    "logic.json",
)


def get_pr_state_dir(pr_number: int, repo: str = "", create: bool = True) -> Path:
    """Get the per-PR state directory.

    When ``create`` is True (default) the directory (and any missing parents)
    is created. Pass ``create=False`` for read-only callers such as
    ``gate inspect-pr`` that must not leave a ghost dir behind for PRs Gate
    never processed.
    """
    root = state_dir()
    if repo:
        d = root / repo_slug(repo) / f"pr{pr_number}"
    else:
        d = root / f"pr{pr_number}"
    if create:
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
            # Stable id (Group 2D ROI diff). Backfilled on-the-fly for
            # legacy verdicts written before compute_finding_id existed.
            "finding_id": f.get("finding_id") or compute_finding_id(f),
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


def _archive_dir_name(sha: str, is_post_fix_rereview: bool) -> str:
    """Compose the per-review archive folder name.

    ISO-8601 UTC timestamp (compact, sortable lexicographically) + a
    short sha prefix + a pre-fix/post-fix suffix. Lexical sort over
    the review archive is therefore chronological, which is what
    ``gate inspect-pr --history`` and the retro-scan walk rely on.
    """
    ts = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    short_sha = (sha or "0" * 8)[:8] or "unknown"
    suffix = "post-fix" if is_post_fix_rereview else "pre-fix"
    return f"{ts}-{short_sha}-{suffix}"


def _write_stage_log(
    archive_path: Path,
    workspace: Path,
    verdict: dict[str, Any],
    sha: str,
    decision: str | None,
    is_post_fix_rereview: bool,
) -> None:
    """Write a stage_log.json summary for the archived review.

    Captures which stages ran, their summary verdicts, elapsed time
    (if the stage file carries one), and whether the stage was cached
    / skipped. This gives ``gate audit retro-scan`` enough metadata to
    detect silent approvals without re-reading every stage blob.
    """
    stages: dict[str, Any] = {}
    for filename in _ARCHIVED_STAGE_FILES + ("verdict.json",):
        src = workspace / filename
        if not src.exists():
            continue
        try:
            stage_doc = json.loads(src.read_text())
        except (OSError, json.JSONDecodeError):
            stages[filename] = {"error": "unreadable"}
            continue
        if not isinstance(stage_doc, dict):
            stages[filename] = {"type": type(stage_doc).__name__}
            continue
        stages[filename] = {
            "decision": stage_doc.get("decision"),
            "verdict": stage_doc.get("verdict"),
            "findings_count": len(stage_doc.get("findings") or []) or None,
            "elapsed_s": stage_doc.get("elapsed_s") or stage_doc.get("elapsed"),
            "cached": stage_doc.get("cached"),
            "skipped": stage_doc.get("skipped"),
            "pass": stage_doc.get("pass"),
            "exit_code": stage_doc.get("exit_code"),
        }
    summary = {
        "written_at": _dt.datetime.now(tz=_dt.UTC).isoformat(),
        "sha": sha,
        "decision": decision or verdict.get("decision"),
        "is_post_fix_rereview": is_post_fix_rereview,
        "findings_count": len(verdict.get("findings") or []),
        "stages": stages,
    }
    atomic_write(archive_path / "stage_log.json", json.dumps(summary, indent=2))


def persist_review_state(
    pr_number: int,
    sha: str,
    workspace: Path,
    decision: str | None = None,
    clone_path: str | Path | None = None,
    repo: str = "",
    is_post_fix_rereview: bool = False,
) -> None:
    """Save review state after completion.

    Ported from persist-review-state.js. Writes current-state pointers
    (``verdict.json``, ``build.json``, …) and — new in PR B.1 — also
    archives the full review into
    ``pr<N>/reviews/<ISO>-<short_sha>-<suffix>/`` so post-fix
    re-reviews no longer clobber the pre-fix evidence. Manages
    ``review_count`` with force-push detection.
    """
    pr_state_dir = get_pr_state_dir(pr_number, repo)
    verdict_path = workspace / "verdict.json"

    if not verdict_path.exists():
        logger.warning(f"No verdict.json in workspace for PR #{pr_number}")
        return

    verdict = json.loads(verdict_path.read_text())

    atomic_write(pr_state_dir / "verdict.json", json.dumps(verdict, indent=2))

    for filename in _ARCHIVED_STAGE_FILES:
        src = workspace / filename
        if src.exists():
            try:
                atomic_write(pr_state_dir / filename, src.read_text())
            except OSError:
                pass

    # Per-review archive (PR B.1). Best-effort — a failure here must
    # never block the current-state pointer writes above, because those
    # pointers are what live consumers (fixer, github renderer) read.
    try:
        reviews_root = pr_state_dir / "reviews"
        reviews_root.mkdir(parents=True, exist_ok=True)
        archive_name = _archive_dir_name(sha, is_post_fix_rereview)
        archive_path = reviews_root / archive_name
        archive_path.mkdir(parents=True, exist_ok=True)
        atomic_write(archive_path / "verdict.json", json.dumps(verdict, indent=2))
        for filename in _ARCHIVED_STAGE_FILES:
            src = workspace / filename
            if src.exists():
                try:
                    atomic_write(archive_path / filename, src.read_text())
                except OSError:
                    pass
        _write_stage_log(
            archive_path, workspace, verdict, sha, decision, is_post_fix_rereview,
        )
    except OSError as e:
        logger.warning(
            f"PR #{pr_number}: per-review archive failed ({e}); "
            "current-state pointers still written."
        )

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


# ── Review archive helpers (PR B.1) ──────────────────────────


def list_review_archives(pr_number: int, repo: str = "") -> list[dict[str, Any]]:
    """Return archived-review metadata for a PR, oldest first.

    Each entry carries ``path`` (the archive dir), ``name`` (its basename
    — an ISO-ish sortable string so simple lexical ordering matches
    chronology), ``timestamp`` / ``sha`` / ``suffix`` parsed out of the
    name, and — when readable — the ``stage_log.json`` summary.
    """
    pr_state_dir = get_pr_state_dir(pr_number, repo, create=False)
    reviews_root = pr_state_dir / "reviews"
    if not reviews_root.exists():
        return []
    archives: list[dict[str, Any]] = []
    for child in sorted(reviews_root.iterdir()):
        if not child.is_dir():
            continue
        parts = child.name.split("-", 2)
        timestamp = parts[0] if parts else ""
        sha = parts[1] if len(parts) > 1 else ""
        suffix = parts[2] if len(parts) > 2 else ""
        summary: dict[str, Any] | None = None
        stage_log = child / "stage_log.json"
        if stage_log.exists():
            try:
                summary = json.loads(stage_log.read_text())
            except (OSError, json.JSONDecodeError):
                summary = None
        archives.append({
            "name": child.name,
            "path": child,
            "timestamp": timestamp,
            "sha": sha,
            "suffix": suffix,
            "summary": summary,
        })
    return archives


def prune_review_archives(
    older_than_seconds: float,
    pr_number: int | None = None,
    repo: str = "",
) -> int:
    """Delete per-review archive directories older than ``older_than_seconds``.

    When ``pr_number`` is given (and ``repo`` if applicable) only that
    PR's archives are scanned; otherwise the entire state tree is
    walked under the configured state root. Returns the number of
    archive directories removed. Used by ``gate prune --reviews``.
    """
    cutoff = time.time() - older_than_seconds
    removed = 0
    targets: list[Path] = []
    root = state_dir()
    if pr_number is not None:
        targets.append(get_pr_state_dir(pr_number, repo, create=False) / "reviews")
    else:
        for pr_dir in root.rglob("pr*/reviews"):
            targets.append(pr_dir)

    for reviews_root in targets:
        if not reviews_root.exists():
            continue
        for child in reviews_root.iterdir():
            if not child.is_dir():
                continue
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                try:
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
                except OSError:
                    pass
    return removed


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

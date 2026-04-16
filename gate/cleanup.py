"""Log rotation, worktree pruning, state cleanup, daily digest.

Ported from cleanup-old-logs.sh and daily-digest.sh.
Supports multi-repo cleanup via get_all_repos().
"""

import gzip
import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from gate import notify
from gate.config import get_all_repos, get_repo_config, load_config, repo_slug, state_dir
from gate.io import atomic_write
from gate.logger import logs_dir, reviews_jsonl
from gate.workspace import prune_worktrees

logger = logging.getLogger(__name__)


def cleanup_logs(max_log_size_mb: int = 10, max_jsonl_lines: int = 5000) -> None:
    """Rotate gate logs and trim JSONL files.

    - Rotates activity.log and gate.log when they exceed max_log_size_mb
    - Trims reviews.jsonl to last max_jsonl_lines entries
    - Compresses old rotated logs
    """
    logs = logs_dir()
    logs.mkdir(parents=True, exist_ok=True)

    for log_name in ("activity.log", "gate.log"):
        log_path = logs / log_name
        if not log_path.exists():
            continue
        size_mb = log_path.stat().st_size / (1024 * 1024)
        if size_mb > max_log_size_mb:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            rotated = logs / f"{log_name}.{ts}"
            log_path.rename(rotated)
            _compress_file(rotated)
            logger.info(f"Rotated {log_name} ({size_mb:.1f}MB)")

    reviews_path = reviews_jsonl()
    if reviews_path.exists():
        _trim_jsonl(reviews_path, max_jsonl_lines)

    disputes = logs / "disputes.jsonl"
    if disputes.exists():
        _trim_jsonl(disputes, max_jsonl_lines)

    live = logs / "live"
    if live.exists():
        cutoff = time.time() - 7 * 86400
        for f in live.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                elif f.is_dir():
                    for sub in f.iterdir():
                        if sub.is_file() and sub.stat().st_mtime < cutoff:
                            sub.unlink()
                    if f.is_dir() and not any(f.iterdir()):
                        f.rmdir()
            except OSError:
                pass

    # Compress old .log files (not .gz)
    for f in logs.iterdir():
        if f.suffix == ".log" and f.name not in ("activity.log", "gate.log"):
            try:
                age = time.time() - f.stat().st_mtime
                if age > 86400:
                    _compress_file(f)
            except OSError:
                pass


def cleanup_worktrees(max_age_hours: int = 24) -> None:
    """Remove stale worktrees older than max_age_hours."""
    config = load_config()
    all_repos = get_all_repos(config)

    cutoff = time.time() - max_age_hours * 3600
    seen_bases: set[str] = set()
    removed = 0

    for repo_cfg in all_repos:
        worktree_base = repo_cfg.get("worktree_base", "/tmp/gate-worktrees")
        if worktree_base not in seen_bases:
            seen_bases.add(worktree_base)
            base = Path(worktree_base)
            if base.exists():
                for entry in base.iterdir():
                    if not entry.is_dir():
                        continue
                    try:
                        if entry.stat().st_mtime < cutoff:
                            shutil.rmtree(entry, ignore_errors=True)
                            removed += 1
                    except OSError:
                        continue

        clone = repo_cfg.get("clone_path", "")
        if clone:
            prune_worktrees(str(Path(clone).expanduser()))

    if removed:
        logger.info(f"Removed {removed} stale worktrees")


def cleanup_state(max_age_days: int = 30) -> None:
    """Remove PR state directories older than max_age_days.

    Handles both legacy (state/pr{N}/) and multi-repo (state/{slug}/pr{N}/) layouts.
    """
    state_root = state_dir()
    if not state_root.exists():
        return

    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for entry in state_root.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith("pr"):
            if (entry / "active_review.json").exists():
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
        else:
            for sub in entry.iterdir():
                if not sub.is_dir() or not sub.name.startswith("pr"):
                    continue
                if (sub / "active_review.json").exists():
                    continue
                try:
                    if sub.stat().st_mtime < cutoff:
                        shutil.rmtree(sub, ignore_errors=True)
                        removed += 1
                except OSError:
                    continue
            if entry.is_dir() and not any(entry.iterdir()):
                try:
                    entry.rmdir()
                except OSError:
                    pass

    if removed:
        logger.info(f"Removed {removed} old PR state directories")


def cleanup_pr_worktrees(pr_number: int, repo: str = "") -> None:
    """Remove any worktrees belonging to a specific PR."""
    config = load_config()
    if repo:
        try:
            repo_cfg = get_repo_config(repo, config)
        except ValueError:
            repo_cfg = {}
    else:
        repo_cfg = config.get("repo", {})
    worktree_base = repo_cfg.get("worktree_base", "/tmp/gate-worktrees")
    base = Path(worktree_base)
    if not base.exists():
        return

    slug = repo_slug(repo) if repo else ""
    prefix = f"{slug}-pr{pr_number}-" if slug else f"pr{pr_number}-"
    removed = 0
    for entry in base.iterdir():
        if entry.is_dir() and entry.name.startswith(prefix):
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1

    if removed:
        logger.info(f"Removed {removed} worktree(s) for PR #{pr_number}")

    repo_path = repo_cfg.get("clone_path", "")
    if repo_path:
        prune_worktrees(str(Path(repo_path).expanduser()))


def cleanup_orphans() -> None:
    """Detect and remove orphaned worktrees and stale active markers.

    Called on server startup. Uses PID liveness checks on active_review.json
    markers to distinguish live reviews from crash leftovers.
    Handles both legacy (state/pr{N}/) and multi-repo (state/{slug}/pr{N}/) layouts.
    """
    state_root = state_dir()
    if not state_root.exists():
        return

    config = load_config()
    all_repos = get_all_repos(config)
    worktree_bases: set[str] = set()
    for repo_cfg in all_repos:
        worktree_bases.add(repo_cfg.get("worktree_base", "/tmp/gate-worktrees"))
    if not worktree_bases:
        worktree_bases.add(config.get("repo", {}).get("worktree_base", "/tmp/gate-worktrees"))

    stale_prefixes: list[str] = []

    def _check_marker(pr_dir: Path, slug: str = "") -> None:
        marker = pr_dir / "active_review.json"
        if not marker.exists():
            return
        try:
            data = json.loads(marker.read_text())
            pid = data.get("pid", 0)
            os.kill(pid, 0)
        except ProcessLookupError:
            marker.unlink(missing_ok=True)
            pr_name = pr_dir.name
            if slug:
                stale_prefixes.append(f"{slug}-{pr_name}-")
            else:
                stale_prefixes.append(f"{pr_name}-")
            msg = f"Removed stale active marker: {slug}/{pr_name}" if slug else \
                f"Removed stale active marker: {pr_name}"
            logger.info(msg)
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    for entry in state_root.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith("pr"):
            _check_marker(entry)
        else:
            for sub in entry.iterdir():
                if sub.is_dir() and sub.name.startswith("pr"):
                    _check_marker(sub, slug=entry.name)

    if not stale_prefixes:
        return

    removed = 0
    for base_path in worktree_bases:
        base = Path(base_path)
        if not base.exists():
            continue
        for wt in base.iterdir():
            if not wt.is_dir():
                continue
            for prefix in stale_prefixes:
                if wt.name.startswith(prefix):
                    shutil.rmtree(wt, ignore_errors=True)
                    removed += 1
                    break

    if removed:
        logger.info(f"Cleaned up {removed} orphaned worktree(s) on startup")

    for repo_cfg in all_repos:
        clone = repo_cfg.get("clone_path", "")
        if clone:
            prune_worktrees(str(Path(clone).expanduser()))


def run_cleanup() -> None:
    """Run all cleanup tasks."""
    cleanup_logs()
    cleanup_worktrees()
    cleanup_state()
    logger.info("Cleanup complete")


def daily_digest() -> None:
    """Send daily metrics digest via ntfy and Discord.

    Reads the last 24 hours of reviews.jsonl and summarizes.
    """
    reviews_path = reviews_jsonl()
    if not reviews_path.exists():
        return

    cutoff = time.time() - 86400
    reviews = []
    try:
        for line in reviews_path.read_text().strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                ts = entry.get("timestamp", "")
                if ts:
                    entry_time = datetime.fromisoformat(ts).timestamp()
                    if entry_time > cutoff:
                        reviews.append(entry)
            except (json.JSONDecodeError, ValueError):
                continue
    except OSError:
        return

    if not reviews:
        return

    total = len(reviews)
    approves = sum(1 for r in reviews if r.get("decision") in ("approve", "approve_with_notes"))
    blocks = sum(1 for r in reviews if r.get("decision") == "request_changes")
    errors = sum(1 for r in reviews if r.get("decision") == "error")
    avg_time = sum(r.get("review_time_seconds", 0) for r in reviews) // max(total, 1)

    summary = (
        f"Reviews: {total} | Approved: {approves} | Blocked: {blocks} | "
        f"Errors: {errors} | Avg time: {avg_time}s"
    )

    repos_seen: dict[str, dict[str, int]] = {}
    for entry in reviews:
        repo = entry.get("repo", "") or "unknown"
        if repo not in repos_seen:
            repos_seen[repo] = {"total": 0, "approved": 0}
        repos_seen[repo]["total"] += 1
        if entry.get("decision") in ("approve", "approve_with_notes"):
            repos_seen[repo]["approved"] += 1
    if len(repos_seen) > 1:
        breakdown = " | ".join(
            f"{r}: {v['total']} ({v['approved']} ok)" for r, v in repos_seen.items()
        )
        summary += f"\nPer-repo: {breakdown}"

    notify.notify("Gate Daily Digest", summary, tags="chart_with_upwards_trend")
    notify.notify_discord("Gate Daily Digest", summary, color=3447003)
    logger.info(f"Daily digest sent: {summary}")


# ── Helpers ──────────────────────────────────────────────────


def _trim_jsonl(path: Path, max_lines: int) -> None:
    """Keep only the last max_lines of a JSONL file (atomic rewrite)."""
    try:
        lines = path.read_text().strip().split("\n")
        if len(lines) > max_lines:
            trimmed = lines[-max_lines:]
            atomic_write(path, "\n".join(trimmed) + "\n")
            logger.info(f"Trimmed {path.name}: {len(lines)} -> {max_lines} lines")
    except OSError:
        pass


def _compress_file(path: Path) -> None:
    """Compress a file with gzip and remove the original."""
    gz_path = path.with_suffix(path.suffix + ".gz")
    try:
        with open(path, "rb") as f_in:
            with gzip.open(gz_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        path.unlink()
    except OSError:
        pass

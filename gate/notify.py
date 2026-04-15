"""ntfy and Discord notifications.

Ported from notify.sh. All notifications are best-effort (never block on failure).
"""

import json
import logging
import os
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def notify(
    title: str,
    message: str,
    tags: str = "information_source",
    priority: str = "default",
    click_url: str = "",
) -> None:
    """Send ntfy notification. No-op if GATE_NTFY_TOPIC not set."""
    topic = os.environ.get("GATE_NTFY_TOPIC")
    if not topic:
        return

    headers = {
        "Title": title,
        "Tags": tags,
        "Priority": priority,
    }
    if click_url:
        headers["Click"] = click_url

    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        logger.debug(f"ntfy send failed: {title}")


def notify_discord(
    title: str, description: str, color: int = 3066993, url: str = ""
) -> None:
    """Send Discord embed. No-op if GATE_DISCORD_WEBHOOK not set."""
    webhook = os.environ.get("GATE_DISCORD_WEBHOOK")
    if not webhook:
        return

    embed: dict = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if url:
        embed["url"] = url

    payload = json.dumps({"embeds": [embed]}).encode("utf-8")
    try:
        req = urllib.request.Request(
            webhook,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        logger.debug(f"Discord send failed: {title}")


# ── Convenience Wrappers (matching notify.sh functions) ──────


def _short_repo(repo: str) -> str:
    return repo.split("/")[-1] if "/" in repo else repo


def _pr_url(pr_number: int, repo: str = "") -> str:
    if not repo:
        return ""
    return f"https://github.com/{repo}/pull/{pr_number}"


def review_complete(pr_number: int, verdict: dict, repo: str = "") -> None:
    """Notify on review completion."""
    decision = verdict.get("decision", "unknown")
    findings = verdict.get("stats", {}).get("total_findings", 0)
    review_time = verdict.get("review_time_seconds", "?")
    repo_label = f" ({_short_repo(repo)})" if repo else ""

    if decision in ("approve", "approve_with_notes"):
        notify(
            f"PR #{pr_number}{repo_label} approved ({review_time}s)",
            verdict.get("summary", ""),
            tags="white_check_mark",
            click_url=_pr_url(pr_number, repo),
        )
        notify_discord(
            f"PR #{pr_number}{repo_label} approved ({review_time}s)",
            verdict.get("summary", ""),
            color=3066993,
        )
    else:
        notify(
            f"PR #{pr_number}{repo_label} blocked ({findings} issues)",
            verdict.get("summary", ""),
            tags="x",
            click_url=_pr_url(pr_number, repo),
        )
        notify_discord(
            f"PR #{pr_number}{repo_label} blocked ({findings} issues)",
            verdict.get("summary", ""),
            color=15158332,
        )


def review_failed(pr_number: int, error: str, repo: str = "") -> None:
    """Notify on review failure."""
    repo_label = f" ({_short_repo(repo)})" if repo else ""
    notify(
        f"PR #{pr_number}{repo_label} review FAILED", error,
        tags="rotating_light", priority="high",
        click_url=_pr_url(pr_number, repo),
    )
    notify_discord(f"PR #{pr_number}{repo_label} review FAILED", error, color=15158332)


def circuit_breaker(pr_number: int, repo: str = "") -> None:
    """Notify on circuit breaker activation."""
    repo_label = f" ({_short_repo(repo)})" if repo else ""
    notify(
        f"PR #{pr_number}{repo_label} circuit breaker",
        "Last 3 reviews were errors. Auto-approving.",
        tags="rotating_light",
        priority="high",
        click_url=_pr_url(pr_number, repo),
    )
    notify_discord(
        f"PR #{pr_number}{repo_label} circuit breaker",
        "Last 3 reviews were errors. Auto-approving.",
        color=15158332,
    )


def fix_started(
    pr_number: int, finding_count: int, risk_level: str,
    repo: str = "",
) -> None:
    """Notify when auto-fix starts."""
    repo_label = f" ({_short_repo(repo)})" if repo else ""
    notify(
        f"PR #{pr_number}{repo_label} auto-fix started",
        f"{finding_count} findings, risk={risk_level}",
        tags="wrench",
        click_url=_pr_url(pr_number, repo),
    )
    notify_discord(
        f"PR #{pr_number}{repo_label} auto-fix started",
        f"{finding_count} findings, risk={risk_level}",
        color=3447003,
    )


def fix_complete(
    pr_number: int, fixed: int, total: int, iterations: int,
    repo: str = "",
) -> None:
    """Notify when auto-fix completes."""
    repo_label = f" ({_short_repo(repo)})" if repo else ""
    notify(
        f"PR #{pr_number}{repo_label} auto-fix complete",
        f"{fixed}/{total} fixed in {iterations} iteration(s)",
        tags="white_check_mark",
        click_url=_pr_url(pr_number, repo),
    )
    notify_discord(
        f"PR #{pr_number}{repo_label} auto-fix complete",
        f"{fixed}/{total} fixed in {iterations} iteration(s)",
        color=3066993,
    )


def fix_failed(
    pr_number: int, reason: str, iterations: int,
    repo: str = "",
) -> None:
    """Notify when auto-fix fails."""
    repo_label = f" ({_short_repo(repo)})" if repo else ""
    notify(
        f"PR #{pr_number}{repo_label} auto-fix failed",
        f"{reason} after {iterations} iteration(s)",
        tags="x",
        priority="high",
        click_url=_pr_url(pr_number, repo),
    )
    notify_discord(
        f"PR #{pr_number}{repo_label} auto-fix failed",
        f"{reason} after {iterations} iteration(s)",
        color=15158332,
    )


def runner_down(runner_id: str) -> None:
    """Notify when a runner is down."""
    notify(
        f"Runner {runner_id} is down",
        "Auto-restart attempted. Check if it recovered.",
        tags="rotating_light",
        priority="high",
    )

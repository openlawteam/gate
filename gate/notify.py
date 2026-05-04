"""ntfy and Discord notifications.

Originally ported from notify.sh (single env-driven ntfy topic + single
env-driven Discord webhook). Reworked to support **per-author and
per-event routing** so the PR author gets pinged directly instead of
the operator playing pager-relay.

Architecture in three layers:

1. :class:`Target` — a single notification destination (``ntfy`` topic
   or ``discord`` webhook URL). Pure data; no I/O.
2. :func:`route_for` — given an event type, optional PR author, and
   optional repo, return the list of targets that should fire. Reads
   the env-driven defaults *and* an optional
   ``notification_routes.toml`` so policy lives in config.
3. The convenience wrappers (:func:`review_complete`,
   :func:`fix_started`, ...) accept an optional ``author`` kwarg and
   delegate to :func:`route_for` + :func:`_send_to`.

Why config not a plugin hook? The brief is explicit: mechanism in OSS,
policy in companion. A TOML file is the smallest possible mechanism;
the companion repo ships its own ``notification_routes.toml`` and the
OSS Gate package never knows the team's identity.

The notification_routes.toml schema:

```toml
# Per-author overrides — applied in addition to env defaults.
# The PR author's GitHub login is the table key.
[authors.alice-handle]
ntfy = "alice-prs"
discord = "https://discord.com/api/webhooks/1234/abcd"

# Per-event extras — fire for ALL PRs on the listed events.
# Useful for an on-call channel that wants every fix-failed.
[events.fix_failed]
extra_ntfy = ["on-call-pager"]

# Per-repo extras — fire for all PRs in this repo.
[repos."myorg/myrepo"]
extra_ntfy = ["myrepo-prs"]
extra_discord = ["https://discord.com/api/webhooks/9999/zzzz"]
```

All sections are optional; an empty/missing file degrades to "use env
defaults only", preserving the historical single-tenant behavior.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import tomllib
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from gate.config import data_dir

logger = logging.getLogger(__name__)


TargetKind = Literal["ntfy", "discord"]


@dataclass(frozen=True)
class Target:
    """A single notification destination.

    ``endpoint`` is interpreted by ``kind``:

    * ``ntfy``    — endpoint is the topic (e.g. ``"alice-prs"``); the
      sender posts to ``https://ntfy.sh/<endpoint>``.
    * ``discord`` — endpoint is the full webhook URL (Discord webhooks
      are not URL-prefixed by the host because operators frequently
      use self-hosted or proxied webhooks).
    """

    kind: TargetKind
    endpoint: str
    extra: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:
        return bool(self.endpoint.strip())


# ── Routing config ──────────────────────────────────────────


def _routes_path() -> Path:
    """Location of ``notification_routes.toml``.

    Lives under the runtime data dir (next to logs) rather than under
    the install dir, so operators can deploy a new version of Gate
    without resetting their routing rules.
    """
    return data_dir() / "notification_routes.toml"


def _load_routes() -> dict:
    path = _routes_path()
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning(f"Failed to load notification_routes.toml: {e}")
        return {}


def _env_default_targets() -> list[Target]:
    out: list[Target] = []
    topic = os.environ.get("GATE_NTFY_TOPIC", "").strip()
    if topic:
        out.append(Target(kind="ntfy", endpoint=topic))
    webhook = os.environ.get("GATE_DISCORD_WEBHOOK", "").strip()
    if webhook:
        out.append(Target(kind="discord", endpoint=webhook))
    return out


def _author_targets(routes: dict, author: str | None) -> list[Target]:
    if not author:
        return []
    authors = routes.get("authors", {}) or {}
    if not isinstance(authors, dict):
        return []
    entry = authors.get(author) or authors.get(author.lower())
    if not isinstance(entry, dict):
        return []
    out: list[Target] = []
    ntfy = entry.get("ntfy")
    if isinstance(ntfy, str) and ntfy.strip():
        out.append(Target(kind="ntfy", endpoint=ntfy.strip()))
    discord = entry.get("discord")
    if isinstance(discord, str) and discord.strip():
        out.append(Target(kind="discord", endpoint=discord.strip()))
    return out


def _section_extras(section: dict, key: str) -> list[Target]:
    if not isinstance(section, dict):
        return []
    out: list[Target] = []
    for topic in section.get("extra_ntfy", []) or []:
        if isinstance(topic, str) and topic.strip():
            out.append(Target(kind="ntfy", endpoint=topic.strip()))
    for webhook in section.get("extra_discord", []) or []:
        if isinstance(webhook, str) and webhook.strip():
            out.append(Target(kind="discord", endpoint=webhook.strip()))
    return out


def route_for(
    event: str,
    *,
    author: str | None = None,
    repo: str | None = None,
) -> list[Target]:
    """Return the deduped list of targets for one notification.

    Resolution order (each layer adds to the set; later layers don't
    suppress earlier ones):

    1. Env defaults (``GATE_NTFY_TOPIC``, ``GATE_DISCORD_WEBHOOK``).
    2. Author override (``[authors.<login>]`` in
       ``notification_routes.toml``).
    3. Event extras (``[events.<event>]``).
    4. Repo extras (``[repos."owner/repo"]``).

    Dedup is by ``(kind, endpoint)``; identical targets are coalesced
    so a webhook listed in two sections doesn't fire twice.
    """
    routes = _load_routes()

    targets: list[Target] = []
    targets.extend(_env_default_targets())
    targets.extend(_author_targets(routes, author))

    events_section = (routes.get("events") or {}).get(event)
    targets.extend(_section_extras(events_section or {}, "events"))

    repos_section = (routes.get("repos") or {}).get(repo or "")
    targets.extend(_section_extras(repos_section or {}, "repos"))

    seen: set[tuple[str, str]] = set()
    out: list[Target] = []
    for t in targets:
        key = (t.kind, t.endpoint)
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


# ── Sending ─────────────────────────────────────────────────


def _send_to(
    target: Target,
    *,
    title: str,
    message: str,
    tags: str = "information_source",
    priority: str = "default",
    click_url: str = "",
    color: int = 3066993,
) -> None:
    """Deliver one notification to one target. Best-effort; never raises."""
    if target.kind == "ntfy":
        _send_ntfy(target.endpoint, title, message, tags, priority, click_url)
        return
    if target.kind == "discord":
        _send_discord(target.endpoint, title, message, color, click_url)
        return
    logger.debug(f"Unknown target kind: {target.kind}")


def _send_ntfy(
    topic: str,
    title: str,
    message: str,
    tags: str,
    priority: str,
    click_url: str,
) -> None:
    headers = {"Title": title, "Tags": tags, "Priority": priority}
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
        logger.debug(f"ntfy send failed: topic={topic} title={title}")


def _send_discord(
    webhook: str,
    title: str,
    description: str,
    color: int,
    url: str,
) -> None:
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
        logger.debug(f"Discord send failed: title={title}")


# ── Backward-compatible env-driven helpers (kept for tests + callers) ──


def notify(
    title: str,
    message: str,
    tags: str = "information_source",
    priority: str = "default",
    click_url: str = "",
) -> None:
    """Send ntfy notification. No-op if GATE_NTFY_TOPIC not set.

    Preserved as the env-driven entry point so existing callers (tests
    and direct uses) keep working unchanged. New code should prefer
    :func:`route_for` + :func:`_send_to` so author/repo routing
    applies.
    """
    topic = os.environ.get("GATE_NTFY_TOPIC")
    if not topic:
        return
    _send_ntfy(topic, title, message, tags, priority, click_url)


def notify_discord(
    title: str, description: str, color: int = 3066993, url: str = ""
) -> None:
    """Send Discord embed. No-op if GATE_DISCORD_WEBHOOK not set."""
    webhook = os.environ.get("GATE_DISCORD_WEBHOOK")
    if not webhook:
        return
    _send_discord(webhook, title, description, color, url)


# ── Convenience wrappers (matching notify.sh functions) ──────


def _short_repo(repo: str) -> str:
    return repo.split("/")[-1] if "/" in repo else repo


def _pr_url(pr_number: int, repo: str = "") -> str:
    if not repo:
        return ""
    return f"https://github.com/{repo}/pull/{pr_number}"


def _route_and_send(
    event: str,
    *,
    title: str,
    message: str,
    tags: str = "information_source",
    priority: str = "default",
    click_url: str = "",
    color: int = 3066993,
    author: str | None = None,
    repo: str | None = None,
) -> None:
    """Resolve targets via :func:`route_for` and dispatch to each."""
    targets = route_for(event, author=author, repo=repo)
    if not targets:
        return
    for target in targets:
        _send_to(
            target,
            title=title,
            message=message,
            tags=tags,
            priority=priority,
            click_url=click_url,
            color=color,
        )


def review_complete(
    pr_number: int,
    verdict: dict,
    repo: str = "",
    *,
    author: str | None = None,
) -> None:
    """Notify on review completion."""
    decision = verdict.get("decision", "unknown")
    findings = verdict.get("stats", {}).get("total_findings", 0)
    review_time = verdict.get("review_time_seconds", "?")
    repo_label = f" ({_short_repo(repo)})" if repo else ""
    summary = verdict.get("summary", "")
    pr_url = _pr_url(pr_number, repo)

    if decision in ("approve", "approve_with_notes"):
        _route_and_send(
            "review_complete",
            title=f"PR #{pr_number}{repo_label} approved ({review_time}s)",
            message=summary,
            tags="white_check_mark",
            click_url=pr_url,
            color=3066993,
            author=author,
            repo=repo,
        )
    else:
        _route_and_send(
            "review_complete",
            title=f"PR #{pr_number}{repo_label} blocked ({findings} issues)",
            message=summary,
            tags="x",
            click_url=pr_url,
            color=15158332,
            author=author,
            repo=repo,
        )


def review_failed(
    pr_number: int, error: str, repo: str = "",
    *,
    author: str | None = None,
) -> None:
    """Notify on review failure."""
    repo_label = f" ({_short_repo(repo)})" if repo else ""
    _route_and_send(
        "review_failed",
        title=f"PR #{pr_number}{repo_label} review FAILED",
        message=error,
        tags="rotating_light",
        priority="high",
        click_url=_pr_url(pr_number, repo),
        color=15158332,
        author=author,
        repo=repo,
    )


def circuit_breaker(
    pr_number: int, repo: str = "",
    *,
    author: str | None = None,
) -> None:
    """Notify on circuit breaker activation."""
    repo_label = f" ({_short_repo(repo)})" if repo else ""
    _route_and_send(
        "circuit_breaker",
        title=f"PR #{pr_number}{repo_label} circuit breaker",
        message="Last 3 reviews were errors. Auto-approving.",
        tags="rotating_light",
        priority="high",
        click_url=_pr_url(pr_number, repo),
        color=15158332,
        author=author,
        repo=repo,
    )


def fix_started(
    pr_number: int, finding_count: int, risk_level: str,
    repo: str = "",
    *,
    author: str | None = None,
) -> None:
    """Notify when auto-fix starts."""
    repo_label = f" ({_short_repo(repo)})" if repo else ""
    _route_and_send(
        "fix_started",
        title=f"PR #{pr_number}{repo_label} auto-fix started",
        message=f"{finding_count} findings, risk={risk_level}",
        tags="wrench",
        click_url=_pr_url(pr_number, repo),
        color=3447003,
        author=author,
        repo=repo,
    )


def fix_complete(
    pr_number: int, fixed: int, total: int, iterations: int,
    repo: str = "",
    *,
    author: str | None = None,
) -> None:
    """Notify when auto-fix completes."""
    repo_label = f" ({_short_repo(repo)})" if repo else ""
    _route_and_send(
        "fix_complete",
        title=f"PR #{pr_number}{repo_label} auto-fix complete",
        message=f"{fixed}/{total} fixed in {iterations} iteration(s)",
        tags="white_check_mark",
        click_url=_pr_url(pr_number, repo),
        color=3066993,
        author=author,
        repo=repo,
    )


def fix_failed(
    pr_number: int, reason: str, iterations: int,
    repo: str = "",
    *,
    author: str | None = None,
) -> None:
    """Notify when auto-fix fails."""
    repo_label = f" ({_short_repo(repo)})" if repo else ""
    _route_and_send(
        "fix_failed",
        title=f"PR #{pr_number}{repo_label} auto-fix failed",
        message=f"{reason} after {iterations} iteration(s)",
        tags="x",
        priority="high",
        click_url=_pr_url(pr_number, repo),
        color=15158332,
        author=author,
        repo=repo,
    )


_last_codex_alert_ts: float = 0.0
_CODEX_ALERT_COOLDOWN_S = 3600  # 1 hour
_codex_alert_lock = threading.Lock()


def codex_unavailable(reason: str) -> None:
    """Notify when the Codex CLI is broken / unavailable for auto-fix.

    Throttled to at most once per hour so a persistently broken binary
    doesn't spam every PR review. Operator-targeted only — no per-author
    routing because by definition no PR is involved.
    """
    global _last_codex_alert_ts
    now = time.monotonic()
    with _codex_alert_lock:
        if now - _last_codex_alert_ts < _CODEX_ALERT_COOLDOWN_S:
            return
        _last_codex_alert_ts = now

    _route_and_send(
        "codex_unavailable",
        title="Codex CLI unavailable — auto-fix paused",
        message=reason,
        tags="warning",
        priority="default",
        color=15105570,  # orange
    )


def runner_down(runner_id: str) -> None:
    """Notify when a runner is down. Operator-targeted only."""
    _route_and_send(
        "runner_down",
        title=f"Runner {runner_id} is down",
        message="Auto-restart attempted. Check if it recovered.",
        tags="rotating_light",
        priority="high",
    )


def quota_auth_drift(reason: str) -> None:
    """Notify when the Claude OAuth token is expired/invalid.

    Operator-targeted only; PR authors don't need to know about
    operator credential issues. Posted as a single high-priority
    alert rather than per-PR so the operator actually refreshes the
    token instead of tuning it out.
    """
    _route_and_send(
        "quota_auth_drift",
        title="Gate: Claude OAuth token looks expired",
        message=f"Quota check is fail-open. Refresh the token. Reason: {reason}",
        tags="warning",
        priority="high",
    )

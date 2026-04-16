"""Anthropic quota checking.

Ported from check-quota.js + quota-helpers.js + refresh-quota.sh Keychain read.
Fail-open: on any error, returns quota_ok=True.
"""

import json
import logging
import os
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from gate.config import state_dir
from gate.io import atomic_write

logger = logging.getLogger(__name__)


def quota_cache_path() -> Path:
    """Return the path to the quota cache file."""
    return state_dir() / "quota-cache.json"


QUOTA_CACHE_MAX_AGE_S = 30 * 60  # 30 minutes
QUOTA_SESSION_THRESHOLD = 80
QUOTA_WEEKLY_THRESHOLD = 95
QUOTA_EXHAUSTED_THRESHOLD = 95


def read_keychain_token() -> str | None:
    """Read Claude OAuth token from macOS Keychain.

    Ported from refresh-quota.sh.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        raw = result.stdout.strip()
        if not raw:
            return None
        data = json.loads(raw)
        return data.get("claudeAiOauth", {}).get("accessToken")
    except (subprocess.SubprocessError, json.JSONDecodeError, KeyError):
        return None


def _fetch_usage(token: str) -> dict:
    """Call Anthropic usage API. Ported from fetchUsage() in check-quota.js."""
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.1",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _write_cache(usage: dict) -> None:
    """Write usage data to cache file."""
    try:
        atomic_write(
            quota_cache_path(),
            json.dumps(
                {
                    "cached_at": datetime.now(timezone.utc).isoformat(),
                    "five_hour": usage.get("five_hour"),
                    "seven_day": usage.get("seven_day"),
                }
            ),
        )
    except OSError:
        logger.warning("Failed to write quota cache")


def _read_cache() -> dict | None:
    """Read cached usage data if fresh enough."""
    try:
        raw = quota_cache_path().read_text()
        cached = json.loads(raw)
        cached_at = datetime.fromisoformat(cached["cached_at"])
        age_s = (datetime.now(timezone.utc) - cached_at).total_seconds()
        if age_s > QUOTA_CACHE_MAX_AGE_S:
            return None
        return cached
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def _fail_open(reason: str) -> dict:
    """Return a fail-open result so reviews are never blocked by monitoring failures."""
    logger.warning(f"Quota check fail-open: {reason}")
    return {
        "quota_ok": True,
        "five_hour_pct": -1,
        "seven_day_pct": -1,
        "resets_at": "",
        "reason": f"fail-open: {reason}",
    }


def check_quota(
    session_threshold: int = QUOTA_SESSION_THRESHOLD,
    weekly_threshold: int = QUOTA_WEEKLY_THRESHOLD,
) -> dict:
    """Check Anthropic usage against thresholds.

    Ported from check-quota.js. Fail-open on all errors.

    Returns:
        Dict with quota_ok, five_hour_pct, seven_day_pct, resets_at, reason.
    """
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not token:
        token = read_keychain_token() or ""
    if not token:
        return _fail_open("CLAUDE_CODE_OAUTH_TOKEN not set and Keychain empty")

    usage = None
    from_api = False
    try:
        usage = _fetch_usage(token)
        from_api = True
    except Exception as e:
        cached = _read_cache()
        if cached:
            logger.info(f"Using cached quota: {e}")
            usage = cached
        else:
            return _fail_open(str(e))

    if usage.get("error"):
        cached = _read_cache()
        if cached:
            usage = cached
            from_api = False
        else:
            error_msg = usage["error"].get("message", json.dumps(usage["error"]))
            return _fail_open(f"API error: {error_msg}")

    if from_api:
        _write_cache(usage)

    five_hour = usage.get("five_hour") or {}
    seven_day = usage.get("seven_day") or {}
    five_hour_pct = five_hour.get("utilization", -1)
    seven_day_pct = seven_day.get("utilization", -1)
    resets_at = five_hour.get("resets_at", "")

    reasons = []
    if isinstance(five_hour_pct, (int, float)) and five_hour_pct >= session_threshold:
        reasons.append(f"5-hour usage at {five_hour_pct}% (threshold: {session_threshold}%)")
    if isinstance(seven_day_pct, (int, float)) and seven_day_pct >= weekly_threshold:
        reasons.append(f"7-day usage at {seven_day_pct}% (threshold: {weekly_threshold}%)")

    quota_ok = len(reasons) == 0
    return {
        "quota_ok": quota_ok,
        "five_hour_pct": five_hour_pct,
        "seven_day_pct": seven_day_pct,
        "resets_at": resets_at,
        "reason": "ok" if quota_ok else "; ".join(reasons),
    }


def check_quota_fast() -> dict | None:
    """Quick quota check for retry decisions (uses cached value if fresh).

    Ported from checkQuotaFast() in quota-helpers.js.
    """
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not token:
        token = read_keychain_token() or ""
    if not token:
        return None

    try:
        usage = _fetch_usage(token)
        pct = (usage.get("five_hour") or {}).get("utilization", -1)
        exhausted = isinstance(pct, (int, float)) and pct >= QUOTA_EXHAUSTED_THRESHOLD
        return {"exhausted": exhausted, "pct": pct}
    except Exception:
        cached = _read_cache()
        if cached:
            pct = (cached.get("five_hour") or {}).get("utilization", -1)
            exhausted = isinstance(pct, (int, float)) and pct >= QUOTA_EXHAUSTED_THRESHOLD
            return {"exhausted": exhausted, "pct": pct}
        return None

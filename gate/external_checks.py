"""Platform-neutral external check-run integration (PR B.2).

Gate's whole premise is "trust but verify," but without this module it
ignores every external CI signal — Vercel preview failures, GitHub
Actions test runs, Netlify deploy previews, CircleCI builds, etc.
They all report through GitHub's check-runs / statuses APIs, and the
PR #22 post-mortem showed Gate silently approving a PR that Vercel
had red-lit.

This module consults both the **modern** check-runs endpoint (used by
GitHub Actions, Buildkite, CircleCI) and the **legacy** statuses
endpoint (still used by Vercel, Netlify, and many third-party
integrations). Both must be merged to get a complete picture.

Design invariants
-----------------

* **Opt-in.** A repo with no ``required_external_checks`` key gets
  today's behaviour exactly — no surprise regressions.
* **Platform-neutral.** ``fetch_check_state`` speaks GitHub's
  API surface only; any CI that reports there works.
* **Cancel-aware.** ``wait_for_pending`` waits on the orchestrator's
  existing ``threading.Event`` so ``gate cancel`` interrupts mid-wait
  without requiring a second cancellation primitive.
* **Fail-closed on pending-timeout.** If a ``blocking`` check is still
  pending after ``wait_seconds``, Gate treats that as a failure — the
  safer direction because silent approvals are the outcome we're
  trying to prevent.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from gate import github as gh

logger = logging.getLogger(__name__)

# Defaults used when the config omits ``[external_checks]``. Intentionally
# conservative: opt-in means no behaviour change by default, and even
# once opted in the wait is short enough that no human notices.
DEFAULT_WAIT_SECONDS: int = 600
DEFAULT_RECHECK_MINUTES: int = 30
DEFAULT_POLL_INTERVAL_S: int = 15

# Normalised conclusion buckets. GitHub's API uses slightly different
# vocabulary per endpoint; ``_normalise_conclusion`` maps both into
# one of these so downstream code can switch on a small set.
CONCLUSION_SUCCESS = "success"
CONCLUSION_FAILURE = "failure"
CONCLUSION_PENDING = "pending"
CONCLUSION_NEUTRAL = "neutral"
CONCLUSION_UNKNOWN = "unknown"

_SUCCESS_VALUES: frozenset[str] = frozenset({"success", "passed"})
_FAILURE_VALUES: frozenset[str] = frozenset({
    "failure", "failed", "cancelled", "timed_out", "action_required",
    "startup_failure", "error",
})
_NEUTRAL_VALUES: frozenset[str] = frozenset({
    "neutral", "skipped", "stale",
})


@dataclass(frozen=True)
class RequiredCheck:
    """A single entry from ``required_external_checks`` in gate.toml.

    ``name`` is matched against GitHub's reported check name. By
    default matching is case-insensitive substring — covers "Vercel"
    → "Vercel – Preview" / "Vercel – Production" automatically. The
    ``match`` field can be set to ``"exact"`` for unambiguous cases
    like "Vercel – Preview" where partial matching would collide.
    """

    name: str
    policy: str = "blocking"
    match: str = "substring"

    def matches(self, check_name: str) -> bool:
        needle = self.name.strip().lower()
        haystack = (check_name or "").strip().lower()
        if self.match == "exact":
            return needle == haystack
        return bool(needle) and needle in haystack


@dataclass
class CheckState:
    """Normalised snapshot of one external check for one SHA."""

    name: str
    conclusion: str
    status: str
    url: str = ""
    source: str = "check-runs"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckClassification:
    """Result of classifying a set of checks against required config."""

    blocking_failures: list[CheckState] = field(default_factory=list)
    blocking_pending: list[CheckState] = field(default_factory=list)
    advisory_failures: list[CheckState] = field(default_factory=list)
    unknown: list[RequiredCheck] = field(default_factory=list)

    @property
    def has_blocking_failure(self) -> bool:
        return bool(self.blocking_failures)

    @property
    def has_blocking_pending(self) -> bool:
        return bool(self.blocking_pending)


def _normalise_conclusion(conclusion: str | None, status: str | None) -> str:
    """Map GitHub's conclusion/status pair to one of our five buckets.

    GitHub reports "conclusion" only when a check has finished; while
    in-flight it's None and ``status`` carries the interesting signal
    ("queued" / "in_progress"). Statuses API uses a single ``state``
    field we pass as ``conclusion`` here so the mapping stays uniform.
    """
    conclusion_l = (conclusion or "").strip().lower()
    status_l = (status or "").strip().lower()

    if conclusion_l in _SUCCESS_VALUES:
        return CONCLUSION_SUCCESS
    if conclusion_l in _FAILURE_VALUES:
        return CONCLUSION_FAILURE
    if conclusion_l in _NEUTRAL_VALUES:
        return CONCLUSION_NEUTRAL

    if conclusion_l == "pending":
        return CONCLUSION_PENDING
    if not conclusion_l and status_l in ("queued", "in_progress", "pending", ""):
        return CONCLUSION_PENDING

    return CONCLUSION_UNKNOWN


def _gh_json(args: list[str]) -> Any:
    """Run a ``gh api`` call and return the parsed JSON body.

    Wraps the existing ``gate.github._gh`` retry helper so we inherit
    its transient-error / connectivity-wait behaviour. Returns None on
    parse failure rather than raising; callers treat that as "no data
    from this endpoint" and fall back to the other one.
    """
    try:
        body = gh._gh(args)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.warning(f"gh api call failed ({args[:3]}): {e}")
        return None
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        logger.warning(f"gh api returned non-JSON for {args[:3]}: {e}")
        return None


def _paginate_check_runs(repo: str, sha: str) -> list[dict[str, Any]]:
    """Fetch all check-runs pages for a SHA, merging ``check_runs`` arrays."""
    all_runs: list[dict[str, Any]] = []
    page = 1
    while True:
        body = _gh_json([
            "api",
            f"repos/{repo}/commits/{sha}/check-runs?per_page=100&page={page}",
        ])
        if not isinstance(body, dict):
            break
        runs = body.get("check_runs")
        if not isinstance(runs, list):
            break
        all_runs.extend(runs)
        if len(runs) < 100:
            break
        page += 1
        if page > 10:
            logger.warning(
                f"check-runs pagination capped at 10 pages for {repo}@{sha[:8]}"
            )
            break
    return all_runs


def fetch_check_state(sha: str, repo: str) -> dict[str, CheckState]:
    """Return a ``{name → CheckState}`` map for one commit.

    Merges results from both GitHub endpoints because they surface
    different CI providers:

    * ``GET /repos/{owner}/{repo}/commits/{sha}/check-runs`` — modern
      (GitHub Actions, CircleCI, Buildkite, Render, ...).
    * ``GET /repos/{owner}/{repo}/commits/{sha}/status`` — legacy
      statuses (Vercel, Netlify, many third-party integrations).

    On collision (both endpoints report a check of the same name) the
    modern check-runs entry wins — it carries richer metadata
    (``details_url``, run status). When one endpoint fails, the other
    still contributes. When both fail the map is empty and classify()
    will treat every required check as "unknown".
    """
    out: dict[str, CheckState] = {}

    runs = _paginate_check_runs(repo, sha)
    for run in runs:
        name = (run.get("name") or "").strip()
        if not name:
            continue
        out[name] = CheckState(
            name=name,
            conclusion=_normalise_conclusion(run.get("conclusion"), run.get("status")),
            status=run.get("status") or "",
            url=run.get("details_url") or run.get("html_url") or "",
            source="check-runs",
            raw=run,
        )

    statuses_body = _gh_json(["api", f"repos/{repo}/commits/{sha}/status"])
    if isinstance(statuses_body, dict):
        contexts = statuses_body.get("statuses")
        if isinstance(contexts, list):
            for status in contexts:
                name = (status.get("context") or "").strip()
                if not name or name in out:
                    continue
                out[name] = CheckState(
                    name=name,
                    conclusion=_normalise_conclusion(status.get("state"), status.get("state")),
                    status=status.get("state") or "",
                    url=status.get("target_url") or "",
                    source="statuses",
                    raw=status,
                )

    return out


def _parse_required(raw: list[Any]) -> list[RequiredCheck]:
    """Normalise the ``required_external_checks`` TOML value.

    TOML emits each entry as a dict; legacy string entries (``["Vercel",
    "tests"]``) are tolerated for convenience with the default policy.
    Unknown keys are silently dropped so future additions don't crash
    older Gate deployments.
    """
    out: list[RequiredCheck] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if isinstance(entry, str):
            if entry.strip():
                out.append(RequiredCheck(name=entry.strip()))
            continue
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        policy = str(entry.get("policy") or "blocking").strip().lower()
        if policy not in ("blocking", "advisory"):
            policy = "blocking"
        match = str(entry.get("match") or "substring").strip().lower()
        if match not in ("substring", "exact"):
            match = "substring"
        out.append(RequiredCheck(name=name, policy=policy, match=match))
    return out


def classify(
    checks: dict[str, CheckState],
    required: list[RequiredCheck] | list[Any],
) -> CheckClassification:
    """Bucket each required check by policy + conclusion.

    * ``blocking_failures`` — required, policy=blocking, conclusion=failure.
    * ``blocking_pending`` — required, policy=blocking, conclusion=pending
      (including reports that haven't arrived yet — those go into the
      ``unknown`` bucket which is treated as pending by
      ``wait_for_pending``).
    * ``advisory_failures`` — required, policy=advisory, conclusion=failure.
    * ``unknown`` — required but no matching check reported at all.
      ``wait_for_pending`` treats this the same as pending because it
      typically means the CI provider hasn't reported yet; once
      ``wait_seconds`` elapses, a still-unknown blocking check
      degrades to a synthetic failure (fail-closed).

    Takes either a list of ``RequiredCheck`` or the raw TOML list for
    caller convenience.
    """
    if required and not isinstance(required[0], RequiredCheck):
        required = _parse_required(required)

    result = CheckClassification()
    for req in required:
        matched = [cs for cs in checks.values() if req.matches(cs.name)]
        if not matched:
            result.unknown.append(req)
            continue
        failing = [m for m in matched if m.conclusion == CONCLUSION_FAILURE]
        pending = [m for m in matched if m.conclusion == CONCLUSION_PENDING]
        if req.policy == "blocking":
            if failing:
                result.blocking_failures.extend(failing)
            elif pending:
                result.blocking_pending.extend(pending)
        else:
            if failing:
                result.advisory_failures.extend(failing)
    return result


def wait_for_pending(
    sha: str,
    repo: str,
    required: list[RequiredCheck] | list[Any],
    cancelled: threading.Event,
    timeout_s: float,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> CheckClassification:
    """Poll until every blocking check has finished, or ``timeout_s`` elapses.

    Returns the final classification. Uses ``Event.wait`` (not
    ``time.sleep``) so ``gate cancel`` flips the event and this
    function returns immediately with whatever the latest snapshot
    was — callers should re-check ``cancelled.is_set()`` after the
    return to decide whether to honour or discard the result.

    Fail-closed contract: once ``timeout_s`` elapses, still-pending
    and still-unknown blocking checks are reported unchanged. The
    orchestrator treats either of those as synthetic failures.
    """
    deadline = time.monotonic() + max(0.0, timeout_s)
    last: CheckClassification = classify(fetch_check_state(sha, repo), required)
    while time.monotonic() < deadline and not cancelled.is_set():
        if not last.has_blocking_pending and not last.unknown:
            return last
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Sleep at most ``poll_interval_s`` but never past the deadline.
        wait = min(poll_interval_s, max(0.0, remaining))
        if cancelled.wait(timeout=wait):
            return last
        last = classify(fetch_check_state(sha, repo), required)
    return last


def external_checks_enabled(config: dict[str, Any]) -> bool:
    """Honour the global master kill-switch.

    ``[external_checks].enabled = false`` short-circuits the whole
    pipeline — useful if GitHub rate limits ever become a real
    concern. Defaults to True so repos that add
    ``required_external_checks`` start working without a second config
    change.
    """
    section = config.get("external_checks") or {}
    if not isinstance(section, dict):
        return True
    return bool(section.get("enabled", True))


def get_wait_seconds(config: dict[str, Any], repo_config: dict[str, Any]) -> int:
    """Resolve per-check wait-seconds: repo override > global > default."""
    if repo_config and isinstance(repo_config.get("external_check_wait_seconds"), int):
        return max(0, int(repo_config["external_check_wait_seconds"]))
    section = config.get("external_checks") or {}
    if isinstance(section.get("wait_seconds_default"), int):
        return max(0, int(section["wait_seconds_default"]))
    return DEFAULT_WAIT_SECONDS


def get_recheck_minutes(config: dict[str, Any], repo_config: dict[str, Any]) -> int:
    """Resolve per-check recheck-minutes: repo override > global > default."""
    if repo_config and isinstance(repo_config.get("external_check_recheck_minutes"), int):
        return max(0, int(repo_config["external_check_recheck_minutes"]))
    section = config.get("external_checks") or {}
    if isinstance(section.get("recheck_minutes_default"), int):
        return max(0, int(section["recheck_minutes_default"]))
    return DEFAULT_RECHECK_MINUTES


def required_from_config(repo_config: dict[str, Any]) -> list[RequiredCheck]:
    """Extract + parse the required-checks list from a repo config dict."""
    return _parse_required(repo_config.get("required_external_checks") or [])

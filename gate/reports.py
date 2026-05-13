"""Aggregate stats from ``reviews.jsonl`` for the ``gate report`` CLI.

Single source of truth for review-volume / verdict / fix-pipeline
metrics. The CLI subcommand in :mod:`gate.cli` is a thin wrapper over
the functions here so the dashboard can reuse the same aggregations
later (e.g. via the SSE adapter exposing a ``/v1/report`` JSON view).

Schema notes — see :mod:`gate.logger` for the writer side. The two
record kinds in ``reviews.jsonl`` are differentiated by:

* Review records (the normal review verdict) — no special flag, has
  ``stages_run``, ``findings_by_severity``, ``decision`` ∈
  {``approve``, ``approve_with_notes``, ``request_changes``,
  ``error``, ``skip``, ``cancelled``}.
* Fix-followup records — ``is_fix_followup: true`` and ``decision`` ∈
  {``fix_succeeded``, ``fix_failed``, ``fix_no_op``, ``fix_skipped``}.
  These do NOT count as fresh reviews; they're aggregated separately.
  ``fix_no_op`` and ``fix_skipped`` are excluded from the success-rate
  denominator (neither represents a real fix attempt).

Cost tracking is intentionally out of scope for Phase 1 — the
``reviews.jsonl`` writer doesn't emit token/cost fields today (see the
plan's "Cost tracking note"). When that data lands, this module will
gain a ``cost_usd`` field on :class:`Report` without breaking existing
JSON consumers.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gate.logger import reviews_jsonl

logger = logging.getLogger(__name__)


# ── Time parsing ────────────────────────────────────────────


_RELATIVE_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_DURATION_UNIT_S = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 60 * 60 * 24,
    "w": 60 * 60 * 24 * 7,
}


def parse_since(spec: str) -> _dt.timedelta:
    """Parse ``"7d"``, ``"24h"``, ``"30d"``, ``"3600s"``, ``"2w"``.

    Strict: a malformed spec raises :class:`ValueError`. Reports CLI
    catches and surfaces the message rather than silently defaulting
    to 0 (which would yield empty reports and confuse the operator).
    """
    if not spec or not isinstance(spec, str):
        raise ValueError(f"--since requires non-empty value, got {spec!r}")
    m = _RELATIVE_DURATION_RE.match(spec)
    if not m:
        raise ValueError(
            f"--since must look like '7d', '24h', '30m', got {spec!r}"
        )
    n = int(m.group(1))
    unit = m.group(2).lower()
    return _dt.timedelta(seconds=n * _DURATION_UNIT_S[unit])


# ── Loading ─────────────────────────────────────────────────


_REVIEW_DECISIONS = frozenset({
    "approve", "approve_with_notes", "request_changes",
    "error", "skip", "cancelled",
})
_FIX_DECISIONS = frozenset(
    {"fix_succeeded", "fix_failed", "fix_no_op", "fix_skipped"}
)


def _is_review(row: dict) -> bool:
    """A row counts as a review (vs. a fix-followup) when it doesn't
    declare itself a fix followup AND its decision isn't one of the
    fix-pipeline outcomes.
    """
    if row.get("is_fix_followup"):
        return False
    return str(row.get("decision", "")) in _REVIEW_DECISIONS


def _is_fix_followup(row: dict) -> bool:
    if row.get("is_fix_followup"):
        return True
    return str(row.get("decision", "")) in _FIX_DECISIONS


def _parse_ts(raw: str | None) -> _dt.datetime | None:
    if not raw:
        return None
    try:
        # Python's isoformat handles "+00:00" and "Z" since 3.11.
        return _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_reviews(
    *,
    since: _dt.timedelta | None = None,
    repo: str = "",
    path: Path | None = None,
) -> list[dict]:
    """Load and filter reviews.jsonl rows.

    ``since`` filters by ``timestamp`` (drops anything older than
    ``now - since``); ``repo`` filters by ``repo`` field. Rows with
    malformed JSON are skipped silently — the log is append-only by
    many writers and we'd rather report 99% than crash.
    """
    rev_path = path if path is not None else reviews_jsonl()
    if not rev_path.exists():
        return []
    cutoff: _dt.datetime | None = None
    if since is not None:
        cutoff = _dt.datetime.now(_dt.UTC) - since
    rows: list[dict] = []
    with open(rev_path, "r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if repo and row.get("repo") != repo:
                continue
            if cutoff is not None:
                ts = _parse_ts(row.get("timestamp"))
                if ts is None or ts < cutoff:
                    continue
            rows.append(row)
    return rows


# ── Aggregation ─────────────────────────────────────────────


@dataclass
class Report:
    """The aggregate result of :func:`summarize`.

    All fields are JSON-serialisable so :func:`format_json` is just
    ``dataclasses.asdict``. Adding new fields is safe; removing or
    renaming fields is a breaking change for any tooling reading the
    JSON output.
    """

    since: str = ""
    repo_filter: str = ""
    total_reviews: int = 0
    total_fix_followups: int = 0
    decisions: dict[str, int] = field(default_factory=dict)
    decisions_by_repo: dict[str, dict[str, int]] = field(default_factory=dict)
    fix_outcomes: dict[str, int] = field(default_factory=dict)
    fix_success_rate: float | None = None
    avg_review_seconds: float | None = None
    p95_review_seconds: float | None = None
    findings_total: int = 0
    findings_by_severity: dict[str, int] = field(default_factory=dict)
    top_finding_categories: list[tuple[str, int]] = field(default_factory=list)
    fast_track_count: int = 0


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = max(0, min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1)))))
    return s[idx]


def summarize(
    rows: list[dict],
    *,
    since_label: str = "",
    repo_label: str = "",
    top_n_categories: int = 10,
) -> Report:
    """Compute a :class:`Report` from filtered rows."""
    review_rows = [r for r in rows if _is_review(r)]
    fix_rows = [r for r in rows if _is_fix_followup(r)]

    decisions: Counter[str] = Counter(
        str(r.get("decision", "unknown")) for r in review_rows
    )
    by_repo: dict[str, Counter[str]] = defaultdict(Counter)
    for r in review_rows:
        by_repo[str(r.get("repo", "") or "(unknown)")][
            str(r.get("decision", "unknown"))
        ] += 1

    fix_outcomes: Counter[str] = Counter(
        str(r.get("decision", "unknown")) for r in fix_rows
    )
    succeeded = fix_outcomes.get("fix_succeeded", 0)
    failed = fix_outcomes.get("fix_failed", 0)
    # Exclude ``fix_no_op`` and ``fix_skipped`` from the rate
    # denominator: neither represents an actual fix attempt that
    # produced a verdict. ``fix_no_op`` is graceful-no-op
    # (approve_with_notes with no mechanical work); ``fix_skipped``
    # is cancellation (supersede / operator cancel / workspace
    # teardown) or policy block (cooldown, soft / lifetime limit).
    fix_attempts = succeeded + failed
    fix_rate: float | None = None
    if fix_attempts:
        fix_rate = succeeded / fix_attempts

    review_secs = [
        float(r["review_time_seconds"])
        for r in review_rows
        if isinstance(r.get("review_time_seconds"), (int, float))
    ]
    avg_secs: float | None = None
    if review_secs:
        avg_secs = sum(review_secs) / len(review_secs)
    p95 = _percentile(review_secs, 95.0)

    findings_total = sum(
        int(r.get("findings", 0) or 0)
        for r in review_rows
        if isinstance(r.get("findings"), (int, float))
    )

    severity_totals: Counter[str] = Counter()
    for r in review_rows:
        sevs = r.get("findings_by_severity") or {}
        if not isinstance(sevs, dict):
            continue
        for sev, count in sevs.items():
            try:
                severity_totals[str(sev)] += int(count)
            except (TypeError, ValueError):
                continue

    category_totals: Counter[str] = Counter()
    for r in review_rows:
        cats = r.get("finding_categories") or []
        if not isinstance(cats, list):
            continue
        for cat in cats:
            if isinstance(cat, str):
                category_totals[cat] += 1

    fast_track = sum(
        1 for r in review_rows if r.get("fast_track_eligible") is True
    )

    return Report(
        since=since_label,
        repo_filter=repo_label,
        total_reviews=len(review_rows),
        total_fix_followups=len(fix_rows),
        decisions=dict(decisions),
        decisions_by_repo={k: dict(v) for k, v in by_repo.items()},
        fix_outcomes=dict(fix_outcomes),
        fix_success_rate=fix_rate,
        avg_review_seconds=avg_secs,
        p95_review_seconds=p95,
        findings_total=findings_total,
        findings_by_severity=dict(severity_totals),
        top_finding_categories=category_totals.most_common(top_n_categories),
        fast_track_count=fast_track,
    )


# ── Formatting ──────────────────────────────────────────────


def format_json(report: Report) -> dict[str, Any]:
    """Return the Report as a JSON-ready dict.

    Stable schema: consumers can rely on this output structure across
    minor versions. Adding fields is safe; renaming/removing is a
    breaking change.
    """
    return {
        "since": report.since,
        "repo_filter": report.repo_filter,
        "totals": {
            "reviews": report.total_reviews,
            "fix_followups": report.total_fix_followups,
            "fast_track_eligible": report.fast_track_count,
            "findings": report.findings_total,
        },
        "decisions": report.decisions,
        "decisions_by_repo": report.decisions_by_repo,
        "fix": {
            "outcomes": report.fix_outcomes,
            "success_rate": report.fix_success_rate,
        },
        "review_seconds": {
            "avg": report.avg_review_seconds,
            "p95": report.p95_review_seconds,
        },
        "findings_by_severity": report.findings_by_severity,
        "top_finding_categories": [
            {"category": cat, "count": count}
            for cat, count in report.top_finding_categories
        ],
    }


def _fmt_int(n: int | None) -> str:
    if n is None:
        return "—"
    return f"{n:,}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def _fmt_seconds(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 90:
        return f"{value:.0f}s"
    return f"{value / 60:.1f}m"


def format_text(report: Report) -> str:
    """Plain-text rendering for terminal output of ``gate report``."""
    lines: list[str] = []
    header_parts = [f"Gate report — last {report.since}" if report.since else "Gate report"]
    if report.repo_filter:
        header_parts.append(f"(repo={report.repo_filter})")
    lines.append(" ".join(header_parts))
    lines.append("=" * 72)
    lines.append(
        f"  Reviews: {_fmt_int(report.total_reviews)}  "
        f"|  Fix followups: {_fmt_int(report.total_fix_followups)}  "
        f"|  Fast-track: {_fmt_int(report.fast_track_count)}"
    )
    lines.append("")
    lines.append("Decisions")
    if report.decisions:
        for decision, count in sorted(
            report.decisions.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            pct = (count / report.total_reviews) if report.total_reviews else 0
            lines.append(
                f"  {decision:<20} {_fmt_int(count):>6}   {pct * 100:5.1f}%"
            )
    else:
        lines.append("  (none)")

    if report.decisions_by_repo:
        lines.append("")
        lines.append("By repo")
        for repo in sorted(report.decisions_by_repo.keys()):
            counts = report.decisions_by_repo[repo]
            total = sum(counts.values())
            inline = "  ".join(
                f"{d}:{c}"
                for d, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            )
            lines.append(f"  {repo}  ({total} total) — {inline}")

    if report.fix_outcomes:
        lines.append("")
        lines.append("Fix pipeline")
        for outcome, count in sorted(
            report.fix_outcomes.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            lines.append(f"  {outcome:<20} {_fmt_int(count):>6}")
        if report.fix_success_rate is not None:
            lines.append(
                f"  success_rate         {_fmt_pct(report.fix_success_rate):>6}"
            )

    lines.append("")
    lines.append("Review time")
    lines.append(f"  avg: {_fmt_seconds(report.avg_review_seconds):<10}"
                 f"  p95: {_fmt_seconds(report.p95_review_seconds)}")

    if report.findings_by_severity:
        lines.append("")
        lines.append(f"Findings ({_fmt_int(report.findings_total)} total)")
        for sev in ("critical", "error", "warning", "info"):
            n = report.findings_by_severity.get(sev, 0)
            if n:
                lines.append(f"  {sev:<10} {_fmt_int(n):>6}")

    if report.top_finding_categories:
        lines.append("")
        lines.append("Top finding categories")
        for cat, count in report.top_finding_categories:
            lines.append(f"  {cat:<24} {_fmt_int(count):>6}")

    return "\n".join(lines)

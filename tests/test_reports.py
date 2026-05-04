"""Tests for ``gate report`` aggregation logic (Phase 1.4)."""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from gate.reports import (
    format_json,
    format_text,
    load_reviews,
    parse_since,
    summarize,
)

# ── Time parsing ────────────────────────────────────────────


class TestParseSince:
    def test_seconds(self):
        assert parse_since("60s") == _dt.timedelta(seconds=60)

    def test_minutes(self):
        assert parse_since("30m") == _dt.timedelta(minutes=30)

    def test_hours(self):
        assert parse_since("24h") == _dt.timedelta(hours=24)

    def test_days(self):
        assert parse_since("7d") == _dt.timedelta(days=7)

    def test_weeks(self):
        assert parse_since("2w") == _dt.timedelta(weeks=2)

    def test_uppercase_unit(self):
        assert parse_since("7D") == _dt.timedelta(days=7)

    def test_whitespace_tolerant(self):
        assert parse_since("  7d  ") == _dt.timedelta(days=7)

    def test_invalid_raises(self):
        for bad in ("", "abc", "7", "d7", "10x"):
            with pytest.raises(ValueError):
                parse_since(bad)


# ── Loading & filtering ─────────────────────────────────────


def _write_reviews(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _ts_ago(td: _dt.timedelta) -> str:
    return (_dt.datetime.now(_dt.UTC) - td).isoformat()


class TestLoadReviews:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_reviews(path=tmp_path / "nope.jsonl") == []

    def test_filters_by_since(self, tmp_path):
        path = tmp_path / "reviews.jsonl"
        _write_reviews(path, [
            {"timestamp": _ts_ago(_dt.timedelta(hours=1)), "decision": "approve"},
            {"timestamp": _ts_ago(_dt.timedelta(days=10)), "decision": "approve"},
        ])
        rows = load_reviews(since=_dt.timedelta(days=2), path=path)
        assert len(rows) == 1

    def test_filters_by_repo(self, tmp_path):
        path = tmp_path / "reviews.jsonl"
        _write_reviews(path, [
            {"timestamp": _ts_ago(_dt.timedelta(hours=1)), "repo": "o/a", "decision": "approve"},
            {"timestamp": _ts_ago(_dt.timedelta(hours=1)), "repo": "o/b", "decision": "approve"},
        ])
        rows = load_reviews(repo="o/a", path=path)
        assert [r["repo"] for r in rows] == ["o/a"]

    def test_skips_malformed_lines(self, tmp_path):
        path = tmp_path / "reviews.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"timestamp": _ts_ago(_dt.timedelta(hours=1)), "decision": "approve"})
            + "\n"
            "this is not json\n"
            + json.dumps({"timestamp": _ts_ago(_dt.timedelta(hours=2)), "decision": "approve"})
            + "\n"
        )
        rows = load_reviews(path=path)
        assert len(rows) == 2


# ── Aggregation ─────────────────────────────────────────────


def _review(decision: str, *, repo: str = "o/r", findings: int = 0,
            severities: dict | None = None,
            categories: list[str] | None = None,
            seconds: int = 30, fast: bool = False) -> dict:
    return {
        "timestamp": _ts_ago(_dt.timedelta(hours=1)),
        "repo": repo,
        "pr": 1,
        "decision": decision,
        "review_time_seconds": seconds,
        "findings": findings,
        "findings_by_severity": severities or {},
        "finding_categories": categories or [],
        "fast_track_eligible": fast,
        "stages_run": 4,
    }


def _fix(outcome: str = "fix_succeeded", *, repo: str = "o/r") -> dict:
    return {
        "timestamp": _ts_ago(_dt.timedelta(hours=1)),
        "repo": repo, "pr": 1, "decision": outcome,
        "is_fix_followup": True, "review_time_seconds": 60,
    }


class TestSummarize:
    def test_empty(self):
        report = summarize([])
        assert report.total_reviews == 0
        assert report.total_fix_followups == 0
        assert report.fix_success_rate is None
        assert report.avg_review_seconds is None

    def test_decision_counts(self):
        rows = [
            _review("approve"),
            _review("approve"),
            _review("request_changes"),
            _review("approve_with_notes"),
        ]
        report = summarize(rows)
        assert report.total_reviews == 4
        assert report.decisions["approve"] == 2
        assert report.decisions["request_changes"] == 1

    def test_decision_by_repo(self):
        rows = [
            _review("approve", repo="o/a"),
            _review("approve", repo="o/a"),
            _review("approve", repo="o/b"),
        ]
        report = summarize(rows)
        assert report.decisions_by_repo["o/a"]["approve"] == 2
        assert report.decisions_by_repo["o/b"]["approve"] == 1

    def test_fix_outcomes_separate_from_reviews(self):
        rows = [
            _review("approve"),
            _fix("fix_succeeded"),
            _fix("fix_failed"),
        ]
        report = summarize(rows)
        assert report.total_reviews == 1
        assert report.total_fix_followups == 2
        assert report.fix_outcomes == {"fix_succeeded": 1, "fix_failed": 1}
        assert report.fix_success_rate == 0.5

    def test_fix_no_op_excluded_from_rate_denominator(self):
        rows = [_fix("fix_succeeded"), _fix("fix_no_op"), _fix("fix_no_op")]
        report = summarize(rows)
        assert report.fix_success_rate == 1.0

    def test_review_time_avg_and_p95(self):
        rows = [_review("approve", seconds=10), _review("approve", seconds=20),
                _review("approve", seconds=30)]
        report = summarize(rows)
        assert report.avg_review_seconds == 20.0
        # With 3 samples sorted [10,20,30], p95 hits the top index → 30.
        assert report.p95_review_seconds == 30.0

    def test_findings_aggregated_across_severities(self):
        rows = [
            _review("request_changes", findings=3,
                    severities={"critical": 1, "error": 2}),
            _review("approve_with_notes", findings=1, severities={"warning": 1}),
        ]
        report = summarize(rows)
        assert report.findings_total == 4
        assert report.findings_by_severity["critical"] == 1
        assert report.findings_by_severity["error"] == 2
        assert report.findings_by_severity["warning"] == 1

    def test_top_finding_categories(self):
        rows = [
            _review("request_changes", categories=["security", "logic"]),
            _review("request_changes", categories=["security", "style"]),
            _review("approve_with_notes", categories=["style"]),
        ]
        report = summarize(rows)
        # security:2, style:2, logic:1
        cats = dict(report.top_finding_categories)
        assert cats["security"] == 2
        assert cats["style"] == 2
        assert cats["logic"] == 1

    def test_fast_track_count(self):
        rows = [
            _review("approve", fast=True),
            _review("approve", fast=False),
            _review("approve", fast=True),
        ]
        report = summarize(rows)
        assert report.fast_track_count == 2


# ── JSON format stability ───────────────────────────────────


class TestFormatJSON:
    def test_top_level_keys_present(self):
        report = summarize([_review("approve"), _fix()])
        payload = format_json(report)
        for key in (
            "since", "repo_filter", "totals", "decisions",
            "decisions_by_repo", "fix", "review_seconds",
            "findings_by_severity", "top_finding_categories",
        ):
            assert key in payload, f"missing JSON key {key}"

    def test_top_finding_categories_is_list_of_objects(self):
        report = summarize([
            _review("request_changes", categories=["security"]),
            _review("request_changes", categories=["security"]),
        ])
        payload = format_json(report)
        assert payload["top_finding_categories"] == [
            {"category": "security", "count": 2}
        ]

    def test_serialisable_to_json(self):
        report = summarize([_review("approve"), _fix()])
        json.dumps(format_json(report))  # no raise


# ── Text format ─────────────────────────────────────────────


class TestFormatText:
    def test_renders_section_headers(self):
        report = summarize(
            [_review("approve"), _review("request_changes"), _fix()],
            since_label="7d",
        )
        out = format_text(report)
        assert "Decisions" in out
        assert "Fix pipeline" in out
        assert "Review time" in out

    def test_renders_empty_report(self):
        out = format_text(summarize([]))
        assert "Reviews:" in out
        assert "(none)" in out

"""Unit tests for gate.tui helper functions (pure logic, no Textual app)."""

import json
import time
from unittest.mock import patch

from gate.tui import (
    _parse_timestamp,
    compute_metrics,
    format_elapsed,
    format_uptime,
    read_recent_reviews,
)


class TestFormatElapsed:
    def test_seconds_only(self):
        now_ms = int(time.time() * 1000)
        assert format_elapsed(now_ms - 45_000) == "45s"

    def test_zero_start_time(self):
        assert format_elapsed(0) == "—"

    def test_minutes_and_seconds(self):
        now_ms = int(time.time() * 1000)
        assert format_elapsed(now_ms - 125_000) == "2m05s"

    def test_exactly_one_minute(self):
        now_ms = int(time.time() * 1000)
        assert format_elapsed(now_ms - 60_000) == "1m00s"

    def test_hours(self):
        now_ms = int(time.time() * 1000)
        assert format_elapsed(now_ms - 3_661_000) == "1h01m"


class TestFormatUptime:
    def test_no_start_time(self):
        assert format_uptime(0) == "—"

    def test_seconds_range(self):
        now_ms = int(time.time() * 1000)
        result = format_uptime(now_ms - 30_000)
        assert result.endswith("s")

    def test_minutes_range(self):
        now_ms = int(time.time() * 1000)
        result = format_uptime(now_ms - 300_000)
        assert result.endswith("m")


class TestParseTimestamp:
    def test_epoch_int(self):
        ts = 1713000000
        result = _parse_timestamp(ts)
        assert result == 1713000000.0

    def test_epoch_float(self):
        ts = 1713000000.123
        result = _parse_timestamp(ts)
        assert result == 1713000000.123

    def test_too_small_epoch(self):
        assert _parse_timestamp(12345) == 0.0

    def test_iso_with_timezone(self):
        result = _parse_timestamp("2024-04-13T10:30:00+00:00")
        assert result > 0

    def test_iso_with_z(self):
        result = _parse_timestamp("2024-04-13T10:30:00Z")
        assert result > 0

    def test_iso_with_microseconds(self):
        result = _parse_timestamp("2024-04-13T10:30:00.123456+00:00")
        assert result > 0

    def test_invalid_string(self):
        assert _parse_timestamp("not-a-date") == 0.0

    def test_none_returns_zero(self):
        assert _parse_timestamp(None) == 0.0


class TestReadRecentReviews:
    def test_no_file(self, tmp_path):
        with patch("gate.tui.REVIEWS_JSONL", tmp_path / "nonexistent.jsonl"):
            assert read_recent_reviews(8) == []

    def test_reads_entries(self, tmp_path):
        jsonl_path = tmp_path / "reviews.jsonl"
        entries = [
            {"pr": i, "decision": "approve", "timestamp": 1713000000 + i}
            for i in range(10)
        ]
        jsonl_path.write_text("\n".join(json.dumps(e) for e in entries))

        with patch("gate.tui.REVIEWS_JSONL", jsonl_path):
            result = read_recent_reviews(3)
        assert len(result) == 3
        assert result[0]["pr"] == 9

    def test_handles_malformed_lines(self, tmp_path):
        jsonl_path = tmp_path / "reviews.jsonl"
        jsonl_path.write_text('{"pr": 1}\n{bad json}\n{"pr": 2}\n')
        with patch("gate.tui.REVIEWS_JSONL", jsonl_path):
            result = read_recent_reviews(5)
        assert len(result) == 2


class TestComputeMetrics:
    def test_no_file(self, tmp_path):
        with patch("gate.tui.REVIEWS_JSONL", tmp_path / "nonexistent.jsonl"):
            m = compute_metrics()
        assert m["total"] == 0

    def test_computes_from_entries(self, tmp_path):
        jsonl_path = tmp_path / "reviews.jsonl"
        now = time.time()
        entries = [
            {"pr": 1, "decision": "approve", "timestamp": now - 100,
             "review_time_seconds": 60, "findings": 0},
            {"pr": 2, "decision": "approve", "timestamp": now - 200,
             "review_time_seconds": 120, "findings": 1},
            {"pr": 3, "decision": "request_changes", "timestamp": now - 300,
             "review_time_seconds": 180, "findings": 3},
        ]
        jsonl_path.write_text("\n".join(json.dumps(e) for e in entries))

        with patch("gate.tui.REVIEWS_JSONL", jsonl_path):
            m = compute_metrics()
        assert m["total"] == 3
        assert m["approved_pct"] > 0

    def test_ignores_old_entries(self, tmp_path):
        jsonl_path = tmp_path / "reviews.jsonl"
        old_ts = time.time() - 100_000
        entries = [
            {"pr": 1, "decision": "approve", "timestamp": old_ts, "review_time_seconds": 60},
        ]
        jsonl_path.write_text("\n".join(json.dumps(e) for e in entries))

        with patch("gate.tui.REVIEWS_JSONL", jsonl_path):
            m = compute_metrics()
        assert m["total"] == 0

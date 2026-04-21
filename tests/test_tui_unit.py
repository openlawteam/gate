"""Unit tests for gate.tui helper functions (pure logic, no Textual app)."""

import json
import time
from unittest.mock import patch

from gate.tui import (
    DECISION_COLORS,
    DECISION_ICONS,
    STAGE_COLORS,
    CompletedDetailScreen,
    _parse_timestamp,
    _sanitize_pane_line,
    compute_metrics,
    format_decision,
    format_elapsed,
    format_fix_pipeline,
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
        with patch("gate.tui.reviews_jsonl", lambda _p=tmp_path / "nonexistent.jsonl": _p):
            assert read_recent_reviews(8) == []

    def test_reads_entries(self, tmp_path):
        jsonl_path = tmp_path / "reviews.jsonl"
        entries = [
            {"pr": i, "decision": "approve", "timestamp": 1713000000 + i}
            for i in range(10)
        ]
        jsonl_path.write_text("\n".join(json.dumps(e) for e in entries))

        with patch("gate.tui.reviews_jsonl", lambda _p=jsonl_path: _p):
            result = read_recent_reviews(3)
        assert len(result) == 3
        assert result[0]["pr"] == 9

    def test_handles_malformed_lines(self, tmp_path):
        jsonl_path = tmp_path / "reviews.jsonl"
        jsonl_path.write_text('{"pr": 1}\n{bad json}\n{"pr": 2}\n')
        with patch("gate.tui.reviews_jsonl", lambda _p=jsonl_path: _p):
            result = read_recent_reviews(5)
        assert len(result) == 2


class TestComputeMetrics:
    def test_no_file(self, tmp_path):
        with patch("gate.tui.reviews_jsonl", lambda _p=tmp_path / "nonexistent.jsonl": _p):
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

        with patch("gate.tui.reviews_jsonl", lambda _p=jsonl_path: _p):
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

        with patch("gate.tui.reviews_jsonl", lambda _p=jsonl_path: _p):
            m = compute_metrics()
        assert m["total"] == 0


class TestFormatFixPipeline:
    def test_known_stage_highlights(self):
        result = format_fix_pipeline("fix-session")
        plain = result.plain
        assert "[ses]" in plain
        assert "boo" in plain

    def test_fix_senior_maps_to_fix_session(self):
        result = format_fix_pipeline("fix-senior")
        plain = result.plain
        assert "[ses]" in plain

    def test_unknown_stage_no_highlight(self):
        result = format_fix_pipeline("unknown-stage")
        plain = result.plain
        assert "[" not in plain

    def test_all_stages_present(self):
        result = format_fix_pipeline("fix-bootstrap")
        plain = result.plain
        for abbrev in ("boo", "ses", "bui", "rer", "com"):
            assert abbrev in plain


class TestSanitizePaneLine:
    def test_strips_carriage_return(self):
        assert _sanitize_pane_line("hello\rworld") == "helloworld"

    def test_strips_backspace_and_escape(self):
        # \x1b (ESC), \x08 (BS), \x07 (BEL) are all in the stripped range
        assert _sanitize_pane_line("\x1b[K\x07clean\x08") == "[Kclean"

    def test_preserves_tab(self):
        assert _sanitize_pane_line("tab\there") == "tab\there"

    def test_preserves_newline(self):
        assert _sanitize_pane_line("line\n") == "line\n"

    def test_preserves_printable_ascii(self):
        s = "Hello, World! 123 @#$%"
        assert _sanitize_pane_line(s) == s

    def test_preserves_unicode(self):
        # Box-drawing glyph and accented char should pass through unchanged
        s = "caf\u00e9 \u2500\u2500\u2500"
        assert _sanitize_pane_line(s) == s

    def test_truncates_long_line(self):
        assert _sanitize_pane_line("a" * 100, width=10) == "aaaaaaaaaa..."

    def test_no_truncation_when_under_width(self):
        assert _sanitize_pane_line("short", width=100) == "short"

    def test_empty_line(self):
        assert _sanitize_pane_line("") == ""

    def test_truncation_uses_default_width_72(self):
        line = "x" * 80
        result = _sanitize_pane_line(line)
        assert result.endswith("...")
        assert len(result) == 75  # 72 chars + "..."

    def test_strips_form_feed_vertical_tab(self):
        # \x0b (VT), \x0c (FF) are in the stripped range
        assert _sanitize_pane_line("a\x0bb\x0cc") == "abc"

    def test_strips_null_byte(self):
        assert _sanitize_pane_line("foo\x00bar") == "foobar"


class TestDecisionRegistry:
    """Pin the decision registry so logger-emitted decisions always
    have a TUI-side icon and color. Legacy polish_legacy entries
    wrote ``fix_succeeded`` / ``fix_failed`` only; PR #18 added
    ``fix_no_op`` for the graceful-no-op path (and
    hopper-senior-declared-clean), which previously fell through to
    the ``?`` fallback in the reviews table."""

    def test_fix_no_op_has_icon(self):
        # Must not fall through to the "?" fallback.
        assert DECISION_ICONS["fix_no_op"] != "?"
        assert DECISION_ICONS["fix_no_op"].strip() != ""

    def test_fix_no_op_has_color(self):
        assert DECISION_COLORS["fix_no_op"] == "dim"

    def test_all_logger_decisions_present(self):
        # Mirrors the ``status → decision`` map in
        # ``gate.logger.log_fix_result``.
        for key in ("fix_succeeded", "fix_failed", "fix_no_op"):
            assert key in DECISION_ICONS
            assert key in DECISION_COLORS

    def test_format_decision_for_fix_no_op(self):
        result = format_decision("fix_no_op")
        assert "fix no op" in result.plain  # underscores stripped
        assert result.plain.startswith(DECISION_ICONS["fix_no_op"])


class TestStageColors:
    def test_fix_polish_registered(self):
        """``fix-polish`` is emitted by the polish self-audit
        (``fixer_polish.py``). Previously missing from
        ``STAGE_COLORS``, so it rendered unstyled in the log."""
        assert "fix-polish" in STAGE_COLORS


class TestBuildFixInfo:
    """Behaviour of ``CompletedDetailScreen._build_fix_info`` — the
    panel shown when the user opens a fix-followup entry from the
    reviews table. Targets the PR #18/#19 telemetry that was being
    written to ``reviews.jsonl`` but not surfaced in the modal."""

    def _render(self, entry: dict) -> str:
        from rich.text import Text
        screen = CompletedDetailScreen.__new__(CompletedDetailScreen)
        screen._entry = entry
        return screen._build_fix_info(entry, Text()).plain

    def test_legacy_entry_renders_core_fields(self):
        out = self._render({
            "decision": "fix_succeeded",
            "original_decision": "approve_with_notes",
            "review_time_seconds": 42,
            "fix_summary": "auto-fix 1/1",
            "is_fix_followup": True,
        })
        assert "fix succeeded" in out
        assert "approve_with_notes" in out
        assert "42s" in out
        assert "auto-fix 1/1" in out
        # Legacy entries don't carry these fields — they must not
        # accidentally print placeholder text.
        assert "Mode:" not in out
        assert "Sub-scopes:" not in out
        assert "Commit Message" not in out

    def test_hopper_entry_renders_subscopes(self):
        out = self._render({
            "decision": "fix_succeeded",
            "original_decision": "approve_with_notes",
            "review_time_seconds": 100,
            "fix_summary": "auto-fix 3/4",
            "pipeline_mode": "hopper",
            "sub_scope_total": 4,
            "sub_scope_committed": 3,
            "sub_scope_reverted": 1,
            "sub_scope_empty": 0,
            "fixed_count": 3,
            "not_fixed_count": 1,
            "commit_message_source": "senior",
            "is_fix_followup": True,
        })
        assert "Mode:" in out
        assert "hopper" in out
        assert "Sub-scopes:" in out
        assert "3/4" in out
        assert "1 reverted" in out
        assert "Commit Message" in out
        assert "senior" in out
        assert "Findings:" in out
        assert "3 fixed" in out
        assert "1 not fixed" in out

    def test_synthesized_commit_shows_reject_reason(self):
        out = self._render({
            "decision": "fix_succeeded",
            "original_decision": "approve_with_notes",
            "review_time_seconds": 30,
            "fix_summary": "auto-fix",
            "commit_message_source": "synthesized",
            "commit_message_reject_reason": "too_short",
            "is_fix_followup": True,
        })
        assert "Source: synthesized" in out
        assert "Reject: too_short" in out

    def test_runaway_guard_is_prominent(self):
        out = self._render({
            "decision": "fix_failed",
            "original_decision": "approve_with_notes",
            "review_time_seconds": 600,
            "fix_summary": "runaway",
            "pipeline_mode": "hopper",
            "sub_scope_total": 5,
            "sub_scope_committed": 1,
            "runaway_guard_hit": True,
            "is_fix_followup": True,
        })
        assert "Runaway:" in out
        assert "guard hit" in out

    def test_wall_clock_hidden_when_equal_to_duration(self):
        out = self._render({
            "decision": "fix_succeeded",
            "original_decision": "approve_with_notes",
            "review_time_seconds": 50,
            "wall_clock_seconds": 50,
            "fix_summary": "auto-fix",
            "pipeline_mode": "hopper",
            "is_fix_followup": True,
        })
        assert "Wall clock:" not in out

    def test_wall_clock_surfaced_when_different(self):
        out = self._render({
            "decision": "fix_succeeded",
            "original_decision": "approve_with_notes",
            "review_time_seconds": 50,
            "wall_clock_seconds": 120,
            "fix_summary": "auto-fix",
            "pipeline_mode": "hopper",
            "is_fix_followup": True,
        })
        assert "Wall clock:" in out
        assert "120s" in out

    def test_fix_no_op_entry_renders(self):
        """The exact entry shape produced by a graceful no-op —
        previously rendered ``?`` as its decision icon."""
        out = self._render({
            "decision": "fix_no_op",
            "original_decision": "approve_with_notes",
            "review_time_seconds": 5,
            "fix_summary": "no mechanical work required",
            "pipeline_mode": "hopper",
            "is_fix_followup": True,
        })
        assert "fix no op" in out
        assert "no mechanical work required" in out

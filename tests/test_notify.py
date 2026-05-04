"""Tests for gate.notify module."""

from unittest.mock import patch

from gate.notify import (
    circuit_breaker,
    fix_complete,
    fix_failed,
    fix_started,
    notify,
    notify_discord,
    review_complete,
    review_failed,
)


def _captured_titles(mock_send_to) -> list[str]:
    """Pull the ``title`` kwarg from every ``_send_to`` call."""
    return [call.kwargs["title"] for call in mock_send_to.call_args_list]


class TestNotify:
    def test_noop_without_topic(self):
        with patch.dict("os.environ", {}, clear=True):
            notify("Test", "Body")

    @patch("gate.notify.urllib.request.urlopen")
    def test_sends_with_topic(self, mock_urlopen):
        with patch.dict("os.environ", {"GATE_NTFY_TOPIC": "test-topic"}):
            notify("Test Title", "Test Body")
            mock_urlopen.assert_called_once()


class TestNotifyDiscord:
    def test_noop_without_webhook(self):
        with patch.dict("os.environ", {}, clear=True):
            notify_discord("Test", "Body")

    @patch("gate.notify.urllib.request.urlopen")
    def test_sends_with_webhook(self, mock_urlopen):
        with patch.dict("os.environ", {"GATE_DISCORD_WEBHOOK": "https://discord.com/webhook"}):
            notify_discord("Test", "Body", color=3066993)
            mock_urlopen.assert_called_once()


class TestConvenienceWrappers:
    @patch("gate.notify._send_to")
    def test_review_complete_approve(self, mock_send):
        with patch.dict("os.environ", {"GATE_NTFY_TOPIC": "default-topic"}):
            verdict = {"decision": "approve", "summary": "OK", "stats": {"total_findings": 0}}
            review_complete(42, verdict)
        titles = _captured_titles(mock_send)
        assert any("approved" in t for t in titles), titles

    @patch("gate.notify._send_to")
    def test_review_complete_request_changes(self, mock_send):
        with patch.dict("os.environ", {"GATE_NTFY_TOPIC": "default-topic"}):
            verdict = {
                "decision": "request_changes",
                "summary": "Issues",
                "stats": {"total_findings": 3},
            }
            review_complete(42, verdict)
        titles = _captured_titles(mock_send)
        assert any("blocked" in t for t in titles), titles

    @patch("gate.notify._send_to")
    def test_review_failed(self, mock_send):
        with patch.dict("os.environ", {"GATE_NTFY_TOPIC": "default-topic"}):
            review_failed(42, "crash")
        titles = _captured_titles(mock_send)
        assert any("FAILED" in t for t in titles), titles

    @patch("gate.notify._send_to")
    def test_circuit_breaker(self, mock_send):
        with patch.dict("os.environ", {"GATE_NTFY_TOPIC": "default-topic"}):
            circuit_breaker(42)
        titles = _captured_titles(mock_send)
        assert any("circuit breaker" in t for t in titles), titles

    @patch("gate.notify._send_to")
    def test_fix_started(self, mock_send):
        with patch.dict("os.environ", {"GATE_NTFY_TOPIC": "default-topic"}):
            fix_started(42, 5, "high")
        titles = _captured_titles(mock_send)
        assert any("auto-fix started" in t for t in titles), titles

    @patch("gate.notify._send_to")
    def test_fix_complete(self, mock_send):
        with patch.dict("os.environ", {"GATE_NTFY_TOPIC": "default-topic"}):
            fix_complete(42, 3, 5, 2)
        titles = _captured_titles(mock_send)
        assert any("auto-fix complete" in t for t in titles), titles

    @patch("gate.notify._send_to")
    def test_fix_failed(self, mock_send):
        with patch.dict("os.environ", {"GATE_NTFY_TOPIC": "default-topic"}):
            fix_failed(42, "timeout", 1)
        titles = _captured_titles(mock_send)
        assert any("auto-fix failed" in t for t in titles), titles

    @patch("gate.notify._send_to")
    def test_noop_when_no_targets(self, mock_send):
        """No env defaults, no routes config → no targets → no send."""
        with patch.dict("os.environ", {}, clear=True):
            review_complete(42, {"decision": "approve", "summary": "OK", "stats": {}})
        assert mock_send.call_count == 0

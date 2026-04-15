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
    @patch("gate.notify.notify")
    @patch("gate.notify.notify_discord")
    def test_review_complete_approve(self, mock_discord, mock_ntfy):
        verdict = {"decision": "approve", "summary": "OK", "stats": {"total_findings": 0}}
        review_complete(42, verdict)
        mock_ntfy.assert_called_once()
        assert "approved" in mock_ntfy.call_args[0][0]

    @patch("gate.notify.notify")
    @patch("gate.notify.notify_discord")
    def test_review_complete_request_changes(self, mock_discord, mock_ntfy):
        verdict = {
            "decision": "request_changes",
            "summary": "Issues",
            "stats": {"total_findings": 3},
        }
        review_complete(42, verdict)
        assert "blocked" in mock_ntfy.call_args[0][0]

    @patch("gate.notify.notify")
    @patch("gate.notify.notify_discord")
    def test_review_failed(self, mock_discord, mock_ntfy):
        review_failed(42, "crash")
        assert "FAILED" in mock_ntfy.call_args[0][0]

    @patch("gate.notify.notify")
    @patch("gate.notify.notify_discord")
    def test_circuit_breaker(self, mock_discord, mock_ntfy):
        circuit_breaker(42)
        assert "circuit breaker" in mock_ntfy.call_args[0][0]

    @patch("gate.notify.notify")
    @patch("gate.notify.notify_discord")
    def test_fix_started(self, mock_discord, mock_ntfy):
        fix_started(42, 5, "high")
        assert "auto-fix started" in mock_ntfy.call_args[0][0]

    @patch("gate.notify.notify")
    @patch("gate.notify.notify_discord")
    def test_fix_complete(self, mock_discord, mock_ntfy):
        fix_complete(42, 3, 5, 2)
        assert "auto-fix complete" in mock_ntfy.call_args[0][0]

    @patch("gate.notify.notify")
    @patch("gate.notify.notify_discord")
    def test_fix_failed(self, mock_discord, mock_ntfy):
        fix_failed(42, "timeout", 1)
        assert "auto-fix failed" in mock_ntfy.call_args[0][0]

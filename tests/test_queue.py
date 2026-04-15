"""Tests for gate.queue module."""

import time
from unittest.mock import MagicMock, patch

import pytest

from gate.queue import ReviewQueue


@pytest.fixture
def review_queue(sample_config, tmp_path):
    """Create a ReviewQueue with mocked orchestrator."""
    sock_path = tmp_path / "test.sock"
    with patch("gate.queue.ReviewOrchestrator"):
        q = ReviewQueue(config=sample_config, socket_path=sock_path)
        yield q
        if q._running:
            q.stop()


class TestEnqueue:
    def test_enqueue_adds_to_queue(self, review_queue):
        review_queue.enqueue(42, "repo", "sha123", "synchronize", "main", [])
        assert not review_queue._queue.empty()

    def test_enqueue_fifo_order(self, review_queue):
        review_queue.enqueue(1, "r", "s1", "sync", "b1", [])
        review_queue.enqueue(2, "r", "s2", "sync", "b2", [])
        review_queue.enqueue(3, "r", "s3", "sync", "b3", [])

        items = []
        while not review_queue._queue.empty():
            priority, pr, kwargs = review_queue._queue.get()
            items.append(pr)
        assert items == [1, 2, 3]

    def test_repush_cancels_active(self, review_queue):
        mock_orch = MagicMock()
        with review_queue._lock:
            review_queue._active[("repo", 42)] = mock_orch

        review_queue.enqueue(42, "repo", "sha456", "synchronize", "main", [])
        mock_orch.cancel.assert_called_once()
        assert ("repo", 42) not in review_queue._active


class TestCancelPr:
    def test_cancel_active_review_with_repo(self, review_queue):
        mock_orch = MagicMock()
        with review_queue._lock:
            review_queue._active[("repo", 42)] = mock_orch

        assert review_queue.cancel_pr(42, "repo") is True
        mock_orch.cancel.assert_called_once()
        assert ("repo", 42) not in review_queue._active

    def test_cancel_legacy_no_repo(self, review_queue):
        mock_orch = MagicMock()
        with review_queue._lock:
            review_queue._active[("repo", 42)] = mock_orch

        assert review_queue.cancel_pr(42) is True
        mock_orch.cancel.assert_called_once()

    def test_cancel_nonexistent_review(self, review_queue):
        assert review_queue.cancel_pr(999) is False


class TestGetActiveReviews:
    def test_returns_snapshot(self, review_queue):
        mock_orch = MagicMock()
        with review_queue._lock:
            review_queue._active[("repo", 42)] = mock_orch

        result = review_queue.get_active_reviews()
        assert ("repo", 42) in result
        assert result is not review_queue._active


class TestDispatcher:
    @patch("gate.queue.quota_mod")
    @patch("gate.queue.ReviewOrchestrator")
    def test_dispatches_when_quota_ok(self, MockOrch, mock_quota, sample_config, tmp_path):
        mock_quota.check_quota.return_value = {"quota_ok": True}
        mock_instance = MagicMock()
        MockOrch.return_value = mock_instance

        sock_path = tmp_path / "test.sock"
        q = ReviewQueue(config=sample_config, socket_path=sock_path)
        q.start()

        q.enqueue(42, "test-org/test-repo", "sha123", "synchronize", "main", [])
        time.sleep(0.5)

        MockOrch.assert_called_once()
        q.stop()

    @patch("gate.queue.quota_mod")
    @patch("gate.queue.ReviewOrchestrator")
    def test_defers_on_low_quota(self, MockOrch, mock_quota, sample_config, tmp_path):
        mock_quota.check_quota.return_value = {"quota_ok": False}

        sock_path = tmp_path / "test.sock"
        q = ReviewQueue(config=sample_config, socket_path=sock_path)
        q.start()

        q.enqueue(42, "test-org/test-repo", "sha123", "synchronize", "main", [])
        time.sleep(0.3)

        MockOrch.assert_not_called()
        assert q._deferred_until > 0
        q.stop()


class TestStop:
    def test_stop_shuts_down_pool(self, review_queue):
        review_queue.start()
        review_queue.stop()
        assert not review_queue._running

    def test_stop_joins_dispatcher(self, review_queue):
        review_queue.start()
        assert review_queue._dispatcher.is_alive()
        review_queue.stop()
        time.sleep(0.1)
        assert not review_queue._dispatcher.is_alive()

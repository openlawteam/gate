"""Tests for gate.queue module."""

import time
from unittest.mock import MagicMock, patch

import pytest

from gate.queue import DEFAULT_MAX_CONCURRENT, ReviewQueue


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
            priority, pr, _token, kwargs = review_queue._queue.get()
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

    def test_cancel_pr_default_reason_is_manual(self, review_queue):
        """Issue #17: ``gate cancel`` routes through ``cancel_pr`` with
        no reason override — the default must be ``"manual"`` so the
        GitHub check lands as ``neutral``, not ``Superseded by newer
        push``."""
        mock_orch = MagicMock()
        with review_queue._lock:
            review_queue._active[("repo", 42)] = mock_orch

        assert review_queue.cancel_pr(42, "repo") is True
        mock_orch.cancel.assert_called_once_with(reason="manual")

    def test_cancel_pr_forwards_explicit_reason(self, review_queue):
        """Callers that know the reason (e.g. a timeout watchdog) must
        be able to override the default."""
        mock_orch = MagicMock()
        with review_queue._lock:
            review_queue._active[("repo", 42)] = mock_orch

        assert review_queue.cancel_pr(42, "repo", reason="timeout") is True
        mock_orch.cancel.assert_called_once_with(reason="timeout")

    def test_cancel_pr_legacy_path_uses_manual_default(self, review_queue):
        """The no-repo legacy lookup must also default to ``manual``."""
        mock_orch = MagicMock()
        with review_queue._lock:
            review_queue._active[("repo", 42)] = mock_orch

        assert review_queue.cancel_pr(42) is True
        mock_orch.cancel.assert_called_once_with(reason="manual")


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


class TestConfigurableConcurrency:
    """Workstream 4b: max_concurrent_reviews is read from config.limits."""

    def test_defaults_to_three(self, sample_config, tmp_path):
        with patch("gate.queue.ReviewOrchestrator"):
            q = ReviewQueue(
                config=sample_config, socket_path=tmp_path / "s.sock"
            )
            assert q._pool._max_workers == DEFAULT_MAX_CONCURRENT

    def test_respects_config_override(self, sample_config, tmp_path):
        cfg = dict(sample_config)
        cfg["limits"] = dict(cfg.get("limits", {}))
        cfg["limits"]["max_concurrent_reviews"] = 5
        with patch("gate.queue.ReviewOrchestrator"):
            q = ReviewQueue(config=cfg, socket_path=tmp_path / "s.sock")
            assert q._pool._max_workers == 5


class TestDispatchCancelsSupersededReview:
    """Workstream 1 regression guard: when `_dispatch_loop` picks up a queue
    item for a ``(repo, pr)`` key that is already in ``_active`` (because a
    newer enqueue landed first), the older orchestrator must be cancelled
    before the newer one overwrites the slot."""

    @patch("gate.queue.quota_mod")
    @patch("gate.queue.ReviewOrchestrator")
    def test_cancels_existing_active_before_overwrite(
        self, MockOrch, mock_quota, sample_config, tmp_path
    ):
        import threading

        mock_quota.check_quota.return_value = {"quota_ok": True}
        new_orch = MagicMock()
        # Block ``run`` so the new orchestrator remains in ``_active`` long
        # enough for us to observe the supersede.
        release = threading.Event()
        new_orch.run.side_effect = lambda: release.wait(timeout=1.0)
        MockOrch.return_value = new_orch

        sock_path = tmp_path / "s.sock"
        q = ReviewQueue(config=sample_config, socket_path=sock_path)

        # Simulate the race: an older orchestrator is already registered in
        # ``_active`` for the same key (e.g. still running a prior review),
        # and a queue item for the same PR is sitting on ``_queue`` waiting
        # for the dispatcher.
        older_orch = MagicMock()
        key = ("test-org/test-repo", 42)
        with q._lock:
            q._active[key] = older_orch
            q._enqueue_counter += 1
            token = q._enqueue_counter
            q._latest_token[key] = token

        q._queue.put((
            time.time(), 42, token,
            {"repo": "test-org/test-repo", "head_sha": "sha",
             "event": "sync", "branch": "main", "labels": []},
        ))

        q.start()
        time.sleep(0.4)

        older_orch.cancel.assert_called_once()
        assert q._active[key] is new_orch
        release.set()
        q.stop()


class TestDuplicateEnqueueSupersede:
    """Two ``enqueue`` calls for the same PR within the same tick must
    result in a single orchestrator being dispatched, not two racing on
    the same worktree path. See PR post-fix-push double-webhook race."""

    @patch("gate.queue.quota_mod")
    @patch("gate.queue.ReviewOrchestrator")
    def test_back_to_back_enqueues_dispatch_once(
        self, MockOrch, mock_quota, sample_config, tmp_path
    ):
        import threading

        mock_quota.check_quota.return_value = {"quota_ok": True}
        release = threading.Event()

        orchestrators = []

        def make_orch(*args, **kwargs):
            m = MagicMock()
            m.run.side_effect = lambda: release.wait(timeout=1.0)
            orchestrators.append(m)
            return m

        MockOrch.side_effect = make_orch

        q = ReviewQueue(config=sample_config, socket_path=tmp_path / "s.sock")

        # Two enqueues back-to-back with the dispatcher quiesced. Only the
        # most recent token survives in ``_latest_token``, so the first
        # queue entry must be dropped as superseded at dispatch time.
        q.enqueue(42, "test-org/test-repo", "sha1", "sync", "main", [])
        q.enqueue(42, "test-org/test-repo", "sha2", "sync", "main", [])

        q.start()
        try:
            # Give the dispatcher time to drain both queue entries.
            for _ in range(50):
                if orchestrators:
                    break
                time.sleep(0.02)
            # Only one orchestrator should ever be constructed.
            time.sleep(0.1)
            assert len(orchestrators) == 1, (
                f"expected 1 orchestrator, got {len(orchestrators)}"
            )
            # And it should be for the newer head_sha.
            call_kwargs = MockOrch.call_args_list[0].kwargs
            assert call_kwargs["head_sha"] == "sha2"
        finally:
            release.set()
            q.stop()


class TestRunReviewIdentityAwarePop:
    """Workstream 1 regression guard: ``_run_review`` must only remove its
    own orchestrator from ``_active`` -- never a newer replacement."""

    def test_does_not_remove_replacement_entry(self, review_queue):
        key = ("repo", 7)
        older = MagicMock()
        older.run.return_value = None
        newer = MagicMock()

        # Simulate the state after a supersede: newer has replaced older in
        # ``_active``, but the older is still finishing its run.
        with review_queue._lock:
            review_queue._active[key] = newer

        review_queue._run_review(key, older)

        assert review_queue._active[key] is newer, (
            "older _run_review finally block must not evict the newer entry"
        )

    def test_removes_own_entry(self, review_queue):
        key = ("repo", 8)
        orch = MagicMock()
        orch.run.return_value = None
        with review_queue._lock:
            review_queue._active[key] = orch

        review_queue._run_review(key, orch)

        assert key not in review_queue._active

    def test_handles_exception_and_removes_own_entry(self, review_queue):
        key = ("repo", 9)
        orch = MagicMock()
        orch.run.side_effect = RuntimeError("boom")
        with review_queue._lock:
            review_queue._active[key] = orch

        review_queue._run_review(key, orch)

        assert key not in review_queue._active


class TestRapidTriplePush:
    """Workstream 1 end-to-end: three consecutive enqueues for the same PR
    must leave exactly one orchestrator active; the prior two must have had
    ``cancel`` called (either by ``enqueue`` or by ``_dispatch_loop``)."""

    @patch("gate.queue.quota_mod")
    @patch("gate.queue.ReviewOrchestrator")
    def test_only_latest_survives(
        self, MockOrch, mock_quota, sample_config, tmp_path
    ):
        import threading

        mock_quota.check_quota.return_value = {"quota_ok": True}
        made: list[MagicMock] = []
        # Block every orchestrator's ``run`` so the active entry persists and
        # we can observe supersede behavior across the sequence.
        release = threading.Event()

        def _factory(*args, **kwargs):
            m = MagicMock()
            m.run.side_effect = lambda: release.wait(timeout=1.0)
            made.append(m)
            return m

        MockOrch.side_effect = _factory

        q = ReviewQueue(config=sample_config, socket_path=tmp_path / "s.sock")
        q.start()

        for sha in ("sha1", "sha2", "sha3"):
            q.enqueue(42, "test-org/test-repo", sha, "synchronize", "main", [])

        # Give the dispatcher enough time to pull all three items.
        time.sleep(0.8)

        # One active review remains; its orchestrator must be the most
        # recently constructed. All earlier orchestrators must have been
        # cancelled.
        assert len(q._active) == 1
        assert q._active[("test-org/test-repo", 42)] is made[-1]
        for earlier in made[:-1]:
            earlier.cancel.assert_called()

        release.set()
        q.stop()


class TestCancelDuringDispatch:
    """Workstream 1: ``cancel_pr`` while a review is active must cancel the
    live orchestrator and remove it from ``_active``."""

    @patch("gate.queue.quota_mod")
    @patch("gate.queue.ReviewOrchestrator")
    def test_cancel_pr_reaches_live_orchestrator(
        self, MockOrch, mock_quota, sample_config, tmp_path
    ):
        import threading

        mock_quota.check_quota.return_value = {"quota_ok": True}
        orch = MagicMock()
        # Make ``run`` block long enough that we can observe the active entry.
        ready = threading.Event()
        orch.run.side_effect = lambda: ready.wait(timeout=1.0)
        MockOrch.return_value = orch

        q = ReviewQueue(config=sample_config, socket_path=tmp_path / "s.sock")
        q.start()
        q.enqueue(99, "test-org/test-repo", "sha", "synchronize", "main", [])

        time.sleep(0.3)
        assert ("test-org/test-repo", 99) in q._active

        assert q.cancel_pr(99, "test-org/test-repo") is True
        orch.cancel.assert_called_once()
        assert ("test-org/test-repo", 99) not in q._active

        ready.set()
        q.stop()

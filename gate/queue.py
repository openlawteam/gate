"""Review queue with concurrency control.

Manages concurrent PR reviews via a thread pool. Ensures only one review
per PR (re-push cancels in-flight).
"""

import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from gate import quota as quota_mod
from gate.config import load_config, resolve_repo_config
from gate.config import socket_path as _default_socket_path
from gate.orchestrator import ReviewOrchestrator

logger = logging.getLogger(__name__)

MAX_CONCURRENT_REVIEWS = 3


class ReviewQueue:
    """Priority queue for PR reviews with concurrent execution.

    Uses a thread pool so multiple PRs can be reviewed simultaneously.
    Each PR gets its own thread, worktree, and tmux windows.
    Re-push for the same PR cancels any in-flight review.
    """

    def __init__(self, config: dict | None = None, socket_path: Path | None = None):
        self.config = config or load_config()
        self._socket_path = socket_path or _default_socket_path()
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._active: dict[tuple[str, int], ReviewOrchestrator] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_REVIEWS, thread_name_prefix="gate-review"
        )
        self._dispatcher = threading.Thread(target=self._dispatch_loop, daemon=True)
        self._running = False
        self._deferred_until: float = 0.0

    def start(self) -> None:
        """Start the dispatcher thread."""
        self._running = True
        self._dispatcher.start()
        logger.debug("Review queue started")

    def stop(self) -> None:
        """Signal dispatcher to stop (best-effort, doesn't kill active reviews)."""
        self._running = False
        self._pool.shutdown(wait=False)
        if self._dispatcher.is_alive():
            self._dispatcher.join(timeout=2.0)

    def enqueue(
        self,
        pr_number: int,
        repo: str,
        head_sha: str,
        event: str,
        branch: str,
        labels: list[str],
    ) -> None:
        """Add a review to the queue. Cancels existing review for same PR+repo."""
        key = (repo, pr_number)
        with self._lock:
            if key in self._active:
                logger.info(f"Cancelling in-flight review for {repo} PR #{pr_number}")
                self._active[key].cancel()
                del self._active[key]

        self._queue.put((
            time.time(),
            pr_number,
            {
                "repo": repo,
                "head_sha": head_sha,
                "event": event,
                "branch": branch,
                "labels": labels,
            },
        ))
        logger.info(f"Enqueued review for {repo} PR #{pr_number}")

    def get_active_reviews(self) -> dict[tuple[str, int], ReviewOrchestrator]:
        """Return snapshot of active reviews."""
        with self._lock:
            return dict(self._active)

    def cancel_pr(self, pr_number: int, repo: str = "") -> bool:
        """Cancel an in-progress review for a PR."""
        with self._lock:
            if repo:
                key = (repo, pr_number)
                if key in self._active:
                    self._active[key].cancel()
                    del self._active[key]
                    return True
            else:
                for key in list(self._active):
                    if key[1] == pr_number:
                        self._active[key].cancel()
                        del self._active[key]
                        return True
        return False

    def _dispatch_loop(self) -> None:
        """Dispatch queued reviews to the thread pool."""
        while self._running:
            if time.time() < self._deferred_until:
                time.sleep(1.0)
                continue

            try:
                priority, pr_number, kwargs = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            quota = quota_mod.check_quota()
            if not quota["quota_ok"]:
                logger.info("Quota low, deferring all dispatch for 60s")
                self._deferred_until = time.time() + 60
                self._queue.put((priority, pr_number, kwargs))
                continue

            repo = kwargs.get("repo", "")
            try:
                orch_config = resolve_repo_config(repo, self.config)
            except ValueError:
                logger.error(f"No config for repo {repo}, skipping PR #{pr_number}")
                continue

            orchestrator = ReviewOrchestrator(
                pr_number=pr_number,
                config=orch_config,
                socket_path=self._socket_path,
                **kwargs,
            )
            key = (repo, pr_number)
            with self._lock:
                self._active[key] = orchestrator
            self._pool.submit(self._run_review, key, orchestrator)

    def _run_review(self, key: tuple[str, int], orchestrator: ReviewOrchestrator) -> None:
        """Run a review in a pool thread. Cleans up active tracking on completion."""
        try:
            orchestrator.run()
        except Exception:
            logger.exception(f"Unhandled error in review for {key[0]} PR #{key[1]}")
        finally:
            with self._lock:
                self._active.pop(key, None)

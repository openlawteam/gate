"""Unix socket JSONL server for Gate.

Manages active reviews, queue state, and health metrics.
Architecture: accept loop + handler threads, event queue for mutations,
broadcast queue for writes.
"""

import atexit
import json
import logging
import queue
import signal
import socket
import threading
import time
from pathlib import Path

from gate import config
from gate.queue import ReviewQueue

logger = logging.getLogger(__name__)


def _current_ms() -> int:
    return int(time.time() * 1000)


class GateServer:
    """Broadcast message server over Unix domain socket.

    Uses a single writer thread to serialize all broadcasts, preventing
    race conditions when multiple client handler threads send concurrently.
    """

    def __init__(self, socket_path: Path, tmux_location: dict | None = None):
        self.socket_path = socket_path
        self.tmux_location = tmux_location
        self.started_at = _current_ms()
        self.clients: list[socket.socket] = []
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.server_socket: socket.socket | None = None
        self.broadcast_queue: queue.Queue = queue.Queue(maxsize=10000)
        self.event_queue: queue.Queue = queue.Queue(maxsize=10000)
        self.writer_thread: threading.Thread | None = None
        self.event_thread: threading.Thread | None = None
        self.reviews: list[dict] = []
        self.review_queue: list[dict] = []
        self.health: dict = {}
        self._log_handler: logging.FileHandler | None = None
        self._review_queue = ReviewQueue(socket_path=socket_path)

    def _find_review(self, review_id: str) -> dict | None:
        return next((r for r in self.reviews if r.get("id") == review_id), None)

    @staticmethod
    def _head_sha_matches(review: dict, message: dict) -> bool:
        """Return True if ``message`` is for the orchestrator currently
        tracked in ``review``.

        ``review_id`` is deterministic per-PR (``{repo_slug}-pr{pr_number}``),
        so when a new push supersedes an in-flight orchestrator, both share
        the same id. Without a finer-grained match, a late
        ``review_cancelled``/``review_completed``/``review_stage_update``
        from the superseded orchestrator would mutate or drop the new
        orchestrator's entry. The fix: lifecycle events from the
        orchestrator carry ``head_sha`` and the server ignores messages
        whose ``head_sha`` does not match the current review's.

        Messages without a ``head_sha`` (e.g. user-initiated cancels from
        the TUI, legacy callers) are accepted for backward compatibility.
        """
        incoming = message.get("head_sha")
        if not incoming:
            return True
        current = review.get("head_sha", "")
        return incoming == current

    def start(self) -> None:
        """Start the server (blocking)."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        from gate.logger import attach_gate_file_handler

        log_path = config.logs_dir() / "activity.log"
        self._log_handler = attach_gate_file_handler(log_path, level=logging.INFO)

        if self.socket_path.exists():
            self.socket_path.unlink()

        self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_socket.bind(str(self.socket_path))
        self.server_socket.listen(5)
        self.server_socket.settimeout(1.0)

        self.writer_thread = threading.Thread(
            target=self._writer_loop, name="server-writer", daemon=True
        )
        self.writer_thread.start()

        self.event_thread = threading.Thread(
            target=self._event_loop, name="server-events", daemon=True
        )
        self.event_thread.start()

        self._review_queue.start()
        logger.debug(f"Server listening on {self.socket_path}")

        self._reaper_thread = threading.Thread(
            target=self._reaper_loop, name="server-reaper", daemon=True
        )
        self._reaper_thread.start()

        try:
            while not self.stop_event.is_set():
                try:
                    conn, _ = self.server_socket.accept()
                    threading.Thread(
                        target=self._handle_client, args=(conn,), daemon=True
                    ).start()
                except socket.timeout:
                    continue
                except Exception as e:
                    if not self.stop_event.is_set():
                        logger.error(f"Accept error: {e}")
        finally:
            self.server_socket.close()
            if self.socket_path.exists():
                self.socket_path.unlink()

    _READ_ONLY_TYPES = frozenset({
        "connect", "ping", "review_list", "queue_list",
        "health_get", "review_request", "cancel_review",
    })

    def _handle_client(self, conn: socket.socket) -> None:
        with self.lock:
            self.clients.append(conn)
        logger.debug(f"Client connected ({len(self.clients)} total)")

        try:
            conn.settimeout(2.0)
            buffer = ""
            while not self.stop_event.is_set():
                try:
                    data = conn.recv(4096)
                    if not data:
                        break
                    buffer += data.decode("utf-8")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        if line.strip():
                            try:
                                message = json.loads(line)
                                msg_type = message.get("type")
                                if msg_type in self._READ_ONLY_TYPES:
                                    self._handle_read_only(message, conn)
                                else:
                                    self._enqueue_event(message, conn)
                            except json.JSONDecodeError:
                                pass
                except socket.timeout:
                    continue
        except Exception as e:
            logger.debug(f"Client error: {e}")
        finally:
            with self.lock:
                if conn in self.clients:
                    self.clients.remove(conn)
            try:
                conn.close()
            except Exception:
                pass
            logger.debug(f"Client disconnected ({len(self.clients)} remaining)")

    def _handle_read_only(self, message: dict, conn: socket.socket) -> None:
        msg_type = message.get("type")

        if msg_type == "connect":
            response: dict = {"type": "connected", "tmux": self.tmux_location}
            review_id = message.get("review_id")
            if review_id:
                review = self._find_review(review_id)
                response["review"] = review
                response["review_found"] = review is not None
            self._send_response(conn, response)

        elif msg_type == "ping":
            self._send_response(conn, {"type": "pong"})

        elif msg_type == "review_list":
            self._send_response(conn, {"type": "review_list", "reviews": self.reviews})

        elif msg_type == "queue_list":
            self._send_response(conn, {"type": "queue_list", "queue": self.review_queue})

        elif msg_type == "health_get":
            self._send_response(conn, {"type": "health_data", "health": self.health})

        elif msg_type == "review_request":
            pr_number = message.get("pr_number")
            repo = message.get("repo", "")
            head_sha = message.get("head_sha", "")
            event = message.get("event", "synchronize")
            branch = message.get("branch", "")
            labels = message.get("labels", [])
            self._review_queue.enqueue(
                pr_number=pr_number,
                repo=repo,
                head_sha=head_sha,
                event=event,
                branch=branch,
                labels=labels,
            )
            logger.info(f"Review request accepted: PR #{pr_number}")
            self._send_response(conn, {"type": "review_accepted", "pr_number": pr_number})

        elif msg_type == "cancel_review":
            pr_number = message.get("pr_number")
            repo = message.get("repo", "")
            cancelled = self._review_queue.cancel_pr(pr_number, repo)
            if cancelled:
                logger.info(f"Review cancelled via socket: PR #{pr_number}")
            self._send_response(conn, {
                "type": "cancel_accepted",
                "pr_number": pr_number,
                "cancelled": cancelled,
            })

    def _handle_mutation(self, message: dict, conn: socket.socket | None) -> None:
        msg_type = message.get("type")

        if msg_type == "review_started":
            review = message.get("review", {})
            if "started_at" not in review:
                review["started_at"] = _current_ms()
            review.setdefault("updated_at", review["started_at"])
            review.setdefault("stage", "")
            existing = self._find_review(review.get("id", ""))
            if existing:
                existing.update(review)
            else:
                self.reviews.append(review)
            self.broadcast({"type": "review_updated", "review": review})
            logger.info(f"Review started: {review.get('id')}")

        elif msg_type == "review_stage_update":
            review_id = message.get("review_id")
            review = self._find_review(review_id)
            if review:
                if not self._head_sha_matches(review, message):
                    logger.debug(
                        f"Ignoring stale review_stage_update for {review_id}: "
                        f"incoming head_sha={message.get('head_sha', '')[:8]} "
                        f"current={review.get('head_sha', '')[:8]}"
                    )
                    return
                incoming_status = message.get("status", "")
                if (review.get("status") == "fixing"
                        and incoming_status != "fixing"):
                    review["updated_at"] = _current_ms()
                    return
                review["stage"] = message.get("stage", "")
                review["status"] = incoming_status
                review["updated_at"] = _current_ms()
                self.broadcast({"type": "review_updated", "review": review})

        elif msg_type == "review_completed":
            review_id = message.get("review_id")
            review = self._find_review(review_id)
            if review:
                if not self._head_sha_matches(review, message):
                    logger.debug(
                        f"Ignoring stale review_completed for {review_id}: "
                        f"incoming head_sha={message.get('head_sha', '')[:8]} "
                        f"current={review.get('head_sha', '')[:8]}"
                    )
                    return
                review["status"] = "completed"
                review["decision"] = message.get("decision", "")
                review["updated_at"] = _current_ms()
                self.broadcast({"type": "review_completed", "review": review})
                self.reviews = [r for r in self.reviews if r.get("id") != review_id]

        elif msg_type == "review_cancelled":
            review_id = message.get("review_id")
            review = self._find_review(review_id)
            if review:
                if not self._head_sha_matches(review, message):
                    logger.debug(
                        f"Ignoring stale review_cancelled for {review_id}: "
                        f"incoming head_sha={message.get('head_sha', '')[:8]} "
                        f"current={review.get('head_sha', '')[:8]}"
                    )
                    return
                pr_number = review.get("pr_number")
                repo = review.get("repo", "")
                if pr_number:
                    self._review_queue.cancel_pr(pr_number, repo)
                review["status"] = "cancelled"
                review["updated_at"] = _current_ms()
                self.broadcast({"type": "review_cancelled", "review": review})
                self.reviews = [r for r in self.reviews if r.get("id") != review_id]

        elif msg_type == "stage_register":
            review_id = message.get("review_id")
            review = self._find_review(review_id)
            if review:
                if not self._head_sha_matches(review, message):
                    logger.debug(
                        f"Ignoring stale stage_register for {review_id}: "
                        f"incoming head_sha={message.get('head_sha', '')[:8]} "
                        f"current={review.get('head_sha', '')[:8]}"
                    )
                    return
                if review.get("status") != "fixing":
                    review["stage"] = message.get("stage", "")
                    review["status"] = "running"
                review["tmux_pane"] = message.get("tmux_pane", "")
                review["pid"] = message.get("pid")
                review["updated_at"] = _current_ms()
                self.broadcast({"type": "review_updated", "review": review})
                logger.debug(f"Stage registered: {review_id} -> {review.get('stage')}")

        elif msg_type == "queue_update":
            self.review_queue = message.get("queue", [])
            self.broadcast({"type": "queue_updated", "queue": self.review_queue})

        elif msg_type == "health_update":
            self.health = message.get("health", {})
            self.broadcast({"type": "health_updated", "health": self.health})

        else:
            logger.debug(f"Unknown message type: {msg_type}")

    def _send_response(self, conn: socket.socket, message: dict) -> None:
        if "ts" not in message:
            message["ts"] = _current_ms()
        response = json.dumps(message) + "\n"
        try:
            conn.sendall(response.encode("utf-8"))
        except Exception as e:
            logger.debug(f"Failed to send response: {e}")

    def _event_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                message, conn = self.event_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._handle_mutation(message, conn)
            except Exception:
                logger.exception(f"Event loop error: {message.get('type')}")

    def _enqueue_event(self, message: dict, conn: socket.socket | None = None) -> None:
        try:
            self.event_queue.put_nowait((message, conn))
        except queue.Full:
            logger.warning(f"Event queue full, dropping: {message.get('type')}")

    def enqueue(self, message: dict) -> None:
        """Public API for in-process callers (TUI) to submit mutations."""
        self._enqueue_event(message)

    def _writer_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                message = self.broadcast_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            self._send_to_clients(message)

    def _send_to_clients(self, message: dict) -> None:
        if "ts" not in message:
            message["ts"] = _current_ms()
        data = (json.dumps(message) + "\n").encode("utf-8")

        with self.lock:
            clients_to_send = list(self.clients)

        dead_clients = []
        for client in clients_to_send:
            try:
                client.settimeout(2.0)
                client.sendall(data)
            except Exception:
                dead_clients.append(client)

        if dead_clients:
            with self.lock:
                for client in dead_clients:
                    if client in self.clients:
                        self.clients.remove(client)
                    try:
                        client.close()
                    except Exception:
                        pass

    def broadcast(self, message: dict) -> bool:
        if "type" not in message:
            return False
        try:
            self.broadcast_queue.put_nowait(message)
            return True
        except queue.Full:
            logger.warning(f"Broadcast queue full, dropping: {message.get('type')}")
            return False

    def stop(self) -> None:
        logger.debug("Server stopping")
        self._review_queue.stop()
        self._send_to_clients({"type": "shutdown"})
        with self.lock:
            for client in self.clients:
                try:
                    client.close()
                except Exception:
                    pass
            self.clients.clear()
        self.stop_event.set()
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass
        if self.event_thread and self.event_thread.is_alive():
            self.event_thread.join(timeout=1.0)
        if self.writer_thread and self.writer_thread.is_alive():
            self.writer_thread.join(timeout=1.0)
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except Exception:
                pass
        logger.debug("Server stopped")
        if self._log_handler:
            gate_logger = logging.getLogger("gate")
            gate_logger.removeHandler(self._log_handler)
            self._log_handler.close()
            self._log_handler = None


    def _reaper_loop(self) -> None:
        """Periodically check for stale reviews stuck in the active list."""
        while not self.stop_event.is_set():
            self.stop_event.wait(60.0)
            if self.stop_event.is_set():
                break
            self._reap_stale_reviews()

    def _reap_stale_reviews(self) -> None:
        """Remove reviews stuck longer than 1 hour from the active list."""
        now = _current_ms()
        timeout_ms = 3600 * 1000
        stale = [
            r for r in self.reviews
            if now - r.get("updated_at", now) > timeout_ms
        ]
        for r in stale:
            r["status"] = "stuck"
            self.broadcast({"type": "review_completed", "review": r})
            self.reviews = [x for x in self.reviews if x.get("id") != r.get("id")]
            logger.warning(f"Reaped stale review: {r.get('id')}")


def start_server_with_tui(socket_path: Path, tmux_location: dict | None = None) -> int:
    """Start the server in a background thread and run the TUI."""
    from gate.tui import run_tui

    server = GateServer(socket_path, tmux_location=tmux_location)
    shutdown_initiated = threading.Event()

    def handle_shutdown_signal(signum, frame):
        if not shutdown_initiated.is_set():
            shutdown_initiated.set()
            raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)

    def cleanup_socket():
        if socket_path.exists():
            try:
                socket_path.unlink()
            except Exception:
                pass

    atexit.register(cleanup_socket)

    server_thread = threading.Thread(target=server.start, name="server", daemon=True)
    server_thread.start()

    for _ in range(50):
        if socket_path.exists():
            break
        time.sleep(0.1)
    else:
        print("Server failed to start")
        server.stop()
        return 1

    try:
        return run_tui(server)
    except KeyboardInterrupt:
        return 0
    finally:
        logger.info("Shutting down server")
        server.stop()
        server_thread.join(timeout=2.0)
        atexit.unregister(cleanup_socket)


def start_server_headless(socket_path: Path) -> int:
    """Start the server without TUI (for LaunchAgent)."""
    server = GateServer(socket_path)

    def handle_shutdown_signal(signum, frame):
        server.stop()

    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)

    def cleanup_socket():
        if socket_path.exists():
            try:
                socket_path.unlink()
            except Exception:
                pass

    atexit.register(cleanup_socket)

    try:
        server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        atexit.unregister(cleanup_socket)
    return 0

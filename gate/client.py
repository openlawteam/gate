"""Unix socket JSONL client for Gate.

Provides both persistent connections (GateConnection) and one-shot
request/response helpers.
"""

import json
import logging
import queue
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _current_ms() -> int:
    return int(time.time() * 1000)


class GateConnection:
    """Persistent bidirectional connection to the Gate server.

    Messages are sent via a queue to avoid blocking. A background thread
    handles connection management, queue draining, and message receiving.
    """

    def __init__(self, socket_path: Path):
        self.socket_path = socket_path
        self.send_queue: queue.Queue = queue.Queue(maxsize=1000)
        self.callback: Callable[[dict[str, Any]], Any] | None = None
        self.on_connect: Callable[[], Any] | None = None
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()

    def start(
        self,
        callback: Callable[[dict[str, Any]], Any] | None = None,
        on_connect: Callable[[], Any] | None = None,
    ) -> None:
        """Start background thread for sending and receiving."""
        if self.thread and self.thread.is_alive():
            return
        self.callback = callback
        self.on_connect = on_connect
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self) -> None:
        sock: socket.socket | None = None
        buffer = ""
        last_connect_attempt = 0.0

        while True:
            if not sock and time.time() - last_connect_attempt > 1.0:
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect(str(self.socket_path))
                    sock.settimeout(0.1)
                    logger.debug(f"Connected to {self.socket_path}")
                    if self.on_connect:
                        try:
                            self.on_connect()
                        except Exception as e:
                            logger.error(f"on_connect callback failed: {e}")
                except Exception:
                    if sock:
                        try:
                            sock.close()
                        except Exception:
                            pass
                        sock = None
                    last_connect_attempt = time.time()

            try:
                msg = self.send_queue.get(timeout=0.1)
                if sock:
                    try:
                        line = json.dumps(msg) + "\n"
                        sock.sendall(line.encode("utf-8"))
                    except Exception:
                        try:
                            sock.close()
                        except Exception:
                            pass
                        sock = None
            except queue.Empty:
                if self.stop_event.is_set():
                    break

            if sock:
                try:
                    data = sock.recv(4096)
                    if not data:
                        try:
                            sock.close()
                        except Exception:
                            pass
                        sock = None
                        buffer = ""
                        continue
                    buffer += data.decode("utf-8")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        if line.strip() and self.callback:
                            try:
                                message = json.loads(line)
                                self.callback(message)
                            except (json.JSONDecodeError, Exception):
                                pass
                except socket.timeout:
                    continue
                except Exception:
                    try:
                        sock.close()
                    except Exception:
                        pass
                    sock = None
                    buffer = ""

        if sock:
            try:
                sock.close()
            except Exception:
                pass

    def emit(self, msg_type: str, **fields) -> bool:
        """Emit message via send queue. Returns True if queued."""
        if not self.thread or not self.thread.is_alive():
            return False
        message = {"type": msg_type, "ts": _current_ms(), **fields}
        try:
            self.send_queue.put_nowait(message)
            return True
        except queue.Full:
            return False

    def stop(self) -> None:
        """Stop background thread gracefully."""
        if not self.thread:
            return
        self.stop_event.set()
        self.thread.join(timeout=0.5)


def send_message(
    socket_path: Path,
    message: dict,
    timeout: float = 2.0,
    wait_for_response: bool = False,
    expected_types: set[str] | None = None,
    max_skipped_frames: int = 10,
) -> dict | None:
    """Send a one-shot message to the server.

    When ``expected_types`` is provided, frames whose ``type`` is not in the set
    are skipped (with a debug log) and we keep reading until a matching frame
    arrives, the cumulative ``timeout`` elapses, or ``max_skipped_frames`` is
    exceeded. This lets the CLI tolerate interleaved broadcasts (e.g.
    ``review_cancelled``) that arrive on the same socket as the request's
    actual response (e.g. ``review_accepted``).
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(socket_path))
        line = json.dumps(message) + "\n"
        sock.sendall(line.encode("utf-8"))
        if wait_for_response:
            buffer = ""
            skipped = 0
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    break
                if not data:
                    break
                buffer += data.decode("utf-8")
                while "\n" in buffer:
                    response_line, buffer = buffer.split("\n", 1)
                    response_line = response_line.strip()
                    if not response_line:
                        continue
                    try:
                        parsed = json.loads(response_line)
                    except json.JSONDecodeError:
                        continue
                    if expected_types is not None and parsed.get("type") not in expected_types:
                        skipped += 1
                        logger.debug(
                            "send_message: skipping unexpected frame type=%s (skipped=%d)",
                            parsed.get("type"),
                            skipped,
                        )
                        if skipped >= max_skipped_frames:
                            sock.close()
                            return None
                        continue
                    sock.close()
                    return parsed
        sock.close()
        return None
    except Exception:
        return None


def connect(socket_path: Path, timeout: float = 2.0) -> dict | None:
    """Connect to the server and get status. Returns None if unreachable."""
    message = {"type": "connect", "ts": _current_ms()}
    response = send_message(socket_path, message, timeout=timeout, wait_for_response=True)
    if response is None or response.get("type") != "connected":
        return None
    return response


def ping(socket_path: Path, timeout: float = 2.0) -> bool:
    """Check if server is running."""
    return connect(socket_path, timeout=timeout) is not None


def list_reviews(socket_path: Path, timeout: float = 2.0) -> list[dict]:
    """List active reviews from the server."""
    response = send_message(
        socket_path, {"type": "review_list"}, timeout=timeout, wait_for_response=True
    )
    if response and response.get("type") == "review_list":
        return response.get("reviews", [])
    return []


def list_queue(socket_path: Path, timeout: float = 2.0) -> list[dict]:
    """List queued reviews from the server."""
    response = send_message(
        socket_path, {"type": "queue_list"}, timeout=timeout, wait_for_response=True
    )
    if response and response.get("type") == "queue_list":
        return response.get("queue", [])
    return []


def get_health(socket_path: Path, timeout: float = 2.0) -> dict:
    """Get health data from the server."""
    response = send_message(
        socket_path, {"type": "health_get"}, timeout=timeout, wait_for_response=True
    )
    if response and response.get("type") == "health_data":
        return response.get("health", {})
    return {}

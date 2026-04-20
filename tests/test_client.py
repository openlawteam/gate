"""Tests for gate.client module."""

import json
import socket as _socket
import tempfile
import threading
import time
from pathlib import Path

from gate.client import GateConnection, connect, ping, send_message
from gate.server import GateServer


def _serve_frames(sock_path: Path, frames: list[dict], delay: float = 0.0):
    """Start a bare unix-socket server that sends the given frames and closes."""
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()
    server = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)

    def _run():
        try:
            conn, _ = server.accept()
            conn.recv(4096)
            for f in frames:
                if delay:
                    time.sleep(delay)
                conn.sendall((json.dumps(f) + "\n").encode("utf-8"))
            conn.close()
        except Exception:
            pass
        finally:
            server.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def _short_sock_path() -> Path:
    """Create a short socket path (AF_UNIX has a 104-byte limit on macOS)."""
    return Path(tempfile.mkdtemp(prefix="gc")) / "s.sock"


def _start_server() -> tuple[GateServer, Path, threading.Thread]:
    sock_path = _short_sock_path()
    server = GateServer(sock_path)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    for _ in range(50):
        if sock_path.exists():
            break
        time.sleep(0.05)
    return server, sock_path, thread


class TestSendMessage:
    def test_send_and_receive(self):
        server, sock_path, thread = _start_server()
        try:
            response = send_message(
                sock_path, {"type": "ping"}, timeout=2.0, wait_for_response=True
            )
            assert response is not None
            assert response["type"] == "pong"
        finally:
            server.stop()
            thread.join(timeout=2.0)

    def test_send_to_nonexistent(self, tmp_path):
        result = send_message(tmp_path / "missing.sock", {"type": "ping"})
        assert result is None


class TestConnect:
    def test_connect_success(self):
        server, sock_path, thread = _start_server()
        try:
            response = connect(sock_path)
            assert response is not None
            assert response["type"] == "connected"
        finally:
            server.stop()
            thread.join(timeout=2.0)

    def test_connect_failure(self, tmp_path):
        result = connect(tmp_path / "missing.sock")
        assert result is None


class TestPing:
    def test_ping_success(self):
        server, sock_path, thread = _start_server()
        try:
            assert ping(sock_path) is True
        finally:
            server.stop()
            thread.join(timeout=2.0)

    def test_ping_failure(self, tmp_path):
        assert ping(tmp_path / "missing.sock") is False


class TestGateConnection:
    def test_emit_without_start(self):
        conn = GateConnection(Path("/tmp/nonexistent.sock"))
        assert conn.emit("test") is False

    def test_stop_without_start(self):
        conn = GateConnection(Path("/tmp/nonexistent.sock"))
        conn.stop()


class TestSendMessageExpectedTypes:
    """Regression tests for PR #216 red-check: interleaved broadcast frames."""

    def test_skips_noise_and_returns_expected(self):
        sock_path = _short_sock_path()
        _serve_frames(
            sock_path,
            [
                {"type": "review_cancelled", "pr_number": 999},
                {"type": "review_accepted", "pr_number": 216},
            ],
        )
        response = send_message(
            sock_path,
            {"type": "review_request"},
            timeout=2.0,
            wait_for_response=True,
            expected_types={"review_accepted", "error"},
        )
        assert response is not None
        assert response["type"] == "review_accepted"
        assert response["pr_number"] == 216

    def test_returns_first_matching_frame(self):
        sock_path = _short_sock_path()
        _serve_frames(sock_path, [{"type": "review_accepted", "pr_number": 1}])
        response = send_message(
            sock_path,
            {"type": "review_request"},
            timeout=2.0,
            wait_for_response=True,
            expected_types={"review_accepted"},
        )
        assert response is not None
        assert response["type"] == "review_accepted"

    def test_times_out_on_only_noise(self):
        sock_path = _short_sock_path()
        # Only noise frames — caller expects "review_accepted" → eventual None.
        _serve_frames(
            sock_path,
            [
                {"type": "review_cancelled"},
                {"type": "progress"},
            ],
        )
        response = send_message(
            sock_path,
            {"type": "review_request"},
            timeout=0.5,
            wait_for_response=True,
            expected_types={"review_accepted"},
        )
        assert response is None

    def test_max_skipped_frames_bound(self):
        sock_path = _short_sock_path()
        noise = [{"type": "broadcast", "i": i} for i in range(20)]
        _serve_frames(sock_path, noise)
        response = send_message(
            sock_path,
            {"type": "review_request"},
            timeout=2.0,
            wait_for_response=True,
            expected_types={"review_accepted"},
            max_skipped_frames=3,
        )
        assert response is None

    def test_backward_compatible_without_filter(self):
        sock_path = _short_sock_path()
        # No expected_types → returns the first frame, even "review_cancelled".
        _serve_frames(sock_path, [{"type": "review_cancelled"}])
        response = send_message(
            sock_path,
            {"type": "review_request"},
            timeout=2.0,
            wait_for_response=True,
        )
        assert response is not None
        assert response["type"] == "review_cancelled"

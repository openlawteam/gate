"""Tests for gate.client module."""

import tempfile
import threading
import time
from pathlib import Path

from gate.client import GateConnection, connect, ping, send_message
from gate.server import GateServer


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

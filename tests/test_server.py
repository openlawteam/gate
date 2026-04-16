"""Tests for gate.server module."""

import json
import socket
import tempfile
import threading
import time
from pathlib import Path

from gate.server import GateServer


def _short_sock_path() -> Path:
    """Create a short socket path (AF_UNIX has a 104-byte limit on macOS)."""
    return Path(tempfile.mkdtemp(prefix="gs")) / "s.sock"


class TestGateServer:
    def test_start_and_stop(self):
        sock_path = _short_sock_path()
        server = GateServer(sock_path)
        server_thread = threading.Thread(target=server.start, daemon=True)
        server_thread.start()

        for _ in range(50):
            if sock_path.exists():
                break
            time.sleep(0.05)

        assert sock_path.exists()
        server.stop()
        server_thread.join(timeout=2.0)

    def test_ping(self):
        sock_path = _short_sock_path()
        server = GateServer(sock_path)
        server_thread = threading.Thread(target=server.start, daemon=True)
        server_thread.start()

        for _ in range(50):
            if sock_path.exists():
                break
            time.sleep(0.05)

        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(2.0)
        conn.connect(str(sock_path))
        conn.sendall(json.dumps({"type": "ping"}).encode("utf-8") + b"\n")

        data = conn.recv(4096)
        response = json.loads(data.decode("utf-8").strip())
        assert response["type"] == "pong"

        conn.close()
        server.stop()
        server_thread.join(timeout=2.0)

    def test_review_lifecycle(self):
        sock_path = _short_sock_path()
        server = GateServer(sock_path)
        server_thread = threading.Thread(target=server.start, daemon=True)
        server_thread.start()

        for _ in range(50):
            if sock_path.exists():
                break
            time.sleep(0.05)

        server.enqueue({
            "type": "review_started",
            "review": {
                "id": "org-repo-pr42", "pr_number": 42,
                "repo": "org/repo", "stage": "triage",
                "status": "running",
            },
        })
        time.sleep(0.2)
        assert len(server.reviews) == 1
        assert server.reviews[0]["id"] == "org-repo-pr42"

        server.enqueue({
            "type": "review_completed",
            "review_id": "org-repo-pr42",
            "decision": "approve",
        })
        time.sleep(0.2)
        assert len(server.reviews) == 0

        server.stop()
        server_thread.join(timeout=2.0)

    def test_broadcast(self):
        sock_path = _short_sock_path()
        server = GateServer(sock_path)
        assert server.broadcast({"type": "test_message"}) is True

    def test_enqueue_event(self):
        sock_path = _short_sock_path()
        server = GateServer(sock_path)
        server.enqueue({"type": "health_update", "health": {"ok": True}})

    def test_stage_register_blocked_during_fixing(self):
        sock_path = _short_sock_path()
        server = GateServer(sock_path)
        server_thread = threading.Thread(target=server.start, daemon=True)
        server_thread.start()
        for _ in range(50):
            if sock_path.exists():
                break
            time.sleep(0.05)

        server.enqueue({
            "type": "review_started",
            "review": {
                "id": "test-pr1", "pr_number": 1,
                "stage": "fix-session", "status": "fixing",
            },
        })
        time.sleep(0.2)

        server.enqueue({
            "type": "stage_register",
            "review_id": "test-pr1",
            "stage": "fix-senior",
            "tmux_pane": "%5",
            "pid": 12345,
        })
        time.sleep(0.2)

        review = server.reviews[0]
        assert review["stage"] == "fix-session"
        assert review["status"] == "fixing"
        assert review["tmux_pane"] == "%5"

        server.stop()
        server_thread.join(timeout=2.0)

    def test_review_stage_update_blocked_during_fixing(self):
        sock_path = _short_sock_path()
        server = GateServer(sock_path)
        server_thread = threading.Thread(target=server.start, daemon=True)
        server_thread.start()
        for _ in range(50):
            if sock_path.exists():
                break
            time.sleep(0.05)

        server.enqueue({
            "type": "review_started",
            "review": {
                "id": "test-pr2", "pr_number": 2,
                "stage": "fix-build", "status": "fixing",
            },
        })
        time.sleep(0.2)

        server.enqueue({
            "type": "review_stage_update",
            "review_id": "test-pr2",
            "stage": "fix-senior",
            "status": "running",
        })
        time.sleep(0.2)

        review = server.reviews[0]
        assert review["status"] == "fixing"
        assert review["stage"] == "fix-build"

        server.stop()
        server_thread.join(timeout=2.0)

    def test_review_stage_update_allowed_with_fixing_status(self):
        sock_path = _short_sock_path()
        server = GateServer(sock_path)
        server_thread = threading.Thread(target=server.start, daemon=True)
        server_thread.start()
        for _ in range(50):
            if sock_path.exists():
                break
            time.sleep(0.05)

        server.enqueue({
            "type": "review_started",
            "review": {
                "id": "test-pr3", "pr_number": 3,
                "stage": "fix-session", "status": "fixing",
            },
        })
        time.sleep(0.2)

        server.enqueue({
            "type": "review_stage_update",
            "review_id": "test-pr3",
            "stage": "fix-build",
            "status": "fixing",
        })
        time.sleep(0.2)

        review = server.reviews[0]
        assert review["status"] == "fixing"
        assert review["stage"] == "fix-build"

        server.stop()
        server_thread.join(timeout=2.0)

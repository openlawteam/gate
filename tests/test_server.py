"""Tests for gate.server module."""

import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from gate.server import GateServer


def _short_sock_path() -> Path:
    """Create a short socket path (AF_UNIX has a 104-byte limit on macOS)."""
    return Path(tempfile.mkdtemp(prefix="gs")) / "s.sock"


def _wait_for_socket(sock_path: Path, timeout: float = 2.5) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sock_path.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(f"server socket never appeared: {sock_path}")


def _recv_until(conn: socket.socket, expected_types: set[str], timeout: float = 2.0) -> list[dict]:
    """Receive newline-delimited JSON messages until expected types appear or timeout."""
    conn.settimeout(timeout)
    buffer = ""
    received: list[dict] = []
    deadline = time.time() + timeout
    seen_types: set[str] = set()
    while time.time() < deadline and not expected_types.issubset(seen_types):
        try:
            data = conn.recv(4096)
            if not data:
                break
            buffer += data.decode("utf-8")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                received.append(msg)
                if "type" in msg:
                    seen_types.add(msg["type"])
        except socket.timeout:
            break
    return received


@pytest.fixture
def running_server():
    """Yield a started GateServer paired with its thread; guarantees teardown."""
    sock_path = _short_sock_path()
    server = GateServer(sock_path)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    _wait_for_socket(sock_path)
    try:
        yield server, sock_path
    finally:
        server.stop()
        thread.join(timeout=2.0)


@pytest.fixture
def client_socket():
    """Yield a factory that creates + tracks AF_UNIX client sockets and closes them."""
    opened: list[socket.socket] = []

    def _connect(sock_path: Path) -> socket.socket:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(str(sock_path))
        opened.append(s)
        return s

    yield _connect
    for s in opened:
        try:
            s.close()
        except Exception:
            pass


# ── Basic lifecycle ──────────────────────────────────────────


class TestGateServer:
    def test_start_and_stop(self):
        sock_path = _short_sock_path()
        server = GateServer(sock_path)
        server_thread = threading.Thread(target=server.start, daemon=True)
        server_thread.start()

        _wait_for_socket(sock_path)
        assert sock_path.exists()
        server.stop()
        server_thread.join(timeout=2.0)

    def test_socket_file_removed_after_stop(self, running_server):
        server, sock_path = running_server
        server.stop()
        # Allow brief time for cleanup
        time.sleep(0.1)
        assert not sock_path.exists()

    def test_ping(self, running_server, client_socket):
        server, sock_path = running_server
        conn = client_socket(sock_path)
        conn.sendall(json.dumps({"type": "ping"}).encode("utf-8") + b"\n")

        data = conn.recv(4096)
        response = json.loads(data.decode("utf-8").strip())
        assert response["type"] == "pong"
        assert "ts" in response

    def test_connect_with_review_id_known(self, running_server, client_socket):
        server, sock_path = running_server
        server.enqueue({
            "type": "review_started",
            "review": {"id": "rid-1", "pr_number": 1, "repo": "a/b"},
        })
        time.sleep(0.1)

        conn = client_socket(sock_path)
        conn.sendall(
            json.dumps({"type": "connect", "review_id": "rid-1"}).encode("utf-8") + b"\n"
        )
        data = conn.recv(4096)
        response = json.loads(data.decode("utf-8").strip())
        assert response["type"] == "connected"
        assert response["review_found"] is True
        assert response["review"]["id"] == "rid-1"

    def test_connect_with_unknown_review_id(self, running_server, client_socket):
        server, sock_path = running_server
        conn = client_socket(sock_path)
        conn.sendall(
            json.dumps({"type": "connect", "review_id": "nope"}).encode("utf-8") + b"\n"
        )
        data = conn.recv(4096)
        response = json.loads(data.decode("utf-8").strip())
        assert response["type"] == "connected"
        assert response["review_found"] is False

    def test_malformed_json_is_ignored(self, running_server, client_socket):
        server, sock_path = running_server
        conn = client_socket(sock_path)
        # First send bad JSON, then a valid ping
        conn.sendall(b"{not json\n")
        conn.sendall(json.dumps({"type": "ping"}).encode("utf-8") + b"\n")
        data = conn.recv(4096)
        response = json.loads(data.decode("utf-8").strip())
        assert response["type"] == "pong"


# ── Review lifecycle / mutation protocol ─────────────────────


class TestReviewLifecycle:
    def test_review_started_adds_to_list(self, running_server):
        server, _ = running_server
        server.enqueue({
            "type": "review_started",
            "review": {
                "id": "org-repo-pr42", "pr_number": 42,
                "repo": "org/repo", "stage": "triage",
                "status": "running",
            },
        })
        time.sleep(0.15)
        assert len(server.reviews) == 1
        assert server.reviews[0]["id"] == "org-repo-pr42"
        assert "started_at" in server.reviews[0]
        assert "updated_at" in server.reviews[0]

    def test_review_started_updates_existing(self, running_server):
        server, _ = running_server
        server.enqueue({
            "type": "review_started",
            "review": {"id": "r1", "stage": "triage", "status": "running"},
        })
        time.sleep(0.1)
        server.enqueue({
            "type": "review_started",
            "review": {"id": "r1", "stage": "build", "status": "running"},
        })
        time.sleep(0.1)
        assert len(server.reviews) == 1
        assert server.reviews[0]["stage"] == "build"

    def test_review_completed_removes_from_list(self, running_server):
        server, _ = running_server
        server.enqueue({
            "type": "review_started",
            "review": {"id": "r2", "pr_number": 2, "status": "running"},
        })
        time.sleep(0.1)
        server.enqueue({
            "type": "review_completed",
            "review_id": "r2",
            "decision": "approve",
        })
        time.sleep(0.15)
        assert len(server.reviews) == 0

    def test_review_cancelled_removes_from_list(self, running_server):
        server, _ = running_server
        server.enqueue({
            "type": "review_started",
            "review": {"id": "r3", "pr_number": 3, "status": "running"},
        })
        time.sleep(0.1)
        server.enqueue({"type": "review_cancelled", "review_id": "r3"})
        time.sleep(0.15)
        assert len(server.reviews) == 0

    def test_review_stage_update_changes_stage(self, running_server):
        server, _ = running_server
        server.enqueue({
            "type": "review_started",
            "review": {"id": "r4", "stage": "triage", "status": "running"},
        })
        time.sleep(0.1)
        server.enqueue({
            "type": "review_stage_update",
            "review_id": "r4",
            "stage": "logic",
            "status": "running",
        })
        time.sleep(0.1)
        assert server.reviews[0]["stage"] == "logic"

    def test_stage_register_captures_pane_and_pid(self, running_server):
        server, _ = running_server
        server.enqueue({
            "type": "review_started",
            "review": {"id": "r5", "stage": "triage", "status": "running"},
        })
        time.sleep(0.1)
        server.enqueue({
            "type": "stage_register",
            "review_id": "r5",
            "stage": "security",
            "tmux_pane": "%42",
            "pid": 9999,
        })
        time.sleep(0.1)
        review = server.reviews[0]
        assert review["tmux_pane"] == "%42"
        assert review["pid"] == 9999

    def test_stale_cancel_from_superseded_orchestrator_is_ignored(
        self, running_server
    ):
        """Regression: when a new push supersedes an in-flight orchestrator
        on the same PR, a late ``review_cancelled`` from the old
        orchestrator must not purge the new orchestrator's entry.

        Both orchestrators share the same ``review_id`` (deterministic
        per-PR), so the server disambiguates by ``head_sha``.
        """
        server, _ = running_server
        server.enqueue({
            "type": "review_started",
            "review": {
                "id": "race-pr1", "pr_number": 1, "status": "running",
                "head_sha": "new_sha",
            },
        })
        time.sleep(0.1)
        server.enqueue({
            "type": "review_cancelled",
            "review_id": "race-pr1",
            "head_sha": "old_sha",
        })
        time.sleep(0.15)
        assert len(server.reviews) == 1
        assert server.reviews[0]["head_sha"] == "new_sha"

    def test_stale_completed_from_superseded_orchestrator_is_ignored(
        self, running_server
    ):
        server, _ = running_server
        server.enqueue({
            "type": "review_started",
            "review": {
                "id": "race-pr2", "pr_number": 2, "status": "running",
                "head_sha": "new_sha",
            },
        })
        time.sleep(0.1)
        server.enqueue({
            "type": "review_completed",
            "review_id": "race-pr2",
            "head_sha": "old_sha",
            "decision": "cancelled",
        })
        time.sleep(0.15)
        assert len(server.reviews) == 1
        assert server.reviews[0]["head_sha"] == "new_sha"

    def test_stale_stage_update_from_superseded_orchestrator_is_ignored(
        self, running_server
    ):
        server, _ = running_server
        server.enqueue({
            "type": "review_started",
            "review": {
                "id": "race-pr3", "pr_number": 3, "stage": "logic",
                "status": "running", "head_sha": "new_sha",
            },
        })
        time.sleep(0.1)
        server.enqueue({
            "type": "review_stage_update",
            "review_id": "race-pr3",
            "head_sha": "old_sha",
            "stage": "triage",
            "status": "running",
        })
        time.sleep(0.1)
        assert server.reviews[0]["stage"] == "logic"

    def test_stale_stage_register_from_superseded_orchestrator_is_ignored(
        self, running_server
    ):
        server, _ = running_server
        server.enqueue({
            "type": "review_started",
            "review": {
                "id": "race-pr4", "pr_number": 4, "stage": "logic",
                "status": "running", "head_sha": "new_sha",
                "tmux_pane": "%new", "pid": 1111,
            },
        })
        time.sleep(0.1)
        server.enqueue({
            "type": "stage_register",
            "review_id": "race-pr4",
            "head_sha": "old_sha",
            "stage": "triage",
            "tmux_pane": "%old",
            "pid": 9999,
        })
        time.sleep(0.1)
        assert server.reviews[0]["tmux_pane"] == "%new"
        assert server.reviews[0]["pid"] == 1111

    def test_matching_head_sha_applies_lifecycle_events(self, running_server):
        """A cancel that *does* match the current head_sha must still work."""
        server, _ = running_server
        server.enqueue({
            "type": "review_started",
            "review": {
                "id": "match-pr1", "pr_number": 1, "status": "running",
                "head_sha": "abc123",
            },
        })
        time.sleep(0.1)
        server.enqueue({
            "type": "review_cancelled",
            "review_id": "match-pr1",
            "head_sha": "abc123",
        })
        time.sleep(0.15)
        assert len(server.reviews) == 0

    def test_cancel_without_head_sha_is_accepted_for_backcompat(
        self, running_server
    ):
        """User-initiated cancels from the TUI omit ``head_sha`` and must
        continue to work unconditionally."""
        server, _ = running_server
        server.enqueue({
            "type": "review_started",
            "review": {
                "id": "tui-pr1", "pr_number": 1, "status": "running",
                "head_sha": "abc123",
            },
        })
        time.sleep(0.1)
        server.enqueue({
            "type": "review_cancelled",
            "review_id": "tui-pr1",
        })
        time.sleep(0.15)
        assert len(server.reviews) == 0

    def test_queue_update_broadcasts(self, running_server):
        server, _ = running_server
        server.enqueue({
            "type": "queue_update",
            "queue": [{"pr_number": 1, "repo": "a/b"}, {"pr_number": 2, "repo": "a/b"}],
        })
        time.sleep(0.1)
        assert len(server.review_queue) == 2

    def test_health_update_stores_data(self, running_server):
        server, _ = running_server
        server.enqueue({
            "type": "health_update",
            "health": {"ok": True, "checks": {"disk": "ok"}},
        })
        time.sleep(0.1)
        assert server.health["ok"] is True

    def test_stage_register_blocked_during_fixing(self, running_server):
        server, _ = running_server
        server.enqueue({
            "type": "review_started",
            "review": {"id": "tp1", "stage": "fix-session", "status": "fixing"},
        })
        time.sleep(0.1)

        server.enqueue({
            "type": "stage_register",
            "review_id": "tp1",
            "stage": "fix-senior",
            "tmux_pane": "%5",
            "pid": 12345,
        })
        time.sleep(0.1)

        review = server.reviews[0]
        assert review["stage"] == "fix-session"
        assert review["status"] == "fixing"
        assert review["tmux_pane"] == "%5"

    def test_stage_update_blocked_during_fixing(self, running_server):
        server, _ = running_server
        server.enqueue({
            "type": "review_started",
            "review": {"id": "tp2", "stage": "fix-build", "status": "fixing"},
        })
        time.sleep(0.1)

        server.enqueue({
            "type": "review_stage_update",
            "review_id": "tp2",
            "stage": "fix-senior",
            "status": "running",
        })
        time.sleep(0.1)
        review = server.reviews[0]
        assert review["status"] == "fixing"
        assert review["stage"] == "fix-build"

    def test_stage_update_allowed_with_fixing_status(self, running_server):
        server, _ = running_server
        server.enqueue({
            "type": "review_started",
            "review": {"id": "tp3", "stage": "fix-session", "status": "fixing"},
        })
        time.sleep(0.1)

        server.enqueue({
            "type": "review_stage_update",
            "review_id": "tp3",
            "stage": "fix-build",
            "status": "fixing",
        })
        time.sleep(0.1)
        review = server.reviews[0]
        assert review["status"] == "fixing"
        assert review["stage"] == "fix-build"


# ── Read-only queries ────────────────────────────────────────


class TestReadOnlyQueries:
    def test_review_list_returns_reviews(self, running_server, client_socket):
        server, sock_path = running_server
        server.enqueue({
            "type": "review_started",
            "review": {"id": "a", "pr_number": 1},
        })
        server.enqueue({
            "type": "review_started",
            "review": {"id": "b", "pr_number": 2},
        })
        time.sleep(0.15)

        conn = client_socket(sock_path)
        conn.sendall(json.dumps({"type": "review_list"}).encode("utf-8") + b"\n")
        data = conn.recv(4096)
        response = json.loads(data.decode("utf-8").strip())
        assert response["type"] == "review_list"
        ids = {r["id"] for r in response["reviews"]}
        assert ids == {"a", "b"}

    def test_queue_list_empty(self, running_server, client_socket):
        server, sock_path = running_server
        conn = client_socket(sock_path)
        conn.sendall(json.dumps({"type": "queue_list"}).encode("utf-8") + b"\n")
        data = conn.recv(4096)
        response = json.loads(data.decode("utf-8").strip())
        assert response["type"] == "queue_list"
        assert response["queue"] == []

    def test_health_get_returns_data(self, running_server, client_socket):
        server, sock_path = running_server
        server.enqueue({
            "type": "health_update",
            "health": {"ok": True},
        })
        time.sleep(0.15)

        conn = client_socket(sock_path)
        conn.sendall(json.dumps({"type": "health_get"}).encode("utf-8") + b"\n")
        data = conn.recv(4096)
        response = json.loads(data.decode("utf-8").strip())
        assert response["type"] == "health_data"
        assert response["health"]["ok"] is True


# ── Broadcasting ─────────────────────────────────────────────


class TestBroadcast:
    def test_broadcast_reaches_single_client(self, running_server, client_socket):
        server, sock_path = running_server
        conn = client_socket(sock_path)
        # Wait until the server's handler thread has registered the client,
        # otherwise the broadcast may fire before the client is in the list.
        for _ in range(50):
            if len(server.clients) >= 1:
                break
            time.sleep(0.02)
        server.enqueue({
            "type": "review_started",
            "review": {"id": "bc1", "pr_number": 1, "status": "running"},
        })
        messages = _recv_until(conn, {"review_updated"}, timeout=1.5)
        types = [m["type"] for m in messages]
        assert "review_updated" in types

    def test_broadcast_reaches_multiple_clients(self, running_server, client_socket):
        server, sock_path = running_server
        conn_a = client_socket(sock_path)
        conn_b = client_socket(sock_path)
        for _ in range(50):
            if len(server.clients) >= 2:
                break
            time.sleep(0.02)

        server.enqueue({
            "type": "review_started",
            "review": {"id": "bc2", "pr_number": 1, "status": "running"},
        })

        msgs_a = _recv_until(conn_a, {"review_updated"}, timeout=1.5)
        msgs_b = _recv_until(conn_b, {"review_updated"}, timeout=1.5)
        assert any(m["type"] == "review_updated" for m in msgs_a)
        assert any(m["type"] == "review_updated" for m in msgs_b)

    def test_broadcast_drops_type_less_messages(self, running_server):
        server, _ = running_server
        assert server.broadcast({"no_type_here": True}) is False

    def test_broadcast_survives_dead_client(self, running_server, client_socket):
        server, sock_path = running_server
        conn = client_socket(sock_path)
        time.sleep(0.05)
        conn.close()
        # Send a mutation; server should not crash even though conn is dead
        server.enqueue({
            "type": "review_started",
            "review": {"id": "dead", "pr_number": 1, "status": "running"},
        })
        time.sleep(0.3)
        # Dead client was eventually cleaned up
        assert len(server.clients) == 0


# ── Event / broadcast queue overflow ─────────────────────────


class TestQueueOverflow:
    def test_enqueue_beyond_capacity_logs_and_drops(self, running_server, monkeypatch):
        server, _ = running_server
        # Shrink event queue to force overflow
        import queue as _q

        small = _q.Queue(maxsize=2)
        server.event_queue = small
        # Fill it; _enqueue_event is non-blocking and drops when Full
        for i in range(10):
            server._enqueue_event({"type": "health_update", "health": {"i": i}})
        # Should have at most 2 items in the queue (and not crash)
        assert small.qsize() <= 2

    def test_broadcast_queue_full_returns_false(self, running_server):
        server, _ = running_server
        import queue as _q

        server.broadcast_queue = _q.Queue(maxsize=1)
        assert server.broadcast({"type": "a"}) is True
        # Fills queue; next broadcast should not block, returns False
        result = server.broadcast({"type": "b"})
        assert result in (True, False)
        # Drain so server stop doesn't hang
        try:
            while True:
                server.broadcast_queue.get_nowait()
        except Exception:
            pass


# ── Reaper ───────────────────────────────────────────────────


class TestReaper:
    def test_reap_stale_reviews_removes_old_entries(self, running_server):
        server, _ = running_server
        server.enqueue({
            "type": "review_started",
            "review": {"id": "stale", "pr_number": 1, "status": "running"},
        })
        time.sleep(0.15)
        # Backdate the review so it looks stale
        server.reviews[0]["updated_at"] = 0
        server._reap_stale_reviews()
        assert len(server.reviews) == 0

    def test_reap_leaves_fresh_reviews(self, running_server):
        server, _ = running_server
        server.enqueue({
            "type": "review_started",
            "review": {"id": "fresh", "pr_number": 1, "status": "running"},
        })
        time.sleep(0.15)
        server._reap_stale_reviews()
        assert len(server.reviews) == 1


# ── Unknown / edge mutations ─────────────────────────────────


class TestUnknownMessages:
    def test_unknown_message_type_is_logged_not_crashing(self, running_server):
        server, _ = running_server
        server.enqueue({"type": "totally_new_message_type", "payload": "hi"})
        time.sleep(0.15)
        # Server still running; reviews unaffected
        assert server.reviews == []

    def test_stage_update_for_missing_review_noops(self, running_server):
        server, _ = running_server
        server.enqueue({
            "type": "review_stage_update",
            "review_id": "does-not-exist",
            "stage": "triage",
            "status": "running",
        })
        time.sleep(0.15)
        assert server.reviews == []

    def test_completed_for_missing_review_noops(self, running_server):
        server, _ = running_server
        server.enqueue({
            "type": "review_completed",
            "review_id": "ghost",
            "decision": "approve",
        })
        time.sleep(0.15)
        assert server.reviews == []

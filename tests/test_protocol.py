"""Tests for the schema-versioned broadcast envelope (Phase 0).

These guard the contract documented in ``docs/protocol.md``: every
broadcast must carry ``v``, ``id``, ``ts``, ``type`` (in that header
position), and ``id`` must increment monotonically across the process
lifetime. RPC responses are deliberately *not* enveloped — that's
verified here too so a future refactor doesn't accidentally start
versioning point-to-point responses.
"""

import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from gate.server import PROTOCOL_VERSION, GateServer


def _short_sock_path() -> Path:
    return Path(tempfile.mkdtemp(prefix="gs")) / "s.sock"


def _wait_for_socket(p: Path, timeout: float = 2.5) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if p.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(p)


def _drain(conn: socket.socket, want_types: set[str], timeout: float = 2.0) -> list[dict]:
    conn.settimeout(timeout)
    buf = ""
    out: list[dict] = []
    seen: set[str] = set()
    deadline = time.time() + timeout
    while time.time() < deadline and not want_types.issubset(seen):
        try:
            data = conn.recv(8192)
            if not data:
                break
            buf += data.decode("utf-8")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                out.append(msg)
                if "type" in msg:
                    seen.add(msg["type"])
        except socket.timeout:
            break
    return out


@pytest.fixture
def running_server():
    sock = _short_sock_path()
    server = GateServer(sock)
    t = threading.Thread(target=server.start, daemon=True)
    t.start()
    _wait_for_socket(sock)
    try:
        yield server, sock
    finally:
        server.stop()
        t.join(timeout=2.0)


def _connect(sock: Path) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect(str(sock))
    return s


# ── Envelope shape ──────────────────────────────────────────


def test_make_envelope_has_required_fields():
    server = GateServer(_short_sock_path())
    env = server._make_envelope({"type": "review_updated", "review": {"id": "x"}})
    assert env["v"] == PROTOCOL_VERSION == 1
    assert env["type"] == "review_updated"
    assert env["id"] == 1
    assert isinstance(env["ts"], int)
    assert env["review"] == {"id": "x"}


def test_envelope_id_is_monotonic():
    server = GateServer(_short_sock_path())
    ids = [
        server._make_envelope({"type": "queue_updated", "queue": []})["id"]
        for _ in range(50)
    ]
    assert ids == list(range(1, 51))


def test_envelope_header_keys_come_first():
    """Documented order in protocol.md: v, id, ts, type, then payload."""
    server = GateServer(_short_sock_path())
    env = server._make_envelope({"type": "review_updated", "review": {}})
    keys = list(env.keys())
    assert keys[:4] == ["v", "id", "ts", "type"]


def test_envelope_payload_type_overrides_collision():
    """If payload sneaks in v/id/ts they must not overwrite the envelope."""
    server = GateServer(_short_sock_path())
    env = server._make_envelope({
        "type": "review_updated",
        "v": 999,
        "id": 999,
        "ts": 0,
        "review": {},
    })
    assert env["v"] == 1
    assert env["id"] == 1
    assert env["ts"] != 0


# ── Live broadcast envelope ─────────────────────────────────


def test_broadcast_event_is_enveloped(running_server):
    server, sock = running_server
    conn = _connect(sock)

    # Drain initial connect ACK so it doesn't pollute our slice.
    conn.sendall(b'{"type":"ping"}\n')
    _drain(conn, {"pong"})

    server.broadcast({"type": "review_updated", "review": {"id": "abc"}})

    msgs = _drain(conn, {"review_updated"})
    review_updates = [m for m in msgs if m.get("type") == "review_updated"]
    assert review_updates, msgs
    env = review_updates[0]
    assert env["v"] == 1
    assert env["type"] == "review_updated"
    assert isinstance(env["id"], int) and env["id"] >= 1
    assert isinstance(env["ts"], int)
    assert env["review"] == {"id": "abc"}
    conn.close()


def test_all_mutation_types_get_enveloped(running_server):
    """Smoke-fire each broadcast event type and assert each emerges enveloped."""
    server, sock = running_server
    conn = _connect(sock)
    conn.sendall(b'{"type":"ping"}\n')
    _drain(conn, {"pong"})

    server.broadcast({"type": "review_updated", "review": {"id": "1"}})
    server.broadcast({"type": "review_completed", "review": {"id": "1"}})
    server.broadcast({"type": "review_cancelled", "review": {"id": "1"}})
    server.broadcast({"type": "queue_updated", "queue": []})
    server.broadcast({"type": "health_updated", "health": {}})

    expected = {
        "review_updated", "review_completed", "review_cancelled",
        "queue_updated", "health_updated",
    }
    msgs = _drain(conn, expected, timeout=3.0)
    by_type = {m["type"]: m for m in msgs if m.get("type") in expected}
    assert set(by_type.keys()) == expected, list(by_type.keys())
    ids_seen: list[int] = []
    for t, env in by_type.items():
        assert env["v"] == 1, t
        assert env["type"] == t
        assert isinstance(env["id"], int)
        assert isinstance(env["ts"], int)
        ids_seen.append(env["id"])
    assert sorted(ids_seen) == ids_seen, "broadcast ids must arrive in order"
    conn.close()


# ── Shutdown is enveloped ───────────────────────────────────


def test_shutdown_event_is_enveloped():
    sock = _short_sock_path()
    server = GateServer(sock)
    t = threading.Thread(target=server.start, daemon=True)
    t.start()
    _wait_for_socket(sock)

    conn = _connect(sock)
    conn.sendall(b'{"type":"ping"}\n')
    _drain(conn, {"pong"})

    server.stop()
    t.join(timeout=2.0)

    conn.settimeout(2.0)
    buf = b""
    deadline = time.time() + 2.0
    while time.time() < deadline:
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
    conn.close()

    shutdown_lines = [
        json.loads(line)
        for line in buf.decode("utf-8").splitlines()
        if line.strip() and json.loads(line).get("type") == "shutdown"
    ]
    assert shutdown_lines, buf
    env = shutdown_lines[0]
    assert env["v"] == 1
    assert env["type"] == "shutdown"
    assert isinstance(env["id"], int) and env["id"] >= 1
    assert isinstance(env["ts"], int)


# ── RPC responses are NOT enveloped ─────────────────────────


def test_rpc_response_is_not_enveloped(running_server):
    """connect/ping/list responses keep ts but get no v/id."""
    _server, sock = running_server
    conn = _connect(sock)
    conn.sendall(b'{"type":"ping"}\n')
    msgs = _drain(conn, {"pong"})
    pong = next(m for m in msgs if m.get("type") == "pong")
    assert "ts" in pong
    assert "v" not in pong
    assert "id" not in pong
    conn.close()


def test_review_list_response_is_not_enveloped(running_server):
    _server, sock = running_server
    conn = _connect(sock)
    conn.sendall(b'{"type":"review_list"}\n')
    msgs = _drain(conn, {"review_list"})
    rl = next(m for m in msgs if m.get("type") == "review_list")
    assert "v" not in rl
    assert "id" not in rl
    assert "reviews" in rl
    conn.close()


# ── Invariant: broadcasts without "type" still rejected ─────


def test_broadcast_without_type_is_rejected():
    server = GateServer(_short_sock_path())
    assert server.broadcast({"review": {}}) is False

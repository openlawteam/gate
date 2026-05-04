"""Stress test the broadcast machinery with many concurrent subscribers.

Phase 0 doesn't *add* multi-subscriber support — :class:`gate.server.GateServer`
already accepts arbitrarily many AF_UNIX clients. What changed is the
envelope, and the new in-process ``subscribe()`` API used by the SSE
adapter. These tests confirm both AF_UNIX *and* in-process subscribers
receive every event in order, with no duplicates, under load.

If a future refactor accidentally drops events on the writer thread or
reorders them between subscribers, these tests will fail.
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from gate.server import GateServer


CLIENT_COUNT = 25
EVENT_COUNT = 500


def _short_sock_path() -> Path:
    return Path(tempfile.mkdtemp(prefix="gs")) / "s.sock"


def _wait_for_socket(p: Path, timeout: float = 2.5) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if p.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(p)


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


def _drain_socket(conn: socket.socket, expected_count: int, timeout: float) -> list[dict]:
    conn.settimeout(timeout)
    buf = ""
    out: list[dict] = []
    deadline = time.time() + timeout
    while time.time() < deadline and len(out) < expected_count:
        try:
            data = conn.recv(65536)
            if not data:
                break
            buf += data.decode("utf-8")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except socket.timeout:
            break
    return out


# ── AF_UNIX multi-subscriber ────────────────────────────────


def test_many_unix_clients_all_receive_all_events_in_order(running_server):
    server, sock = running_server
    clients: list[socket.socket] = []
    for _ in range(CLIENT_COUNT):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(str(sock))
        clients.append(s)

    # The server accepts in a single loop; give it time to register
    # all CLIENT_COUNT clients into ``self.clients`` before we start
    # broadcasting (otherwise the first events fan out to fewer than
    # CLIENT_COUNT subscribers).
    deadline = time.time() + 3.0
    while time.time() < deadline:
        with server.lock:
            if len(server.clients) >= CLIENT_COUNT:
                break
        time.sleep(0.05)
    with server.lock:
        assert len(server.clients) == CLIENT_COUNT, (
            f"server only registered {len(server.clients)}/{CLIENT_COUNT} clients"
        )

    for i in range(EVENT_COUNT):
        server.broadcast({"type": "review_updated", "review": {"i": i}})

    received: list[list[dict]] = []
    threads: list[threading.Thread] = []

    def _drain_one(c, idx):
        received.append([])
        msgs = _drain_socket(c, EVENT_COUNT, timeout=8.0)
        review_msgs = [
            m for m in msgs
            if m.get("type") == "review_updated" and "review" in m
        ]
        received[idx] = review_msgs

    for idx, c in enumerate(clients):
        t = threading.Thread(target=_drain_one, args=(c, idx), daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    for c in clients:
        c.close()

    for idx, msgs in enumerate(received):
        assert len(msgs) == EVENT_COUNT, (
            f"client {idx} got {len(msgs)} of {EVENT_COUNT} events"
        )
        ids = [m["id"] for m in msgs]
        assert ids == sorted(ids), f"client {idx} received events out of order"
        assert len(set(ids)) == len(ids), f"client {idx} received duplicate ids"
        first = received[0]
        assert [m["review"]["i"] for m in msgs] == [
            m["review"]["i"] for m in first
        ], f"client {idx} received different payload sequence than client 0"


# ── In-process subscribe() multi-subscriber ─────────────────


def test_many_inprocess_subscribers_all_receive_all_events_in_order(running_server):
    server, _ = running_server
    queues = [server.subscribe(maxsize=EVENT_COUNT * 2) for _ in range(CLIENT_COUNT)]

    for i in range(EVENT_COUNT):
        server.broadcast({"type": "review_updated", "review": {"i": i}})

    deadline = time.time() + 5.0
    received: list[list[dict]] = [[] for _ in queues]
    while time.time() < deadline:
        if all(len(r) >= EVENT_COUNT for r in received):
            break
        for idx, q in enumerate(queues):
            try:
                while True:
                    received[idx].append(q.get_nowait())
            except Exception:
                pass
        time.sleep(0.01)

    for idx, msgs in enumerate(received):
        assert len(msgs) == EVENT_COUNT, (
            f"subscriber {idx} got {len(msgs)} of {EVENT_COUNT}"
        )
        ids = [m["id"] for m in msgs]
        assert ids == sorted(ids), f"subscriber {idx} out of order"
        assert len(set(ids)) == len(ids), f"subscriber {idx} duplicates"
        assert all(m["v"] == 1 for m in msgs)


def test_slow_inprocess_subscriber_is_evicted_not_blocking(running_server):
    """A subscriber whose queue fills must be evicted, not back-pressure."""
    server, _ = running_server

    fast = server.subscribe(maxsize=EVENT_COUNT * 2)
    slow = server.subscribe(maxsize=2)

    for i in range(50):
        server.broadcast({"type": "review_updated", "review": {"i": i}})

    deadline = time.time() + 5.0
    fast_msgs: list[dict] = []
    while time.time() < deadline and len(fast_msgs) < 50:
        try:
            while True:
                fast_msgs.append(fast.get_nowait())
        except Exception:
            time.sleep(0.01)

    assert len(fast_msgs) == 50, (
        "fast subscriber should not lose events even if a slow peer was evicted"
    )

    with server._subscriber_lock:
        assert slow not in server._subscribers, "slow subscriber should be evicted"


# ── Mixed subscriber types ──────────────────────────────────


def test_unix_and_inprocess_subscribers_see_identical_envelope_sequence(running_server):
    """Both transports see the same id/ts/type sequence for the same broadcasts."""
    server, sock = running_server

    in_proc = server.subscribe(maxsize=2 * EVENT_COUNT)

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5.0)
    s.connect(str(sock))
    deadline = time.time() + 2.0
    while time.time() < deadline:
        with server.lock:
            if len(server.clients) >= 1:
                break
        time.sleep(0.05)

    for i in range(EVENT_COUNT):
        server.broadcast({"type": "queue_updated", "queue": [{"i": i}]})

    socket_msgs = _drain_socket(s, EVENT_COUNT, timeout=8.0)
    socket_msgs = [m for m in socket_msgs if m.get("type") == "queue_updated"]
    s.close()

    deadline = time.time() + 5.0
    inproc_msgs: list[dict] = []
    while time.time() < deadline and len(inproc_msgs) < EVENT_COUNT:
        try:
            while True:
                inproc_msgs.append(in_proc.get_nowait())
        except Exception:
            time.sleep(0.01)

    assert len(socket_msgs) == EVENT_COUNT
    assert len(inproc_msgs) == EVENT_COUNT
    socket_ids = [m["id"] for m in socket_msgs]
    inproc_ids = [m["id"] for m in inproc_msgs]
    assert socket_ids == inproc_ids, (
        "AF_UNIX and in-process subscribers must see the same id sequence"
    )

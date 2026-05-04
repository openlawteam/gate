"""Tests for the HTTP/SSE adapter (gate.web_events).

Coverage targets:

* Auth: missing/invalid bearer rejected; ``token=`` querystring rejected
  even when a valid header is present.
* /v1/health: returns liveness without auth.
* /v1/state: returns initial-frame snapshot with auth.
* /v1/events: SSE framing (``id:`` ``event:`` ``data:``); replays
  buffered events; ``Last-Event-ID`` resume serves from buffer when
  in-range and falls back to ``state_resync`` when out-of-range; live
  events stream after backlog.
* The ring buffer reflects every broadcast routed through the server.

Tests use httpx ``ASGITransport`` so we don't bind real ports — fast
and deterministic.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest

from gate.server import GateServer
from gate.web_events import (
    DEFAULT_RING_BUFFER_SIZE,
    _EventRingBuffer,
    _format_sse_frame,
    _parse_last_event_id,
    _split_addr,
    create_app,
)


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
        yield server
    finally:
        server.stop()
        t.join(timeout=2.0)


@pytest.fixture
def app_and_server(running_server):
    app = create_app(running_server, token="test-token", ring_buffer_size=50)
    yield app, running_server


# ── Helpers ─────────────────────────────────────────────────


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _parse_frame(frame: str) -> dict:
    event: dict[str, str] = {}
    for line in frame.splitlines():
        if line.startswith(":"):
            event["__heartbeat__"] = line[1:].strip()
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        event[key.strip()] = value.lstrip()
    return event


def _split_frames(byts: bytes) -> list[dict]:
    events: list[dict] = []
    text = byts.decode("utf-8")
    for chunk in text.split("\n\n"):
        if chunk.strip():
            events.append(_parse_frame(chunk))
    return events


def _make_client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://t")


class _MockRequest:
    """Stand-in for ``starlette.requests.Request`` for generator tests.

    The SSE generator only touches ``request.receive`` (to detect
    ``http.disconnect``). Tests drive disconnects by setting
    :attr:`disconnected`.
    """

    def __init__(self) -> None:
        self.disconnected = asyncio.Event()

    async def receive(self) -> dict:
        await self.disconnected.wait()
        return {"type": "http.disconnect"}


async def _drain_generator(
    gen, *, max_frames: int, timeout: float = 2.0
) -> list[dict]:
    """Pull up to ``max_frames`` complete SSE frames out of an async generator."""
    frames: list[dict] = []
    deadline = time.monotonic() + timeout
    while len(frames) < max_frames:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            chunk = await asyncio.wait_for(gen.__anext__(), timeout=remaining)
        except (asyncio.TimeoutError, StopAsyncIteration):
            break
        for ev in _split_frames(chunk):
            frames.append(ev)
            if len(frames) >= max_frames:
                break
    return frames


# ── Pure helpers ────────────────────────────────────────────


class TestPureHelpers:
    def test_format_sse_frame(self):
        env = {"v": 1, "id": 7, "ts": 123, "type": "review_updated", "review": {}}
        frame = _format_sse_frame(env).decode("utf-8")
        lines = frame.splitlines()
        assert lines[0] == "id: 7"
        assert lines[1] == "event: review_updated"
        assert lines[2].startswith("data: ")
        assert json.loads(lines[2].removeprefix("data: ")) == env
        assert frame.endswith("\n\n")

    def test_parse_last_event_id(self):
        assert _parse_last_event_id(None) == 0
        assert _parse_last_event_id("") == 0
        assert _parse_last_event_id("42") == 42
        assert _parse_last_event_id("not-an-int") == 0

    def test_split_addr(self):
        assert _split_addr(":7080") == ("127.0.0.1", 7080)
        assert _split_addr("0.0.0.0:7080") == ("0.0.0.0", 7080)
        assert _split_addr("127.0.0.1:9999") == ("127.0.0.1", 9999)
        with pytest.raises(ValueError):
            _split_addr("7080")
        with pytest.raises(ValueError):
            _split_addr(":abc")
        with pytest.raises(ValueError):
            _split_addr(":99999")


class TestRingBuffer:
    def test_buffer_returns_all_when_last_id_zero(self):
        ring = _EventRingBuffer(10)
        for i in range(5):
            ring.append({"id": i + 1, "type": "x"})
        out, ok = ring.since(0)
        assert ok is True
        assert [e["id"] for e in out] == [1, 2, 3, 4, 5]

    def test_buffer_returns_strictly_newer_than_last_id(self):
        ring = _EventRingBuffer(10)
        for i in range(5):
            ring.append({"id": i + 1, "type": "x"})
        out, ok = ring.since(3)
        assert ok is True
        assert [e["id"] for e in out] == [4, 5]

    def test_buffer_aged_out_id_is_unservable(self):
        ring = _EventRingBuffer(3)
        for i in range(10):
            ring.append({"id": i + 1, "type": "x"})
        out, ok = ring.since(2)
        assert ok is False
        assert out == []

    def test_buffer_id_higher_than_newest_is_unservable(self):
        ring = _EventRingBuffer(10)
        for i in range(3):
            ring.append({"id": i + 1, "type": "x"})
        out, ok = ring.since(99)
        assert ok is False

    def test_buffer_exact_boundary_servable(self):
        ring = _EventRingBuffer(5)
        for i in range(5):
            ring.append({"id": i + 1, "type": "x"})
        out, ok = ring.since(1)
        assert ok is True
        assert [e["id"] for e in out] == [2, 3, 4, 5]


# ── App construction ────────────────────────────────────────


def test_create_app_rejects_empty_token(running_server):
    with pytest.raises(ValueError):
        create_app(running_server, token="")


def test_create_app_default_buffer_size(running_server):
    app = create_app(running_server, token="t")
    assert isinstance(app.state.gate_ring, _EventRingBuffer)
    assert app.state.gate_ring._buf.maxlen == DEFAULT_RING_BUFFER_SIZE


# ── /v1/health ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_no_auth_required(app_and_server):
    app, _ = app_and_server
    async with _make_client(app) as client:
        r = await client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["version"] == 1
    assert isinstance(body["started_at"], int)


# ── Auth ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_state_requires_auth(app_and_server):
    app, _ = app_and_server
    async with _make_client(app) as client:
        r = await client.get("/v1/state")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_state_rejects_wrong_token(app_and_server):
    app, _ = app_and_server
    async with _make_client(app) as client:
        r = await client.get("/v1/state", headers=_bearer("wrong"))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_state_accepts_valid_bearer(app_and_server):
    app, server = app_and_server
    server.reviews.append({"id": "myorg-myrepo-pr1", "pr_number": 1})
    server.review_queue.append({"pr_number": 2})
    async with _make_client(app) as client:
        r = await client.get("/v1/state", headers=_bearer("test-token"))
    assert r.status_code == 200
    body = r.json()
    assert body["reviews"][0]["pr_number"] == 1
    assert body["queue"][0]["pr_number"] == 2


@pytest.mark.asyncio
async def test_querystring_token_explicitly_rejected(app_and_server):
    """A misconfigured client must fail loud, not silently authenticate."""
    app, _ = app_and_server
    async with _make_client(app) as client:
        r = await client.get(
            "/v1/state?token=test-token", headers=_bearer("test-token")
        )
    assert r.status_code == 400
    assert "Authorization" in r.json()["detail"]


# ── /v1/events SSE stream ───────────────────────────────────


@pytest.mark.asyncio
async def test_events_requires_auth(app_and_server):
    app, _ = app_and_server
    async with _make_client(app) as client:
        r = await client.get("/v1/events")
    assert r.status_code == 401


# ── Streaming generator unit tests ──────────────────────────
#
# httpx's ASGITransport buffers streaming responses fully before
# delivering chunks (see https://github.com/encode/httpx/issues/2186),
# which makes it unsuitable for SSE end-to-end tests. Drive the
# generator directly instead — same code path, none of the buffering.


@pytest.mark.asyncio
async def test_event_stream_replays_backlog_then_emits_live(running_server):
    from gate.web_events import _event_stream, _EventRingBuffer

    server = running_server
    ring = _EventRingBuffer(50)
    server.subscribe(maxsize=100)

    server.broadcast({"type": "review_updated", "review": {"id": "early-1"}})
    server.broadcast({"type": "review_updated", "review": {"id": "early-2"}})
    await asyncio.sleep(0.2)
    for env in [
        {"v": 1, "id": 1, "ts": 1, "type": "review_updated", "review": {"id": "early-1"}},
        {"v": 1, "id": 2, "ts": 2, "type": "review_updated", "review": {"id": "early-2"}},
    ]:
        ring.append(env)

    req = _MockRequest()
    gen = _event_stream(req, ring, last_event_id=0)

    backlog = await _drain_generator(gen, max_frames=2, timeout=2.0)
    assert [json.loads(b["data"])["review"]["id"] for b in backlog] == [
        "early-1", "early-2"
    ]

    ring.append({
        "v": 1, "id": 3, "ts": 3,
        "type": "review_updated", "review": {"id": "live-1"},
    })
    live = await _drain_generator(gen, max_frames=1, timeout=2.0)
    assert live, "expected live event"
    assert json.loads(live[0]["data"])["review"]["id"] == "live-1"

    req.disconnected.set()
    await asyncio.wait_for(gen.aclose(), timeout=1.0)


@pytest.mark.asyncio
async def test_event_stream_resume_in_range_serves_from_buffer():
    from gate.web_events import _event_stream, _EventRingBuffer

    ring = _EventRingBuffer(50)
    for i in range(5):
        ring.append({
            "v": 1, "id": i + 1, "ts": i,
            "type": "review_updated", "review": {"id": f"e-{i}"},
        })

    req = _MockRequest()
    gen = _event_stream(req, ring, last_event_id=3)
    frames = await _drain_generator(gen, max_frames=2, timeout=2.0)

    payloads = [json.loads(f["data"]) for f in frames]
    assert [p["id"] for p in payloads] == [4, 5]

    req.disconnected.set()
    await asyncio.wait_for(gen.aclose(), timeout=1.0)


@pytest.mark.asyncio
async def test_event_stream_resume_out_of_range_emits_state_resync():
    from gate.web_events import _event_stream, _EventRingBuffer

    ring = _EventRingBuffer(20)
    for i in range(20):
        ring.append({
            "v": 1, "id": 100 + i, "ts": 0,
            "type": "review_updated", "review": {},
        })

    req = _MockRequest()
    gen = _event_stream(req, ring, last_event_id=1)
    frames = await _drain_generator(gen, max_frames=1, timeout=2.0)
    assert frames, "expected state_resync"
    payload = json.loads(frames[0]["data"])
    assert payload["type"] == "state_resync"
    assert payload["reason"] == "buffer_overflow"

    req.disconnected.set()
    await asyncio.wait_for(gen.aclose(), timeout=1.0)


@pytest.mark.asyncio
async def test_event_stream_disconnect_terminates_generator():
    from gate.web_events import _event_stream, _EventRingBuffer

    ring = _EventRingBuffer(10)
    req = _MockRequest()
    gen = _event_stream(req, ring, last_event_id=0)

    # Drain initial backlog (none).
    await _drain_generator(gen, max_frames=0, timeout=0.1)

    req.disconnected.set()
    # The generator must terminate — if disconnect detection is broken,
    # this will hang and the test timeout fires.
    await asyncio.wait_for(gen.aclose(), timeout=1.0)


@pytest.mark.asyncio
async def test_event_stream_shutdown_event_terminates_stream():
    from gate.web_events import _event_stream, _EventRingBuffer

    ring = _EventRingBuffer(10)
    ring.append({
        "v": 1, "id": 1, "ts": 0,
        "type": "review_updated", "review": {"id": "x"},
    })
    req = _MockRequest()
    gen = _event_stream(req, ring, last_event_id=0)

    backlog = await _drain_generator(gen, max_frames=1, timeout=1.0)
    assert backlog

    ring.append({"v": 1, "id": 2, "ts": 0, "type": "shutdown"})
    final = await _drain_generator(gen, max_frames=1, timeout=2.0)
    assert final
    assert json.loads(final[0]["data"])["type"] == "shutdown"

    # After yielding shutdown, the generator returns. Confirm it's exhausted.
    with pytest.raises((StopAsyncIteration, asyncio.TimeoutError)):
        await asyncio.wait_for(gen.__anext__(), timeout=0.5)

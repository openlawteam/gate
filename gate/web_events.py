"""HTTP/SSE adapter for the Gate event protocol.

Re-broadcasts the AF_UNIX broadcast stream as Server-Sent Events over
HTTP, so external clients (the team dashboard, custom tooling) can
subscribe without speaking the AF_UNIX framing or running on the same
machine.

Design notes:

* The adapter is **in-process**. It registers as an
  :meth:`gate.server.GateServer.subscribe` consumer rather than going
  back through the AF_UNIX socket. This avoids the JSON encode → socket
  → JSON decode round-trip per event.
* A single background pump thread consumes from the server-subscription
  queue and writes into a ring buffer. Per-HTTP-client SSE generators
  read from the ring buffer; this lets ``Last-Event-ID`` resume work
  across short network blips even if no client is currently connected.
* Auth is bearer-token-only and rejects ``token=`` querystrings
  explicitly so misconfigured clients fail loud rather than silently
  leaking the token into proxy access logs.
* CORS is off by default (production deployment serves the dashboard
  static bundle from the same FastAPI app — same-origin). The optional
  ``cors_origin`` argument enables CORS for a single origin during
  development.

This module imports ``fastapi`` lazily inside :func:`create_app` so
``import gate.web_events`` does not pull in FastAPI for users who never
run ``gate up --serve-events``. The ``web`` extra
(``pip install gate[web]``) provides ``fastapi`` and ``uvicorn``.
"""

from __future__ import annotations

import json
import logging
import queue
import secrets
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

# Hoisted to module top-level on purpose: ``from __future__ import
# annotations`` turns every annotation into a string, and FastAPI then
# resolves each route's parameter types by looking them up in the
# *defining function's __globals__*. That globals dict is the module
# scope — not the enclosing function — so ``Request`` MUST live at
# module scope or FastAPI silently treats ``request: Request`` as a
# query parameter named ``request``. Importing here is fine: this
# module is only imported when ``gate up --serve-events`` is used.
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

if TYPE_CHECKING:
    from gate.server import GateServer

logger = logging.getLogger(__name__)

DEFAULT_RING_BUFFER_SIZE = 1000
SUBSCRIBER_QUEUE_SIZE = 2000


def _current_ms() -> int:
    return int(time.time() * 1000)


class _EventRingBuffer:
    """Bounded in-memory buffer of recently broadcast envelopes.

    Used to serve ``Last-Event-ID`` resume requests. Older events are
    dropped silently as new ones arrive — a client whose
    ``Last-Event-ID`` has aged out gets a synthetic ``state_resync``
    event instructing it to refetch ``/v1/state``.

    Threading model:

    * Single producer (the :class:`_SubscriberPump` thread) calls
      :meth:`append`.
    * Many consumers (one async per HTTP client) read via
      :meth:`since`/:meth:`snapshot` and wake on
      :meth:`add_async_waiter` / :meth:`remove_async_waiter`.

    Async waiters are :class:`asyncio.Event` instances handed back to
    the SSE generator. The producer thread sets them via
    ``loop.call_soon_threadsafe`` so disconnect / cancellation is
    detected immediately by the asyncio scheduler, instead of polling
    via ``asyncio.to_thread``.
    """

    def __init__(self, capacity: int) -> None:
        self._buf: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._waiters: list[tuple[Any, Any]] = []  # (loop, event)

    def append(self, envelope: dict) -> None:
        with self._lock:
            self._buf.append(envelope)
            waiters = list(self._waiters)
        for loop, event in waiters:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                # Loop already closed; the consumer has gone, drop it.
                self.remove_async_waiter(loop, event)

    def add_async_waiter(self, loop: Any, event: Any) -> None:
        with self._lock:
            self._waiters.append((loop, event))

    def remove_async_waiter(self, loop: Any, event: Any) -> None:
        with self._lock:
            try:
                self._waiters.remove((loop, event))
            except ValueError:
                pass

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._buf)

    def since(self, last_id: int) -> tuple[list[dict], bool]:
        """Return events strictly newer than ``last_id``.

        Second tuple element is ``True`` when the buffer can serve the
        request (i.e. ``last_id`` is either ``0`` or appears in the
        buffer's range), ``False`` when the request must be rejected
        with a ``state_resync`` because the requested id has already
        aged out (or is from a previous server-process incarnation).
        """
        with self._lock:
            if not self._buf:
                return [], True
            oldest_id = self._buf[0].get("id", 0)
            newest_id = self._buf[-1].get("id", 0)
            if last_id == 0:
                return list(self._buf), True
            if last_id > newest_id:
                return [], False
            if last_id < oldest_id - 1:
                return [], False
            return [
                e for e in self._buf
                if isinstance(e.get("id"), int) and e["id"] > last_id
            ], True

    def newer_than(self, last_seen_id: int) -> list[dict]:
        """Snapshot of events with ``id`` strictly greater than ``last_seen_id``."""
        with self._lock:
            return [
                e for e in self._buf
                if isinstance(e.get("id"), int) and e["id"] > last_seen_id
            ]


class _SubscriberPump:
    """Drains a server subscription queue into the ring buffer.

    Lives in a single daemon thread. Stops cleanly when the server's
    ``shutdown`` envelope arrives or when :meth:`stop` is called.
    """

    def __init__(
        self,
        server: GateServer,
        ring: _EventRingBuffer,
    ) -> None:
        self._server = server
        self._ring = ring
        self._queue = server.subscribe(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="sse-pump", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._server.unsubscribe(self._queue)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                envelope = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._ring.append(envelope)
            if envelope.get("type") == "shutdown":
                break


def _format_sse_frame(envelope: dict) -> bytes:
    """Render a single SSE frame from an enveloped event."""
    data = json.dumps(envelope, separators=(",", ":"))
    eid = envelope.get("id", 0)
    etype = envelope.get("type", "message")
    return f"id: {eid}\nevent: {etype}\ndata: {data}\n\n".encode("utf-8")


def _parse_last_event_id(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _state_snapshot(server: GateServer) -> dict[str, Any]:
    """Initial-frame snapshot for ``/v1/state``."""
    return {
        "reviews": list(server.reviews),
        "queue": list(server.review_queue),
        "health": dict(server.health),
        "started_at": server.started_at,
    }


def create_app(
    server: GateServer,
    token: str,
    *,
    ring_buffer_size: int = DEFAULT_RING_BUFFER_SIZE,
    cors_origin: str | None = None,
) -> "FastAPI":
    """Construct the FastAPI app for ``gate up --serve-events``.

    The app wires:

    * a single background pump that drains broadcasts into a ring buffer
    * three HTTP routes (``/v1/health``, ``/v1/state``, ``/v1/events``)
    * bearer-token auth (rejects ``token=`` querystring)

    Importable for tests without binding a port (use FastAPI's
    ``TestClient`` or httpx ``ASGITransport``).
    """
    if not token:
        raise ValueError(
            "create_app requires a non-empty bearer token; refusing to "
            "expose the SSE adapter unauthenticated"
        )

    ring = _EventRingBuffer(ring_buffer_size)
    pump = _SubscriberPump(server, ring)
    # Start the pump eagerly — by the time create_app returns, every
    # broadcast that follows must land in the ring buffer. Deferring to
    # the lifespan handler would mean tests using httpx's ASGITransport
    # (which doesn't run lifespan by default) would silently drop events.
    pump.start()

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        try:
            yield
        finally:
            pump.stop()

    app = FastAPI(
        title="Gate event stream",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )

    if cors_origin:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=[cors_origin],
            allow_methods=["GET"],
            allow_headers=["Authorization", "Last-Event-ID"],
        )

    app.state.gate_server = server
    app.state.gate_ring = ring
    app.state.gate_pump = pump
    app.state.gate_token = token

    def _check_auth(request: Request) -> None:
        # Reject querystring tokens explicitly so a misconfigured client
        # fails loud instead of silently leaking the token via proxy logs.
        if request.query_params.get("token"):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Bearer token must be provided via the Authorization "
                    "header, not a querystring parameter"
                ),
            )
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token")
        provided = authorization.removeprefix("Bearer ").strip()
        if not secrets.compare_digest(provided, token):
            raise HTTPException(status_code=403, detail="Invalid bearer token")

    @app.get("/v1/health")
    def health() -> JSONResponse:
        return JSONResponse({
            "ok": True,
            "started_at": server.started_at,
            "version": 1,
        })

    @app.get("/v1/state")
    def state(request: Request) -> JSONResponse:
        _check_auth(request)
        return JSONResponse(_state_snapshot(server))

    @app.get("/v1/events")
    async def events(request: Request) -> StreamingResponse:
        _check_auth(request)
        last_event_id = _parse_last_event_id(
            request.headers.get("last-event-id")
            or request.query_params.get("last_event_id")
        )
        return StreamingResponse(
            _event_stream(request, ring, last_event_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return app


async def _event_stream(
    request: Request,
    ring: _EventRingBuffer,
    last_event_id: int,
):
    """SSE generator: replay buffered events then stream live ones.

    Behavior:

    1. Determine whether ``last_event_id`` can be served from the ring
       buffer; if not, emit a ``state_resync`` instructing the client
       to refetch ``/v1/state``.
    2. Replay any buffered events with ``id > last_event_id``.
    3. Wait on an asyncio Event woken by the pump thread when new
       events land in the ring buffer; emit them as they arrive.
    4. Heartbeat every ~15s with an SSE comment so proxies don't reap
       the connection as idle (Cloudflare Tunnel free tier reaps idle
       HTTP/2 streams aggressively).
    5. Exit cleanly when the client disconnects or the request is
       cancelled. The waiter is unregistered in ``finally`` so the
       ring buffer never accumulates stale waker references.
    """
    import asyncio

    HEARTBEAT_INTERVAL_S = 15.0

    last_seen_id = last_event_id

    backlog, servable = ring.since(last_event_id)
    if not servable:
        resync_envelope = {
            "v": 1,
            "id": 0,
            "ts": _current_ms(),
            "type": "state_resync",
            "reason": "buffer_overflow",
        }
        yield _format_sse_frame(resync_envelope)
        backlog = ring.snapshot()

    for env in backlog:
        if isinstance(env.get("id"), int):
            last_seen_id = max(last_seen_id, env["id"])
        yield _format_sse_frame(env)

    loop = asyncio.get_running_loop()
    wakeup = asyncio.Event()
    ring.add_async_waiter(loop, wakeup)

    async def _await_disconnect() -> None:
        while True:
            message = await request.receive()
            if message.get("type") == "http.disconnect":
                return

    wakeup_task = asyncio.create_task(wakeup.wait(), name="sse-wakeup")
    disconnect_task = asyncio.create_task(_await_disconnect(), name="sse-disconnect")

    try:
        while True:
            # Drain any events that landed *before* the waiter was
            # registered (or while we were yielding earlier frames).
            # Without this, an event broadcast in between backlog
            # replay and waiter setup is silently dropped.
            new_events = ring.newer_than(last_seen_id)
            if new_events:
                for env in new_events:
                    if isinstance(env.get("id"), int):
                        last_seen_id = max(last_seen_id, env["id"])
                    yield _format_sse_frame(env)
                    if env.get("type") == "shutdown":
                        return
                wakeup.clear()
                continue

            done, _ = await asyncio.wait(
                {wakeup_task, disconnect_task},
                timeout=HEARTBEAT_INTERVAL_S,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if disconnect_task in done:
                return

            if wakeup_task in done:
                wakeup.clear()
                wakeup_task = asyncio.create_task(
                    wakeup.wait(), name="sse-wakeup"
                )
                continue

            yield b": heartbeat\n\n"
    finally:
        ring.remove_async_waiter(loop, wakeup)
        for t in (wakeup_task, disconnect_task):
            if not t.done():
                t.cancel()


def _split_addr(addr: str) -> tuple[str, int]:
    """Parse ``HOST:PORT`` or ``:PORT`` into ``(host, port)``.

    A bare ``:7080`` defaults to ``127.0.0.1`` (loopback) so the SSE
    endpoint is not accidentally exposed on all interfaces. Operators
    who want it bound to ``0.0.0.0`` (e.g. behind a Cloudflare tunnel
    on the same box) must say so explicitly.
    """
    if ":" not in addr:
        raise ValueError(
            f"--serve-events expects HOST:PORT or :PORT, got: {addr!r}"
        )
    host, _, port = addr.rpartition(":")
    if not host:
        host = "127.0.0.1"
    try:
        port_int = int(port)
    except ValueError as e:
        raise ValueError(f"--serve-events port must be int, got: {port!r}") from e
    if not (1 <= port_int <= 65535):
        raise ValueError(f"--serve-events port out of range: {port_int}")
    return host, port_int


def run_sse_server(
    server: GateServer,
    addr: str,
    token: str,
    *,
    ring_buffer_size: int = DEFAULT_RING_BUFFER_SIZE,
    cors_origin: str | None = None,
) -> threading.Thread:
    """Spin up uvicorn in a daemon thread.

    Returns the thread so the caller can ``join`` during shutdown if
    desired. The server runs until the process exits.

    Raises ``ImportError`` (with install hint) if the ``web`` extra is
    not installed.
    """
    try:
        import uvicorn
    except ImportError as e:
        raise ImportError(
            "gate up --serve-events requires the 'web' extra. Install with: "
            "pip install 'gate[web]'"
        ) from e

    host, port = _split_addr(addr)
    app = create_app(
        server,
        token,
        ring_buffer_size=ring_buffer_size,
        cors_origin=cors_origin,
    )
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        loop="asyncio",
    )
    uvicorn_server = uvicorn.Server(config)

    def _run() -> None:
        try:
            uvicorn_server.run()
        except Exception:
            logger.exception("SSE server crashed")

    thread = threading.Thread(target=_run, name="sse-uvicorn", daemon=True)
    thread.start()
    logger.info(f"SSE adapter listening on http://{host}:{port}")
    return thread

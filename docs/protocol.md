# Gate event protocol

This document is the source of truth for messages flowing on Gate's IPC
surfaces. There are two surfaces:

1. **`server.sock`** — local AF_UNIX domain socket. Newline-delimited
   JSON. The TUI, the `gate` CLI's `gate status` / `gate cancel`
   commands, and the SSE adapter all connect here. It is **not**
   network-reachable.
2. **`/v1/events`** — HTTP Server-Sent Events stream exposed by
   `gate up --serve-events ADDR`. Re-broadcasts the AF_UNIX events to
   external clients (e.g. the team dashboard) over HTTP, with bearer
   token auth.

Schema versioning, the broadcast envelope, and every event/request type
are documented below.

---

## Versioning

The protocol is versioned with a single integer, currently `1`.

* The version lives in the broadcast envelope's `v` field and in the
  SSE URL path (`/v1/events`).
* The two are kept in lockstep: a new major version means a new envelope
  `v` **and** a new URL path (`/v2/events`), so deployments can serve
  both for a deprecation window.
* **Additive** changes (new event types, new fields on existing events)
  do **not** bump `v`. Consumers MUST ignore unknown event types and
  unknown fields. This is the protocol's main forward-compat rule.
* **Breaking** changes (renamed fields, removed fields, type changes,
  semantic changes to existing fields) bump `v` and add a new endpoint.

When implementing a consumer:

* Ignore events whose `type` you don't recognise.
* Ignore fields on known events that you don't recognise.
* Tolerate `v` being a value you don't recognise — log and drop, do not
  crash.

---

## Transport: AF_UNIX socket (`server.sock`)

* Socket path: `gate.config.socket_path()` (typically
  `<data_dir>/server.sock`).
* Framing: newline-delimited JSON (`\n` after each message). One JSON
  object per line.
* Direction: full-duplex. Clients send request messages; the server
  sends response and broadcast messages on the same socket.
* No handshake required, but clients SHOULD send a `connect` message
  immediately after connecting so the server can stamp the connection
  with optional review context.

### Broadcast envelope

Every server-to-client **broadcast** wraps its payload in a canonical
envelope:

```json
{
  "v": 1,
  "id": 4271,
  "ts": 1735927482103,
  "type": "review_updated",
  "review": { "...": "..." }
}
```

Fields:

| Field  | Type    | Description                                                                                |
|--------|---------|--------------------------------------------------------------------------------------------|
| `v`    | integer | Protocol version. Currently `1`.                                                           |
| `id`   | integer | Per-server-process monotonic broadcast sequence number, starting at `1`.                   |
| `ts`   | integer | Wall-clock timestamp (ms since Unix epoch) at the moment the envelope was assembled.       |
| `type` | string  | Event type. See the catalogue below.                                                       |
| ...    | varies  | Type-specific payload fields, merged into the envelope.                                    |

The envelope is assigned at the broadcast queue's dequeue point, so the
on-the-wire `id` order matches the order clients see the events. After a
server restart, `id` resets to `1`.

### Request/response (RPC) framing

Read-only RPC messages flow on the same socket but are **not**
enveloped — they carry only `type`, a `ts` stamp, and the response
payload. RPC responses are point-to-point and do not need sequence
numbers.

---

## Transport: HTTP / SSE (`gate up --serve-events`)

When run with `--serve-events ADDR`, `gate up` exposes a small FastAPI
app:

| Method | Path           | Auth       | Description                                                                       |
|--------|----------------|------------|-----------------------------------------------------------------------------------|
| GET    | `/v1/health`   | none       | Liveness check. Returns `{"ok": true, "started_at": <ms>, "version": 1}`.         |
| GET    | `/v1/state`    | bearer     | Initial-frame snapshot: `{"reviews": [...], "queue": [...], "health": {...}}`.    |
| GET    | `/v1/events`   | bearer     | SSE stream of broadcast envelopes.                                                |

### Auth

Bearer token in the `Authorization` header (`Authorization: Bearer
<token>`). The token is set via the `GATE_SSE_TOKEN` environment
variable; the server refuses to start with `--serve-events` unset if the
token is empty.

Tokens MUST NOT be passed via querystring — they would leak into proxy
access logs and into browser history. The server rejects requests with a
`token` querystring parameter even if the header is present, so a
misconfigured client fails loud rather than appearing to succeed.

This rules out the legacy `EventSource` browser API (which can't send
custom headers). The recommended client pattern is `fetch` + a
`ReadableStream` reader, which all modern browsers and Node runtimes
support natively.

### SSE frame format

Each event is emitted as a standard SSE frame:

```
id: 4271
event: review_updated
data: {"v":1,"id":4271,"ts":1735927482103,"type":"review_updated","review":{...}}

```

* `id:` — the envelope's `id` field, repeated so the browser/runtime
  populates `Last-Event-ID` automatically on reconnect.
* `event:` — the envelope's `type`, so clients can attach
  per-type listeners.
* `data:` — the full enveloped JSON, exactly as sent on the AF_UNIX
  socket. This duplication is intentional: clients that want a single
  parser can ignore `event:` and parse `data:` only.

Frames are terminated by a blank line, per the SSE spec.

### Reconnection and `Last-Event-ID`

The SSE adapter maintains an in-memory ring buffer of the last 1000
broadcasts (configurable via `GATE_SSE_BUFFER_SIZE`). When a client
reconnects with `Last-Event-ID: N`:

* If `N+1` is in the buffer, the adapter replays from `N+1` and then
  continues with live events. No client-visible gap.
* If `N+1` has already aged out (or `N` is from a previous
  server-process incarnation), the adapter sends a synthetic
  `state_resync` event:

  ```json
  {"v":1,"id":1,"ts":...,"type":"state_resync","reason":"buffer_overflow"}
  ```

  Clients receiving `state_resync` MUST re-fetch `/v1/state` and treat
  any prior state as stale.

Server-process restarts also trigger a synthetic `state_resync` because
`id` counters reset and prior `Last-Event-ID` values no longer make
sense. To detect this without parsing reasons, clients can compare the
`/v1/health` response's `started_at` across reconnects.

---

## Event catalogue

All events listed below are broadcast through the envelope and are
available on both the AF_UNIX socket and the SSE stream.

### `review_updated`

Emitted when a review's stage, status, or any other tracked field
changes. Sent on every transition through the orchestrator pipeline.

```json
{
  "v": 1, "id": 4271, "ts": 1735927482103,
  "type": "review_updated",
  "review": {
    "id": "myorg-myrepo-pr123",
    "pr_number": 123,
    "repo": "myorg/myrepo",
    "head_sha": "abc123def...",
    "stage": "security",
    "status": "running",
    "started_at": 1735927400000,
    "updated_at": 1735927482103,
    "tmux_pane": "1.2",
    "pid": 12345
  }
}
```

Payload field: `review` — the full current snapshot of the review
object. Consumers should treat this as authoritative for the listed
fields (replace, don't merge).

### `review_completed`

Emitted exactly once per review when the orchestrator reaches a
terminal verdict (or the reaper marks it `stuck`). The review is
removed from the server's `reviews` list immediately after emission.

```json
{
  "v": 1, "id": 4283, "ts": 1735927510221,
  "type": "review_completed",
  "review": {
    "id": "myorg-myrepo-pr123",
    "pr_number": 123,
    "repo": "myorg/myrepo",
    "status": "completed",
    "decision": "approve",
    "updated_at": 1735927510221
  }
}
```

Payload field: `review`. The `status` is `completed` for a normal
verdict and `stuck` if the reaper fired (review hadn't updated for
> 1 hour).

### `review_cancelled`

Emitted when a review is cancelled (either via `cancel_review` over the
socket, or because a newer push superseded an in-flight orchestrator).
The review is removed from the `reviews` list after emission.

```json
{
  "v": 1, "id": 4290, "ts": 1735927520331,
  "type": "review_cancelled",
  "review": {
    "id": "myorg-myrepo-pr123",
    "status": "cancelled",
    "updated_at": 1735927520331
  }
}
```

### `queue_updated`

Emitted whenever the pending-review queue changes (PR added, PR popped
to start running, queue reordered).

```json
{
  "v": 1, "id": 4275, "ts": 1735927490000,
  "type": "queue_updated",
  "queue": [
    {"pr_number": 124, "repo": "myorg/myrepo", "queued_at": 1735927450000},
    {"pr_number": 125, "repo": "myorg/myrepo", "queued_at": 1735927470000}
  ]
}
```

### `health_updated`

Emitted when the server's cached health snapshot changes. Today no
in-process producer writes to this field; it's reserved for future
periodic health publishing.

```json
{
  "v": 1, "id": 4280, "ts": 1735927500000,
  "type": "health_updated",
  "health": { "...": "..." }
}
```

### `shutdown`

Sent once when the server is stopping. After this event the connection
will close. Clients should treat this as a signal to enter a
reconnection state, not as a fatal error.

```json
{"v": 1, "id": 9999, "ts": 1735927999999, "type": "shutdown"}
```

### `state_resync` (SSE only)

Synthesised by the SSE adapter (not the AF_UNIX server) when a
reconnecting client's `Last-Event-ID` cannot be served from the ring
buffer. See "Reconnection" above.

```json
{
  "v": 1, "id": 1, "ts": 1735928000000,
  "type": "state_resync",
  "reason": "buffer_overflow"
}
```

`reason` is one of: `buffer_overflow` (id aged out), `server_restart`
(server-process `id` counter reset), `unknown` (id higher than current).

---

## Request catalogue (AF_UNIX RPC)

All requests are JSON objects with a `type` field. Responses are JSON
objects with a matching response `type`. RPC responses are **not**
enveloped (no `v`/`id`).

### `connect`

Optional handshake. Stamps the connection with review context for the
TUI's "connect to a specific review" flow.

Request: `{"type": "connect", "review_id": "myorg-myrepo-pr123"}` (the
`review_id` is optional).

Response: `{"type": "connected", "ts": ..., "tmux": {...}, "review": {...}|null, "review_found": bool}`.

### `ping`

Liveness check.

Request: `{"type": "ping"}` → Response: `{"type": "pong", "ts": ...}`.

### `review_list`

Snapshot of all currently-active reviews.

Request: `{"type": "review_list"}` →
Response: `{"type": "review_list", "ts": ..., "reviews": [...]}`.

### `queue_list`

Snapshot of the pending-review queue.

Request: `{"type": "queue_list"}` →
Response: `{"type": "queue_list", "ts": ..., "queue": [...]}`.

### `health_get`

Snapshot of the cached health dict.

Request: `{"type": "health_get"}` →
Response: `{"type": "health_data", "ts": ..., "health": {...}}`.

### `review_request`

Enqueue a new review.

Request:
```json
{
  "type": "review_request",
  "pr_number": 123,
  "repo": "myorg/myrepo",
  "head_sha": "abc...",
  "event": "synchronize",
  "branch": "feature/foo",
  "labels": ["needs-review"]
}
```

Response: `{"type": "review_accepted", "ts": ..., "pr_number": 123}`.

### `cancel_review`

Cancel a queued or running review.

Request: `{"type": "cancel_review", "pr_number": 123, "repo": "myorg/myrepo"}`.

Response: `{"type": "cancel_accepted", "ts": ..., "pr_number": 123, "cancelled": true|false}`.

---

## Mutation messages (server-internal)

These are emitted by the orchestrator and runner processes, sent to the
server over the AF_UNIX socket, and trigger the server to broadcast the
corresponding event. They are documented here for completeness; most
consumers will not produce them.

| Mutation type           | Triggers broadcast       | Required fields                                        |
|-------------------------|--------------------------|--------------------------------------------------------|
| `review_started`        | `review_updated`         | `review`                                               |
| `review_stage_update`   | `review_updated`         | `review_id`, `stage`, `status`, `head_sha`             |
| `review_completed`      | `review_completed`       | `review_id`, `decision`, `head_sha`                    |
| `review_cancelled`      | `review_cancelled`       | `review_id`, `head_sha`                                |
| `stage_register`        | `review_updated`         | `review_id`, `stage`, `tmux_pane`, `pid`, `head_sha`   |
| `queue_update`          | `queue_updated`          | `queue`                                                |
| `health_update`         | `health_updated`         | `health`                                               |

`head_sha` matters for de-duplication: when a new push supersedes an
in-flight orchestrator, both share the same `review_id`. The server
ignores stale lifecycle messages whose `head_sha` no longer matches the
current review's. Mutations without `head_sha` (e.g. user-initiated
cancels) are accepted unconditionally.

---

## Multi-subscriber semantics

The server already supports an arbitrary number of concurrent
subscribers (see `GateServer._handle_client`). All broadcasts are sent
to every connected client through a single writer thread, so order is
deterministic across subscribers and there is no broadcast ordering
skew.

A slow consumer that fails to drain its socket buffer will eventually
trigger a write timeout (`SO_SNDTIMEO` is 2 seconds). The server then
removes the client and continues serving the rest. Clients are
responsible for reconnecting; the SSE adapter handles this transparently
for HTTP clients.

---

## Implementation notes for client authors

* **Reconnect, don't fail.** Both the AF_UNIX socket and the SSE stream
  can drop for benign reasons (server restart, Cloudflare Tunnel idle
  timeout). Clients should reconnect with backoff, send `Last-Event-ID`
  (SSE) or re-issue snapshot requests (`review_list`, `queue_list`).
* **Treat snapshots as truth.** The `/v1/state` payload (and the
  matching `review_list` / `queue_list` RPC responses) is the
  authoritative initial frame. Apply broadcasts on top.
* **Be permissive in what you accept.** Unknown `type` values, unknown
  fields on known events, and unknown `v` values must not crash the
  client.
* **Don't depend on `id` survival across server restarts.** `id` is an
  in-memory counter. After a restart it resets to `1`. Use
  `state_resync` (SSE) or refetch (`review_list`) to recover.

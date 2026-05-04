"""Action audit log for slash commands and other operator-initiated actions.

Distinct on purpose from:

* :mod:`gate.audit` — *self-reflection* over Gate's own review outcomes
  (silent approvals, retro contradictions). Asks "did Gate get it
  right?".
* ``reviews.jsonl`` (via :mod:`gate.logger`) — review-result history,
  the source of truth for ``gate report`` and the dashboard's trend
  charts.

The actions log answers a third question — "who did what, when, and
to which PR?" — that neither of the other two captures. Slash command
verbs (``/gate skip``, ``/gate bypass``, ``/gate rerun``) write here so
team-internal accountability survives a server restart and can be
sliced ``--since=24h`` after an incident.

Schema (one JSON object per line in ``logs/actions.jsonl``):

```json
{
  "ts": 1735927482103,
  "actor": "alice-handle",
  "verb": "bypass",
  "repo": "myorg/myrepo",
  "pr_number": 123,
  "args": {"link": "https://linear.app/..."},
  "outcome": "ok",
  "detail": ""
}
```

* ``ts``        — wall-clock ms since epoch.
* ``actor``     — GitHub login of the commenter (or ``"system"`` for
  non-human actions).
* ``verb``      — slash-command verb without the ``/gate`` prefix.
* ``repo``      — full ``owner/repo`` slug.
* ``pr_number`` — integer PR number, or 0 when not PR-scoped.
* ``args``      — verb-specific argument dict (e.g.
  ``{"reason": "docs only"}``); kept open-ended so future verbs add
  fields without bumping the schema.
* ``outcome``   — ``"ok"`` | ``"rejected"`` | ``"error"``.
* ``detail``    — human-readable string for ``rejected`` / ``error``;
  blank for ``ok``.

The schema is *additive*: consumers MUST tolerate unknown fields, just
like the broadcast event protocol.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Literal

from gate.config import logs_dir

logger = logging.getLogger(__name__)

ACTIONS_LOG_NAME = "actions.jsonl"

Outcome = Literal["ok", "rejected", "error"]


def actions_log_path() -> Path:
    return logs_dir() / ACTIONS_LOG_NAME


_write_lock = threading.Lock()


def record_action(
    *,
    verb: str,
    actor: str,
    repo: str,
    pr_number: int,
    args: dict[str, Any] | None = None,
    outcome: Outcome = "ok",
    detail: str = "",
) -> None:
    """Append a single action record. Best-effort — never raises.

    Thread-safe via a process-local lock. Append is line-atomic on
    POSIX up to PIPE_BUF (4096B), and our records are small (~200B
    typical), so concurrent writes from the slash-command poller
    thread and the orchestrator thread don't interleave in practice.
    """
    record = {
        "ts": int(time.time() * 1000),
        "actor": actor or "system",
        "verb": verb,
        "repo": repo,
        "pr_number": int(pr_number) if pr_number else 0,
        "args": dict(args) if args else {},
        "outcome": outcome,
        "detail": detail,
    }
    path = actions_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with _write_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        logger.exception("Failed to append actions log record")


def read_actions(
    *,
    since_ms: int | None = None,
    verb: str | None = None,
    repo: str | None = None,
) -> Iterable[dict]:
    """Yield action records, optionally filtered.

    Reads the whole log; sized for human queries (10s/100s of records
    over a 7-day window), not high-throughput streaming.
    """
    path = actions_log_path()
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if since_ms is not None and rec.get("ts", 0) < since_ms:
                continue
            if verb is not None and rec.get("verb") != verb:
                continue
            if repo is not None and rec.get("repo") != repo:
                continue
            yield rec

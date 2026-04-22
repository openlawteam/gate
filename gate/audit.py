"""Self-reflection audit helpers (PR B.1 + B.2).

Exposes two related checks that close the "Gate missed X, we only
noticed weeks later" gap surfaced by the PR #22 post-mortem:

* ``retro_scan`` — walks every archived review under
  ``state/*/pr*/reviews/`` and reports **silent approvals**: verdicts
  that cleared a PR despite a stage (build / lint / tests) actually
  failing. Promoted from a one-shot ``/tmp/retro_scan_silent_approvals.py``
  script so the capability isn't lost to the next ``rm -rf /tmp``.
* ``list_contradictions`` — reads ``state/<repo>/pr<N>/contradictions/``
  written by the post-hoc recheck thread (see
  ``gate.external_checks``) and returns them newest-first, optionally
  filtered by age.

Both functions return plain-dict summaries; the CLI wiring lives in
``gate.cli`` under the new ``audit`` subcommand family.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any, Iterable

from gate.config import state_dir

_APPROVE_DECISIONS: frozenset[str] = frozenset({"approve", "approve_with_notes"})
_STAGE_FILES_TO_CHECK: tuple[str, ...] = ("build.json",)


def _iter_review_archives(root: Path) -> Iterable[Path]:
    """Yield every per-review archive directory under ``root``.

    Archives live at ``state/<slug>/pr<N>/reviews/<ISO>-<sha>-<suffix>/``.
    Uses ``rglob`` rather than a fixed depth because state layouts vary
    (with vs without repo slug) and the retro-scan must be tolerant of
    both historical shapes.
    """
    for reviews_dir in root.rglob("reviews"):
        if not reviews_dir.is_dir():
            continue
        if reviews_dir.parent.name.startswith("pr"):
            for child in sorted(reviews_dir.iterdir()):
                if child.is_dir():
                    yield child


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _build_has_failure(build: dict[str, Any]) -> list[str]:
    """Return human-readable reasons the build doc signals failure.

    A stage (typecheck / lint / tests) is considered failing if any of:

    * ``pass`` is explicitly False;
    * ``parse_failure`` is True (added in PR #22 to stop
      "non-zero exit, zero parsed findings" rationalisations);
    * ``exit_code`` is truthy non-zero.

    Returned list is empty when no signals fire.
    """
    reasons: list[str] = []
    for stage_name in ("typecheck", "lint", "tests"):
        stage = build.get(stage_name)
        if not isinstance(stage, dict):
            continue
        if stage.get("pass") is False:
            reasons.append(f"{stage_name}.pass=false")
        if stage.get("parse_failure"):
            reasons.append(f"{stage_name}.parse_failure=true")
        exit_code = stage.get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            reasons.append(f"{stage_name}.exit_code={exit_code}")
    top_issues = build.get("blocking_issues")
    if isinstance(top_issues, list) and top_issues:
        reasons.append(f"blocking_issues={len(top_issues)}")
    return reasons


def retro_scan(root: Path | None = None) -> list[dict[str, Any]]:
    """Walk per-review archives and report silent approvals.

    A silent approval is an archived review whose verdict is
    ``approve`` / ``approve_with_notes`` while its build artifact
    records a stage failure. Each hit is returned as a dict with
    ``path`` (archive dir), ``decision``, ``reasons``, ``sha``, and
    ``timestamp``. Non-archived layouts (pre-PR-B.1 state trees) are
    skipped silently since they were overwritten anyway — the whole
    point of PR B.1 is that the archive layout exists going forward.
    """
    base = root or state_dir()
    hits: list[dict[str, Any]] = []
    for archive in _iter_review_archives(base):
        verdict = _read_json(archive / "verdict.json")
        if not verdict:
            continue
        decision = verdict.get("decision") or ""
        if decision not in _APPROVE_DECISIONS:
            continue
        build = _read_json(archive / "build.json")
        if not build:
            continue
        reasons = _build_has_failure(build)
        if not reasons:
            continue
        parts = archive.name.split("-", 2)
        timestamp = parts[0] if parts else ""
        sha = parts[1] if len(parts) > 1 else ""
        hits.append({
            "path": str(archive),
            "pr_dir": str(archive.parent.parent),
            "decision": decision,
            "reasons": reasons,
            "sha": sha,
            "timestamp": timestamp,
        })
    return hits


def list_contradictions(
    since_seconds: float | None = None,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """Return contradiction records newest-first.

    Each contradiction is a JSON file under
    ``state/<slug>/pr<N>/contradictions/<ISO>-<check>.json`` written by
    the post-hoc recheck thread (PR B.2) when an external check flips
    from green to red after Gate already approved the PR. The file
    carries the verdict snapshot + the failing check payload.

    When ``since_seconds`` is given, only files newer than ``now -
    since_seconds`` are returned. ``root`` defaults to the configured
    state dir; explicit override is for tests.
    """
    base = root or state_dir()
    now = _dt.datetime.now(tz=_dt.UTC).timestamp()
    cutoff = (now - since_seconds) if since_seconds is not None else None
    hits: list[dict[str, Any]] = []
    for contradiction_dir in base.rglob("contradictions"):
        if not contradiction_dir.is_dir():
            continue
        if not contradiction_dir.parent.name.startswith("pr"):
            continue
        for child in contradiction_dir.iterdir():
            if not child.is_file() or child.suffix != ".json":
                continue
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            if cutoff is not None and mtime < cutoff:
                continue
            doc = _read_json(child)
            hits.append({
                "path": str(child),
                "pr_dir": str(contradiction_dir.parent),
                "name": child.stem,
                "mtime": mtime,
                "data": doc or {},
            })
    hits.sort(key=lambda h: h["mtime"], reverse=True)
    return hits

"""Sticky review-summary comment.

Gate posts one canonical, human-readable PR comment per PR (not per
review cycle). It's edited in place every time the orchestrator
re-runs, so the team always has one place to read the current verdict,
the blocking findings, the override controls — without scrolling
through years of review history.

The comment is *separate* from the GitHub review object that
:func:`gate.github.post_review` posts (which is what counts for branch
protection). The sticky comment is for humans; the review object is
for the machine.

Identification:

* Every sticky comment starts with the marker
  ``<!-- gate:sticky:v1 -->`` so :func:`_find_sticky_comment` can
  locate it without GitHub having an "owned by app X" concept on
  fine-grained PAT installs.
* The marker is versioned (``v1``) so a future major reformat that
  breaks consumers can introduce a new marker side-by-side.

Rendering:

* The default template ships at ``gate/templates/sticky.md.j2`` and is
  rendered with Jinja2.
* Overrides use a search path: ``Path.cwd() / ".gate" / "templates"``
  → ``data_dir() / "templates"`` → the package default. The first hit
  wins. This is intentionally a *file-based* extension rather than a
  Python plugin hook; companion repos that want a different tone
  ("tighter for terse repos", "GIF-rich for the platform team") can
  ship a single ``.gate/templates/sticky.md.j2`` and review it through
  the normal PR process.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from gate.config import data_dir, gate_dir

logger = logging.getLogger(__name__)

STICKY_MARKER = "<!-- gate:sticky:v1 -->"

_BLOCKING_SEVERITIES = frozenset({"critical", "error"})


def _template_search_paths() -> list[Path]:
    """Return Jinja loader search paths in priority order (highest first)."""
    return [
        Path.cwd() / ".gate" / "templates",
        data_dir() / "templates",
        gate_dir() / "gate" / "templates",
    ]


def _render_finding_for_template(f: dict[str, Any]) -> dict[str, Any]:
    """Flatten a finding into the shape expected by sticky.md.j2.

    Mirrors :class:`gate.schemas.Finding` for the fields used by the
    template; not all callers populate every field, so each lookup is
    defensive.
    """
    location_bits: list[str] = []
    file_path = f.get("file") or ""
    if file_path:
        location_bits.append(str(file_path))
        line = f.get("line")
        if line not in (None, ""):
            location_bits.append(f"L{line}")
    location = ":".join(location_bits) if location_bits else "(unspecified)"
    return {
        "severity": str(f.get("severity", "info") or "info").lower(),
        "location": location,
        "message": str(f.get("message", "")).strip() or "(no message)",
        "finding_id": str(f.get("finding_id", "") or ""),
    }


def _split_blocking(findings: list[dict]) -> tuple[list[dict], list[dict]]:
    """Partition findings into (blocking, non-blocking).

    A finding is considered blocking when its severity is ``critical``
    or ``error`` AND it isn't explicitly tagged as not-introduced-by-PR
    (we don't block authors on pre-existing latent issues).
    """
    blocking: list[dict] = []
    non_blocking: list[dict] = []
    for f in findings:
        sev = str(f.get("severity", "")).lower()
        if sev in _BLOCKING_SEVERITIES and f.get("introduced_by_pr") is not False:
            blocking.append(_render_finding_for_template(f))
        else:
            non_blocking.append(_render_finding_for_template(f))
    return blocking, non_blocking


def _summarise_build(build: dict | None) -> str:
    """Compact human-readable build summary, or empty string if no signals."""
    if not isinstance(build, dict):
        return ""
    parts: list[str] = []
    for stage_name in ("typecheck", "lint", "tests"):
        stage = build.get(stage_name)
        if not isinstance(stage, dict):
            continue
        passed = stage.get("pass")
        if passed is True:
            parts.append(f"`{stage_name}` :white_check_mark:")
        elif passed is False:
            parts.append(f"`{stage_name}` :x:")
        elif stage.get("parse_failure"):
            parts.append(f"`{stage_name}` :warning: parse_failure")
        elif stage.get("exit_code") not in (None, 0):
            parts.append(f"`{stage_name}` exit={stage['exit_code']}")
    if not parts:
        return ""
    return " &nbsp;·&nbsp; ".join(parts)


def _stats_line(verdict: dict, blocking_count: int, non_blocking_count: int) -> str:
    stats = verdict.get("stats", {}) or {}
    stages_run = stats.get("stages_run", "?")
    return (
        f"{blocking_count} blocking &nbsp;·&nbsp; {non_blocking_count} other "
        f"&nbsp;·&nbsp; {stages_run} stages run"
    )


_DECISION_LABELS: dict[str, str] = {
    "approve": "Approved",
    "approve_with_notes": "Approved with notes",
    "request_changes": "Changes requested",
}


def render_sticky(verdict: dict, build: dict | None) -> str:
    """Render the sticky comment markdown for a verdict + build pair.

    Importable for tests so the rendering logic is exercised without
    touching ``gh``. Includes :data:`STICKY_MARKER` as the first line.
    """
    from jinja2 import (
        ChoiceLoader,
        Environment,
        FileSystemLoader,
        PackageLoader,
        select_autoescape,
    )

    loaders = []
    for p in _template_search_paths():
        if p.exists():
            loaders.append(FileSystemLoader(str(p)))
    loaders.append(PackageLoader("gate", "templates"))

    env = Environment(
        loader=ChoiceLoader(loaders),
        autoescape=select_autoescape(disabled_extensions=("md", "j2")),
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )

    decision = str(verdict.get("decision", "approve") or "approve")
    findings = verdict.get("findings") or []
    if not isinstance(findings, list):
        findings = []
    blocking, non_blocking = _split_blocking(findings)
    approved = decision in ("approve", "approve_with_notes")

    context = {
        "marker": STICKY_MARKER,
        "decision": decision,
        "decision_label": _DECISION_LABELS.get(
            decision, decision.replace("_", " ").title()
        ),
        "confidence": str(verdict.get("confidence", "unknown") or "unknown"),
        "summary": (str(verdict.get("summary", "")) or "_(no summary)_").strip(),
        "review_time": verdict.get("review_time_seconds"),
        "approved": approved,
        "blocking_findings": blocking,
        "non_blocking_findings": non_blocking,
        "has_non_blocking": bool(non_blocking),
        "build_summary": _summarise_build(build),
        "stats_line": _stats_line(verdict, len(blocking), len(non_blocking)),
        "updated_at": _dt.datetime.now(tz=_dt.UTC).strftime(
            "%Y-%m-%d %H:%M UTC"
        ),
    }

    template = env.get_template("sticky.md.j2")
    return template.render(**context)


def _find_sticky_comment(repo: str, pr_number: int) -> int | None:
    """Return the GitHub comment ID of the sticky comment, or None.

    Walks the PR's issue comments looking for the first one whose body
    starts with :data:`STICKY_MARKER`. Returns the integer comment ID
    that the GitHub Issues API uses for PATCH/DELETE.
    """
    from gate.github import _gh

    try:
        out = _gh([
            "api",
            f"repos/{repo}/issues/{pr_number}/comments",
            "--paginate",
            "-q", ".[] | {id: .id, body: .body}",
            "--method", "GET",
        ])
    except subprocess.CalledProcessError as e:
        logger.warning(
            f"PR #{pr_number}: failed to list comments for sticky lookup: {e}"
        )
        return None

    # `gh api -q` with `.[]` against a paginated array yields one JSON
    # object per line. Parse defensively — older gh emits the array
    # form in a single line.
    candidates: list[dict] = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, list):
            candidates.extend(o for o in obj if isinstance(o, dict))
        elif isinstance(obj, dict):
            candidates.append(obj)

    for c in candidates:
        body = c.get("body") or ""
        if isinstance(body, str) and body.startswith(STICKY_MARKER):
            cid = c.get("id")
            if isinstance(cid, int):
                return cid
    return None


def upsert_sticky_summary(
    repo: str,
    pr_number: int,
    verdict: dict,
    build: dict | None,
) -> None:
    """Render and post (or update) the sticky summary for this PR.

    Idempotent: if the rendered markdown is byte-identical to the
    existing comment, the PATCH is skipped so we don't churn the PR
    timeline with no-op edits on every push.

    Best-effort by design: callers (currently
    :func:`gate.github.post_review`) wrap this in a try/except. A
    failed sticky update never fails a review.
    """
    from gate.github import _gh

    body = render_sticky(verdict, build)
    existing_id = _find_sticky_comment(repo, pr_number)

    if existing_id is None:
        _gh([
            "api",
            f"repos/{repo}/issues/{pr_number}/comments",
            "-X", "POST",
            "-f", f"body={body}",
        ])
        logger.info(f"PR #{pr_number}: posted sticky summary comment")
        return

    try:
        existing_body = _gh([
            "api",
            f"repos/{repo}/issues/comments/{existing_id}",
            "-q", ".body",
        ]).strip()
    except subprocess.CalledProcessError:
        existing_body = ""

    if existing_body == body.strip():
        logger.debug(
            f"PR #{pr_number}: sticky summary unchanged, skipping PATCH"
        )
        return

    _gh([
        "api",
        f"repos/{repo}/issues/comments/{existing_id}",
        "-X", "PATCH",
        "-f", f"body={body}",
    ])
    logger.info(f"PR #{pr_number}: updated sticky summary comment {existing_id}")

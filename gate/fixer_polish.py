"""Legacy per-finding polish loop (``fix_pipeline.mode = "polish_legacy"``).

.. deprecated::
    Superseded by the holistic hopper-mode pipeline driven directly by
    ``fix-senior.md`` + ``gate checkpoint`` (see Part 3 of the gate
    hardening plan). Kept for rollback + bake-in only — slated for
    deletion ~2 weeks after hopper mode stabilises in production. The
    retirement checklist (``fixer_polish.py``, ``prompts/fix-polish.md``,
    ``fixer._run_polish_path``, ``_reprompt_trivial_skips``, the
    ``fix_polish_loop_enabled`` / ``polish_per_finding_timeout_seconds``
    / ``polish_loop_total_budget_s`` config keys, and related tests)
    lives in the plan's "Legacy retire" section.

Runs one focused fix-senior attempt per finding, in descending fixability
order (trivial → scoped → broad). Each attempt gets a fresh Codex
bootstrap so the junior does not carry "I'm done" state from the previous
finding into the next one (audit A4). The senior Claude session is
reused across findings so cross-finding context (cursor rules, project
layout, what was already changed) accumulates naturally — exactly the
"senior reviewing multiple lodes" pattern hopper uses.

Design constraints:

1. **Feature flag.** The polish loop is only entered when
   ``fix_polish_loop_enabled`` is true per-repo (audit A8). When the flag
   is off, the caller falls back to the monolithic fix-senior run.
2. **Per-finding budgets.** Each fixability class has its own timeout
   (see ``config.get_polish_timeouts``). ``broad=0`` means skip — broad
   findings are added to ``not_fixed`` with reason
   ``"skipped_broad_in_polish"`` without dispatching the junior against
   them.
3. **Total budget.** A wall-clock cap (``polish_loop_total_budget_s``)
   prevents runaway loops. Findings not yet attempted when the budget
   is exhausted are added to ``not_fixed`` with reason
   ``"deferred_by_budget"`` (audit A5).
4. **Build checkpoints.** After each successful finding, we run
   ``build_verify`` and create a local ``git commit --no-verify``
   checkpoint. If a later finding breaks the build, we ``git reset
   --hard`` to the last checkpoint and mark just that one finding
   ``not_fixed`` (audit A6). This preserves good fixes even when a
   later finding goes wrong.
5. **TUI compatibility.** We re-emit the existing ``fix-session`` /
   ``fix-build`` stage names from inside the loop so the Textual UI's
   ``FIX_STAGES`` array still renders (audit A11). The internal
   per-finding progress is logged to the live log instead.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from gate import github as _github  # noqa: F401 (reserved for comment emission)
from gate import prompt
from gate.codex import bootstrap_codex
from gate.config import (
    build_claude_env,
    get_polish_timeouts,
    get_polish_total_budget_s,
)
from gate.logger import write_live_log
from gate.runner import StructuredRunner, run_with_retry
from gate.schemas import StageResult

if TYPE_CHECKING:
    from gate.fixer import FixPipeline

logger = logging.getLogger(__name__)


# ── Ordering ────────────────────────────────────────────────

_FIXABILITY_ORDER = {"trivial": 0, "scoped": 1, "unknown": 2, "broad": 3}


def _sort_by_fixability(findings: list[dict]) -> list[dict]:
    """Sort findings trivial → scoped → unknown → broad.

    Keeps stable order within each bucket so the same PR produces the
    same checkpoint sequence across retries (makes live logs easier
    to diff).
    """
    indexed = sorted(
        enumerate(findings),
        key=lambda pair: (
            _FIXABILITY_ORDER.get(pair[1].get("fixability", "unknown"), 2),
            pair[0],
        ),
    )
    return [f for _, f in indexed]


# ── Single-finding prompt rendering ─────────────────────────


def _render_single_finding_prompt(finding: dict, context_findings: list[dict]) -> str:
    """Render a single-finding fix-senior prompt.

    We reuse ``fix-senior.md`` verbatim but swap the findings payload to
    contain *only* this finding. ``context_findings`` is the full tagged
    list from the parent pipeline — we include it as comment metadata so
    the senior can see the broader review picture without being tempted
    to batch fixes. The polish_mode_section is forced on because this
    loop only runs when ``is_polish`` is true.
    """
    other_summaries = [
        {
            "finding_id": f["finding_id"],
            "file": f.get("file"),
            "fixability": f.get("fixability"),
            "message": (f.get("message") or "")[:80],
        }
        for f in context_findings
        if f.get("finding_id") != finding.get("finding_id")
    ]
    others_json = json.dumps(other_summaries, indent=2)
    return (
        "CURRENT FOCUS (single finding):\n\n"
        f"```json\n{json.dumps([finding], indent=2)}\n```\n\n"
        "Other findings in this PR (for awareness — do NOT attempt them "
        "in this call, they will be dispatched separately):\n\n"
        f"```json\n{others_json}\n```\n"
    )


# ── Per-finding execution ───────────────────────────────────


def _attempt_finding(
    pipeline: "FixPipeline",
    finding: dict,
    timeout_s: int,
) -> dict:
    """Attempt to fix exactly one finding. Returns a result dict.

    Result shape:
        {
          "finding_id": str,
          "fixed": bool,
          "entry": dict,        # fixed[] or not_fixed[] entry for fix.json
          "has_changes": bool,
        }
    """
    workspace = pipeline.workspace
    finding_id = finding.get("finding_id", "unknown")
    file = finding.get("file", "?")

    # 1. Fresh Codex bootstrap for this finding (audit A4).
    write_live_log(
        pipeline.pr_number,
        f"polish[{finding_id}] bootstrapping Codex for {file}",
        prefix="fix", repo=pipeline.repo,
    )
    from gate.fixer import _build_codex_bootstrap_prompt  # local import to avoid cycle
    codex_prompt = _build_codex_bootstrap_prompt()
    _, thread_id = bootstrap_codex(codex_prompt, str(workspace), env=build_claude_env())
    if not thread_id:
        logger.warning(f"polish[{finding_id}] codex bootstrap failed")
        return {
            "finding_id": finding_id,
            "fixed": False,
            "has_changes": False,
            "entry": {
                "finding_id": finding_id,
                "file": file,
                "line": finding.get("line", 0),
                "finding_message": finding.get("message", "")[:200],
                "reason": "codex_bootstrap_failed",
                "detail": (
                    "Codex agent failed to start for this polish attempt; "
                    "the senior could not delegate work."
                ),
            },
        }

    # Update pipeline state so resume calls pick up the new thread.
    pipeline.codex_thread_id = thread_id
    fix_env = {
        "GATE_CODEX_THREAD_ID": thread_id,
        "GATE_FIX_WORKSPACE": str(workspace),
    }
    (workspace / "fix-env.json").write_text(json.dumps(fix_env))

    # 2. Record the pre-attempt commit so we can diff and revert this
    #    finding alone if the build breaks afterwards.
    pre_sha = _git_head_sha(workspace)

    # 3. Rewrite verdict.json to expose just this finding to fix-senior's
    #    prompt builder. Stash the original so we can restore after.
    verdict_path = workspace / "verdict.json"
    verdict_backup = verdict_path.read_text() if verdict_path.exists() else ""
    try:
        scoped_verdict = dict(pipeline.verdict)
        scoped_verdict["findings"] = [finding]
        verdict_path.write_text(json.dumps(scoped_verdict, indent=2))

        # 4. Dispatch fix-senior. First finding uses a fresh tmux +
        #    session; subsequent findings resume the senior.
        if pipeline.session_id:
            result = _resume_fix_senior(pipeline, finding, timeout_s)
        else:
            result = _spawn_fix_senior(pipeline, finding, timeout_s)
    finally:
        if verdict_backup:
            verdict_path.write_text(verdict_backup)

    # 5. Interpret result. If the senior claims a fix, verify the build
    #    at a new checkpoint; if the build breaks, revert the new commits
    #    and demote to not_fixed (audit A6).
    fix_json = result.get("fix_json") or {}
    fixed_entries = fix_json.get("fixed") or []
    not_fixed_entries = fix_json.get("not_fixed") or []

    if result.get("has_changes"):
        from gate.fixer import build_verify
        bv = build_verify(workspace, pipeline.build, config=pipeline.config)
        if not bv.get("pass"):
            write_live_log(
                pipeline.pr_number,
                f"polish[{finding_id}] build failed, reverting just this finding",
                prefix="fix", repo=pipeline.repo,
            )
            _reset_to(workspace, pre_sha)
            return {
                "finding_id": finding_id,
                "fixed": False,
                "has_changes": False,
                "entry": {
                    "finding_id": finding_id,
                    "file": file,
                    "line": finding.get("line", 0),
                    "finding_message": finding.get("message", "")[:200],
                    "reason": "would_break_build",
                    "detail": (
                        f"Per-finding build verify failed after attempt "
                        f"(typecheck={bv.get('typecheck_errors', 0)}, "
                        f"lint={bv.get('lint_errors', 0)}). Reverted."
                    ),
                },
            }

        # Create a lightweight checkpoint commit — squashed later by
        # the top-level commit. Non-fatal if this fails.
        _git_checkpoint(workspace, f"gate-polish checkpoint {finding_id}")

        # Pick the best entry the senior wrote for this finding (by id),
        # fall back to a synthesized entry if the senior omitted it.
        matched = next(
            (e for e in fixed_entries if e.get("finding_id") == finding_id),
            None,
        )
        entry = matched or {
            "finding_id": finding_id,
            "file": file,
            "line": finding.get("line", 0),
            "finding_message": finding.get("message", "")[:200],
            "fix_description": f"polish loop modified {file} (senior did not describe)",
            "_description_synthesized": True,
        }
        return {
            "finding_id": finding_id,
            "fixed": True,
            "has_changes": True,
            "entry": entry,
        }

    # No changes. Prefer the senior's own not_fixed entry; otherwise
    # synthesize one.
    matched = next(
        (e for e in not_fixed_entries if e.get("finding_id") == finding_id),
        None,
    )
    if matched:
        return {
            "finding_id": finding_id,
            "fixed": False,
            "has_changes": False,
            "entry": matched,
        }
    return {
        "finding_id": finding_id,
        "fixed": False,
        "has_changes": False,
        "entry": {
            "finding_id": finding_id,
            "file": file,
            "line": finding.get("line", 0),
            "finding_message": finding.get("message", "")[:200],
            "reason": "deferred",
            "detail": (
                "Polish attempt produced no changes and the senior did "
                "not provide a reason; treat as requiring human review."
            ),
        },
    }


def _spawn_fix_senior(pipeline: "FixPipeline", finding: dict, timeout_s: int) -> dict:
    """Start a fresh fix-senior session for the first polish finding.

    Mirrors ``FixPipeline._run_fix_session`` but with the per-finding
    timeout and the current (scoped) ``verdict.json``. Shares state via
    ``pipeline`` so subsequent polish findings can ``_resume_fix_senior``.
    """
    from gate.claude import spawn_review_stage
    from gate.config import repo_slug
    from gate.fixer import _get_changed_files
    from gate.tmux import kill_window

    workspace = pipeline.workspace
    result_file = workspace / "fix-senior-result.json"
    result_file.unlink(missing_ok=True)
    fix_json_path = workspace / "fix.json"
    fix_json_path.unlink(missing_ok=True)

    review_id = (
        f"{repo_slug(pipeline.repo)}-pr{pipeline.pr_number}"
        if pipeline.repo else f"pr{pipeline.pr_number}"
    )
    pane_id = spawn_review_stage(
        review_id=review_id,
        stage="fix-senior",
        workspace=str(workspace),
        socket_path=str(pipeline.socket_path) if pipeline.socket_path else None,
        repo=pipeline.repo,
    )
    if not pane_id:
        return {"fix_json": None, "has_changes": False}

    pipeline.fix_pane_id = pane_id
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pipeline._cancelled.is_set():
            kill_window(pane_id)
            return {"fix_json": None, "has_changes": False}
        if result_file.exists():
            break
        time.sleep(5)

    session_file = workspace / "fix-senior-session-id.txt"
    if session_file.exists():
        pipeline.session_id = session_file.read_text().strip()
    kill_window(pane_id)

    fix_json = _read_json(workspace, "fix.json") or _read_json(
        workspace, "fix-senior-findings.json"
    )
    has_changes = len(_get_changed_files(workspace)) > 0
    return {"fix_json": fix_json, "has_changes": has_changes}


def _resume_fix_senior(pipeline: "FixPipeline", finding: dict, timeout_s: int) -> dict:
    """Resume the senior Claude session with a single-finding focus prompt."""
    from gate.fixer import _get_changed_files

    workspace = pipeline.workspace
    focus = _render_single_finding_prompt(finding, pipeline._polish_context)
    resume_prompt = (
        "# Polish loop — next finding\n\n"
        "The pipeline has just bootstrapped a **fresh** Codex session for "
        "you. Your prior `gate-code` commands ran against an older thread "
        "that no longer exists — all subsequent `gate-code` calls use the "
        "new thread via the updated env. Do not reference the previous "
        "thread's state.\n\n"
        "Address ONLY the following finding. Emit `fixed[]` or "
        "`not_fixed[]` with this finding's `finding_id`. Do not touch "
        "other findings in this call.\n\n"
        f"{focus}\n"
        "When you are done, write fix-senior-findings.json as usual "
        "(one entry in fixed[] OR not_fixed[]) and stop."
    )
    # Clear previous result files so we can distinguish this attempt.
    (workspace / "fix.json").unlink(missing_ok=True)
    (workspace / "fix-senior-findings.json").unlink(missing_ok=True)

    env = build_claude_env()
    env["GATE_CODEX_THREAD_ID"] = pipeline.codex_thread_id
    env["GATE_FIX_WORKSPACE"] = str(workspace)

    from gate.fixer import RESUME_MAX_TURNS
    cmd = [
        "claude",
        "--dangerously-skip-permissions",
        "--print",
        "--resume",
        pipeline.session_id or "",
        "--model",
        pipeline.config.get("models", {}).get("fix_senior", "opus"),
        "--max-turns",
        str(RESUME_MAX_TURNS),
        resume_prompt,
    ]
    try:
        with open(workspace / "polish-resume-stdout.log", "ab") as out, open(
            workspace / "polish-resume-stderr.log", "ab"
        ) as err:
            subprocess.run(
                cmd,
                env=env,
                cwd=str(workspace),
                timeout=timeout_s,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
            )
    except subprocess.TimeoutExpired:
        logger.warning(f"polish resume timed out after {timeout_s}s")
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning(f"polish resume failed: {e}")

    fix_json = _read_json(workspace, "fix.json") or _read_json(
        workspace, "fix-senior-findings.json"
    )
    has_changes = len(_get_changed_files(workspace)) > 0
    return {"fix_json": fix_json, "has_changes": has_changes}


# ── Self-audit pass (1G + audit A21) ────────────────────────


def _run_fix_polish_audit(pipeline: "FixPipeline") -> dict | None:
    """Run ``fix-polish.md`` once over the accumulated diff as a self-audit.

    Uses the existing ``StructuredRunner`` + ``fix-polish`` schema, so
    this adds ~20 lines rather than a new subsystem. Returns the parsed
    result dict or ``None`` if the prompt could not be rendered.
    """
    try:
        template = prompt.load("fix-polish")
    except FileNotFoundError:
        logger.info("fix-polish.md not present; skipping self-audit")
        return None

    # write_diff so $fix_diff is fresh
    from gate.fixer import write_diff
    write_diff(pipeline.workspace)

    vars_dict = prompt.build_vars(
        pipeline.workspace, "fix-polish", {}, pipeline.config
    )
    assembled = prompt.safe_substitute(template, vars_dict, "polish-audit")

    def _run() -> StageResult:
        return StructuredRunner().run(
            "fix-polish", assembled, pipeline.workspace, pipeline.config
        )

    result = run_with_retry(_run, "fix-polish", pipeline.config)
    data = result.data or {}
    (pipeline.workspace / "fix-polish.json").write_text(json.dumps(data, indent=2))
    clean = data.get("clean", True)
    corrections = data.get("corrections", []) or []
    write_live_log(
        pipeline.pr_number,
        f"fix-polish self-audit: clean={clean} corrections={len(corrections)}",
        prefix="fix", repo=pipeline.repo,
    )
    return data


# ── Top-level loop ──────────────────────────────────────────


def run_polish_loop(
    pipeline: "FixPipeline",
    findings: list[dict],
) -> dict:
    """Execute the polish loop and return an aggregate ``fix.json`` dict.

    The caller (``FixPipeline.run``) is responsible for:
    - deciding whether to enter the polish loop (feature flag)
    - running re-review + commit_and_push on the aggregate afterwards

    This function only handles the per-finding dispatch, build
    checkpointing, and self-audit.
    """
    pipeline._polish_context = list(findings)
    timeouts = get_polish_timeouts(pipeline.config)
    total_budget_s = get_polish_total_budget_s(pipeline.config)
    started_at = time.monotonic()

    # Audit A11: re-emit the canonical TUI stage names so the FIX_STAGES
    # pill lights up in order (bootstrap -> session -> build -> rereview
    # -> commit) even though polish mode runs a per-finding loop.
    pipeline._emit_fix_stage("fix-bootstrap")
    ordered = _sort_by_fixability(findings)
    write_live_log(
        pipeline.pr_number,
        f"polish loop: {len(ordered)} findings, "
        f"budget={total_budget_s}s, timeouts={timeouts}",
        prefix="fix", repo=pipeline.repo,
    )

    fixed: list[dict] = []
    not_fixed: list[dict] = []

    for idx, finding in enumerate(ordered, start=1):
        if pipeline._cancelled.is_set():
            write_live_log(
                pipeline.pr_number, "polish loop cancelled",
                prefix="fix", repo=pipeline.repo,
            )
            break

        cls = finding.get("fixability", "unknown")
        per_finding_timeout = int(timeouts.get(cls, timeouts.get("unknown", 180)))
        elapsed = time.monotonic() - started_at

        if total_budget_s and elapsed >= total_budget_s:
            not_fixed.append({
                "finding_id": finding.get("finding_id"),
                "file": finding.get("file"),
                "line": finding.get("line"),
                "finding_message": (finding.get("message") or "")[:200],
                "reason": "deferred_by_budget",
                "detail": (
                    f"Polish loop exhausted its {total_budget_s}s total "
                    f"budget before this finding could be attempted."
                ),
            })
            continue

        if per_finding_timeout <= 0:
            # broad (or explicitly skipped) — add to not_fixed without
            # dispatching. This is the exit path for broad/architectural
            # findings (e.g. file-size ceilings) that shouldn't consume
            # the polish budget.
            not_fixed.append({
                "finding_id": finding.get("finding_id"),
                "file": finding.get("file"),
                "line": finding.get("line"),
                "finding_message": (finding.get("message") or "")[:200],
                "reason": "skipped_broad_in_polish",
                "detail": (
                    f"Finding classified `{cls}`; polish loop does not "
                    "attempt broad/architectural changes (timeout=0)."
                ),
            })
            continue

        # Clamp per-finding timeout to remaining total budget so a
        # trivial at the end of a nearly-empty budget cannot overrun.
        if total_budget_s:
            remaining = max(30, int(total_budget_s - elapsed))
            per_finding_timeout = min(per_finding_timeout, remaining)

        pipeline._emit_fix_stage("fix-session")  # audit A11: reuse existing stage name
        write_live_log(
            pipeline.pr_number,
            f"polish[{idx}/{len(ordered)}] {cls} {finding.get('file')} "
            f"(timeout={per_finding_timeout}s)",
            prefix="fix", repo=pipeline.repo,
        )

        outcome = _attempt_finding(pipeline, finding, per_finding_timeout)
        if outcome.get("fixed"):
            fixed.append(outcome["entry"])
        else:
            not_fixed.append(outcome["entry"])

        pipeline._emit_fix_stage("fix-build")

    # Run the fix-polish.md self-audit once at the end (1G).
    audit = _run_fix_polish_audit(pipeline)
    if audit and not audit.get("clean", True):
        corrections = audit.get("corrections", []) or []
        write_live_log(
            pipeline.pr_number,
            f"polish self-audit flagged {len(corrections)} corrections; "
            "logging for human review.",
            prefix="fix", repo=pipeline.repo,
        )
        # We deliberately do NOT auto-apply audit corrections: the senior
        # already did its best pass per finding, and a trailing
        # auto-correct would re-introduce the exact all-or-nothing
        # failure mode the polish loop was designed to eliminate. Instead
        # surface corrections in the PR comment / log and let the
        # follow-up iteration pick them up when the human next pushes.

    return {
        "fixed": fixed,
        "not_fixed": not_fixed,
        "stats": {
            "total_findings": len(findings),
            "fixed": len(fixed),
            "not_fixed": len(not_fixed),
            "files_modified": 0,
            "files_created": 0,
            "loop_elapsed_s": int(time.monotonic() - started_at),
        },
        "polish_self_audit": audit or {"clean": True, "corrections": []},
    }


# ── Small helpers ───────────────────────────────────────────


def _read_json(workspace: Path, filename: str) -> dict | None:
    path = workspace / filename
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def _git_head_sha(workspace: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(workspace), capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return ""


def _git_checkpoint(workspace: Path, message: str) -> None:
    """Create a local-only checkpoint commit. Non-fatal if it fails."""
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(workspace), capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "--no-verify",
             "--allow-empty", "-m", message],
            cwd=str(workspace), capture_output=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning(f"checkpoint commit failed: {e}")


def _reset_to(workspace: Path, sha: str) -> None:
    if not sha:
        return
    try:
        subprocess.run(
            ["git", "reset", "--hard", sha],
            cwd=str(workspace), capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=str(workspace), capture_output=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning(f"git reset to {sha[:8]} failed: {e}")

"""Codex delegation bridge (senior/junior model).

Ported from gate-code.js. Claude senior invokes this via bash to delegate
work to Codex junior, which resumes an existing thread.

Usage pattern (from Claude's bash tool inside fix-senior session):
    # First, write directions to gate-directions.md using your file tool.
    # Always overwrite the file before each call (do not append).
    # Then redirect stdin from it:
    gate-code <stage> < gate-directions.md

Stages: prep, design, implement, audit
"""

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from gate import codex as _codex
from gate.codex import run_codex
from gate.io import atomic_write
from gate.prompt import safe_substitute

# Interval (seconds) between heartbeat prints emitted while a codex
# junior stage is running. Exposed as a module attribute so tests can
# reduce it, and so it can be tuned independently of runner stuck
# timeouts (currently runner hard_timeout_s=1200 — a 30s heartbeat
# keeps the senior pane "active" with ~40 samples before any hit).
HEARTBEAT_INTERVAL_S = 30.0

logger = logging.getLogger(__name__)

ALLOWED_STAGES = ["prep", "design", "implement", "audit"]


def _load_prompt_template(stage_name: str) -> str | None:
    """Load the gate-{stage}.md prompt template."""
    from gate.config import gate_dir

    prompt_path = gate_dir() / "prompts" / f"gate-{stage_name}.md"
    try:
        return prompt_path.read_text().strip()
    except OSError:
        return None


def _next_version(workspace: Path, stage_name: str) -> int | None:
    """Return the next version number for stage artifacts.

    First run uses base names. Re-runs use _1, _2, etc.
    """
    if not (workspace / f"{stage_name}.out.md").exists():
        return None
    n = 1
    while (workspace / f"{stage_name}_{n}.out.md").exists():
        n += 1
    return n


def run_code_stage(
    stage: str,
    request: str,
    workspace: Path,
    thread_id: str,
    config: dict | None = None,
) -> int:
    """Run a Codex code stage (prep, design, implement, audit).

    Ported from gate-code.js main(). Loads the prompt template,
    substitutes $request, runs Codex via resume, and prints output.

    Args:
        stage: Stage name (prep, design, implement, audit).
        request: The directions/request text from senior Claude.
        workspace: Workspace directory for artifacts.
        thread_id: Codex thread ID to resume.
        config: Pre-loaded gate config. When None, loaded once here. Threading
            it from the caller avoids hidden ``load_config()`` calls inside
            ``resolve_repo_config``.

    Returns:
        Exit code (0 on success).
    """
    if stage not in ALLOWED_STAGES:
        logger.error(f"Unknown stage: {stage}. Allowed: {ALLOWED_STAGES}")
        return 1

    template = _load_prompt_template(stage)
    if not template:
        logger.error(f"Prompt template not found: gate-{stage}.md")
        return 1

    # Merge profile variables so $typecheck_cmd etc. resolve in gate-implement.md
    import json as _json

    from gate import profiles

    workspace_path = workspace
    repo_config = {}

    pr_meta = workspace_path / "pr-metadata.json"
    if pr_meta.exists():
        try:
            meta = _json.loads(pr_meta.read_text())
        except (OSError, _json.JSONDecodeError):
            logger.exception("gate-code: failed to read pr-metadata.json")
            meta = {}
        repo_name = meta.get("repo", "")
        if repo_name:
            from gate.config import resolve_repo_config
            try:
                full_cfg = resolve_repo_config(repo_name, config)
                repo_config = full_cfg.get("repo", {})
            except ValueError:
                logger.warning(f"gate-code: no repo config for {repo_name}")

    profile = profiles.resolve_profile(repo_config, workspace_path)
    vars_dict = {"request": request}
    vars_dict.update({k: v for k, v in profile.items() if isinstance(v, str)})
    prompt_text = safe_substitute(template, vars_dict, f"gate-code-{stage}")

    version = _next_version(workspace, stage)
    suffix = stage if version is None else f"{stage}_{version}"

    input_path = workspace / f"{suffix}.in.md"
    atomic_write(input_path, prompt_text)

    output_path = workspace / f"{suffix}.out.md"
    stdout_log_path = workspace / f"{suffix}.codex.log"

    logger.info(
        f"gate-code {stage} starting (thread={thread_id[:8]}, request={len(request)} chars)"
    )

    from gate.config import build_claude_env

    env = build_claude_env()

    # Heartbeat so senior's tmux pane keeps changing now that codex's own
    # stdout is redirected to stdout_log. Without this, the runner's
    # stuck-detector (runner._check_activity, hard_timeout_s=1200) could
    # fire on a long junior call whose only in-pane signal is Claude
    # Code's spinner. gate-code's own stdout still inherits to senior's
    # Bash tool, so prints here are visible in the pane.
    stop_heartbeat = threading.Event()
    start_time = time.monotonic()

    def _heartbeat() -> None:
        while not stop_heartbeat.wait(timeout=HEARTBEAT_INTERVAL_S):
            elapsed = int(time.monotonic() - start_time)
            print(
                f"gate-code: {stage} still running ({elapsed}s)",
                flush=True,
            )

    heartbeat_thread = threading.Thread(
        target=_heartbeat, name=f"gate-code-heartbeat-{stage}", daemon=True
    )
    heartbeat_thread.start()

    try:
        exit_code, cmd = run_codex(
            prompt_text,
            str(workspace),
            str(output_path),
            thread_id,
            env=env,
            stdout_log=str(stdout_log_path),
        )
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=1.0)

    if output_path.exists():
        content = output_path.read_text()
        if content:
            print(content)

    if exit_code == 0:
        logger.info(f"gate-code {stage} complete ({suffix})")
    else:
        logger.warning(f"gate-code {stage} failed (exit={exit_code})")

    return exit_code


def main() -> int:
    """CLI entry point for gate-code command.

    Called by Claude senior via bash. Directions must be piped in from a
    file (never a heredoc — heredocs corrupt directions whose content
    contains the terminator or other shell metacharacters, and are a
    known shell-injection vector when the terminator is user-chosen):

        # First overwrite gate-directions.md with your directions via a
        # file-writing tool, then:
        gate-code <stage> < gate-directions.md
    """
    if len(sys.argv) < 2 or sys.argv[1] not in ALLOWED_STAGES:
        print(f"Usage: gate-code <{'|'.join(ALLOWED_STAGES)}>", file=sys.stderr)
        print("  Pipe directions via stdin.", file=sys.stderr)
        return 1

    stage = sys.argv[1]

    # Fix 2c: if we receive SIGTERM (orchestrator cancel) or SIGHUP
    # (tmux pane died), take the currently-running codex subprocess
    # down with us instead of orphaning it onto the user's quota.
    # We forward the signal using exit code (128 + signum) so callers
    # see a conventional signal-exit status.
    def _handle_signal(signum: int, _frame) -> None:
        logger.warning(
            "gate-code %s received signal %s — terminating active codex",
            stage, signum,
        )
        _codex.terminate_active(signal.SIGTERM)
        sys.exit(128 + signum)

    for _sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(_sig, _handle_signal)
        except (ValueError, OSError):
            # ValueError: handler installation from a non-main thread.
            # OSError: platform doesn't support the signal (not macOS/Linux).
            # Either way, the runner's pkill sweep (Fix 2d) is the backstop.
            pass

    thread_id = os.environ.get("GATE_CODEX_THREAD_ID", "")
    if not thread_id:
        print("GATE_CODEX_THREAD_ID not set — run codex bootstrap first", file=sys.stderr)
        return 1

    workspace_str = os.environ.get("GATE_FIX_WORKSPACE", "")
    if not workspace_str:
        print("GATE_FIX_WORKSPACE not set", file=sys.stderr)
        return 1

    try:
        request = sys.stdin.read().strip()
    except Exception:
        request = ""
    if not request:
        print("No directions provided on stdin", file=sys.stderr)
        return 1

    from gate.config import load_config
    config = load_config()
    return run_code_stage(stage, request, Path(workspace_str), thread_id, config=config)

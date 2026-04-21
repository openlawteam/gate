"""Codex CLI wrapper for Gate.

Provides bootstrap_codex() to create a fresh Codex session and
run_codex() to resume an existing thread.

Both spawners use ``subprocess.Popen(..., start_new_session=True)`` so
the codex subprocess runs in its *own* process group. This is the
anchor for Fix 2 (orphan-leak cleanup): signal handlers can target the
whole codex process tree with a single ``os.killpg`` call, and neither
a SIGHUP on the tmux pane nor a SIGTERM on gate-code leaves codex
quietly chewing through quota.
"""

import json
import logging
import os
import signal
import subprocess
import threading

logger = logging.getLogger(__name__)

CODEX_FLAGS = "--dangerously-bypass-approvals-and-sandbox"

# Timeout (seconds) applied to both bootstrap and resume codex runs.
# Long-running by design — a full junior stage can easily exceed 10
# minutes. See `gate.code.HEARTBEAT_INTERVAL_S` for the mechanism that
# keeps senior's tmux pane from triggering the runner stuck-detector
# during these long calls.
CODEX_TIMEOUT_S = 2400

# Grace period (seconds) between SIGTERM and SIGKILL when terminating
# the codex process group. Short on purpose — codex does not catch
# SIGTERM gracefully, and the orchestrator cares about prompt exit.
_TERM_GRACE_S = 2.0

# Reference to the currently-running codex Popen, or None when no codex
# call is in flight. Updated (serialized by ``_active_lock``) around each
# communicate() inside a try/finally so signal handlers in
# ``gate.code.main`` can reach out and kill the process group even when
# the call is blocked inside a subprocess syscall. Only one codex call
# is ever in flight per gate-code process, so a single slot is enough.
_active_proc: subprocess.Popen | None = None
_active_lock = threading.Lock()


def _set_active(proc: subprocess.Popen | None) -> None:
    global _active_proc
    with _active_lock:
        _active_proc = proc


def terminate_active(sig: int = signal.SIGTERM) -> bool:
    """Signal the currently-running codex process group.

    Invoked from ``gate.code.main``'s SIGTERM/SIGHUP handlers so that a
    tmux pane SIGHUP (or an orchestrator SIGTERM cascade) tears down
    codex instead of orphaning it onto the user's quota.

    Returns True if a signal was dispatched, False if nothing was
    active. Best-effort: swallows ProcessLookupError (race: codex
    already exited) and PermissionError (should never happen, but we
    do not want a noisy signal handler).
    """
    with _active_lock:
        proc = _active_proc
    if proc is None:
        return False
    try:
        os.killpg(proc.pid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        logger.warning("terminate_active: permission denied: %s", exc)
        return False


def _kill_and_wait(proc: subprocess.Popen) -> None:
    """Send SIGTERM to the codex pgroup, wait briefly, then SIGKILL.

    Used on TimeoutExpired so we never leave a codex process orphaned
    when `Popen.communicate(timeout=…)` returns: that API *raises* on
    timeout but does not kill the child for us the way
    ``subprocess.run`` does.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=_TERM_GRACE_S)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=_TERM_GRACE_S)
        except subprocess.TimeoutExpired:
            logger.error("codex pgroup still alive after SIGKILL (pid=%s)", proc.pid)


def bootstrap_codex(
    prompt: str, cwd: str, env: dict | None = None
) -> tuple[int, str | None]:
    """Bootstrap a new Codex session and return its thread ID.

    Runs codex exec --json to create a fresh session. Parses the thread_id
    from the JSONL output and discards everything else.

    Args:
        prompt: The prompt text to send to Codex.
        cwd: Working directory for Codex.
        env: Optional environment dict. Uses inherited env if None.

    Returns:
        (exit_code, thread_id) tuple. thread_id is None on failure.
        Exit code is 127 if codex not found, 130 on KeyboardInterrupt.
    """
    cmd = ["codex", "exec", CODEX_FLAGS, "--json", prompt]

    logger.debug(f"Bootstrapping codex session in {cwd}")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        logger.error("codex command not found")
        return 127, None

    _set_active(proc)
    try:
        try:
            stdout, _ = proc.communicate(timeout=CODEX_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            logger.error("codex bootstrap timed out")
            _kill_and_wait(proc)
            return 1, None
        except KeyboardInterrupt:
            _kill_and_wait(proc)
            return 130, None
    finally:
        _set_active(None)

    thread_id = _parse_thread_id(stdout or "")
    if proc.returncode == 0 and not thread_id:
        logger.error("Failed to parse thread_id from codex output")

    return proc.returncode, thread_id


def run_codex(
    prompt: str,
    cwd: str,
    output_file: str,
    thread_id: str,
    env: dict | None = None,
    stdout_log: str | None = None,
) -> tuple[int, list[str]]:
    """Run Codex by resuming an existing session.

    Args:
        prompt: The prompt text to send to Codex.
        cwd: Working directory for Codex.
        output_file: Path to write the final agent message.
        thread_id: Codex thread ID to resume.
        env: Optional environment dict. Uses inherited env if None.
        stdout_log: Optional path to redirect codex subprocess stdout to.
            When set, codex's reasoning chatter and tool-use logs are
            captured to the file instead of flowing to the parent's
            stdout (which would otherwise inherit to senior Claude's
            Bash tool and inflate its context). The final agent message
            still writes to ``output_file`` via codex's ``-o`` flag.
            Stderr remains inherited so genuine errors surface normally.

    Returns:
        (exit_code, cmd) tuple. Exit code is 127 if codex not found,
        130 on KeyboardInterrupt, 1 on timeout.
    """
    cmd = [
        "codex",
        "exec",
        CODEX_FLAGS,
        "-o",
        output_file,
        "resume",
        thread_id,
        prompt,
    ]

    logger.debug(f"Running: codex exec resume {thread_id[:8]}... in {cwd}")

    stdout_handle = None
    try:
        if stdout_log:
            stdout_handle = open(stdout_log, "ab")
        popen_kwargs: dict = {
            "cwd": cwd,
            "env": env,
            "start_new_session": True,
        }
        if stdout_handle is not None:
            popen_kwargs["stdout"] = stdout_handle

        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except FileNotFoundError:
            logger.error("codex command not found")
            return 127, cmd

        _set_active(proc)
        try:
            try:
                proc.communicate(timeout=CODEX_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                logger.error("codex run timed out")
                _kill_and_wait(proc)
                return 1, cmd
            except KeyboardInterrupt:
                _kill_and_wait(proc)
                return 130, cmd
        finally:
            _set_active(None)

        return proc.returncode, cmd
    finally:
        if stdout_handle is not None:
            try:
                stdout_handle.close()
            except OSError:
                pass


def _parse_thread_id(stdout: str) -> str | None:
    """Parse thread_id from the first JSONL line of codex --json output.

    Looks for: {"type":"thread.started","thread_id":"<uuid>"}

    Args:
        stdout: Raw stdout from codex exec --json.

    Returns:
        The thread_id string, or None if not found.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if event.get("type") == "thread.started" and "thread_id" in event:
                return event["thread_id"]
        except json.JSONDecodeError:
            continue
    return None

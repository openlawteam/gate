"""Codex CLI wrapper for Gate.

Provides bootstrap_codex() to create a fresh Codex session and
run_codex() to resume an existing thread.
"""

import json
import logging
import subprocess

logger = logging.getLogger(__name__)

CODEX_FLAGS = "--dangerously-bypass-approvals-and-sandbox"


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
        result = subprocess.run(
            cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, text=True,
            timeout=2400,
        )
    except subprocess.TimeoutExpired:
        logger.error("codex bootstrap timed out")
        return 1, None
    except FileNotFoundError:
        logger.error("codex command not found")
        return 127, None
    except KeyboardInterrupt:
        return 130, None

    thread_id = _parse_thread_id(result.stdout)
    if result.returncode == 0 and not thread_id:
        logger.error("Failed to parse thread_id from codex output")

    return result.returncode, thread_id


def run_codex(
    prompt: str,
    cwd: str,
    output_file: str,
    thread_id: str,
    env: dict | None = None,
) -> tuple[int, list[str]]:
    """Run Codex by resuming an existing session.

    Args:
        prompt: The prompt text to send to Codex.
        cwd: Working directory for Codex.
        output_file: Path to write the final agent message.
        thread_id: Codex thread ID to resume.
        env: Optional environment dict. Uses inherited env if None.

    Returns:
        (exit_code, cmd) tuple. Exit code is 127 if codex not found,
        130 on KeyboardInterrupt.
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

    try:
        result = subprocess.run(cmd, cwd=cwd, env=env, timeout=2400)
        return result.returncode, cmd
    except subprocess.TimeoutExpired:
        logger.error("codex run timed out")
        return 1, cmd
    except FileNotFoundError:
        logger.error("codex command not found")
        return 127, cmd
    except KeyboardInterrupt:
        return 130, cmd


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

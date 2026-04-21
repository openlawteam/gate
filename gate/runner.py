"""Review stage runners — the core execution engine.

Contains:
- ReviewRunner: Runs agent stages inside tmux windows.
  Claude runs interactively; prompts instruct it to write result files.
- StructuredRunner: Runs structured stages inline (no tmux) via --print + --json-schema.
- run_with_retry: Retry wrapper with exponential backoff for rate limits/transient errors.
- extract_error_message: Stderr parsing helper.

Architecture:
  tmux window → gate process <review_id> <stage> → subprocess.Popen(claude) → interactive
"""

import json
import logging
import os
import random
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from gate import prompt
from gate.config import build_claude_env
from gate.extract import extract_stage_output
from gate.schemas import STAGE_EFFORT, STAGE_SCHEMAS, StageResult, build_fallback
from gate.tmux import capture_pane, get_current_pane_id, rename_window, send_keys

logger = logging.getLogger(__name__)

ERROR_LINES = 5
MONITOR_INTERVAL = 5.0


def extract_error_message(stderr_bytes: bytes) -> str | None:
    """Extract last N lines from stderr as error message."""
    if not stderr_bytes:
        return None

    text = stderr_bytes.decode("utf-8", errors="replace")
    lines = text.strip().splitlines()
    if not lines:
        return None

    tail = lines[-ERROR_LINES:]
    return "\n".join(tail)


def _is_rate_limited(stderr_text: str) -> bool:
    """Check if stderr indicates a rate limit error."""
    return any(s in stderr_text for s in ("rate limit", "429", "overloaded"))


def _is_transient(stderr_text: str) -> bool:
    """Check if stderr indicates a transient network error."""
    import re

    return bool(re.search(r"ETIMEDOUT|ECONNRESET|ECONNREFUSED", stderr_text, re.IGNORECASE))


class ReviewRunner:
    """Runs a single Claude agent stage inside a tmux window.

    This class runs as the `gate process` command inside a tmux pane.

    Lifecycle:
    1. Signal handlers
    2. Server connection + registration (optional — skipped if no socket)
    3. Setup (load prompt, prepare context)
    4. Run Claude subprocess (inherits pane TTY)
    5. Monitor thread polls capture_pane() for stuck detection
    6. Extract result from file Claude wrote
    7. Cleanup
    """

    def __init__(
        self,
        review_id: str,
        stage: str,
        workspace: Path,
        config: dict,
        socket_path: Path | None = None,
    ):
        self.review_id = review_id
        self.stage = stage
        self.workspace = workspace
        self.config = config
        self.socket_path = socket_path
        self.connection = None  # GateConnection, Phase 4

        self._prompt_text: str = ""
        self._session_id: str = ""

        # Activity monitor state
        self._monitor_thread: threading.Thread | None = None
        self._monitor_stop = threading.Event()
        self._last_snapshot: str | None = None
        self._stuck_since: float | None = None
        self._pane_id: str | None = None
        self._proc: subprocess.Popen | None = None

        # Completion tracking
        self._done = threading.Event()

    def run(self) -> int:
        """Run Claude for this stage. Returns exit code.

        Server connection is optional so Phase 1 can test runners standalone
        before the server exists (Phase 4). When socket_path is None, the
        runner writes results to files only and skips IPC.
        """
        original_sigint = signal.signal(signal.SIGINT, self._handle_signal)
        original_sigterm = signal.signal(signal.SIGTERM, self._handle_signal)

        try:
            if self.socket_path:
                from gate.client import GateConnection
                from gate.tmux import get_current_pane_id

                self.connection = GateConnection(self.socket_path)
                self.connection.start()
                self.connection.emit(
                    "stage_register",
                    review_id=self.review_id,
                    stage=self.stage,
                    tmux_pane=get_current_pane_id() or "",
                    pid=os.getpid(),
                )

            # Setup: load prompt, build command
            err = self._setup()
            if err is not None:
                self._emit_state("error", f"Setup failed: {err}")
                return 1

            # Run Claude (blocking)
            exit_code, error_msg = self._run_claude()

            if exit_code != 0 and exit_code != 130:
                msg = error_msg or f"Exited with code {exit_code}"
                self._emit_state("error", msg)
                return exit_code

            # Extract findings and write result envelope for orchestrator
            result = self._extract_and_write_result()
            if result:
                self._emit_state("done", f"{self.stage} complete")
            else:
                self._emit_state("error", "No result extracted")

            return exit_code

        except Exception as exc:
            self._emit_state("error", str(exc))
            return 1
        finally:
            self._stop_monitor()
            signal.signal(signal.SIGINT, original_sigint)
            signal.signal(signal.SIGTERM, original_sigterm)
            if self.connection:
                self.connection.stop()

    def _setup(self) -> str | None:
        """Load prompt template, substitute vars, write assembled prompt."""
        try:
            template = prompt.load(self.stage)
        except FileNotFoundError as e:
            return str(e)

        env_vars = self._load_env_vars()
        vars_dict = prompt.build_vars(self.workspace, self.stage, env_vars, self.config)
        assembled = prompt.safe_substitute(template, vars_dict, f"runner-{self.stage}")

        self._prompt_text = assembled

        if self.stage == "fix-senior":
            no_codex = self.workspace / "no-codex.txt"
            if no_codex.exists():
                self._prompt_text += (
                    "\n\n## IMPORTANT: Codex delegation unavailable\n\n"
                    "The `gate-code` delegation tool is not available in this session. "
                    "You must implement all fixes directly yourself using your own tools. "
                    "Do NOT attempt to run `gate-code` commands.\n"
                )

        self._session_id = self._load_or_create_session_id()
        return None

    def _build_command(self) -> tuple[list[str], str]:
        """Build the Claude CLI command."""
        cmd = ["claude", "--dangerously-skip-permissions"]
        cmd += ["--session-id", self._session_id]

        model_key = self.stage.replace("-", "_")
        model = self.config.get("models", {}).get(model_key, "sonnet")
        cmd += ["--model", model]

        effort = STAGE_EFFORT.get(self.stage)
        if effort:
            cmd += ["--effort", effort]

        # Context file (Gap #3)
        context_file = self.workspace / f"{self.stage}-context.md"
        if context_file.exists():
            cmd += ["--append-system-prompt-file", str(context_file)]

        # Prompt as positional arg
        cmd.append(self._prompt_text)

        return cmd, str(self.workspace)

    def _run_claude(self) -> tuple[int, str | None]:
        """Run Claude subprocess.

        Claude inherits the pane's TTY (stdout goes to the pane).
        Only stderr is piped for error capture.

        A dismiss thread runs in parallel: once the findings file appears
        and the screen stabilizes, it sends Ctrl-C to exit Claude cleanly.
        """
        cmd, cwd = self._build_command()
        env = build_claude_env()

        # Load fix-env.json for fix stages (tmux doesn't inherit fixer's env)
        fix_env_path = self.workspace / "fix-env.json"
        if fix_env_path.exists():
            try:
                fix_env = json.loads(fix_env_path.read_text())
                env.update(fix_env)
            except (json.JSONDecodeError, OSError):
                pass

        logger.debug(f"Running: {' '.join(cmd[:3])}...")

        try:
            self._proc = subprocess.Popen(cmd, env=env, stderr=subprocess.PIPE, cwd=cwd)
            self._emit_state("running", f"Claude running ({self.stage})")
            self._start_monitor()

            # Start dismiss thread
            if self._pane_id:
                threading.Thread(
                    target=self._wait_and_dismiss_claude,
                    name=f"{self.stage}-dismiss",
                    daemon=True,
                ).start()

            _, stderr_data = self._proc.communicate()

            if self._proc.returncode != 0:
                if stderr_data:
                    self._persist_stderr(stderr_data)
                    error_msg = extract_error_message(stderr_data)
                    return self._proc.returncode, error_msg
                logger.warning(
                    f"[{self.stage}] Claude exited {self._proc.returncode} with no stderr"
                )

            return self._proc.returncode, None
        except FileNotFoundError:
            logger.error("claude command not found")
            return 127, "claude command not found"
        except KeyboardInterrupt:
            return 130, None

    def _persist_stderr(self, stderr_data: bytes) -> None:
        """Write Claude's stderr to a sidecar file in the workspace.

        The runner subprocess lives inside a backgrounded tmux window that
        is torn down shortly after exit (see ``gate/claude.py``), so any
        stderr we don't persist is unrecoverable. The sidecar gives the
        operator something concrete to read after the fact.
        """
        try:
            path = self.workspace / f"{self.stage}-runner-stderr.log"
            path.write_bytes(stderr_data)
        except OSError as exc:
            logger.warning(f"Failed to persist runner stderr to {path}: {exc}")

    def _wait_and_dismiss_claude(self) -> None:
        """Wait for findings file, screen stability, then send Ctrl-C to exit Claude."""
        findings_path = self.workspace / f"{self.stage}-findings.json"

        # Phase 1: wait for the findings file to appear
        while not self._done.is_set():
            if self._monitor_stop.is_set():
                return
            if findings_path.exists():
                self._done.set()
                logger.info(f"Findings file detected for {self.review_id}/{self.stage}")
                break
            self._done.wait(timeout=MONITOR_INTERVAL)

        if not self._pane_id:
            return

        # Phase 2: wait for screen to stabilize, then dismiss
        while not self._monitor_stop.is_set():
            logger.debug(f"Dismiss: waiting for screen to stabilize ({self.stage})")

            last_snapshot = None
            while not self._monitor_stop.is_set():
                self._monitor_stop.wait(MONITOR_INTERVAL)
                snapshot = capture_pane(self._pane_id)
                if snapshot is None:
                    return
                if snapshot == last_snapshot:
                    break
                last_snapshot = snapshot

            if self._monitor_stop.is_set():
                return

            logger.info(f"Screen stable, sending Ctrl-C to dismiss Claude ({self.stage})")
            send_keys(self._pane_id, "C-c")
            send_keys(self._pane_id, "C-c")

    def _handle_signal(self, signum: int, frame) -> None:
        """Handle shutdown signals gracefully.

        Fix 2d: Before raising/exiting, sweep any direct children of
        this runner process with ``pkill -TERM -P <our pid>``. This is
        the backstop for the codex orphan-leak fix: tmux propagates
        SIGHUP to claude, claude's own handler *should* tear down its
        gate-code child, and gate-code's Fix 2c handler should take
        codex down with it. But if any link in that chain breaks (e.g.
        claude is still mid-Bash-tool when the pane dies), this sweep
        ensures nothing we directly spawned is left orphaned on the
        user's quota.

        ``pkill -P`` only reaches direct children — grandchildren rely
        on each link propagating the signal further. Failures here are
        swallowed because this is best-effort cleanup on a path that is
        already exiting.
        """
        logger.debug(f"Received signal {signum}")
        try:
            subprocess.run(
                ["pkill", "-TERM", "-P", str(os.getpid())],
                timeout=2,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("pkill sweep in _handle_signal failed: %s", exc)
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        sys.exit(128 + signum)

    def _start_monitor(self) -> None:
        """Start the activity monitor thread."""
        self._pane_id = get_current_pane_id()
        if not self._pane_id:
            logger.debug("Not in tmux, skipping activity monitor")
            return

        rename_window(self._pane_id, f"{self.review_id}-{self.stage}")
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="activity-monitor", daemon=True
        )
        self._monitor_thread.start()
        logger.debug(f"Started activity monitor for pane {self._pane_id}")

    def _stop_monitor(self) -> None:
        """Stop the activity monitor thread."""
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_stop.set()
            self._monitor_thread.join(timeout=1.0)
            logger.debug("Stopped activity monitor")

    def _monitor_loop(self) -> None:
        """Monitor loop that checks for activity every MONITOR_INTERVAL seconds."""
        while not self._monitor_stop.wait(MONITOR_INTERVAL):
            self._check_activity()

    def _check_activity(self) -> None:
        """Check tmux pane for activity and update state accordingly.

        Skips stuck detection once done (dismiss thread handles exit).
        Keeps hard timeout as safety net.
        """
        if not self._pane_id:
            return

        # Skip stuck detection once done — dismiss thread handles exit
        if self._done.is_set():
            return

        snapshot = capture_pane(self._pane_id)
        if snapshot is None:
            logger.debug("Failed to capture pane, stopping monitor")
            self._monitor_stop.set()
            return

        if snapshot == self._last_snapshot:
            now = time.monotonic()
            if self._stuck_since is None:
                self._stuck_since = now - MONITOR_INTERVAL
            duration_sec = int(now - self._stuck_since)
            self._emit_state("stuck", f"No output for {duration_sec}s")

            # Hard timeout: kill subprocess if stuck too long
            hard_timeout = self.config.get("timeouts", {}).get("hard_timeout_s", 1200)
            if duration_sec >= hard_timeout and self._proc and self._proc.poll() is None:
                logger.warning(
                    f"Hard timeout ({hard_timeout}s) reached for {self.review_id}/{self.stage}"
                )
                self._proc.terminate()
                self._emit_state("error", f"Hard timeout after {duration_sec}s stuck")
        else:
            if self._stuck_since is not None:
                self._emit_state("running", "Claude running")
            self._stuck_since = None
            self._last_snapshot = snapshot

    def _emit_state(self, status: str, message: str = "") -> None:
        """Emit state to server via IPC. No-op when running standalone (Phase 1)."""
        logger.info(f"[{self.review_id}/{self.stage}] {status}: {message}")
        if self.connection:
            self.connection.emit(
                "review_stage_update",
                review_id=self.review_id,
                stage=self.stage,
                status=status,
                message=message,
            )

    def _extract_and_write_result(self) -> dict | None:
        """Read the findings file Claude wrote, then write a StageResult envelope.

        Claude is instructed to write its findings to {stage}-findings.json.
        This method reads that, wraps it in a StageResult-compatible envelope,
        and writes {stage}-result.json — which the orchestrator polls for.
        """
        findings = None

        # Primary: read the findings file Claude was prompted to write
        findings_path = self.workspace / f"{self.stage}-findings.json"
        if findings_path.exists():
            try:
                findings = json.loads(findings_path.read_text())
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON in {findings_path}")

        # Fallback: extract from Claude's conversation transcript
        if findings is None:
            raw_path = self.workspace / f"{self.stage}-raw.json"
            if raw_path.exists():
                findings = extract_stage_output(raw_path, self.stage)

        # Write the result envelope that the orchestrator polls for
        result_path = self.workspace / f"{self.stage}-result.json"
        if findings is not None:
            envelope = {"success": True, "data": findings}
        else:
            envelope = {"success": False, "data": build_fallback(self.stage)}
        result_path.write_text(json.dumps(envelope, indent=2))

        return findings

    def _load_env_vars(self) -> dict:
        """Load PR metadata from the workspace file written by the orchestrator."""
        meta_path = self.workspace / "pr-metadata.json"
        if meta_path.exists():
            try:
                return json.loads(meta_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {"pr_title": "", "pr_body": "", "pr_author": ""}

    def _load_or_create_session_id(self) -> str:
        """Load existing session ID or create a new one."""
        session_file = self.workspace / f"{self.stage}-session-id.txt"
        if session_file.exists():
            return session_file.read_text().strip()
        import uuid

        session_id = str(uuid.uuid4())
        session_file.write_text(session_id)
        return session_id


class StructuredRunner:
    """Run a structured Claude stage inline (no tmux window).

    Used for stages that return structured JSON via --json-schema.
    These complete in ~30s and don't benefit from pane monitoring.
    Stages: triage, verdict, fix-rereview, fix-plan, fix-polish.
    """

    def run(
        self,
        stage: str,
        prompt_text: str,
        workspace: Path,
        config: dict,
    ) -> StageResult:
        """Run Claude with --print and capture structured JSON output."""
        cmd = ["claude", "--dangerously-skip-permissions", "--print"]

        model_key = stage.replace("-", "_")
        model = config.get("models", {}).get(model_key, "sonnet")
        cmd += ["--model", model]
        cmd += ["--output-format", "json"]
        cmd += ["--max-turns", "3"]
        cmd += ["--tools", ""]

        schema = STAGE_SCHEMAS.get(stage)
        if schema:
            cmd += ["--json-schema", json.dumps(schema)]

        effort = STAGE_EFFORT.get(stage)
        if effort:
            cmd += ["--effort", effort]

        cmd.append(prompt_text)

        env = build_claude_env()
        timeout = config.get("timeouts", {}).get("structured_stage_s", 120)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                cwd=str(workspace),
                timeout=timeout,
            )

            if proc.returncode != 0:
                stderr = proc.stderr or ""
                if _is_rate_limited(stderr):
                    return StageResult(
                        stage=stage, success=False, data={}, is_rate_limited=True
                    )
                if _is_transient(stderr):
                    return StageResult(
                        stage=stage, success=False, data={}, is_transient=True
                    )
                return StageResult.fallback(stage)

            # Parse structured output from stdout
            result = self._parse_output(proc.stdout, stage)
            if not result:
                return StageResult.fallback(stage)
            return StageResult(stage=stage, success=True, data=result)

        except subprocess.TimeoutExpired:
            return StageResult.fallback(stage)
        except FileNotFoundError:
            return StageResult(
                stage=stage,
                success=False,
                data=build_fallback(stage),
                error="claude command not found",
            )

    def _parse_output(self, stdout: str, stage: str) -> dict | None:
        """Parse Claude's structured output.

        Handles both --json-schema envelope format and raw JSON.
        """
        from gate.extract import extract_json_from_text

        if not stdout or not stdout.strip():
            return None

        # Try JSON schema envelope first (Claude wraps in {structured_output: ...})
        if STAGE_SCHEMAS.get(stage):
            try:
                envelope = json.loads(stdout)
                if isinstance(envelope, dict) and "structured_output" in envelope:
                    return envelope["structured_output"]
            except json.JSONDecodeError:
                pass

        # Fallback: extract JSON from text
        return extract_json_from_text(stdout)


def run_with_retry(
    run_fn: Callable[[], StageResult],
    stage: str,
    config: dict,
    max_retries: int | None = None,
) -> StageResult:
    """Retry a stage with exponential backoff on rate limits/transient errors.

    Ported from the retry loop in run-stage.js callClaude().

    Args:
        run_fn: Callable that returns StageResult (either agent or structured run).
        stage: Stage name for fallback generation.
        config: Config dict with retry settings.
        max_retries: Override max retries (defaults to config value).
    """
    retry_config = config.get("retry", {})
    max_retries = max_retries or retry_config.get("max_retries", 4)
    base_delay = retry_config.get("base_delay_s", 60)
    transient_delay = retry_config.get("transient_base_delay_s", 10)

    for attempt in range(1, max_retries + 1):
        result = run_fn()

        if result.success:
            return result

        if result.cancelled:
            return result

        if result.is_rate_limited and attempt < max_retries:
            delay = base_delay * (2 ** (attempt - 1)) * (0.5 + random.random())
            logger.info(
                f"[{stage}] Rate limited, attempt {attempt}/{max_retries}, "
                f"waiting {delay:.0f}s"
            )
            time.sleep(delay)
            continue

        if result.is_transient and attempt < max_retries:
            delay = transient_delay * (2 ** (attempt - 1)) * (0.5 + random.random())
            logger.info(
                f"[{stage}] Transient error, attempt {attempt}/{max_retries}, "
                f"waiting {delay:.0f}s"
            )
            time.sleep(delay)
            continue

        # Non-retryable error
        break

    return StageResult.fallback(stage)

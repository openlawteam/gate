"""Codex delegation bridge (senior/junior model).

Ported from gate-code.js. Claude senior invokes this via bash to delegate
work to Codex junior, which resumes an existing thread.

Usage pattern (from Claude's bash tool inside fix-senior session):
    gate-code <stage> <<'EOF'
    <directions for junior engineer>
    EOF

Stages: prep, design, implement, audit
"""

import logging
import os
import sys
from pathlib import Path

from gate.codex import run_codex
from gate.prompt import safe_substitute

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


def _atomic_write(path: Path, content: str) -> None:
    """Write content to a file atomically via tmp + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def run_code_stage(
    stage: str,
    request: str,
    workspace: Path,
    thread_id: str,
) -> int:
    """Run a Codex code stage (prep, design, implement, audit).

    Ported from gate-code.js main(). Loads the prompt template,
    substitutes $request, runs Codex via resume, and prints output.

    Args:
        stage: Stage name (prep, design, implement, audit).
        request: The directions/request text from senior Claude.
        workspace: Workspace directory for artifacts.
        thread_id: Codex thread ID to resume.

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

    prompt_text = safe_substitute(template, {"request": request}, f"gate-code-{stage}")

    version = _next_version(workspace, stage)
    suffix = stage if version is None else f"{stage}_{version}"

    input_path = workspace / f"{suffix}.in.md"
    _atomic_write(input_path, prompt_text)

    output_path = workspace / f"{suffix}.out.md"

    logger.info(
        f"gate-code {stage} starting (thread={thread_id[:8]}, request={len(request)} chars)"
    )

    from gate.config import build_claude_env

    env = build_claude_env()
    env["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY", "")

    exit_code, cmd = run_codex(
        prompt_text,
        str(workspace),
        str(output_path),
        thread_id,
        env=env,
    )

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

    Called by Claude senior via bash:
        gate-code <stage> <<'EOF'
        <directions>
        EOF
    """
    if len(sys.argv) < 2 or sys.argv[1] not in ALLOWED_STAGES:
        print(f"Usage: gate-code <{'|'.join(ALLOWED_STAGES)}>", file=sys.stderr)
        print("  Pipe directions via stdin.", file=sys.stderr)
        return 1

    stage = sys.argv[1]

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

    return run_code_stage(stage, request, Path(workspace_str), thread_id)

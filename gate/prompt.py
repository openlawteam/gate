"""Prompt loading and variable substitution.

Ported from:
- safeSubstitute() in shared/utils.js
- buildVars() and buildDiffOrSummary() in run-stage.js
"""

import json
import logging
import re
import subprocess
from pathlib import Path

from gate.config import gate_dir

logger = logging.getLogger(__name__)

PROMPTS_DIR = gate_dir() / "prompts"

MAX_DIFF_BYTES = 500_000
MAX_PR_BODY_BYTES = 50_000
MAX_FILE_LIST_BYTES = 50_000
TRIAGE_DIFF_BUDGET_BYTES = 150_000


def safe_substitute(template: str, vars: dict[str, str], caller: str = "") -> str:
    """Replace $var_name tokens. Leave unresolved tokens in place and log them.

    Ported from safeSubstitute() in shared/utils.js.
    Uses a regex that matches $variable_name tokens (lowercase + underscore).
    """

    def replacer(match: re.Match) -> str:
        key = match.group(1)
        if key in vars:
            return vars[key]
        logger.warning(f"[{caller}] unresolved variable: ${key}")
        return match.group(0)

    return re.sub(r"\$([a-z_][a-z0-9_]*)", replacer, template)


def load(name: str) -> str:
    """Load a prompt template by name.

    Looks for prompts/<name>.md in the gate directory.

    Args:
        name: Prompt name (with or without .md extension).

    Returns:
        The prompt template text.

    Raises:
        FileNotFoundError: If the prompt file doesn't exist.
    """
    if name.endswith(".md"):
        path = PROMPTS_DIR / name
    else:
        path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {path}")
    return path.read_text().strip()


def truncate(text: str, max_bytes: int, label: str) -> str:
    """Truncate text to max_bytes, appending a truncation notice.

    Ported from truncate() in shared/utils.js.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated + f"\n\n[{label} truncated at {max_bytes // 1024}KB]"


def _read_file(path: Path) -> str:
    """Read a file, returning empty string on any error."""
    try:
        return path.read_text()
    except (OSError, FileNotFoundError):
        return ""


def _read_json_file(path: Path) -> dict | None:
    """Read and parse a JSON file, returning None on any error."""
    raw = _read_file(path)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def build_diff_or_summary(
    workspace: Path,
    budget_bytes: int = TRIAGE_DIFF_BUDGET_BYTES,
    config: dict | None = None,
) -> str:
    """Full diff if under budget, otherwise per-file summary.

    Ported from buildDiffOrSummary() in run-stage.js.
    """
    diff = _read_file(workspace / "diff.txt")
    if len(diff.encode("utf-8")) <= budget_bytes:
        return diff

    default_branch = (config or {}).get("repo", {}).get("default_branch", "main")
    diff_ref = f"origin/{default_branch}...HEAD"

    diff_stats = _read_file(workspace / "diff_stats.txt")
    changed_files = [
        f for f in _read_file(workspace / "changed_files.txt").split("\n") if f.strip()
    ]

    parts = [
        f"[Note: Full diff exceeds {budget_bytes // 1024}KB — showing per-file summary "
        f"for triage classification. Full diff is available to downstream review stages.]\n",
        f"## Diff Stats\n{diff_stats}\n",
        "## Per-File Preview (first 10 lines of each file's diff)\n",
    ]

    for file in changed_files:
        try:
            result = subprocess.run(
                ["git", "diff", diff_ref, "--", file],
                capture_output=True,
                text=True,
                cwd=str(workspace),
                timeout=5,
            )
            file_diff = result.stdout.strip()
            lines = file_diff.split("\n")[:15]
            file_diff = "\n".join(lines)
            parts.append(f"### {file}\n```\n{file_diff}\n```\n")
        except (subprocess.SubprocessError, OSError):
            parts.append(f"### {file}\n[Could not extract file diff]\n")

    return "\n".join(parts)


def build_vars(
    workspace: Path,
    stage: str,
    env_vars: dict,
    config: dict | None = None,
) -> dict[str, str]:
    """Assemble all template variables for a stage.

    Ported from buildVars() in run-stage.js. Reads context files from the
    workspace and PR metadata from env_vars.

    Args:
        workspace: Path to the review worktree.
        stage: Stage name (triage, architecture, etc.).
        env_vars: Dict with PR metadata (pr_title, pr_body, pr_author, etc.).
        config: Optional gate.toml config dict. Limits read from [limits] section.

    Returns:
        Dict of variable name -> value for template substitution.
    """
    limits = (config or {}).get("limits", {})
    max_diff = limits.get("max_diff_bytes", MAX_DIFF_BYTES)
    max_pr_body = limits.get("max_pr_body_bytes", MAX_PR_BODY_BYTES)
    max_file_list = limits.get("max_file_list_bytes", MAX_FILE_LIST_BYTES)

    diff = _read_file(workspace / "diff.txt")
    changed_files = _read_file(workspace / "changed_files.txt")
    file_count = _read_file(workspace / "file_count.txt").strip()
    lines_changed = _read_file(workspace / "lines_changed.txt").strip()

    build_json = _read_file(workspace / "build.json")
    triage_json = _read_file(workspace / "triage.json")
    architecture_json = _read_file(workspace / "architecture.json")
    security_json = _read_file(workspace / "security.json")
    logic_json = _read_file(workspace / "logic.json")

    triage = _read_json_file(workspace / "triage.json")
    architecture = _read_json_file(workspace / "architecture.json")
    security = _read_json_file(workspace / "security.json")

    repo_cfg = (config or {}).get("repo", {})
    cursor_rules_path = repo_cfg.get("cursor_rules", "")
    if cursor_rules_path:
        cursor_rules = _read_file(Path(cursor_rules_path))
    else:
        cursor_rules = _read_file(gate_dir() / "config" / "cursor-rules.md")

    prior_review_json = _read_file(workspace / "prior-review.json")
    diff_stats = _read_file(workspace / "diff_stats.txt")

    diff_or_summary = build_diff_or_summary(workspace, config=config) if stage == "triage" else diff

    verdict = _read_json_file(workspace / "verdict.json")
    fixable_findings = []
    if verdict and isinstance(verdict.get("findings"), list):
        fixable_findings = [
            f
            for f in verdict["findings"]
            if f.get("introduced_by_pr") is not False
            and f.get("severity") in ("critical", "error", "warning")
        ]

    blocklist_path = repo_cfg.get("fix_blocklist", "")
    if blocklist_path:
        blocklist = _read_file(Path(blocklist_path))
    else:
        blocklist = _read_file(gate_dir() / "config" / "fix-blocklist.txt")

    prep_context = _read_file(workspace / "fix-prep.json")
    fix_plan = _read_file(workspace / "fix-plan.json")

    return {
        "pr_title": env_vars.get("pr_title", ""),
        "pr_body": truncate(env_vars.get("pr_body", ""), max_pr_body, "PR body"),
        "pr_author": env_vars.get("pr_author", ""),
        "pr_number": env_vars.get("pr_number", ""),
        "diff": truncate(diff, max_diff, "Diff"),
        "diff_or_summary": diff_or_summary,
        "diff_stats": diff_stats,
        "file_list": truncate(changed_files, max_file_list, "File list"),
        "changed_files": truncate(changed_files, max_file_list, "File list"),
        "file_count": file_count,
        "lines_changed": lines_changed,
        "build_results": build_json,
        "build_json": build_json,
        "compiled_cursor_rules": cursor_rules,
        "triage_json": triage_json,
        "triage_summary": (triage or {}).get("summary", "Triage: not yet run"),
        "risk_level": (triage or {}).get("risk_level", "medium"),
        "architecture_json": architecture_json,
        "architecture_summary": (architecture or {}).get(
            "summary", "Architecture review: skipped by triage"
        ),
        "security_json": security_json,
        "security_summary": (security or {}).get(
            "summary", "Security review: skipped by triage"
        ),
        "logic_json": logic_json,
        "prior_review_json": prior_review_json or '{ "has_prior": false }',
        "fix_diff": _read_file(workspace / "fix-diff.txt"),
        "tsc_errors": (
            _read_file(workspace / "fix-build-tsc-errors.txt") or "(no build errors)"
        ),
        "lint_errors": (
            _read_file(workspace / "fix-build-lint-errors.txt") or "(no lint errors)"
        ),
        "findings_json": json.dumps(fixable_findings, indent=2),
        "blocklist": blocklist or "(no blocklist configured)",
        "prep_context": prep_context or "(prep phase skipped)",
        "fix_plan": fix_plan or "(plan phase skipped — fix all findings using your judgment)",
        "previous_attempt_context": _read_file(workspace / "fix-previous-attempt.txt") or "(first attempt)",
        "bot_account": (config or {}).get("repo", {}).get("bot_account", "gate-bot"),
    }

"""Setup helpers for gate init and gate add-repo."""

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from gate import profiles
from gate.config import gate_dir

_REQUIRED_TOOLS = [
    ("git", ["git", "--version"]),
    ("tmux", ["tmux", "-V"]),
    ("claude CLI", ["claude", "--version"]),
    ("gh CLI", ["gh", "--version"]),
]

_OPTIONAL_TOOLS = [
    ("node", ["node", "--version"]),
    ("codex CLI", ["codex", "--version"]),
]

_PLACEHOLDER_REPO = "your-org/your-repo"

_MODELS_DEFAULTS = {
    "triage": "sonnet",
    "architecture": "sonnet",
    "security": "opus",
    "logic": "opus",
    "verdict": "sonnet",
    "fix_senior": "opus",
    "fix_rereview": "sonnet",
}

_TIMEOUTS_DEFAULTS = {
    "agent_stage_s": 900,
    "structured_stage_s": 120,
    "fix_session_s": 2400,
    "stuck_threshold_s": 120,
    "hard_timeout_s": 1200,
}

_RETRY_DEFAULTS = {
    "max_retries": 4,
    "base_delay_s": 60,
    "transient_base_delay_s": 10,
}

_LIMITS_DEFAULTS = {
    "max_review_cycles": 5,
    "max_fix_attempts_soft": 3,
    "max_fix_attempts_total": 6,
    "fix_cooldown_s": 600,
    "max_diff_bytes": 512000,
    "max_pr_body_bytes": 51200,
    "max_file_list_bytes": 51200,
    "triage_diff_budget_bytes": 153600,
}

_MULTI_REPO_DOCS = """\
# Multi-repo format (use instead of [repo]):
# [[repos]]
# name = "org/repo-a"
# clone_path = "~/repo-a"
# default_branch = "main"
# bot_account = "gate-bot"
# escalation_reviewers = ""
# project_type = "node"    # auto-detected: node, python, go, rust, none
# cursor_rules = ""        # optional, defaults to config/cursor-rules.md
# fix_blocklist = ""        # optional, defaults to config/fix-blocklist.txt
# worktree_base = "/tmp/gate-worktrees"
#
# # Per-repo build command overrides:
# # build.typecheck_cmd = "npx tsc --noEmit"
# # build.lint_cmd = "npm run lint:check"
# # build.test_cmd = "npm run test:run"
#
# # Per-repo limit/timeout/retry overrides:
# # limits.max_fix_attempts_total = 0
# # timeouts.agent_stage_s = 600
#
# [[repos]]
# name = "org/repo-b"
# clone_path = "~/repo-b"
"""


def check_prerequisites() -> tuple[list[tuple[str, bool, str]], bool]:
    """Check required and optional CLI tools.

    Returns (checks_list, all_required_ok).
    """
    checks: list[tuple[str, bool, str]] = []
    all_required_ok = True

    for name, cmd in _REQUIRED_TOOLS:
        _check_tool(name, cmd, checks)
        if checks[-1][1] is False:
            all_required_ok = False

    for name, cmd in _OPTIONAL_TOOLS:
        _check_tool(name, cmd, checks)

    return checks, all_required_ok


def _check_tool(
    name: str, cmd: list[str], checks: list[tuple[str, bool, str]],
) -> None:
    path = shutil.which(cmd[0])
    if not path:
        checks.append((name, False, "not found in PATH"))
        return
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            detail = result.stderr.strip().split("\n")[0] or f"exit code {result.returncode}"
            checks.append((name, False, detail))
            return
        version = result.stdout.strip().split("\n")[0]
        checks.append((name, True, version))
    except (subprocess.SubprocessError, OSError) as e:
        checks.append((name, False, str(e)))


def print_checks(checks: list[tuple[str, bool, str]]) -> None:
    """Print aligned check results in doctor-style format."""
    if not checks:
        return
    max_label = max(len(c[0]) for c in checks)
    for label, ok, detail in checks:
        dots = "." * (max_label + 4 - len(label))
        status = "OK" if ok else "FAIL"
        line = f"  {label} {dots} {status}"
        if detail:
            line += f" ({detail})"
        print(line)


def detect_gh_user() -> str | None:
    """Detect the authenticated GitHub username via gh CLI."""
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def validate_clone_path(path_str: str) -> tuple[bool, str]:
    """Validate that a path exists and is a git repository."""
    expanded = Path(path_str).expanduser()
    if not expanded.exists():
        return False, f"{expanded} does not exist"
    try:
        result = subprocess.run(
            ["git", "-C", str(expanded), "rev-parse", "--git-dir"],
            capture_output=True, timeout=5,
        )
        if result.returncode != 0:
            return False, f"{expanded} is not a git repository"
    except (subprocess.SubprocessError, OSError) as e:
        return False, f"git check failed: {e}"
    return True, str(expanded)


def prompt_repo_config(defaults: dict | None = None) -> dict:
    """Interactive prompts for a single repo configuration.

    Returns dict with keys: name, clone_path, default_branch,
    bot_account, worktree_base, escalation_reviewers.
    """
    defaults = defaults or {}

    while True:
        repo = input(f"Repository to review (owner/repo): ").strip()
        if repo.count("/") == 1 and all(repo.split("/")):
            break
        print("  Invalid format. Use owner/repo (e.g. myorg/myapp)")

    repo_name = repo.split("/")[1]
    default_clone = defaults.get("clone_path", f"~/{repo_name}")
    while True:
        clone = input(f"Local clone path [{default_clone}]: ").strip() or default_clone
        ok, detail = validate_clone_path(clone)
        if ok:
            break
        print(f"  {detail}")

    default_branch = defaults.get("default_branch", "main")
    branch = input(f"Default branch [{default_branch}]: ").strip() or default_branch

    detected_user = detect_gh_user()
    default_bot = defaults.get("bot_account") or detected_user or "gate-bot"
    bot = input(f"Bot account for fix commits [{default_bot}]: ").strip() or default_bot

    default_wt = defaults.get("worktree_base", "/tmp/gate-worktrees")
    wt_base = input(f"Worktree base directory [{default_wt}]: ").strip() or default_wt

    # Auto-detect project type
    detected_type = profiles.detect_project_type(Path(clone))
    if detected_type != "none":
        print(f"  Detected project type: {detected_type}")
        type_answer = input(f"  Use this project type? [Y/n/other]: ").strip().lower()
        if type_answer in ("", "y", "yes"):
            project_type = detected_type
        elif type_answer in ("n", "no"):
            project_type = input("  Enter project type (node/python/go/rust/none): ").strip() or "none"
        else:
            project_type = type_answer
    else:
        project_type = input("  Project type (node/python/go/rust/none) [none]: ").strip() or "none"

    return {
        "name": repo,
        "clone_path": clone,
        "default_branch": branch,
        "bot_account": bot,
        "worktree_base": wt_base,
        "escalation_reviewers": defaults.get("escalation_reviewers", ""),
        "project_type": project_type,
    }


_REPO_KEY_ORDER = [
    "name", "clone_path", "worktree_base", "bot_account",
    "escalation_reviewers", "default_branch", "project_type",
    "cursor_rules", "fix_blocklist",
]


def _emit_value(lines: list[str], key: str, val) -> None:
    """Emit a single key-value pair in TOML format, handling nested dicts and lists."""
    if isinstance(val, dict):
        for subkey, subval in val.items():
            _emit_value(lines, f"{key}.{subkey}", subval)
    elif isinstance(val, list):
        items = ", ".join(f'"{v}"' if isinstance(v, str) else str(v) for v in val)
        lines.append(f"{key} = [{items}]")
    elif isinstance(val, str):
        lines.append(f'{key} = "{val}"')
    elif isinstance(val, bool):
        lines.append(f'{key} = {"true" if val else "false"}')
    else:
        lines.append(f"{key} = {val}")


def format_repo_toml(repo_config: dict, header: str = "[[repos]]") -> str:
    """Format a repo config dict as a TOML section.

    Known keys are emitted in canonical order, followed by any remaining
    keys (build overrides, per-repo limits, etc.) in sorted order.
    """
    lines = [header]
    seen: set[str] = set()
    for key in _REPO_KEY_ORDER:
        if key in repo_config:
            _emit_value(lines, key, repo_config[key])
            seen.add(key)
    for key in sorted(repo_config.keys()):
        if key not in seen:
            _emit_value(lines, key, repo_config[key])
    return "\n".join(lines)


def _format_section(name: str, data: dict, defaults: dict) -> str:
    """Format a TOML section with values from data, falling back to defaults."""
    merged = dict(defaults)
    merged.update(data)
    lines = [f"[{name}]"]
    for key in defaults:
        val = merged.get(key, defaults[key])
        if isinstance(val, str):
            lines.append(f'{key} = "{val}"')
        else:
            lines.append(f"{key} = {val}")
    return "\n".join(lines)


def format_full_config(
    repos: list[dict], globals_config: dict | None = None,
) -> str:
    """Generate a complete gate.toml from repo list and global config."""
    globals_config = globals_config or {}
    parts: list[str] = []

    if len(repos) == 1:
        parts.append("# Single-repo format (backward compatible):")
        parts.append(format_repo_toml(repos[0], header="[repo]"))
        parts.append("")
        parts.append(_MULTI_REPO_DOCS.rstrip())
    else:
        for repo in repos:
            parts.append(format_repo_toml(repo, header="[[repos]]"))
            parts.append("")

    parts.append("")
    parts.append(_format_section(
        "models", globals_config.get("models", {}), _MODELS_DEFAULTS))
    parts.append("")
    parts.append(_format_section(
        "timeouts", globals_config.get("timeouts", {}), _TIMEOUTS_DEFAULTS))
    parts.append("")
    parts.append(_format_section(
        "retry", globals_config.get("retry", {}), _RETRY_DEFAULTS))
    parts.append("")
    parts.append(_format_section(
        "limits", globals_config.get("limits", {}), _LIMITS_DEFAULTS))
    parts.append("")

    return "\n".join(parts)


def validate_env_vars() -> list[tuple[str, bool, str]]:
    """Check environment variables needed by Gate."""
    checks: list[tuple[str, bool, str]] = []

    pat = os.environ.get("GATE_PAT", "")
    if pat:
        checks.append(("GATE_PAT", True, f"set ({len(pat)} chars)"))
    else:
        checks.append(("GATE_PAT", False, "not set — see .env.example"))

    claude_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if claude_token:
        checks.append(("CLAUDE_CODE_OAUTH_TOKEN", True, f"set ({len(claude_token)} chars)"))
    elif sys.platform == "darwin":
        try:
            from gate.quota import read_keychain_token
            token = read_keychain_token()
            if token:
                checks.append(("CLAUDE_CODE_OAUTH_TOKEN", True, f"Keychain ({len(token)} chars)"))
            else:
                checks.append(("CLAUDE_CODE_OAUTH_TOKEN", False, "not in env or Keychain"))
        except Exception:
            checks.append(("CLAUDE_CODE_OAUTH_TOKEN", False, "not in env or Keychain"))
    else:
        checks.append(("CLAUDE_CODE_OAUTH_TOKEN", False, "not set"))

    openai = os.environ.get("OPENAI_API_KEY", "")
    if openai:
        checks.append(("OPENAI_API_KEY", True, f"set ({len(openai)} chars)"))
    else:
        checks.append(("OPENAI_API_KEY", True, "not set (optional — needed for Codex fix pipeline)"))

    return checks


def is_placeholder_config(config_path: Path) -> bool:
    """Check if every configured repo still has the placeholder name."""
    if not config_path.exists():
        return False
    try:
        data = tomllib.loads(config_path.read_text())
    except Exception:
        return False
    if "repos" in data:
        return len(data["repos"]) > 0 and all(
            r.get("name") == _PLACEHOLDER_REPO for r in data["repos"]
        )
    if "repo" in data:
        return data["repo"].get("name") == _PLACEHOLDER_REPO
    return False


def copy_workflow(clone_path: Path, interactive: bool = True) -> bool:
    """Copy the Gate workflow file to a repository's .github/workflows/."""
    source = gate_dir() / "workflows" / "gate-review.yml"
    if not source.exists():
        print(f"  Warning: workflow template not found at {source}")
        return False

    target = clone_path / ".github" / "workflows" / "gate-review.yml"

    if target.exists():
        if not interactive:
            return False
        answer = input(f"Overwrite existing {target}? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            return False

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text())
    print(f"  Workflow copied to {target}")
    print("  Remember to commit and push this file in your repository.")
    return True

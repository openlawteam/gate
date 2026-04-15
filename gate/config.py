"""Configuration for Gate."""

import os
import tomllib
from pathlib import Path

GATE_DIR = Path(__file__).resolve().parent.parent

MODEL_ALIASES = {
    "sonnet": "sonnet",
    "opus": "opus",
    "haiku": "haiku",
}

CLAUDE_ENV_KEYS = {
    "HOME": lambda: os.environ.get("HOME", str(Path.home())),
    "PATH": lambda: os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"),
    "CLAUDE_CODE_OAUTH_TOKEN": lambda: os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
    "CLAUDE_CODE_ENTRYPOINT": lambda: "cli",
    "TERM": lambda: os.environ.get("TERM", "xterm-256color"),
    "NODE_ENV": lambda: "",
    "ENABLE_CLAUDEAI_MCP_SERVERS": lambda: "false",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": lambda: "1",
    "DISABLE_AUTOUPDATER": lambda: "1",
    "MCP_CONNECTION_NONBLOCKING": lambda: "true",
    "CLAUDE_ENABLE_STREAM_WATCHDOG": lambda: "1",
    "CLAUDE_STREAM_IDLE_TIMEOUT_MS": lambda: "90000",
    "GATE_CODEX_THREAD_ID": lambda: os.environ.get("GATE_CODEX_THREAD_ID", ""),
    "GATE_FIX_WORKSPACE": lambda: os.environ.get("GATE_FIX_WORKSPACE", ""),
    "GATE_PAT": lambda: os.environ.get("GATE_PAT", ""),
    "GH_TOKEN": lambda: os.environ.get("GATE_PAT", ""),
}


def gate_dir() -> Path:
    """Return the gate root directory."""
    return GATE_DIR


def load_config() -> dict:
    """Load configuration from config/gate.toml.

    Returns merged config dict. Falls back to empty dict if file missing.
    """
    config_file = gate_dir() / "config" / "gate.toml"
    if not config_file.exists():
        return {}
    try:
        return tomllib.loads(config_file.read_text())
    except tomllib.TOMLDecodeError as e:
        import logging
        logging.getLogger(__name__).warning(f"Invalid gate.toml: {e}, using defaults")
        return {}


def repo_slug(repo_name: str) -> str:
    """Convert 'owner/repo' to filesystem-safe 'owner-repo'."""
    return repo_name.replace("/", "-")


def get_all_repos(config: dict | None = None) -> list[dict]:
    """Return list of repo config dicts. Handles both [repo] and [[repos]] formats."""
    if config is None:
        config = load_config()
    if "repos" in config:
        return config["repos"]
    if "repo" in config:
        return [config["repo"]]
    return []


def get_repo_config(repo_name: str, config: dict | None = None) -> dict:
    """Look up repo config by GitHub name. Raises ValueError if not found."""
    for repo in get_all_repos(config):
        if repo.get("name") == repo_name:
            return repo
    raise ValueError(f"No config found for repo: {repo_name}")


def resolve_repo_config(repo_name: str, config: dict | None = None) -> dict:
    """Return a copy of config with config['repo'] set to the matching repo entry.

    Per-repo overrides for limits, timeouts, and retry are merged into the
    global sections so downstream code sees them transparently.
    """
    if config is None:
        config = load_config()
    config = dict(config)
    repo = get_repo_config(repo_name, config)
    config["repo"] = repo
    for section in ("limits", "timeouts", "retry"):
        if section in repo:
            merged = dict(config.get(section, {}))
            merged.update(repo[section])
            config[section] = merged
    return config


def build_claude_env() -> dict[str, str]:
    """Build the sandboxed environment dict for Claude subprocesses.

    Claude must not inherit the full environment — only explicitly
    allowed keys are passed through.
    """
    return {k: v() for k, v in CLAUDE_ENV_KEYS.items()}

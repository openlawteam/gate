"""Configuration for Gate.

``GATE_DIR`` is the package/install root (prompts, workflows, default config
snippets). ``GATE_DATA_DIR`` is the OS-native runtime data root (state, logs,
socket, caches) resolved via ``platformdirs``.

Tests override ``GATE_DIR`` and ``GATE_DATA_DIR`` at runtime through the
isolation fixture, which is why both are mutable module-level globals and why
``gate_dir()`` / ``data_dir()`` are functions rather than constants.
"""

import os
import tomllib
from pathlib import Path

from platformdirs import user_data_dir

GATE_DIR = Path(__file__).resolve().parent.parent
GATE_DATA_DIR = Path(user_data_dir("gate"))

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
    """Return the gate install/package root (prompts, workflows, defaults)."""
    return GATE_DIR


def data_dir() -> Path:
    """Return the OS-native runtime data directory (state, logs, socket)."""
    return GATE_DATA_DIR


def state_dir() -> Path:
    """Return the per-PR state directory root."""
    return data_dir() / "state"


def logs_dir() -> Path:
    """Return the runtime logs directory."""
    return data_dir() / "logs"


def socket_path() -> Path:
    """Return the default Unix socket path for the Gate server."""
    return data_dir() / "server.sock"


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


def get_repo_bool(config: dict, key: str, default: bool) -> bool:
    """Fetch a boolean override from ``config["repo"]`` with a safe default.

    Central helper so the polish-loop and approve-with-notes flags have one
    well-typed read path. TOML already returns proper booleans, so this is
    mostly a ``.get(...)`` wrapper with defensive coercion.
    """
    repo = (config or {}).get("repo", {}) or {}
    raw = repo.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1", "yes", "on")
    return bool(raw)


def get_polish_timeouts(config: dict) -> dict[str, int]:
    """Return per-fixability polish-loop timeouts (seconds).

    Defaults: trivial=180s, scoped=600s, broad=0 (skip). The "0" sentinel
    tells the polish loop to add the finding to ``not_fixed`` immediately
    with reason ``"skipped_broad_in_polish"`` instead of dispatching the
    junior against it — broad findings almost always require architectural
    changes that do not fit inside a bounded polish budget.
    """
    defaults = {"trivial": 180, "scoped": 600, "broad": 0}
    override = ((config or {}).get("repo", {}) or {}).get(
        "polish_per_finding_timeout_seconds", {}
    )
    if not isinstance(override, dict):
        return defaults
    merged = dict(defaults)
    for key, value in override.items():
        try:
            merged[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return merged


def get_polish_total_budget_s(config: dict) -> int:
    """Return the total wall-clock budget for the polish loop in seconds."""
    repo = (config or {}).get("repo", {}) or {}
    raw = repo.get("polish_loop_total_budget_s", 1800)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 1800


_VALID_FIX_PIPELINE_MODES = ("hopper", "polish_legacy")


def get_fix_pipeline_mode(config: dict) -> str:
    """Return the active fix-pipeline mode (``"hopper"`` | ``"polish_legacy"``).

    Resolution order (first non-empty wins):
      1. ``[fix_pipeline].mode`` (new section — preferred going forward)
      2. ``[repo].fix_pipeline_mode``
      3. Default ``"polish_legacy"`` (pre-rollout default; flip per repo
         during cutover — see the hardening plan's rollout section).
    """
    if not isinstance(config, dict):
        return "polish_legacy"
    fp = config.get("fix_pipeline")
    if isinstance(fp, dict):
        mode = str(fp.get("mode", "")).strip().lower()
        if mode in _VALID_FIX_PIPELINE_MODES:
            return mode
    repo = config.get("repo", {}) or {}
    mode = str(repo.get("fix_pipeline_mode", "")).strip().lower()
    if mode in _VALID_FIX_PIPELINE_MODES:
        return mode
    return "polish_legacy"


def get_fix_pipeline_max_wall_clock_s(config: dict) -> int:
    """Runaway guard for hopper-mode fix runs (default 4h)."""
    default = 14400
    if not isinstance(config, dict):
        return default
    fp = config.get("fix_pipeline")
    if isinstance(fp, dict) and fp.get("max_wall_clock_s") is not None:
        try:
            return int(fp["max_wall_clock_s"])
        except (TypeError, ValueError):
            pass
    return default


def get_fix_pipeline_senior_session_timeout_s(config: dict) -> int:
    """Per-senior-session timeout (default 2h). Separate from the overall cap."""
    default = 7200
    if not isinstance(config, dict):
        return default
    fp = config.get("fix_pipeline")
    if isinstance(fp, dict) and fp.get("senior_session_timeout_s") is not None:
        try:
            return int(fp["senior_session_timeout_s"])
        except (TypeError, ValueError):
            pass
    return default


def get_fix_pipeline_max_subscope_iterations(config: dict) -> int:
    """Max implement/audit iterations per sub-scope before defer (default 3)."""
    default = 3
    if not isinstance(config, dict):
        return default
    fp = config.get("fix_pipeline")
    if isinstance(fp, dict) and fp.get("max_subscope_iterations") is not None:
        try:
            return int(fp["max_subscope_iterations"])
        except (TypeError, ValueError):
            pass
    return default


def build_claude_env() -> dict[str, str]:
    """Build the sandboxed environment dict for Claude subprocesses.

    Claude must not inherit the full environment — only explicitly
    allowed keys are passed through.
    """
    return {k: v() for k, v in CLAUDE_ENV_KEYS.items()}

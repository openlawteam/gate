"""Project type profiles for language-agnostic build/lint/test commands."""

from collections.abc import Iterable
from pathlib import Path
from typing import Any

PROFILES: dict[str, dict[str, str]] = {
    "node": {
        "language": "TypeScript/JavaScript",
        "typecheck_cmd": "npx tsc --noEmit",
        "typecheck_cmds": [],
        "lint_cmd": "npm run lint:check",
        "lint_cmds": [],
        "scoped_lint_cmd": "",
        "scoped_lint_cwd": "",
        "test_cmd": "npm run test:run",
        "test_cmds": [],
        "test_file_pattern": "*.test.ts",
        "dep_install_cmd": "npm ci",
        "dep_file": "package.json",
        "config_files": "tsconfig.json, .eslintrc, package.json",
        "env_access_pattern": "process.env",
        "import_style": "import { x } from './module'",
        "source_root": ".",
        "test_dir": ".",
        "verify_cmd": "",
    },
    "python": {
        "language": "Python",
        "typecheck_cmd": "",
        "typecheck_cmds": [],
        "lint_cmd": "ruff check .",
        "lint_cmds": [],
        "scoped_lint_cmd": "",
        "scoped_lint_cwd": "",
        "test_cmd": "python -m pytest",
        "test_cmds": [],
        "test_file_pattern": "test_*.py",
        "dep_install_cmd": "",
        "dep_file": "pyproject.toml",
        "config_files": "pyproject.toml, ruff.toml",
        "env_access_pattern": "os.environ",
        "import_style": "from module import func",
        "source_root": ".",
        "test_dir": ".",
        "verify_cmd": "",
    },
    "go": {
        "language": "Go",
        "typecheck_cmd": "go vet ./...",
        "typecheck_cmds": [],
        "lint_cmd": "golangci-lint run",
        "lint_cmds": [],
        "scoped_lint_cmd": "",
        "scoped_lint_cwd": "",
        "test_cmd": "go test ./...",
        "test_cmds": [],
        "test_file_pattern": "*_test.go",
        "dep_install_cmd": "go mod download",
        "dep_file": "go.mod",
        "config_files": "go.mod, go.sum",
        "env_access_pattern": "os.Getenv()",
        "import_style": 'import "package/module"',
        "source_root": ".",
        "test_dir": ".",
        "verify_cmd": "",
    },
    "rust": {
        "language": "Rust",
        "typecheck_cmd": "cargo check",
        "typecheck_cmds": [],
        "lint_cmd": "cargo clippy",
        "lint_cmds": [],
        "scoped_lint_cmd": "",
        "scoped_lint_cwd": "",
        "test_cmd": "cargo test",
        "test_cmds": [],
        "test_file_pattern": "*_test.rs or #[test]",
        "dep_install_cmd": "",
        "dep_file": "Cargo.toml",
        "config_files": "Cargo.toml, .cargo/config.toml",
        "env_access_pattern": "std::env::var()",
        "import_style": "use crate::module;",
        "source_root": ".",
        "test_dir": ".",
        "verify_cmd": "",
    },
    "none": {
        "language": "Unknown",
        "typecheck_cmd": "",
        "typecheck_cmds": [],
        "lint_cmd": "",
        "lint_cmds": [],
        "scoped_lint_cmd": "",
        "scoped_lint_cwd": "",
        "test_cmd": "",
        "test_cmds": [],
        "test_file_pattern": "",
        "dep_install_cmd": "",
        "dep_file": "",
        "config_files": "",
        "env_access_pattern": "",
        "import_style": "",
        "source_root": ".",
        "test_dir": ".",
        "verify_cmd": "",
    },
}


def detect_project_type(repo_path: Path) -> str:
    """Detect project type from repo files. Returns profile key."""
    if (repo_path / "package.json").exists():
        return "node"
    if (repo_path / "pyproject.toml").exists() or (repo_path / "setup.py").exists():
        return "python"
    if (repo_path / "go.mod").exists():
        return "go"
    if (repo_path / "Cargo.toml").exists():
        return "rust"
    return "none"


def resolve_profile(repo_config: dict, repo_path: Path | None = None) -> dict:
    """Resolve the effective build profile for a repo.

    Priority: repo_config["build"] overrides > profile defaults > empty.
    """
    ptype = repo_config.get("project_type", "")
    if not ptype and repo_path:
        ptype = detect_project_type(Path(repo_path))
    profile = dict(PROFILES.get(ptype, PROFILES["none"]))
    build_overrides = repo_config.get("build", {})
    profile.update(build_overrides)
    profile["project_type"] = ptype
    return profile


def command_list(profile: dict[str, Any], key: str) -> list[str]:
    """Return one or more commands for a profile field.

    ``key`` is the singular command key, e.g. ``"typecheck_cmd"``. Repos may
    alternatively configure ``typecheck_cmds = ["cmd a", "cmd b"]`` for
    monorepos or multi-runtime projects. Empty strings are ignored.
    """
    plural_key = f"{key}s"
    plural = profile.get(plural_key)
    if isinstance(plural, str):
        if plural.strip():
            return [plural]
    if isinstance(plural, Iterable):
        cmds = [str(cmd).strip() for cmd in plural if str(cmd).strip()]
        if cmds:
            return cmds

    singular = profile.get(key, "")
    if isinstance(singular, str):
        return [singular] if singular.strip() else []
    return []


def command_display(profile: dict[str, Any], key: str) -> str:
    """Render configured commands for prompt variables and docs."""
    return " && ".join(command_list(profile, key))

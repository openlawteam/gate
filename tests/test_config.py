"""Tests for gate.config module."""

import os
from pathlib import Path
from unittest.mock import patch

from gate.config import (
    build_claude_env,
    gate_dir,
    get_all_repos,
    get_repo_config,
    load_config,
    repo_slug,
    resolve_repo_config,
)


class TestGateDir:
    def test_returns_path(self):
        result = gate_dir()
        assert isinstance(result, Path)

    def test_is_parent_of_gate_package(self, real_gate_dir):
        assert (real_gate_dir / "gate" / "__init__.py").exists()


class TestLoadConfig:
    def test_loads_toml(self):
        config = load_config()
        assert isinstance(config, dict)
        assert "repos" in config
        assert len(config["repos"]) > 0
        assert config["repos"][0]["name"]

    def test_models_section(self):
        config = load_config()
        assert config["models"]["triage"] == "sonnet"
        assert config["models"]["security"] == "opus"

    def test_timeouts_section(self):
        config = load_config()
        assert config["timeouts"]["agent_stage_s"] == 900
        assert config["timeouts"]["structured_stage_s"] == 120

    def test_retry_section(self):
        config = load_config()
        assert config["retry"]["max_retries"] == 4
        assert config["retry"]["base_delay_s"] == 60

    def test_limits_section(self):
        config = load_config()
        assert config["limits"]["max_review_cycles"] == 5
        assert config["limits"]["max_diff_bytes"] == 512000

    def test_missing_config_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("gate.config.GATE_DIR", tmp_path)
        config = load_config()
        assert config == {}


class TestRepoSlug:
    def test_replaces_slash(self):
        assert repo_slug("org/repo") == "org-repo"

    def test_no_slash(self):
        assert repo_slug("just-a-name") == "just-a-name"

    def test_multiple_slashes(self):
        assert repo_slug("a/b/c") == "a-b-c"


class TestGetAllRepos:
    def test_single_repo_format(self, sample_config):
        repos = get_all_repos(sample_config)
        assert len(repos) == 1
        assert repos[0]["name"] == "test-org/test-repo"

    def test_multi_repo_format(self, multi_repo_config):
        repos = get_all_repos(multi_repo_config)
        assert len(repos) == 2
        assert repos[0]["name"] == "org-a/repo-a"
        assert repos[1]["name"] == "org-b/repo-b"

    def test_empty_config(self):
        assert get_all_repos({}) == []

    def test_repos_takes_precedence(self):
        config = {
            "repo": {"name": "single"},
            "repos": [{"name": "multi-a"}, {"name": "multi-b"}],
        }
        repos = get_all_repos(config)
        assert len(repos) == 2


class TestGetRepoConfig:
    def test_found(self, sample_config):
        cfg = get_repo_config("test-org/test-repo", sample_config)
        assert cfg["clone_path"] == "~/test-repo"

    def test_not_found(self, sample_config):
        import pytest
        with pytest.raises(ValueError, match="No config found"):
            get_repo_config("nonexistent/repo", sample_config)

    def test_multi_repo(self, multi_repo_config):
        cfg = get_repo_config("org-b/repo-b", multi_repo_config)
        assert cfg["clone_path"] == "~/repo-b"


class TestResolveRepoConfig:
    def test_merges_repo(self, sample_config):
        resolved = resolve_repo_config("test-org/test-repo", sample_config)
        assert resolved["repo"]["name"] == "test-org/test-repo"
        assert "models" in resolved

    def test_multi_repo(self, multi_repo_config):
        resolved = resolve_repo_config("org-a/repo-a", multi_repo_config)
        assert resolved["repo"]["name"] == "org-a/repo-a"
        assert resolved["repo"]["bot_account"] == "bot-a"

    def test_not_found_raises(self, sample_config):
        import pytest
        with pytest.raises(ValueError):
            resolve_repo_config("nonexistent/repo", sample_config)

    def test_per_repo_limits_override(self):
        config = {
            "repos": [
                {
                    "name": "org/repo",
                    "clone_path": "~/repo",
                    "limits": {"max_fix_attempts_total": 0},
                },
            ],
            "limits": {"max_fix_attempts_total": 6, "max_review_cycles": 5},
        }
        resolved = resolve_repo_config("org/repo", config)
        assert resolved["limits"]["max_fix_attempts_total"] == 0
        assert resolved["limits"]["max_review_cycles"] == 5

    def test_per_repo_timeouts_override(self):
        config = {
            "repos": [
                {
                    "name": "org/repo",
                    "clone_path": "~/repo",
                    "timeouts": {"agent_stage_s": 600},
                },
            ],
            "timeouts": {"agent_stage_s": 900, "hard_timeout_s": 1200},
        }
        resolved = resolve_repo_config("org/repo", config)
        assert resolved["timeouts"]["agent_stage_s"] == 600
        assert resolved["timeouts"]["hard_timeout_s"] == 1200

    def test_per_repo_retry_override(self):
        config = {
            "repos": [
                {
                    "name": "org/repo",
                    "clone_path": "~/repo",
                    "retry": {"max_retries": 2},
                },
            ],
            "retry": {"max_retries": 4, "base_delay_s": 60},
        }
        resolved = resolve_repo_config("org/repo", config)
        assert resolved["retry"]["max_retries"] == 2
        assert resolved["retry"]["base_delay_s"] == 60

    def test_no_per_repo_overrides_leaves_globals_intact(self):
        config = {
            "repos": [{"name": "org/repo", "clone_path": "~/repo"}],
            "limits": {"max_review_cycles": 5},
        }
        resolved = resolve_repo_config("org/repo", config)
        assert resolved["limits"]["max_review_cycles"] == 5


class TestBuildClaudeEnv:
    def test_returns_dict(self):
        env = build_claude_env()
        assert isinstance(env, dict)

    def test_has_required_keys(self):
        env = build_claude_env()
        required = [
            "HOME",
            "PATH",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_ENTRYPOINT",
            "TERM",
        ]
        for key in required:
            assert key in env, f"Missing key: {key}"

    def test_entrypoint_is_cli(self):
        env = build_claude_env()
        assert env["CLAUDE_CODE_ENTRYPOINT"] == "cli"

    def test_mcp_disabled(self):
        env = build_claude_env()
        assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"

    def test_auto_memory_disabled(self):
        env = build_claude_env()
        assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"

    def test_does_not_leak_full_env(self):
        env = build_claude_env()
        assert "SHELL" not in env
        assert "USER" not in env

    def test_passes_through_oauth_token(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "test-token"}):
            env = build_claude_env()
            assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "test-token"

    def test_does_not_include_openai_key(self):
        env = build_claude_env()
        assert "OPENAI_API_KEY" not in env

"""Tests for gate.setup module."""

import json
import subprocess
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gate import setup


class TestCheckPrerequisites:
    @patch("gate.setup.subprocess.run")
    @patch("gate.setup.shutil.which")
    def test_all_tools_present(self, mock_which, mock_run):
        mock_which.return_value = "/usr/bin/tool"
        mock_run.return_value = MagicMock(stdout="tool v1.0\n", returncode=0)

        checks, all_ok = setup.check_prerequisites()
        assert all_ok is True
        assert all(c[1] for c in checks if c[0] in ("git", "tmux", "claude CLI", "gh CLI"))

    @patch("gate.setup.subprocess.run")
    @patch("gate.setup.shutil.which")
    def test_required_tool_missing(self, mock_which, mock_run):
        def which_side_effect(cmd):
            return None if cmd == "git" else "/usr/bin/tool"
        mock_which.side_effect = which_side_effect
        mock_run.return_value = MagicMock(stdout="v1.0\n", returncode=0)

        checks, all_ok = setup.check_prerequisites()
        assert all_ok is False
        git_check = [c for c in checks if c[0] == "git"][0]
        assert git_check[1] is False

    @patch("gate.setup.subprocess.run")
    @patch("gate.setup.shutil.which")
    def test_optional_tool_missing(self, mock_which, mock_run):
        def which_side_effect(cmd):
            return None if cmd == "node" else "/usr/bin/tool"
        mock_which.side_effect = which_side_effect
        mock_run.return_value = MagicMock(stdout="v1.0\n", returncode=0)

        checks, all_ok = setup.check_prerequisites()
        assert all_ok is True
        node_check = [c for c in checks if c[0] == "node"][0]
        assert node_check[1] is False

    @patch("gate.setup.subprocess.run")
    @patch("gate.setup.shutil.which")
    def test_tool_raises_subprocess_error(self, mock_which, mock_run):
        mock_which.return_value = "/usr/bin/tool"
        mock_run.side_effect = subprocess.SubprocessError("timeout")

        checks, all_ok = setup.check_prerequisites()
        assert all_ok is False
        assert all(c[1] is False for c in checks if c[0] in ("git", "tmux", "claude CLI", "gh CLI"))


class TestDetectGhUser:
    @patch("gate.setup.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="myuser\n")
        assert setup.detect_gh_user() == "myuser"

    @patch("gate.setup.subprocess.run")
    def test_not_installed(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        assert setup.detect_gh_user() is None

    @patch("gate.setup.subprocess.run")
    def test_api_fails(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert setup.detect_gh_user() is None

    @patch("gate.setup.subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=5)
        assert setup.detect_gh_user() is None


class TestValidateClonePath:
    @patch("gate.setup.subprocess.run")
    def test_valid_git_repo(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        ok, detail = setup.validate_clone_path(str(tmp_path))
        assert ok is True
        assert detail == str(tmp_path)

    def test_path_does_not_exist(self):
        ok, detail = setup.validate_clone_path("/nonexistent/path")
        assert ok is False
        assert "does not exist" in detail

    @patch("gate.setup.subprocess.run")
    def test_not_a_git_repo(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=128)
        ok, detail = setup.validate_clone_path(str(tmp_path))
        assert ok is False
        assert "not a git repository" in detail


class TestPromptRepoConfig:
    @patch("gate.setup.profiles.detect_project_type", return_value="node")
    @patch("gate.setup.detect_gh_user", return_value="testuser")
    @patch("gate.setup.validate_clone_path", return_value=(True, "/home/user/myrepo"))
    @patch("builtins.input", side_effect=[
        "myorg/myrepo",     # repo
        "/home/user/myrepo",  # clone_path
        "",                   # branch (default)
        "",                   # bot (default)
        "",                   # worktree_base (default)
        "",                   # project type confirm (Y)
    ])
    def test_valid_inputs(self, _inp, _val, _gh, _detect):
        result = setup.prompt_repo_config()
        assert result["name"] == "myorg/myrepo"
        assert result["clone_path"] == "/home/user/myrepo"
        assert result["default_branch"] == "main"
        assert result["bot_account"] == "testuser"
        assert result["worktree_base"] == "/tmp/gate-worktrees"
        assert result["escalation_reviewers"] == ""
        assert result["project_type"] == "node"

    @patch("gate.setup.profiles.detect_project_type", return_value="none")
    @patch("gate.setup.detect_gh_user", return_value=None)
    @patch("gate.setup.validate_clone_path", return_value=(True, "/tmp/repo"))
    @patch("builtins.input", side_effect=[
        "noslash",        # invalid
        "org/repo",       # valid
        "/tmp/repo",      # clone_path
        "develop",        # branch
        "",               # bot (defaults to gate-bot since gh user is None)
        "/tmp/wt",        # worktree_base
        "python",         # project type (manual since detect returned "none")
    ])
    def test_invalid_repo_then_valid(self, _inp, _val, _gh, _detect):
        result = setup.prompt_repo_config()
        assert result["name"] == "org/repo"
        assert result["default_branch"] == "develop"
        assert result["bot_account"] == "gate-bot"
        assert result["worktree_base"] == "/tmp/wt"
        assert result["project_type"] == "python"


class TestFormatRepoToml:
    def test_single_repo_header(self):
        cfg = {
            "name": "org/repo", "clone_path": "~/repo",
            "worktree_base": "/tmp/wt", "bot_account": "bot",
            "escalation_reviewers": "", "default_branch": "main",
        }
        result = setup.format_repo_toml(cfg, header="[repo]")
        assert result.startswith("[repo]")
        parsed = tomllib.loads(result)
        assert parsed["repo"]["name"] == "org/repo"

    def test_multi_repo_header(self):
        cfg = {
            "name": "org/repo", "clone_path": "~/repo",
            "worktree_base": "/tmp/wt", "bot_account": "bot",
            "escalation_reviewers": "", "default_branch": "main",
        }
        result = setup.format_repo_toml(cfg, header="[[repos]]")
        assert result.startswith("[[repos]]")
        parsed = tomllib.loads(result)
        assert parsed["repos"][0]["name"] == "org/repo"

    def test_values_with_special_chars(self):
        cfg = {
            "name": "org/repo", "clone_path": "/path with spaces/repo",
            "worktree_base": "/tmp/wt", "bot_account": "bot",
            "escalation_reviewers": "", "default_branch": "main",
        }
        result = setup.format_repo_toml(cfg, header="[repo]")
        parsed = tomllib.loads(result)
        assert parsed["repo"]["clone_path"] == "/path with spaces/repo"


class TestFormatRepoTomlExtended:
    def test_project_type_key(self):
        cfg = {
            "name": "org/repo", "clone_path": "~/repo",
            "worktree_base": "/tmp/wt", "bot_account": "bot",
            "escalation_reviewers": "", "default_branch": "main",
            "project_type": "python",
        }
        result = setup.format_repo_toml(cfg, header="[repo]")
        parsed = tomllib.loads(result)
        assert parsed["repo"]["project_type"] == "python"

    def test_nested_build_overrides(self):
        cfg = {
            "name": "org/repo", "clone_path": "~/repo",
            "project_type": "python",
            "build": {
                "lint_cmd": "ruff check gate/",
                "test_cmd": "pytest tests/",
            },
        }
        result = setup.format_repo_toml(cfg, header="[repo]")
        parsed = tomllib.loads(result)
        assert parsed["repo"]["build"]["lint_cmd"] == "ruff check gate/"
        assert parsed["repo"]["build"]["test_cmd"] == "pytest tests/"

    def test_list_values(self):
        cfg = {
            "name": "org/repo", "clone_path": "~/repo",
            "escalation_reviewers": ["user1", "user2"],
        }
        result = setup.format_repo_toml(cfg, header="[repo]")
        parsed = tomllib.loads(result)
        assert parsed["repo"]["escalation_reviewers"] == ["user1", "user2"]

    def test_cursor_rules_key(self):
        cfg = {
            "name": "org/repo", "clone_path": "~/repo",
            "cursor_rules": "/path/to/rules.md",
            "fix_blocklist": "/path/to/blocklist.txt",
        }
        result = setup.format_repo_toml(cfg, header="[repo]")
        parsed = tomllib.loads(result)
        assert parsed["repo"]["cursor_rules"] == "/path/to/rules.md"
        assert parsed["repo"]["fix_blocklist"] == "/path/to/blocklist.txt"

    def test_nested_limits_overrides(self):
        cfg = {
            "name": "org/repo", "clone_path": "~/repo",
            "limits": {"max_fix_attempts_total": 0},
        }
        result = setup.format_repo_toml(cfg, header="[repo]")
        parsed = tomllib.loads(result)
        assert parsed["repo"]["limits"]["max_fix_attempts_total"] == 0

    def test_round_trip_all_keys(self):
        cfg = {
            "name": "org/repo", "clone_path": "~/repo",
            "worktree_base": "/tmp/wt", "bot_account": "bot",
            "escalation_reviewers": "", "default_branch": "main",
            "project_type": "node",
            "cursor_rules": "/path/to/rules.md",
            "fix_blocklist": "/path/to/blocklist.txt",
            "build": {"lint_cmd": "custom-lint"},
        }
        result = setup.format_repo_toml(cfg, header="[repo]")
        parsed = tomllib.loads(result)
        repo = parsed["repo"]
        assert repo["name"] == "org/repo"
        assert repo["project_type"] == "node"
        assert repo["cursor_rules"] == "/path/to/rules.md"
        assert repo["build"]["lint_cmd"] == "custom-lint"

    def test_boolean_values(self):
        cfg = {
            "name": "org/repo", "clone_path": "~/repo",
            "auto_fix": True,
        }
        result = setup.format_repo_toml(cfg, header="[repo]")
        parsed = tomllib.loads(result)
        assert parsed["repo"]["auto_fix"] is True


class TestFormatFullConfig:
    def test_single_repo(self):
        repos = [{
            "name": "org/repo", "clone_path": "~/repo",
            "worktree_base": "/tmp/wt", "bot_account": "bot",
            "escalation_reviewers": "", "default_branch": "main",
        }]
        content = setup.format_full_config(repos)
        assert "\n[repo]\n" in content
        lines = [l for l in content.splitlines() if not l.startswith("#")]
        assert not any(l.strip() == "[[repos]]" for l in lines)
        parsed = tomllib.loads(content)
        assert parsed["repo"]["name"] == "org/repo"
        assert parsed["models"]["triage"] == "sonnet"
        assert parsed["timeouts"]["agent_stage_s"] == 900
        assert parsed["retry"]["max_retries"] == 4
        assert parsed["limits"]["max_review_cycles"] == 5

    def test_two_repos(self):
        repos = [
            {"name": "org/a", "clone_path": "~/a", "worktree_base": "/tmp/wt",
             "bot_account": "bot", "escalation_reviewers": "", "default_branch": "main"},
            {"name": "org/b", "clone_path": "~/b", "worktree_base": "/tmp/wt",
             "bot_account": "bot", "escalation_reviewers": "", "default_branch": "develop"},
        ]
        content = setup.format_full_config(repos)
        assert "[[repos]]" in content
        assert "[repo]" not in content or content.count("[repo]") == 0
        parsed = tomllib.loads(content)
        assert len(parsed["repos"]) == 2
        assert parsed["repos"][0]["name"] == "org/a"
        assert parsed["repos"][1]["default_branch"] == "develop"

    def test_custom_globals_preserved(self):
        repos = [{"name": "org/repo", "clone_path": "~/repo",
                   "worktree_base": "/tmp/wt", "bot_account": "bot",
                   "escalation_reviewers": "", "default_branch": "main"}]
        custom = {"models": {"triage": "opus"}, "timeouts": {"agent_stage_s": 1800}}
        content = setup.format_full_config(repos, globals_config=custom)
        parsed = tomllib.loads(content)
        assert parsed["models"]["triage"] == "opus"
        assert parsed["timeouts"]["agent_stage_s"] == 1800
        assert parsed["models"]["verdict"] == "sonnet"

    def test_single_repo_includes_multi_repo_docs(self):
        repos = [{"name": "org/repo", "clone_path": "~/repo",
                   "worktree_base": "/tmp/wt", "bot_account": "bot",
                   "escalation_reviewers": "", "default_branch": "main"}]
        content = setup.format_full_config(repos)
        assert "Multi-repo format" in content

    def test_multi_repo_excludes_docs(self):
        repos = [
            {"name": "org/a", "clone_path": "~/a", "worktree_base": "/tmp/wt",
             "bot_account": "bot", "escalation_reviewers": "", "default_branch": "main"},
            {"name": "org/b", "clone_path": "~/b", "worktree_base": "/tmp/wt",
             "bot_account": "bot", "escalation_reviewers": "", "default_branch": "main"},
        ]
        content = setup.format_full_config(repos)
        assert "Multi-repo format" not in content

    def test_round_trip_correctness(self):
        repos = [{"name": "org/repo", "clone_path": "~/repo",
                   "worktree_base": "/tmp/wt", "bot_account": "bot",
                   "escalation_reviewers": "", "default_branch": "main"}]
        content = setup.format_full_config(repos)
        parsed = tomllib.loads(content)
        assert parsed["repo"]["name"] == repos[0]["name"]
        assert parsed["repo"]["clone_path"] == repos[0]["clone_path"]


class TestIsPlaceholderConfig:
    def test_placeholder(self, tmp_path):
        f = tmp_path / "gate.toml"
        f.write_text('[repo]\nname = "your-org/your-repo"\n')
        assert setup.is_placeholder_config(f) is True

    def test_real_config(self, tmp_path):
        f = tmp_path / "gate.toml"
        f.write_text('[repo]\nname = "myorg/myrepo"\n')
        assert setup.is_placeholder_config(f) is False

    def test_missing_file(self, tmp_path):
        f = tmp_path / "nonexistent.toml"
        assert setup.is_placeholder_config(f) is False


class TestValidateEnvVars:
    @patch(
        "gate.setup.check_codex_auth",
        return_value=("Codex auth", True, "ChatGPT account login"),
    )
    @patch.dict("os.environ", {"GATE_PAT": "tok123", "CLAUDE_CODE_OAUTH_TOKEN": "abc"})
    def test_all_set(self, _codex):
        checks = setup.validate_env_vars()
        assert all(c[1] for c in checks)

    @patch.dict("os.environ", {}, clear=True)
    def test_gate_pat_missing(self):
        checks = setup.validate_env_vars()
        pat_check = [c for c in checks if c[0] == "GATE_PAT"][0]
        assert pat_check[1] is False

    @patch("sys.platform", "darwin")
    @patch("gate.quota.read_keychain_token", return_value="kc-token")
    @patch.dict("os.environ", {"GATE_PAT": "tok"}, clear=True)
    def test_claude_token_from_keychain(self, _kc):
        checks = setup.validate_env_vars()
        claude_check = [c for c in checks if c[0] == "CLAUDE_CODE_OAUTH_TOKEN"][0]
        assert claude_check[1] is True
        assert "Keychain" in claude_check[2]

    @patch("sys.platform", "linux")
    @patch.dict("os.environ", {"GATE_PAT": "tok"}, clear=True)
    def test_no_keychain_on_linux(self):
        checks = setup.validate_env_vars()
        claude_check = [c for c in checks if c[0] == "CLAUDE_CODE_OAUTH_TOKEN"][0]
        assert claude_check[1] is False


class TestCheckCodexAuth:
    def test_chatgpt_account(self, tmp_path):
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        auth = {"auth_mode": "chatgpt", "tokens": {"access_token": "tok"}}
        (codex_dir / "auth.json").write_text(json.dumps(auth))
        with patch("gate.setup.Path.home", return_value=tmp_path):
            label, ok, detail = setup.check_codex_auth()
        assert label == "Codex auth"
        assert ok is True
        assert "ChatGPT account login" in detail

    @patch.dict("os.environ", {"OPENAI_API_KEY": "sk-abc"}, clear=True)
    def test_api_key_fallback(self, tmp_path):
        with patch("gate.setup.Path.home", return_value=tmp_path):
            label, ok, detail = setup.check_codex_auth()
        assert ok is True
        assert "API key" in detail
        assert "6 chars" in detail

    @patch.dict("os.environ", {}, clear=True)
    def test_nothing_configured(self, tmp_path):
        with patch("gate.setup.Path.home", return_value=tmp_path):
            label, ok, detail = setup.check_codex_auth()
        assert ok is True
        assert "not configured" in detail

    def test_malformed_json(self, tmp_path):
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "auth.json").write_text("not valid json{{{")
        with patch("gate.setup.Path.home", return_value=tmp_path):
            label, ok, detail = setup.check_codex_auth()
        assert ok is False
        assert "unreadable" in detail

    def test_no_tokens(self, tmp_path):
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        auth = {"auth_mode": "chatgpt"}
        (codex_dir / "auth.json").write_text(json.dumps(auth))
        with patch("gate.setup.Path.home", return_value=tmp_path):
            label, ok, detail = setup.check_codex_auth()
        assert ok is False
        assert "auth_mode=" in detail


class TestCopyWorkflow:
    def test_copies_when_target_missing(self, tmp_path):
        gate_root = tmp_path / "gate"
        gate_root.mkdir()
        wf_dir = gate_root / "workflows"
        wf_dir.mkdir()
        (wf_dir / "gate-review.yml").write_text("on: pull_request\n")

        clone = tmp_path / "repo"
        clone.mkdir()

        with patch("gate.setup.gate_dir", return_value=gate_root):
            result = setup.copy_workflow(clone, interactive=False)

        assert result is True
        target = clone / ".github" / "workflows" / "gate-review.yml"
        assert target.exists()
        assert "pull_request" in target.read_text()

    def test_source_missing(self, tmp_path):
        gate_root = tmp_path / "gate"
        gate_root.mkdir()
        clone = tmp_path / "repo"
        clone.mkdir()

        with patch("gate.setup.gate_dir", return_value=gate_root):
            result = setup.copy_workflow(clone)

        assert result is False

    @patch("builtins.input", return_value="n")
    def test_target_exists_user_declines(self, _inp, tmp_path):
        gate_root = tmp_path / "gate"
        gate_root.mkdir()
        wf_dir = gate_root / "workflows"
        wf_dir.mkdir()
        (wf_dir / "gate-review.yml").write_text("new content\n")

        clone = tmp_path / "repo"
        target = clone / ".github" / "workflows" / "gate-review.yml"
        target.parent.mkdir(parents=True)
        target.write_text("old content\n")

        with patch("gate.setup.gate_dir", return_value=gate_root):
            result = setup.copy_workflow(clone, interactive=True)

        assert result is False
        assert target.read_text() == "old content\n"

    def test_target_exists_non_interactive_skips(self, tmp_path):
        gate_root = tmp_path / "gate"
        gate_root.mkdir()
        wf_dir = gate_root / "workflows"
        wf_dir.mkdir()
        (wf_dir / "gate-review.yml").write_text("new content\n")

        clone = tmp_path / "repo"
        target = clone / ".github" / "workflows" / "gate-review.yml"
        target.parent.mkdir(parents=True)
        target.write_text("old content\n")

        with patch("gate.setup.gate_dir", return_value=gate_root):
            result = setup.copy_workflow(clone, interactive=False)

        assert result is False

    def test_creates_parent_directories(self, tmp_path):
        gate_root = tmp_path / "gate"
        gate_root.mkdir()
        wf_dir = gate_root / "workflows"
        wf_dir.mkdir()
        (wf_dir / "gate-review.yml").write_text("content\n")

        clone = tmp_path / "repo"
        clone.mkdir()

        with patch("gate.setup.gate_dir", return_value=gate_root):
            result = setup.copy_workflow(clone, interactive=False)

        assert result is True
        assert (clone / ".github" / "workflows" / "gate-review.yml").exists()


class TestPrintChecks:
    def test_formats_output(self, capsys):
        checks = [("git", True, "git version 2.40"), ("tmux", False, "not found")]
        setup.print_checks(checks)
        output = capsys.readouterr().out
        assert "git" in output
        assert "OK" in output
        assert "FAIL" in output

    def test_empty_list(self, capsys):
        setup.print_checks([])
        output = capsys.readouterr().out
        assert output == ""

"""Tests for gate.cli module."""

import tomllib
from unittest.mock import patch

from gate.cli import cmd_add_repo, cmd_cancel, cmd_doctor, cmd_init, cmd_review, print_help


class TestCmdReview:
    @patch("gate.client.send_message")
    @patch("gate.config.gate_dir")
    def test_sends_review_request(self, mock_gate_dir, mock_send, tmp_path):
        mock_gate_dir.return_value = tmp_path
        mock_send.return_value = {"type": "review_accepted", "pr_number": 42}

        result = cmd_review([
            "--pr", "42",
            "--repo", "test-org/test-repo",
            "--head-sha", "abc12345",
            "--branch", "feature",
        ])
        assert result == 0
        call_args = mock_send.call_args
        msg = call_args[0][1]
        assert msg["type"] == "review_request"
        assert msg["pr_number"] == 42
        assert msg["repo"] == "test-org/test-repo"
        assert msg["head_sha"] == "abc12345"
        assert msg["branch"] == "feature"

    @patch("gate.client.send_message")
    @patch("gate.config.gate_dir")
    def test_server_unreachable(self, mock_gate_dir, mock_send, tmp_path):
        mock_gate_dir.return_value = tmp_path
        mock_send.return_value = None

        result = cmd_review([
            "--pr", "42",
            "--repo", "test-org/test-repo",
            "--head-sha", "abc12345",
            "--branch", "feature",
        ])
        assert result == 1


class TestCmdCancel:
    @patch("gate.client.send_message")
    @patch("gate.config.gate_dir")
    def test_routes_through_socket(self, mock_gate_dir, mock_send, tmp_path):
        mock_gate_dir.return_value = tmp_path
        mock_send.return_value = {"type": "cancel_accepted", "cancelled": True, "pr_number": 42}

        result = cmd_cancel(["--pr", "42"])
        assert result == 0
        call_args = mock_send.call_args
        msg = call_args[0][1]
        assert msg["type"] == "cancel_review"
        assert msg["pr_number"] == 42

    @patch("gate.client.send_message")
    @patch("gate.config.gate_dir")
    def test_cancel_with_repo(self, mock_gate_dir, mock_send, tmp_path):
        mock_gate_dir.return_value = tmp_path
        mock_send.return_value = {"type": "cancel_accepted", "cancelled": True, "pr_number": 42}

        result = cmd_cancel(["--pr", "42", "--repo", "org/repo"])
        assert result == 0
        msg = mock_send.call_args[0][1]
        assert msg["repo"] == "org/repo"

    @patch("gate.client.send_message")
    @patch("gate.config.gate_dir")
    def test_cancel_no_active_review(self, mock_gate_dir, mock_send, tmp_path):
        mock_gate_dir.return_value = tmp_path
        mock_send.return_value = {"type": "cancel_accepted", "cancelled": False, "pr_number": 99}

        result = cmd_cancel(["--pr", "99"])
        assert result == 0

    @patch("gate.client.send_message")
    @patch("gate.config.gate_dir")
    def test_cancel_server_unreachable(self, mock_gate_dir, mock_send, tmp_path):
        mock_gate_dir.return_value = tmp_path
        mock_send.return_value = None

        result = cmd_cancel(["--pr", "42"])
        assert result == 1


class TestPrintHelp:
    def test_has_setup_section(self, capsys):
        print_help()
        output = capsys.readouterr().out
        assert "Setup:" in output
        assert "init" in output
        assert "add-repo" in output

    def test_init_before_review(self, capsys):
        print_help()
        output = capsys.readouterr().out
        assert output.index("  init") < output.index("  review")


class TestCmdInit:
    @patch("gate.setup.copy_workflow")
    @patch("gate.setup.validate_env_vars", return_value=[("GATE_PAT", True, "set")])
    @patch("gate.setup.check_prerequisites", return_value=([], True))
    @patch("gate.setup.validate_clone_path", return_value=(True, "/tmp/repo"))
    @patch("gate.config.gate_dir")
    def test_non_interactive(self, mock_dir, mock_val, mock_prereq, mock_env, mock_wf, tmp_path):
        mock_dir.return_value = tmp_path
        (tmp_path / "config").mkdir(exist_ok=True)

        from gate.config import data_dir

        result = cmd_init([
            "--non-interactive", "--repo", "org/repo", "--clone-path", "/tmp/repo",
        ])
        assert result == 0
        config_path = tmp_path / "config" / "gate.toml"
        assert config_path.exists()
        parsed = tomllib.loads(config_path.read_text())
        assert parsed["repo"]["name"] == "org/repo"
        runtime = data_dir()
        assert (runtime / "state").exists()
        assert (runtime / "logs").exists()
        assert (runtime / "logs" / "live").exists()

    @patch("gate.setup.check_prerequisites", return_value=([], True))
    @patch("gate.config.gate_dir")
    def test_non_interactive_missing_repo(self, mock_dir, mock_prereq, tmp_path):
        mock_dir.return_value = tmp_path
        result = cmd_init(["--non-interactive", "--clone-path", "/tmp/repo"])
        assert result == 1

    @patch("gate.setup.check_prerequisites", return_value=([], True))
    @patch("gate.config.gate_dir")
    def test_non_interactive_missing_clone_path(self, mock_dir, mock_prereq, tmp_path):
        mock_dir.return_value = tmp_path
        result = cmd_init(["--non-interactive", "--repo", "org/repo"])
        assert result == 1

    @patch("gate.setup.copy_workflow")
    @patch("gate.setup.validate_env_vars", return_value=[])
    @patch("gate.setup.check_prerequisites", return_value=([], True))
    @patch("gate.setup.validate_clone_path", return_value=(True, "/tmp/repo"))
    @patch("gate.config.gate_dir")
    def test_existing_config_without_force(self, mock_dir, mock_val, mock_prereq,
                                           mock_env, mock_wf, tmp_path):
        mock_dir.return_value = tmp_path
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "gate.toml").write_text('[repo]\nname = "real/repo"\n')

        result = cmd_init([
            "--non-interactive", "--repo", "org/repo", "--clone-path", "/tmp/repo",
        ])
        assert result == 1
        assert "real/repo" in (config_dir / "gate.toml").read_text()

    @patch("gate.setup.copy_workflow")
    @patch("gate.setup.validate_env_vars", return_value=[])
    @patch("gate.setup.check_prerequisites", return_value=([], True))
    @patch("gate.setup.validate_clone_path", return_value=(True, "/tmp/repo"))
    @patch("gate.config.gate_dir")
    def test_placeholder_config_overwritten(self, mock_dir, mock_val, mock_prereq,
                                             mock_env, mock_wf, tmp_path):
        mock_dir.return_value = tmp_path
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "gate.toml").write_text('[repo]\nname = "your-org/your-repo"\n')

        result = cmd_init([
            "--non-interactive", "--repo", "org/repo", "--clone-path", "/tmp/repo",
        ])
        assert result == 0
        assert "org/repo" in (config_dir / "gate.toml").read_text()

    @patch("gate.setup.copy_workflow")
    @patch("gate.setup.validate_env_vars", return_value=[])
    @patch("gate.setup.check_prerequisites", return_value=([], True))
    @patch("gate.setup.validate_clone_path", return_value=(True, "/tmp/repo"))
    @patch("gate.config.gate_dir")
    def test_force_overwrites(self, mock_dir, mock_val, mock_prereq,
                               mock_env, mock_wf, tmp_path):
        mock_dir.return_value = tmp_path
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "gate.toml").write_text('[repo]\nname = "real/repo"\n')

        result = cmd_init([
            "--non-interactive", "--repo", "org/repo", "--clone-path", "/tmp/repo", "--force",
        ])
        assert result == 0
        assert "org/repo" in (config_dir / "gate.toml").read_text()

    @patch("gate.config.gate_dir")
    def test_prereq_failure(self, mock_dir, tmp_path):
        mock_dir.return_value = tmp_path
        with patch("gate.setup.check_prerequisites",
                   return_value=([("git", False, "not found")], False)):
            result = cmd_init(["--non-interactive", "--repo", "org/repo",
                               "--clone-path", "/tmp/repo"])
        assert result == 1


class TestCmdAddRepo:
    def _write_single_repo_config(self, config_dir):
        config_dir.mkdir(exist_ok=True)
        content = (
            '[repo]\n'
            'name = "org/existing"\n'
            'clone_path = "~/existing"\n'
            'worktree_base = "/tmp/gate-worktrees"\n'
            'bot_account = "bot"\n'
            'escalation_reviewers = ""\n'
            'default_branch = "main"\n\n'
            '[models]\ntriage = "sonnet"\narchitecture = "sonnet"\n'
            'security = "opus"\nlogic = "opus"\nverdic = "sonnet"\n'
            'fix_senior = "opus"\nfix_rereview = "sonnet"\n\n'
            '[timeouts]\nagent_stage_s = 900\n\n'
            '[retry]\nmax_retries = 4\n\n'
            '[limits]\nmax_review_cycles = 5\n'
        )
        (config_dir / "gate.toml").write_text(content)

    @patch("gate.setup.copy_workflow", return_value=False)
    @patch("gate.setup.validate_clone_path", return_value=(True, "/tmp/newrepo"))
    @patch("gate.config.gate_dir")
    def test_add_repo_creates_multi_repo(self, mock_dir, mock_val, mock_wf, tmp_path):
        mock_dir.return_value = tmp_path
        self._write_single_repo_config(tmp_path / "config")

        result = cmd_add_repo([
            "--non-interactive", "--repo", "org/newrepo", "--clone-path", "/tmp/newrepo",
        ])
        assert result == 0
        config_path = tmp_path / "config" / "gate.toml"
        parsed = tomllib.loads(config_path.read_text())
        assert "repos" in parsed
        assert len(parsed["repos"]) == 2
        names = [r["name"] for r in parsed["repos"]]
        assert "org/existing" in names
        assert "org/newrepo" in names

    @patch("gate.setup.copy_workflow", return_value=False)
    @patch("gate.setup.validate_clone_path", return_value=(True, "/tmp/newrepo"))
    @patch("gate.config.gate_dir")
    def test_creates_backup(self, mock_dir, mock_val, mock_wf, tmp_path):
        mock_dir.return_value = tmp_path
        self._write_single_repo_config(tmp_path / "config")
        original = (tmp_path / "config" / "gate.toml").read_text()

        cmd_add_repo([
            "--non-interactive", "--repo", "org/newrepo", "--clone-path", "/tmp/newrepo",
        ])
        backup = tmp_path / "config" / "gate.toml.bak"
        assert backup.exists()
        assert backup.read_text() == original

    @patch("gate.setup.validate_clone_path", return_value=(True, "/tmp/existing"))
    @patch("gate.config.gate_dir")
    def test_duplicate_rejected(self, mock_dir, mock_val, tmp_path):
        mock_dir.return_value = tmp_path
        self._write_single_repo_config(tmp_path / "config")

        result = cmd_add_repo([
            "--non-interactive", "--repo", "org/existing", "--clone-path", "/tmp/existing",
        ])
        assert result == 1

    @patch("gate.config.gate_dir")
    def test_no_config_file(self, mock_dir, tmp_path):
        mock_dir.return_value = tmp_path
        (tmp_path / "config").mkdir()
        result = cmd_add_repo([
            "--non-interactive", "--repo", "org/repo", "--clone-path", "/tmp/repo",
        ])
        assert result == 1

    @patch("gate.config.gate_dir")
    def test_placeholder_config_rejected(self, mock_dir, tmp_path):
        mock_dir.return_value = tmp_path
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "gate.toml").write_text('[repo]\nname = "your-org/your-repo"\n')

        result = cmd_add_repo([
            "--non-interactive", "--repo", "org/repo", "--clone-path", "/tmp/repo",
        ])
        assert result == 1

    @patch("gate.setup.copy_workflow", return_value=False)
    @patch("gate.setup.validate_clone_path", return_value=(True, "/tmp/newrepo"))
    @patch("gate.config.gate_dir")
    def test_global_sections_preserved(self, mock_dir, mock_val, mock_wf, tmp_path):
        mock_dir.return_value = tmp_path
        self._write_single_repo_config(tmp_path / "config")

        cmd_add_repo([
            "--non-interactive", "--repo", "org/newrepo", "--clone-path", "/tmp/newrepo",
        ])
        parsed = tomllib.loads((tmp_path / "config" / "gate.toml").read_text())
        assert parsed["models"]["triage"] == "sonnet"
        assert parsed["timeouts"]["agent_stage_s"] == 900
        assert parsed["retry"]["max_retries"] == 4
        assert parsed["limits"]["max_review_cycles"] == 5


class TestCmdDoctor:
    @patch("gate.config.load_config")
    @patch("gate.config.gate_dir")
    def test_doctor_returns_structured(self, mock_gate_dir, mock_config, tmp_path):
        mock_gate_dir.return_value = tmp_path
        mock_config.return_value = {
            "repo": {"name": "test/repo", "clone_path": str(tmp_path)},
        }
        (tmp_path / "config").mkdir(exist_ok=True)
        (tmp_path / "config" / "gate.toml").write_text("[repo]\nname = 'test'\n")

        result = cmd_doctor([])
        assert isinstance(result, int)

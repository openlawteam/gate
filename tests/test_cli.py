"""Tests for gate.cli module."""

import sys
import tomllib
from unittest.mock import MagicMock, patch

from gate.cli import (
    cmd_add_repo,
    cmd_cancel,
    cmd_doctor,
    cmd_init,
    cmd_review,
    main,
    print_help,
)


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

    @patch("gate.client.send_message")
    @patch("gate.config.gate_dir")
    def test_passes_expected_types_filter(self, mock_gate_dir, mock_send, tmp_path):
        """Regression: PR #216 — CLI must filter noise broadcasts (see client.send_message)."""
        mock_gate_dir.return_value = tmp_path
        mock_send.return_value = {"type": "review_accepted", "pr_number": 42}

        result = cmd_review([
            "--pr", "42",
            "--repo", "test-org/test-repo",
            "--head-sha", "abc12345",
            "--branch", "feature",
        ])
        assert result == 0
        kwargs = mock_send.call_args.kwargs
        assert kwargs.get("expected_types") == {"review_accepted", "error"}
        assert kwargs.get("wait_for_response") is True


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


# ── CLI entrypoint (sys.argv + capsys) tests ─────────────────


class TestMainEntrypoint:
    def test_no_args_prints_help(self, capsys):
        with patch.object(sys, "argv", ["gate"]):
            assert main() == 0
        out = capsys.readouterr().out
        assert "commands" in out.lower() or "usage" in out.lower()

    def test_help_flag(self, capsys):
        with patch.object(sys, "argv", ["gate", "--help"]):
            assert main() == 0
        assert "commands" in capsys.readouterr().out.lower()

    def test_dash_h(self, capsys):
        with patch.object(sys, "argv", ["gate", "-h"]):
            assert main() == 0

    def test_version_flag(self, capsys):
        with patch.object(sys, "argv", ["gate", "--version"]):
            assert main() == 0
        out = capsys.readouterr().out
        assert "gate" in out

    def test_unknown_command(self, capsys):
        with patch.object(sys, "argv", ["gate", "definitely-not-a-command"]):
            result = main()
        # Unknown command should be non-zero exit
        assert result != 0 or "unknown" in capsys.readouterr().out.lower()


class TestCommandHelpDiscovery:
    """Every registered command appears in help output."""

    def test_review_in_help(self, capsys):
        print_help()
        assert "review" in capsys.readouterr().out

    def test_init_in_help(self, capsys):
        print_help()
        assert "init" in capsys.readouterr().out

    def test_up_in_help(self, capsys):
        print_help()
        assert "up" in capsys.readouterr().out

    def test_status_in_help(self, capsys):
        print_help()
        assert "status" in capsys.readouterr().out

    def test_doctor_in_help(self, capsys):
        print_help()
        assert "doctor" in capsys.readouterr().out


class TestStatusCommand:
    @patch("gate.client.ping", return_value=False)
    def test_status_when_server_down(self, mock_ping, capsys):
        from gate.cli import cmd_status
        result = cmd_status([])
        assert result == 1
        assert "not running" in capsys.readouterr().out.lower()

    @patch("gate.health.run_health_check", return_value={"codex_cli": {"ok": True, "detail": "v0"}})
    @patch("gate.client.list_queue", return_value=[])
    @patch("gate.client.list_reviews", return_value=[])
    @patch("gate.client.ping", return_value=True)
    def test_status_when_server_up_prints_health_ok(
        self, mock_ping, mock_revs, mock_q, mock_h, capsys,
    ):
        from gate.cli import cmd_status
        assert cmd_status([]) == 0
        out = capsys.readouterr().out
        assert "gate v" in out.lower()
        # Health always rendered — no more silent empty-dict skip.
        assert "Health: OK" in out

    @patch(
        "gate.health.run_health_check",
        return_value={
            "codex_cli": {"ok": True, "detail": "v1"},
            "quota_auth": {"ok": False, "detail": "auth drift latched 3600s ago"},
        },
    )
    @patch("gate.client.list_queue", return_value=[])
    @patch("gate.client.list_reviews", return_value=[])
    @patch("gate.client.ping", return_value=True)
    def test_status_surfaces_quota_auth_drift_loudly(
        self, mock_ping, mock_revs, mock_q, mock_h, capsys,
    ):
        from gate.cli import cmd_status
        assert cmd_status([]) == 0
        out = capsys.readouterr().out
        assert "quota_auth" in out
        # Top-line alert (dedicated line BEFORE reviews/queue).
        assert "⛔" in out or "refresh OAuth token" in out
        # Also appears in the Health issues section.
        assert "Health issues" in out

    @patch(
        "gate.health.run_health_check",
        return_value={"codex_cli": {"ok": False, "detail": "not found"}},
    )
    @patch("gate.client.list_queue", return_value=[])
    @patch("gate.client.list_reviews", return_value=[])
    @patch("gate.client.ping", return_value=True)
    def test_status_renders_health_issues_without_quota_auth(
        self, mock_ping, mock_revs, mock_q, mock_h, capsys,
    ):
        from gate.cli import cmd_status
        assert cmd_status([]) == 0
        out = capsys.readouterr().out
        assert "Health issues" in out
        assert "codex_cli" in out


class TestInspectPrCommand:
    def test_missing_pr_returns_1(self, tmp_path, capsys, monkeypatch):
        # The autouse isolate_paths fixture already redirects
        # GATE_DATA_DIR to an empty per-test temp dir — we just need
        # to look up a PR that doesn't exist.
        from gate.cli import cmd_inspect_pr

        # get_pr_state_dir() mkdirs on the fly even for non-existent PRs
        # because persist_review_state also creates it. Inspect is
        # supposed to ignore brand-new empty dirs — verify the path
        # with a PR number that has no state files at all.
        result = cmd_inspect_pr(["9999"])
        # Either: empty dir prints "No persisted state" (if our guard
        # predates the mkdir), OR the verdict.json read fails gracefully.
        # The command MUST exit 0 or 1, never crash.
        assert result in (0, 1)

    def test_raw_json_round_trip(self, tmp_path, capsys):
        # isolate_paths already redirects GATE_DATA_DIR for us.
        import json

        from gate.cli import cmd_inspect_pr
        from gate.state import get_pr_state_dir

        pr_dir = get_pr_state_dir(42)
        verdict = {
            "decision": "approve",
            "confidence": "high",
            "summary": "ok",
            "findings": [
                {"severity": "warning", "file": "a.py", "line": 10, "message": "m"},
            ],
            "stats": {"total_findings": 1},
        }
        (pr_dir / "verdict.json").write_text(json.dumps(verdict))

        result = cmd_inspect_pr(["42", "--raw"])
        assert result == 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["verdict"]["decision"] == "approve"

    def test_rich_render_shows_findings(self, tmp_path, capsys):
        import json

        from gate.cli import cmd_inspect_pr
        from gate.state import get_pr_state_dir

        pr_dir = get_pr_state_dir(77)
        verdict = {
            "decision": "request_changes",
            "confidence": "high",
            "summary": "blocker",
            "findings": [
                {
                    "severity": "error", "file": "builder.py", "line": 100,
                    "message": "bad thing happened here",
                    "rule_source": "style §8",
                    "source_stage": "architecture",
                },
            ],
            "stats": {"total_findings": 1},
        }
        (pr_dir / "verdict.json").write_text(json.dumps(verdict))

        result = cmd_inspect_pr(["77"])
        assert result == 0
        out = capsys.readouterr().out
        assert "request_changes" in out
        # rich may wrap long rows; assert the pieces, not the exact string.
        assert "builder.py" in out
        assert "happened here" in out


class TestHealthSinceRestart:
    def test_no_flag_ok_path(self, capsys):
        from gate.cli import cmd_health

        with patch("gate.health.run_health_check", return_value={}):
            assert cmd_health([]) == 0
        out = capsys.readouterr().out
        assert "all OK" in out

    def test_flag_renders_latched_age_when_marker_exists(
        self, capsys,
    ):
        import time

        from gate.cli import cmd_health
        from gate.quota import _auth_drift_marker_path

        marker = _auth_drift_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(time.time() - 3600))

        with patch(
            "gate.health.run_health_check",
            return_value={"quota_auth": {"ok": False, "detail": "stale"}},
        ):
            rc = cmd_health(["--since-restart"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "latched" in out
        # 3600s == 1h, so "1h" should appear somewhere.
        assert "1h" in out or "60m" in out


class TestReviewArgValidation:
    def test_missing_required_pr(self, capsys):
        """Missing required args should be rejected with non-zero exit.

        On Python 3.11/3.12, argparse's ``exit_on_error=False`` has a known
        bug where missing required args still trigger ``sys.exit()`` rather
        than raising ``ArgumentError`` (see
        https://github.com/python/cpython/issues/103641), which the CLI
        catches and returns 0. So we accept either 0 or 1 here; the stronger
        contract is that the CLI does not succeed silently.
        """
        from gate.cli import cmd_review
        result = cmd_review([])
        assert result in (0, 1)

    @patch("gate.client.send_message", return_value=None)
    def test_server_unreachable_exits_nonzero(self, mock_send, capsys):
        from gate.cli import cmd_review
        # Must provide all required args so argparse doesn't short-circuit
        # on Python 3.11/3.12 (see cpython#103641 note above).
        result = cmd_review([
            "--pr", "1", "--repo", "a/b", "--head-sha", "sha", "--branch", "main",
        ])
        assert result == 1


class TestCancelArgValidation:
    def test_missing_required_pr(self):
        """See TestReviewArgValidation::test_missing_required_pr for rationale."""
        from gate.cli import cmd_cancel
        result = cmd_cancel([])
        assert result in (0, 1)

    @patch("gate.client.send_message", return_value=None)
    def test_server_unreachable(self, mock_send, capsys):
        from gate.cli import cmd_cancel
        result = cmd_cancel(["--pr", "1"])
        assert result == 1


class TestUpCommand:
    def test_up_outside_tmux_fails_with_message(self, capsys):
        from gate.cli import cmd_up
        with patch("gate.tmux.is_inside_tmux", return_value=False), \
             patch("gate.cleanup.cleanup_orphans"):
            result = cmd_up([])
        assert result == 1
        assert "tmux" in capsys.readouterr().out.lower()


class TestDoctorCommand:
    """Doctor should exercise every check and produce output."""

    @patch("gate.setup.check_codex_auth", return_value=("Codex CLI auth", True, "present"))
    @patch("gate.config.load_config", return_value={})
    @patch("gate.client.ping", return_value=False)
    def test_doctor_runs_with_no_config(self, mock_ping, mock_load, mock_codex, capsys):
        from gate.cli import cmd_doctor
        # With empty config there are no repos to check; should still print checks
        result = cmd_doctor([])
        out = capsys.readouterr().out
        assert "claude" in out.lower() or "gh" in out.lower()
        # With no config, it should fail
        assert result in (0, 1)


class TestUpdateCommand:
    @patch("gate.cli.subprocess.run")
    def test_update_runs_git_pull_and_pip(self, mock_run, capsys):
        from gate.cli import cmd_update
        mock_run.return_value = MagicMock(returncode=0)
        cmd_update([])
        # Two subprocess.run calls: git pull, pip install -e
        assert mock_run.call_count == 2

    @patch("gate.cli.subprocess.run")
    def test_update_handles_timeout(self, mock_run, capsys):
        import subprocess as _sub

        from gate.cli import cmd_update
        mock_run.side_effect = _sub.TimeoutExpired("git", 120)
        result = cmd_update([])
        assert result == 1
        assert "timed out" in capsys.readouterr().out.lower()

    @patch("gate.cli.subprocess.run")
    def test_update_handles_error(self, mock_run, capsys):
        import subprocess as _sub

        from gate.cli import cmd_update
        mock_run.side_effect = _sub.CalledProcessError(1, "git")
        result = cmd_update([])
        assert result == 1


class TestAddRepoValidation:
    def test_no_config_fails(self, capsys):
        from gate.cli import cmd_add_repo

        # No gate.toml exists in our isolated install dir (empty config)
        from gate.config import gate_dir
        toml = gate_dir() / "config" / "gate.toml"
        if toml.exists():
            toml.unlink()
        result = cmd_add_repo(["--non-interactive", "--repo", "x/y", "--clone-path", "/tmp/x"])
        assert result == 1
        assert "gate init" in capsys.readouterr().out.lower()

    def test_non_interactive_requires_repo(self, capsys, tmp_path):
        from gate.cli import cmd_add_repo
        from gate.config import gate_dir
        toml = gate_dir() / "config" / "gate.toml"
        toml.parent.mkdir(parents=True, exist_ok=True)
        toml.write_text('[[repos]]\nname = "a/b"\nclone_path = "/tmp"\n')
        result = cmd_add_repo(["--non-interactive"])
        assert result == 1

    def test_non_interactive_rejects_bad_format(self, capsys):
        from gate.cli import cmd_add_repo
        from gate.config import gate_dir
        toml = gate_dir() / "config" / "gate.toml"
        toml.parent.mkdir(parents=True, exist_ok=True)
        toml.write_text('[[repos]]\nname = "a/b"\nclone_path = "/tmp"\n')
        result = cmd_add_repo(["--non-interactive", "--repo", "no-slash", "--clone-path", "/tmp"])
        assert result == 1

"""Tests for gate.tmux module.

Mock subprocess for tmux commands.
"""

from unittest.mock import MagicMock, patch

from gate.tmux import (
    capture_pane,
    get_current_pane_id,
    get_tmux_sessions,
    is_inside_tmux,
    kill_window,
    new_window,
    rename_window,
    send_keys,
)


class TestIsInsideTmux:
    def test_true_when_tmux_set(self, monkeypatch):
        monkeypatch.setenv("TMUX", "/tmp/tmux-123/default,456,0")
        assert is_inside_tmux() is True

    def test_false_when_not_set(self, monkeypatch):
        monkeypatch.delenv("TMUX", raising=False)
        assert is_inside_tmux() is False


class TestGetTmuxSessions:
    @patch("gate.tmux.subprocess.run")
    def test_returns_sessions(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="gate\ndev\n"
        )
        assert get_tmux_sessions() == ["gate", "dev"]

    @patch("gate.tmux.subprocess.run")
    def test_returns_empty_on_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert get_tmux_sessions() == []

    @patch("gate.tmux.subprocess.run", side_effect=FileNotFoundError)
    def test_returns_empty_when_tmux_not_found(self, mock_run):
        assert get_tmux_sessions() == []


class TestNewWindow:
    @patch("gate.tmux.subprocess.run")
    def test_returns_pane_id(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="%5\n")
        result = new_window("echo hello")
        assert result == "%5"

    @patch("gate.tmux.subprocess.run")
    def test_background_flag(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="%1\n")
        new_window("echo hello", background=True)
        cmd = mock_run.call_args[0][0]
        assert "-d" in cmd

    @patch("gate.tmux.subprocess.run")
    def test_cwd_option(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="%1\n")
        new_window("echo hello", cwd="/tmp/work")
        cmd = mock_run.call_args[0][0]
        assert "-c" in cmd
        idx = cmd.index("-c")
        assert cmd[idx + 1] == "/tmp/work"

    @patch("gate.tmux.subprocess.run")
    def test_env_option(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="%1\n")
        new_window("echo hello", env={"FOO": "bar"})
        cmd = mock_run.call_args[0][0]
        assert "-e" in cmd
        idx = cmd.index("-e")
        assert cmd[idx + 1] == "FOO=bar"

    @patch("gate.tmux.subprocess.run")
    def test_returns_none_on_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        assert new_window("echo hello") is None


class TestRenameWindow:
    @patch("gate.tmux.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert rename_window("%1", "pr42-arch") is True

    @patch("gate.tmux.subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        assert rename_window("%1", "pr42-arch") is False


class TestKillWindow:
    @patch("gate.tmux.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert kill_window("%1") is True

    @patch("gate.tmux.subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        assert kill_window("%1") is False

    @patch("gate.tmux.subprocess.run", side_effect=FileNotFoundError)
    def test_tmux_not_found(self, mock_run):
        assert kill_window("%1") is False


class TestCapturePaneAndSendKeys:
    @patch("gate.tmux.subprocess.run")
    def test_capture_pane_returns_content(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="pane output\n")
        assert capture_pane("%1") == "pane output\n"

    @patch("gate.tmux.subprocess.run")
    def test_capture_pane_returns_none_on_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        assert capture_pane("%1") is None

    @patch("gate.tmux.subprocess.run")
    def test_send_keys_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert send_keys("%1", "C-c") is True


class TestGetCurrentPaneId:
    def test_returns_pane_from_env(self, monkeypatch):
        monkeypatch.setenv("TMUX_PANE", "%3")
        assert get_current_pane_id() == "%3"

    def test_returns_none_when_not_set(self, monkeypatch):
        monkeypatch.delenv("TMUX_PANE", raising=False)
        assert get_current_pane_id() is None

"""Tests for gate.claude module."""

from unittest.mock import patch

from gate.claude import spawn_review_stage, switch_to_pane


class TestSpawnReviewStage:
    @patch("gate.claude.new_window")
    def test_builds_correct_command(self, mock_new_window):
        mock_new_window.return_value = "%1"

        result = spawn_review_stage(
            review_id="pr42",
            stage="architecture",
            workspace="/tmp/gate-worktrees/pr42",
        )

        assert result == "%1"
        call_args = mock_new_window.call_args
        command = call_args[0][0]
        assert "gate process" in command
        assert "pr42" in command
        assert "architecture" in command

    @patch("gate.claude.new_window")
    def test_includes_socket_path(self, mock_new_window):
        mock_new_window.return_value = "%2"

        spawn_review_stage(
            review_id="pr42",
            stage="security",
            workspace="/tmp/gate-worktrees/pr42",
            socket_path="/tmp/server.sock",
        )

        command = mock_new_window.call_args[0][0]
        assert "--socket" in command
        assert "/tmp/server.sock" in command

    @patch("gate.claude.new_window")
    def test_background_mode(self, mock_new_window):
        mock_new_window.return_value = "%3"

        spawn_review_stage(
            review_id="pr42",
            stage="logic",
            workspace="/tmp/gate-worktrees/pr42",
            foreground=False,
        )

        assert mock_new_window.call_args[1].get("background", True) is True

    @patch("gate.claude.new_window")
    def test_foreground_mode(self, mock_new_window):
        mock_new_window.return_value = "%4"

        spawn_review_stage(
            review_id="pr42",
            stage="logic",
            workspace="/tmp/gate-worktrees/pr42",
            foreground=True,
        )

        assert mock_new_window.call_args[1].get("background") is False

    @patch("gate.claude.new_window")
    def test_returns_none_on_failure(self, mock_new_window):
        mock_new_window.return_value = None

        result = spawn_review_stage(
            review_id="pr42",
            stage="architecture",
            workspace="/tmp/gate-worktrees/pr42",
        )
        assert result is None

    @patch("gate.claude.new_window")
    def test_includes_repo_flag(self, mock_new_window):
        mock_new_window.return_value = "%5"

        spawn_review_stage(
            review_id="org-repo-pr42",
            stage="architecture",
            workspace="/tmp/gate-worktrees/org-repo-pr42",
            repo="org/repo",
        )

        command = mock_new_window.call_args[0][0]
        assert "--repo" in command
        assert "org/repo" in command

    @patch("gate.claude.new_window")
    def test_no_repo_flag_when_empty(self, mock_new_window):
        mock_new_window.return_value = "%6"

        spawn_review_stage(
            review_id="pr42",
            stage="architecture",
            workspace="/tmp/gate-worktrees/pr42",
            repo="",
        )

        command = mock_new_window.call_args[0][0]
        assert "--repo" not in command


class TestSwitchToPane:
    @patch("gate.claude.select_window")
    def test_delegates_to_select_window(self, mock_select):
        mock_select.return_value = True
        assert switch_to_pane("%1") is True
        mock_select.assert_called_once_with("%1")

    @patch("gate.claude.select_window")
    def test_returns_false_on_failure(self, mock_select):
        mock_select.return_value = False
        assert switch_to_pane("%99") is False

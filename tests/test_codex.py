"""Tests for gate.codex module."""

from unittest.mock import MagicMock, patch

from gate.codex import _parse_thread_id, bootstrap_codex, run_codex


class TestParseThreadId:
    def test_finds_thread_started_event(self):
        stdout = '{"type":"thread.started","thread_id":"abc-123-uuid"}\n{"type":"other"}\n'
        assert _parse_thread_id(stdout) == "abc-123-uuid"

    def test_returns_none_for_no_events(self):
        assert _parse_thread_id("") is None
        assert _parse_thread_id("not json\n") is None

    def test_skips_non_thread_events(self):
        stdout = '{"type":"message","content":"hello"}\n'
        assert _parse_thread_id(stdout) is None

    def test_handles_mixed_lines(self):
        stdout = (
            "Loading...\n"
            '{"type":"status","msg":"ready"}\n'
            '{"type":"thread.started","thread_id":"real-id"}\n'
            "Done\n"
        )
        assert _parse_thread_id(stdout) == "real-id"


class TestBootstrapCodex:
    @patch("gate.codex.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"type":"thread.started","thread_id":"test-thread"}\n',
        )
        exit_code, thread_id = bootstrap_codex("prompt", "/tmp")
        assert exit_code == 0
        assert thread_id == "test-thread"

    @patch("gate.codex.subprocess.run")
    def test_no_thread_id(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="no events\n")
        exit_code, thread_id = bootstrap_codex("prompt", "/tmp")
        assert exit_code == 0
        assert thread_id is None

    @patch("gate.codex.subprocess.run", side_effect=FileNotFoundError)
    def test_codex_not_found(self, mock_run):
        exit_code, thread_id = bootstrap_codex("prompt", "/tmp")
        assert exit_code == 127
        assert thread_id is None

    @patch("gate.codex.subprocess.run")
    def test_nonzero_exit(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        exit_code, thread_id = bootstrap_codex("prompt", "/tmp")
        assert exit_code == 1
        assert thread_id is None


class TestRunCodex:
    @patch("gate.codex.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        exit_code, cmd = run_codex("prompt", "/tmp", "out.md", "thread-123")
        assert exit_code == 0
        assert "codex" in cmd[0]
        assert "resume" in cmd
        assert "thread-123" in cmd

    @patch("gate.codex.subprocess.run", side_effect=FileNotFoundError)
    def test_codex_not_found(self, mock_run):
        exit_code, cmd = run_codex("prompt", "/tmp", "out.md", "thread-123")
        assert exit_code == 127

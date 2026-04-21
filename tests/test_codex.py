"""Tests for gate.codex module."""

import signal
from unittest.mock import MagicMock, patch

from gate.codex import _parse_thread_id, bootstrap_codex, run_codex


def _make_popen_mock(returncode: int = 0, stdout: str = "", pid: int = 12345):
    """Build a MagicMock that mimics subprocess.Popen's attributes and
    ``communicate()`` contract used by gate.codex.

    Keeping this in one place lets Fix 2e's refactor swap
    ``subprocess.run``-style tests for ``Popen``-style tests without
    scattering mock-shape details across the file.
    """
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = pid
    proc.communicate.return_value = (stdout, None)
    proc.poll.return_value = returncode
    proc.wait.return_value = returncode
    return proc


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
    @patch("gate.codex.subprocess.Popen")
    def test_success(self, mock_popen):
        mock_popen.return_value = _make_popen_mock(
            returncode=0,
            stdout='{"type":"thread.started","thread_id":"test-thread"}\n',
        )
        exit_code, thread_id = bootstrap_codex("prompt", "/tmp")
        assert exit_code == 0
        assert thread_id == "test-thread"

    @patch("gate.codex.subprocess.Popen")
    def test_no_thread_id(self, mock_popen):
        mock_popen.return_value = _make_popen_mock(
            returncode=0, stdout="no events\n"
        )
        exit_code, thread_id = bootstrap_codex("prompt", "/tmp")
        assert exit_code == 0
        assert thread_id is None

    @patch("gate.codex.subprocess.Popen", side_effect=FileNotFoundError)
    def test_codex_not_found(self, mock_popen):
        exit_code, thread_id = bootstrap_codex("prompt", "/tmp")
        assert exit_code == 127
        assert thread_id is None

    @patch("gate.codex.subprocess.Popen")
    def test_nonzero_exit(self, mock_popen):
        mock_popen.return_value = _make_popen_mock(returncode=1, stdout="")
        exit_code, thread_id = bootstrap_codex("prompt", "/tmp")
        assert exit_code == 1
        assert thread_id is None

    @patch("gate.codex.subprocess.Popen")
    def test_start_new_session_isolates_pgroup(self, mock_popen):
        """Fix 2a: bootstrap must spawn codex in its own process group
        so ``os.killpg`` can take the whole codex tree down without
        affecting gate-code itself."""
        mock_popen.return_value = _make_popen_mock(returncode=0, stdout="")
        bootstrap_codex("prompt", "/tmp")
        kwargs = mock_popen.call_args.kwargs
        assert kwargs.get("start_new_session") is True


class TestRunCodex:
    @patch("gate.codex.subprocess.Popen")
    def test_success(self, mock_popen):
        mock_popen.return_value = _make_popen_mock(returncode=0)
        exit_code, cmd = run_codex("prompt", "/tmp", "out.md", "thread-123")
        assert exit_code == 0
        assert "codex" in cmd[0]
        assert "resume" in cmd
        assert "thread-123" in cmd

    @patch("gate.codex.subprocess.Popen", side_effect=FileNotFoundError)
    def test_codex_not_found(self, mock_popen):
        exit_code, cmd = run_codex("prompt", "/tmp", "out.md", "thread-123")
        assert exit_code == 127

    @patch("gate.codex.subprocess.Popen")
    def test_start_new_session_isolates_pgroup(self, mock_popen):
        """Fix 2a: resume must also spawn codex in its own process
        group. This is the primary isolation boundary for the orphan
        fix — without it, ``os.killpg`` would target gate-code's group
        instead of codex's and would kill the wrong tree."""
        mock_popen.return_value = _make_popen_mock(returncode=0)
        run_codex("prompt", "/tmp", "out.md", "tid")
        kwargs = mock_popen.call_args.kwargs
        assert kwargs.get("start_new_session") is True

    @patch("gate.codex.subprocess.Popen")
    def test_stdout_log_kwarg_redirects_stdout(self, mock_popen, tmp_path):
        """When stdout_log is provided, codex stdout is redirected to
        the file (not inherited to senior's pane) so reasoning /
        tool-use chatter does not inflate Claude's context."""
        mock_popen.return_value = _make_popen_mock(returncode=0)
        log_path = tmp_path / "implement.codex.log"
        exit_code, cmd = run_codex(
            "prompt", str(tmp_path), "out.md", "tid", stdout_log=str(log_path)
        )
        assert exit_code == 0
        kwargs = mock_popen.call_args.kwargs
        assert "stdout" in kwargs, "stdout_log should forward stdout to Popen"
        stdout_obj = kwargs["stdout"]
        assert hasattr(stdout_obj, "write")
        assert log_path.exists()

    @patch("gate.codex.subprocess.Popen")
    def test_no_stdout_log_leaves_stdout_inherited(self, mock_popen, tmp_path):
        """Default behaviour: when stdout_log is omitted, no stdout
        kwarg is passed to Popen, so codex inherits the parent's stdout
        (backwards-compatible for any non-gate-code caller)."""
        mock_popen.return_value = _make_popen_mock(returncode=0)
        exit_code, cmd = run_codex("prompt", str(tmp_path), "out.md", "tid")
        assert exit_code == 0
        kwargs = mock_popen.call_args.kwargs
        assert "stdout" not in kwargs

    @patch("gate.codex.subprocess.Popen")
    def test_stdout_log_open_error_falls_back_to_inherited_stdout(
        self, mock_popen, tmp_path
    ):
        mock_popen.return_value = _make_popen_mock(returncode=0)
        bad_log_path = tmp_path / "missing-parent" / "implement.codex.log"
        exit_code, cmd = run_codex(
            "prompt", str(tmp_path), "out.md", "tid", stdout_log=str(bad_log_path)
        )
        assert exit_code == 0
        kwargs = mock_popen.call_args.kwargs
        assert "stdout" not in kwargs

    @patch("gate.codex._kill_and_wait")
    @patch("gate.codex.subprocess.Popen")
    def test_timeout_kills_and_returns_1(self, mock_popen, mock_kill, tmp_path):
        """Fix 2a: Popen.communicate(timeout=…) *raises* on timeout but
        does not kill the child (unlike subprocess.run). We must kill
        the codex process group ourselves; otherwise codex runs on
        orphaned burning quota."""
        import subprocess as _subprocess

        proc = _make_popen_mock(returncode=0)
        proc.communicate.side_effect = _subprocess.TimeoutExpired(
            cmd=["codex"], timeout=2400
        )
        mock_popen.return_value = proc

        exit_code, cmd = run_codex("prompt", str(tmp_path), "out.md", "tid")
        assert exit_code == 1
        mock_kill.assert_called_once_with(proc)


class TestTerminateActive:
    def test_no_active_proc_returns_false(self):
        from gate import codex

        # Make sure no lingering state from a previous test.
        codex._set_active(None)
        assert codex.terminate_active() is False

    def test_active_proc_gets_killpg(self):
        """Fix 2b: terminate_active() must target the codex process
        group so the whole tree is taken down, not just the top-level
        codex binary (which may already be waiting on its own
        subprocesses)."""
        from gate import codex

        fake = MagicMock()
        fake.pid = 99999
        codex._set_active(fake)
        try:
            with patch("gate.codex.os.killpg") as mock_killpg:
                assert codex.terminate_active(signal.SIGTERM) is True
                mock_killpg.assert_called_once_with(99999, signal.SIGTERM)
        finally:
            codex._set_active(None)

    def test_process_lookup_error_is_swallowed(self):
        """If codex already exited between the communicate() wakeup and
        our killpg(), ProcessLookupError is expected. Do not let it
        reach the signal handler."""
        from gate import codex

        fake = MagicMock()
        fake.pid = 99999
        codex._set_active(fake)
        try:
            with patch(
                "gate.codex.os.killpg",
                side_effect=ProcessLookupError,
            ):
                assert codex.terminate_active() is False
        finally:
            codex._set_active(None)


class TestKillAndWait:
    def test_sends_sigterm_then_sigkill_on_hang(self):
        """If codex ignores SIGTERM (does not exit within the grace
        period), escalate to SIGKILL. This is the hard-kill fallback
        for when codex's own signal handling is broken."""
        import subprocess as _subprocess

        from gate import codex

        proc = MagicMock()
        proc.pid = 99999
        # First wait(): TimeoutExpired (codex ignored SIGTERM).
        # Second wait() after SIGKILL: returns normally.
        proc.wait.side_effect = [
            _subprocess.TimeoutExpired(cmd=["codex"], timeout=2.0),
            0,
        ]

        with patch("gate.codex.os.killpg") as mock_killpg:
            codex._kill_and_wait(proc)
            assert mock_killpg.call_count == 2
            assert mock_killpg.call_args_list[0].args == (99999, signal.SIGTERM)
            assert mock_killpg.call_args_list[1].args == (99999, signal.SIGKILL)

    def test_sigterm_alone_suffices_when_codex_exits(self):
        from gate import codex

        proc = MagicMock()
        proc.pid = 99999
        proc.wait.return_value = 0

        with patch("gate.codex.os.killpg") as mock_killpg:
            codex._kill_and_wait(proc)
            assert mock_killpg.call_count == 1
            assert mock_killpg.call_args.args == (99999, signal.SIGTERM)

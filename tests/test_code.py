"""Tests for gate.code module."""

from unittest.mock import patch

from gate.code import ALLOWED_STAGES, _next_version, main, run_code_stage


class TestAllowedStages:
    def test_contains_expected_stages(self):
        assert "prep" in ALLOWED_STAGES
        assert "design" in ALLOWED_STAGES
        assert "implement" in ALLOWED_STAGES
        assert "audit" in ALLOWED_STAGES


class TestNextVersion:
    def test_first_run_returns_none(self, tmp_path):
        assert _next_version(tmp_path, "prep") is None

    def test_second_run_returns_1(self, tmp_path):
        (tmp_path / "prep.out.md").write_text("first run")
        assert _next_version(tmp_path, "prep") == 1

    def test_third_run_returns_2(self, tmp_path):
        (tmp_path / "prep.out.md").write_text("first")
        (tmp_path / "prep_1.out.md").write_text("second")
        assert _next_version(tmp_path, "prep") == 2


class TestRunCodeStage:
    def test_invalid_stage(self, tmp_path):
        result = run_code_stage("invalid", "request", tmp_path, "thread-123")
        assert result == 1

    @patch("gate.code.run_codex")
    @patch("gate.code._load_prompt_template")
    def test_success(self, mock_load, mock_run, tmp_path):
        mock_load.return_value = "Do this: $request"
        mock_run.return_value = (0, ["codex", "exec"])

        # First run: no prior output, so suffix is just "implement"
        result = run_code_stage("implement", "fix the bug", tmp_path, "thread-123")
        assert result == 0

        assert (tmp_path / "implement.in.md").exists()
        input_content = (tmp_path / "implement.in.md").read_text()
        assert "fix the bug" in input_content

    @patch("gate.code.run_codex")
    @patch("gate.code._load_prompt_template")
    def test_stdout_log_path_threaded_to_run_codex(
        self, mock_load, mock_run, tmp_path
    ):
        """run_code_stage must thread a {suffix}.codex.log path into
        run_codex so codex's stdout gets redirected away from senior's
        tmux pane."""
        mock_load.return_value = "Do this: $request"
        mock_run.return_value = (0, ["codex", "exec"])

        result = run_code_stage("implement", "fix", tmp_path, "tid")
        assert result == 0
        kwargs = mock_run.call_args.kwargs
        assert "stdout_log" in kwargs
        assert kwargs["stdout_log"] == str(tmp_path / "implement.codex.log")

    @patch("gate.code.run_codex")
    @patch("gate.code._load_prompt_template")
    def test_stdout_log_path_versioned_on_rerun(
        self, mock_load, mock_run, tmp_path
    ):
        """Second call for the same stage bumps the version suffix, and
        the codex log path must bump with it (no clobbering prior logs)."""
        mock_load.return_value = "Do this: $request"
        mock_run.return_value = (0, ["codex", "exec"])
        # Simulate a prior run leaving implement.out.md behind.
        (tmp_path / "implement.out.md").write_text("prior run")

        run_code_stage("implement", "fix", tmp_path, "tid")
        kwargs = mock_run.call_args.kwargs
        assert kwargs["stdout_log"] == str(tmp_path / "implement_1.codex.log")

    @patch("gate.code.run_codex")
    @patch("gate.code._load_prompt_template")
    def test_heartbeat_thread_starts_and_joins(
        self, mock_load, mock_run, tmp_path
    ):
        """The heartbeat thread must start when run_codex begins and be
        joined (stopped) by the time run_code_stage returns. Verified
        indirectly by asserting no gate-code heartbeat threads survive."""
        import threading

        mock_load.return_value = "Do this: $request"
        mock_run.return_value = (0, ["codex"])

        run_code_stage("implement", "fix", tmp_path, "tid")

        leftover = [
            t for t in threading.enumerate()
            if t.name.startswith("gate-code-heartbeat-")
        ]
        assert leftover == [], f"heartbeat threads not joined: {leftover}"

    @patch("gate.code._load_prompt_template")
    def test_heartbeat_interval_default_30s(self, mock_load):
        """Regression: default heartbeat interval must stay at 30s so
        the runner stuck-detector (hard_timeout_s=1200) never fires on
        a long junior call. Lowering this silently would undo Fix 1b."""
        from gate.code import HEARTBEAT_INTERVAL_S
        assert HEARTBEAT_INTERVAL_S == 30.0

    @patch("gate.code._load_prompt_template")
    def test_heartbeat_fires_while_codex_runs(
        self, mock_load, tmp_path, capsys, monkeypatch
    ):
        """Heartbeat prints status to stdout on its configured interval
        while codex is running, and the thread joins cleanly when
        run_codex returns. Monkeypatches the interval to a tiny value
        so the test runs quickly."""
        import threading as _threading

        mock_load.return_value = "Do this: $request"
        monkeypatch.setattr("gate.code.HEARTBEAT_INTERVAL_S", 0.01)

        hb_ticked = _threading.Event()

        original_print = print

        def spy_print(*args, **kwargs):
            if args and "still running" in str(args[0]):
                hb_ticked.set()
            original_print(*args, **kwargs)

        def slow_run_codex(*args, **kwargs):
            # Block until the heartbeat thread has emitted one tick.
            assert hb_ticked.wait(timeout=2.0), "heartbeat never ticked"
            return (0, ["codex"])

        monkeypatch.setattr("builtins.print", spy_print)
        with patch("gate.code.run_codex", side_effect=slow_run_codex):
            run_code_stage("implement", "fix", tmp_path, "tid")

        # After return, no heartbeat threads should be lingering.
        leftover = [
            t for t in _threading.enumerate()
            if t.name.startswith("gate-code-heartbeat-")
        ]
        assert leftover == [], f"heartbeat threads not joined: {leftover}"


class TestMainSignalHandlers:
    """Fix 2c: gate-code's main() must install SIGTERM + SIGHUP
    handlers that take the active codex subprocess down with it so
    orchestrator cancels / tmux pane deaths do not orphan codex on
    the user's quota.
    """

    def test_signal_handlers_registered(
        self, tmp_path, monkeypatch
    ):
        import signal as _signal

        monkeypatch.setenv("GATE_CODEX_THREAD_ID", "tid")
        monkeypatch.setenv("GATE_FIX_WORKSPACE", str(tmp_path))
        monkeypatch.setattr("sys.argv", ["gate-code", "implement"])
        monkeypatch.setattr("sys.stdin", type("S", (), {"read": lambda self=None: "directions"})())

        registered: dict[int, object] = {}

        def capture_signal(signum, handler):
            registered[signum] = handler
            return _signal.SIG_DFL

        with patch("gate.code.signal.signal", side_effect=capture_signal), \
             patch("gate.code.run_code_stage", return_value=0):
            try:
                main()
            except SystemExit:
                pass

        assert _signal.SIGTERM in registered
        assert _signal.SIGHUP in registered

    def test_signal_handler_terminates_active_codex_and_exits(
        self, tmp_path, monkeypatch
    ):
        """Installed handler must call codex.terminate_active(SIGTERM)
        and then exit with 128 + signum."""
        import signal as _signal

        monkeypatch.setenv("GATE_CODEX_THREAD_ID", "tid")
        monkeypatch.setenv("GATE_FIX_WORKSPACE", str(tmp_path))
        monkeypatch.setattr("sys.argv", ["gate-code", "implement"])
        monkeypatch.setattr("sys.stdin", type("S", (), {"read": lambda self=None: "directions"})())

        captured: dict[int, object] = {}

        def capture_signal(signum, handler):
            captured[signum] = handler
            return _signal.SIG_DFL

        with patch("gate.code.signal.signal", side_effect=capture_signal), \
             patch("gate.code.run_code_stage", return_value=0):
            try:
                main()
            except SystemExit:
                pass

        handler = captured[_signal.SIGTERM]
        with patch("gate.code._codex.terminate_active") as mock_term, \
             patch("gate.code.sys.exit") as mock_exit:
            handler(_signal.SIGTERM, None)
            mock_term.assert_called_once_with(_signal.SIGTERM)
            mock_exit.assert_called_once_with(128 + _signal.SIGTERM)

    @patch("gate.code._load_prompt_template", return_value=None)
    def test_missing_template(self, mock_load, tmp_path):
        result = run_code_stage("prep", "request", tmp_path, "thread-123")
        assert result == 1


# ── Review-warning regressions ──────────────────────────────


class TestRunCodeStageConfigThreading:
    """Regression: run_code_stage must accept a pre-loaded config dict
    and thread it to resolve_repo_config, avoiding hidden load_config()
    calls from the inner helper.
    """

    def test_accepts_config_kw(self):
        import inspect

        from gate.code import run_code_stage
        sig = inspect.signature(run_code_stage)
        assert "config" in sig.parameters

    def test_threads_config_to_resolve_repo_config(self, tmp_path):
        from unittest.mock import patch
        # Workspace has pr-metadata.json with a repo name
        (tmp_path / "pr-metadata.json").write_text('{"repo": "org/repo"}')

        with patch("gate.code._load_prompt_template", return_value="ok $request"), \
             patch("gate.code.run_codex", return_value=(0, ["codex"])), \
             patch("gate.profiles.resolve_profile", return_value={}), \
             patch("gate.config.resolve_repo_config") as rrc, \
             patch("gate.config.load_config") as load:
            rrc.return_value = {"repo": {"name": "org/repo"}}
            cfg = {"repos": [{"name": "org/repo"}]}
            from gate.code import run_code_stage
            run_code_stage("prep", "req", tmp_path, "tid", config=cfg)
            # resolve_repo_config got our config, no hidden reload
            rrc.assert_called_once()
            assert rrc.call_args.args[0] == "org/repo"
            assert rrc.call_args.args[1] is cfg
            load.assert_not_called()


class TestRunCodeStageErrorLogging:
    """Regression: corrupt pr-metadata.json must emit a log line instead
    of being silently swallowed by a bare ``except Exception: pass``.
    """

    def test_corrupt_pr_metadata_logs_exception(self, tmp_path, caplog):
        import logging
        from unittest.mock import patch
        (tmp_path / "pr-metadata.json").write_text("{not json")
        with patch("gate.code._load_prompt_template", return_value="ok $request"), \
             patch("gate.code.run_codex", return_value=(0, ["codex"])), \
             patch("gate.profiles.resolve_profile", return_value={}):
            with caplog.at_level(logging.WARNING, logger="gate.code"):
                from gate.code import run_code_stage
                run_code_stage("prep", "req", tmp_path, "tid", config={})
        assert any(
            "pr-metadata" in rec.message or "failed to read" in rec.message
            for rec in caplog.records
        )

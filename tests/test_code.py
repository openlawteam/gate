"""Tests for gate.code module."""

from unittest.mock import patch

from gate.code import ALLOWED_STAGES, _next_version, run_code_stage


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

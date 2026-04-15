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

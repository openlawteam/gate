"""Tests for hopper-mode config accessors in gate.config.

Coverage matrix:

- ``get_fix_pipeline_mode`` resolves from ``[fix_pipeline]``, falls back to
  ``[repo].fix_pipeline_mode``, defaults to ``"polish_legacy"``, rejects
  invalid values.
- ``get_fix_pipeline_max_wall_clock_s`` / ``senior_session_timeout_s`` /
  ``max_subscope_iterations`` parse ints and defend against garbage.
"""

from gate.config import (
    get_fix_pipeline_max_subscope_iterations,
    get_fix_pipeline_max_wall_clock_s,
    get_fix_pipeline_mode,
    get_fix_pipeline_senior_session_timeout_s,
)


class TestGetFixPipelineMode:
    def test_default_is_polish_legacy(self):
        assert get_fix_pipeline_mode({}) == "polish_legacy"

    def test_fix_pipeline_section_wins(self):
        assert (
            get_fix_pipeline_mode({"fix_pipeline": {"mode": "hopper"}})
            == "hopper"
        )

    def test_repo_mode_is_honoured(self):
        assert (
            get_fix_pipeline_mode({"repo": {"fix_pipeline_mode": "hopper"}})
            == "hopper"
        )

    def test_fix_pipeline_overrides_repo(self):
        cfg = {
            "fix_pipeline": {"mode": "hopper"},
            "repo": {"fix_pipeline_mode": "polish_legacy"},
        }
        assert get_fix_pipeline_mode(cfg) == "hopper"

    def test_invalid_mode_falls_back_to_default(self):
        assert (
            get_fix_pipeline_mode({"fix_pipeline": {"mode": "bogus"}})
            == "polish_legacy"
        )

    def test_case_insensitive(self):
        assert (
            get_fix_pipeline_mode({"fix_pipeline": {"mode": "HOPPER"}})
            == "hopper"
        )

    def test_garbage_config_does_not_crash(self):
        assert get_fix_pipeline_mode(None) == "polish_legacy"  # type: ignore[arg-type]
        assert get_fix_pipeline_mode("not a dict") == "polish_legacy"  # type: ignore[arg-type]


class TestGetFixPipelineMaxWallClockS:
    def test_default(self):
        assert get_fix_pipeline_max_wall_clock_s({}) == 14400

    def test_overrides(self):
        assert (
            get_fix_pipeline_max_wall_clock_s(
                {"fix_pipeline": {"max_wall_clock_s": 60}}
            )
            == 60
        )

    def test_bad_value_falls_back(self):
        assert (
            get_fix_pipeline_max_wall_clock_s(
                {"fix_pipeline": {"max_wall_clock_s": "not-an-int"}}
            )
            == 14400
        )


class TestGetFixPipelineSeniorSessionTimeoutS:
    def test_default(self):
        assert get_fix_pipeline_senior_session_timeout_s({}) == 7200

    def test_overrides(self):
        assert (
            get_fix_pipeline_senior_session_timeout_s(
                {"fix_pipeline": {"senior_session_timeout_s": 900}}
            )
            == 900
        )


class TestGetFixPipelineMaxSubscopeIterations:
    def test_default(self):
        assert get_fix_pipeline_max_subscope_iterations({}) == 3

    def test_overrides(self):
        assert (
            get_fix_pipeline_max_subscope_iterations(
                {"fix_pipeline": {"max_subscope_iterations": 5}}
            )
            == 5
        )

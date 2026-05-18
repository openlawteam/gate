"""Tests for hopper-mode config accessors in gate.config.

Coverage matrix:

- ``get_fix_pipeline_mode`` resolves from ``[fix_pipeline]``, falls back to
  ``[repo].fix_pipeline_mode``, defaults to ``"polish_legacy"``, rejects
  invalid values.
- ``get_fix_pipeline_max_wall_clock_s`` / ``senior_session_timeout_s`` /
  ``max_subscope_iterations`` parse ints and defend against garbage.
"""

from gate.config import (
    get_fix_max_iterations,
    get_fix_pipeline_max_subscope_iterations,
    get_fix_pipeline_max_wall_clock_s,
    get_fix_pipeline_mode,
    get_fix_pipeline_senior_session_timeout_s,
    get_pre_push_config,
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


class TestGetFixMaxIterations:
    """Outer fix loop iteration budget — bumped from 2 to 3 to absorb a
    pre-push test regression in iter 3 (PR #399 motivation).
    """

    def test_default_is_3(self):
        assert get_fix_max_iterations({}) == 3

    def test_fix_pipeline_section_wins(self):
        assert (
            get_fix_max_iterations({"fix_pipeline": {"max_iterations": 5}})
            == 5
        )

    def test_repo_override(self):
        assert (
            get_fix_max_iterations({"repo": {"max_fix_iterations": 4}})
            == 4
        )

    def test_fix_pipeline_overrides_repo(self):
        cfg = {
            "fix_pipeline": {"max_iterations": 10},
            "repo": {"max_fix_iterations": 2},
        }
        assert get_fix_max_iterations(cfg) == 10

    def test_bad_value_falls_back(self):
        assert (
            get_fix_max_iterations(
                {"fix_pipeline": {"max_iterations": "not-an-int"}}
            )
            == 3
        )

    def test_garbage_config_does_not_crash(self):
        assert get_fix_max_iterations(None) == 3  # type: ignore[arg-type]
        assert get_fix_max_iterations("not a dict") == 3  # type: ignore[arg-type]


class TestGetPrePushConfig:
    """Strict pre-push gate config (PR #399)."""

    def test_defaults(self):
        """Strict-by-default: opt-out only via ``pre_push_disable``."""
        cfg = get_pre_push_config({})
        assert cfg["strict"] is True
        assert cfg["disable"] is False
        assert cfg["cmds"] == []
        assert cfg["timeout_s"] == 600

    def test_reads_strict_flag(self):
        cfg = get_pre_push_config(
            {"repo": {"build": {"pre_push_strict": False}}}
        )
        assert cfg["strict"] is False

    def test_reads_disable_flag(self):
        cfg = get_pre_push_config(
            {"repo": {"build": {"pre_push_disable": True}}}
        )
        assert cfg["disable"] is True

    def test_reads_cmds_list(self):
        cfg = get_pre_push_config({
            "repo": {"build": {"pre_push_verify_cmds": ["npm test", "npm run drift"]}}
        })
        assert cfg["cmds"] == ["npm test", "npm run drift"]

    def test_reads_timeout(self):
        cfg = get_pre_push_config(
            {"repo": {"build": {"pre_push_timeout_s": 1200}}}
        )
        assert cfg["timeout_s"] == 1200

    def test_garbage_timeout_falls_back(self):
        cfg = get_pre_push_config(
            {"repo": {"build": {"pre_push_timeout_s": "not-an-int"}}}
        )
        assert cfg["timeout_s"] == 600

    def test_garbage_cmds_falls_back_to_empty(self):
        cfg = get_pre_push_config(
            {"repo": {"build": {"pre_push_verify_cmds": "not-a-list"}}}
        )
        assert cfg["cmds"] == []

    def test_none_config_does_not_crash(self):
        cfg = get_pre_push_config(None)  # type: ignore[arg-type]
        assert cfg["strict"] is True

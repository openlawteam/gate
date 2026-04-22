"""Unit tests for gate.external_checks (PR B.2)."""

from __future__ import annotations

import json
import threading
from unittest.mock import patch

import pytest

from gate import external_checks as ec


class TestRequiredCheck:
    def test_substring_matches_case_insensitive(self):
        req = ec.RequiredCheck(name="Vercel")
        assert req.matches("Vercel – Preview")
        assert req.matches("vercel – production")
        assert not req.matches("Netlify")

    def test_exact_match_does_not_substring(self):
        req = ec.RequiredCheck(name="Vercel – Preview", match="exact")
        assert req.matches("Vercel – Preview")
        assert not req.matches("Vercel – Production")

    def test_empty_name_matches_nothing(self):
        req = ec.RequiredCheck(name="")
        assert not req.matches("Vercel – Preview")


class TestNormaliseConclusion:
    def test_success(self):
        assert ec._normalise_conclusion("success", "completed") == ec.CONCLUSION_SUCCESS
        assert ec._normalise_conclusion("SUCCESS", None) == ec.CONCLUSION_SUCCESS

    def test_failure_family(self):
        for v in ("failure", "failed", "timed_out", "cancelled",
                  "action_required", "error"):
            assert ec._normalise_conclusion(v, None) == ec.CONCLUSION_FAILURE

    def test_in_progress_maps_to_pending(self):
        assert ec._normalise_conclusion(None, "in_progress") == ec.CONCLUSION_PENDING
        assert ec._normalise_conclusion(None, "queued") == ec.CONCLUSION_PENDING
        assert ec._normalise_conclusion("", "") == ec.CONCLUSION_PENDING

    def test_neutral_family(self):
        assert ec._normalise_conclusion("skipped", None) == ec.CONCLUSION_NEUTRAL
        assert ec._normalise_conclusion("stale", None) == ec.CONCLUSION_NEUTRAL

    def test_unknown(self):
        assert ec._normalise_conclusion("garbage", "ok?") == ec.CONCLUSION_UNKNOWN


class TestParseRequired:
    def test_dict_entries(self):
        parsed = ec._parse_required([
            {"name": "Vercel", "policy": "blocking"},
            {"name": "tests", "policy": "advisory", "match": "exact"},
        ])
        assert [r.name for r in parsed] == ["Vercel", "tests"]
        assert parsed[0].policy == "blocking"
        assert parsed[1].policy == "advisory"
        assert parsed[1].match == "exact"

    def test_string_entries_default_policy(self):
        parsed = ec._parse_required(["Vercel", "tests"])
        assert all(r.policy == "blocking" for r in parsed)

    def test_unknown_policy_defaults_to_blocking(self):
        parsed = ec._parse_required([{"name": "foo", "policy": "maybe"}])
        assert parsed[0].policy == "blocking"

    def test_empty_and_non_list_returns_empty(self):
        assert ec._parse_required([]) == []
        assert ec._parse_required(None) == []  # type: ignore[arg-type]
        assert ec._parse_required({"name": "x"}) == []  # type: ignore[arg-type]


class TestFetchCheckState:
    def test_merges_both_endpoints(self):
        modern_runs = {"check_runs": [
            {"name": "test", "status": "completed", "conclusion": "success",
             "details_url": "https://gh/test"},
        ]}
        legacy = {"statuses": [
            {"context": "Vercel – Preview", "state": "failure",
             "target_url": "https://vercel/u"},
        ]}

        def _fake_gh(args, timeout=None):  # noqa: ARG001
            if "check-runs" in args[1]:
                return json.dumps(modern_runs)
            if args[1].endswith("/status"):
                return json.dumps(legacy)
            return ""

        with patch("gate.external_checks.gh._gh", side_effect=_fake_gh):
            checks = ec.fetch_check_state("sha", "o/r")
        assert set(checks) == {"test", "Vercel – Preview"}
        assert checks["test"].conclusion == ec.CONCLUSION_SUCCESS
        assert checks["test"].source == "check-runs"
        assert checks["Vercel – Preview"].conclusion == ec.CONCLUSION_FAILURE
        assert checks["Vercel – Preview"].source == "statuses"

    def test_modern_wins_on_name_collision(self):
        modern = {"check_runs": [
            {"name": "dup", "status": "completed", "conclusion": "success"},
        ]}
        legacy = {"statuses": [
            {"context": "dup", "state": "failure"},
        ]}

        def _fake_gh(args, timeout=None):  # noqa: ARG001
            return json.dumps(modern if "check-runs" in args[1] else legacy)

        with patch("gate.external_checks.gh._gh", side_effect=_fake_gh):
            checks = ec.fetch_check_state("sha", "o/r")
        assert checks["dup"].source == "check-runs"
        assert checks["dup"].conclusion == ec.CONCLUSION_SUCCESS

    def test_both_endpoints_fail_returns_empty(self):
        import subprocess

        def _fail(args, timeout=None):  # noqa: ARG001
            raise subprocess.CalledProcessError(1, "gh", stderr="boom")

        with patch("gate.external_checks.gh._gh", side_effect=_fail):
            assert ec.fetch_check_state("sha", "o/r") == {}


class TestClassify:
    def _make(self, name, conclusion, status="completed"):
        return ec.CheckState(name=name, conclusion=conclusion, status=status)

    def test_blocking_failure(self):
        checks = {"Vercel – Preview": self._make("Vercel – Preview", ec.CONCLUSION_FAILURE)}
        result = ec.classify(checks, [ec.RequiredCheck(name="Vercel")])
        assert result.has_blocking_failure
        assert not result.has_blocking_pending
        assert result.blocking_failures[0].name == "Vercel – Preview"

    def test_blocking_pending_not_counted_as_failure(self):
        checks = {
            "Vercel – Preview": self._make(
                "Vercel – Preview", ec.CONCLUSION_PENDING, "in_progress",
            ),
        }
        result = ec.classify(checks, [ec.RequiredCheck(name="Vercel")])
        assert not result.has_blocking_failure
        assert result.has_blocking_pending

    def test_advisory_failure_not_blocking(self):
        checks = {"tests": self._make("tests", ec.CONCLUSION_FAILURE)}
        result = ec.classify(
            checks, [ec.RequiredCheck(name="tests", policy="advisory")],
        )
        assert not result.has_blocking_failure
        assert result.advisory_failures

    def test_unknown_when_no_matching_check(self):
        checks = {"Random": self._make("Random", ec.CONCLUSION_SUCCESS)}
        result = ec.classify(checks, [ec.RequiredCheck(name="Vercel")])
        assert not result.has_blocking_failure
        assert len(result.unknown) == 1
        assert result.unknown[0].name == "Vercel"

    def test_accepts_raw_toml_list(self):
        checks = {"Vercel – Preview": self._make("Vercel – Preview", ec.CONCLUSION_FAILURE)}
        result = ec.classify(checks, [{"name": "Vercel", "policy": "blocking"}])
        assert result.has_blocking_failure


class TestWaitForPending:
    def test_returns_immediately_when_already_terminal(self):
        checks = {"tests": ec.CheckState(
            name="tests", conclusion=ec.CONCLUSION_SUCCESS, status="completed",
        )}
        with patch("gate.external_checks.fetch_check_state", return_value=checks):
            result = ec.wait_for_pending(
                "sha", "o/r",
                required=[ec.RequiredCheck(name="tests")],
                cancelled=threading.Event(),
                timeout_s=0.5,
                poll_interval_s=0.01,
            )
        assert not result.has_blocking_pending
        assert not result.has_blocking_failure

    def test_polls_until_green(self):
        sequence = [
            {"tests": ec.CheckState(name="tests",
                                    conclusion=ec.CONCLUSION_PENDING,
                                    status="in_progress")},
            {"tests": ec.CheckState(name="tests",
                                    conclusion=ec.CONCLUSION_PENDING,
                                    status="in_progress")},
            {"tests": ec.CheckState(name="tests",
                                    conclusion=ec.CONCLUSION_SUCCESS,
                                    status="completed")},
        ]
        calls = {"n": 0}

        def _fake(_sha, _repo):
            i = min(calls["n"], len(sequence) - 1)
            calls["n"] += 1
            return sequence[i]

        with patch("gate.external_checks.fetch_check_state", side_effect=_fake):
            result = ec.wait_for_pending(
                "sha", "o/r",
                required=[ec.RequiredCheck(name="tests")],
                cancelled=threading.Event(),
                timeout_s=2.0,
                poll_interval_s=0.01,
            )
        assert calls["n"] >= 2
        assert not result.has_blocking_pending

    def test_cancel_short_circuits(self):
        event = threading.Event()
        event.set()
        with patch(
            "gate.external_checks.fetch_check_state",
            return_value={
                "tests": ec.CheckState(
                    name="tests", conclusion=ec.CONCLUSION_PENDING,
                    status="in_progress",
                ),
            },
        ):
            result = ec.wait_for_pending(
                "sha", "o/r",
                required=[ec.RequiredCheck(name="tests")],
                cancelled=event,
                timeout_s=5.0,
                poll_interval_s=0.01,
            )
        # We return the initial snapshot; still pending.
        assert result.has_blocking_pending

    def test_times_out_leaves_blocking_pending(self):
        with patch(
            "gate.external_checks.fetch_check_state",
            return_value={
                "tests": ec.CheckState(
                    name="tests", conclusion=ec.CONCLUSION_PENDING,
                    status="in_progress",
                ),
            },
        ):
            result = ec.wait_for_pending(
                "sha", "o/r",
                required=[ec.RequiredCheck(name="tests")],
                cancelled=threading.Event(),
                timeout_s=0.05,
                poll_interval_s=0.01,
            )
        assert result.has_blocking_pending


class TestResolvers:
    def test_enabled_defaults_true(self):
        assert ec.external_checks_enabled({}) is True
        assert ec.external_checks_enabled({"external_checks": {"enabled": False}}) is False

    def test_wait_seconds_precedence(self):
        cfg = {"external_checks": {"wait_seconds_default": 120}}
        repo_cfg = {"external_check_wait_seconds": 300}
        assert ec.get_wait_seconds(cfg, repo_cfg) == 300
        assert ec.get_wait_seconds(cfg, {}) == 120
        assert ec.get_wait_seconds({}, {}) == ec.DEFAULT_WAIT_SECONDS

    def test_recheck_minutes_precedence(self):
        cfg = {"external_checks": {"recheck_minutes_default": 15}}
        repo_cfg = {"external_check_recheck_minutes": 45}
        assert ec.get_recheck_minutes(cfg, repo_cfg) == 45
        assert ec.get_recheck_minutes(cfg, {}) == 15
        assert ec.get_recheck_minutes({}, {}) == ec.DEFAULT_RECHECK_MINUTES


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

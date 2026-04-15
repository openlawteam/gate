"""Tests for gate.builder module."""

from pathlib import Path

from gate.builder import _parse_lint, _parse_test, _parse_tsc, compare_builds, run_build


class TestParseTsc:
    def test_clean_build(self):
        result = _parse_tsc("", 0)
        assert result["pass"] is True
        assert result["error_count"] == 0

    def test_errors(self):
        log = "src/foo.ts(10,5): error TS2322: Type 'string' is not assignable.\n"
        result = _parse_tsc(log, 1)
        assert result["pass"] is False
        assert result["error_count"] == 1
        assert result["errors"][0]["file"] == "src/foo.ts"
        assert result["errors"][0]["line"] == 10
        assert result["errors"][0]["code"] == "TS2322"

    def test_multiple_errors(self):
        log = (
            "a.ts(1,1): error TS1: msg1\n"
            "b.ts(2,3): error TS2: msg2\n"
        )
        result = _parse_tsc(log, 1)
        assert result["error_count"] == 2


class TestParseLint:
    def test_clean(self):
        result = _parse_lint("", 0)
        assert result["pass"] is True
        assert result["error_count"] == 0
        assert result["warning_count"] == 0

    def test_warnings(self):
        log = "src/foo.ts\n  10:5  warning  Unused variable  no-unused-vars\n"
        result = _parse_lint(log, 0)
        assert result["warning_count"] == 1
        assert result["warnings"][0]["rule"] == "no-unused-vars"

    def test_errors(self):
        log = "src/bar.ts\n  5:1  error  Missing return  consistent-return\n"
        result = _parse_lint(log, 1)
        assert result["pass"] is False
        assert result["error_count"] == 1


class TestParseTest:
    def test_all_passed(self):
        log = "Tests 42 passed (42)"
        result = _parse_test(log, 0)
        assert result["pass"] is True
        assert result["passed"] == 42
        assert result["total"] == 42

    def test_failures(self):
        log = "Tests 40 passed | 2 failed (42)\nFAIL src/broken.test.ts\n"
        result = _parse_test(log, 1)
        assert result["pass"] is False
        assert result["failed"] == 2
        assert len(result["failures"]) == 1

    def test_no_summary(self):
        log = "3 passed\n1 failed"
        result = _parse_test(log, 1)
        assert result["passed"] == 3
        assert result["failed"] == 1


class TestRunBuildSkip:
    def test_run_build_skips_without_package_json(self, tmp_path):
        result = run_build(tmp_path)
        assert result["overall_pass"] is True
        assert result["skipped"] is True
        assert result["blocking_issues"] == []
        assert result["typescript"]["pass"] is True
        assert result["lint"]["pass"] is True
        assert result["tests"]["pass"] is True

    def test_run_build_does_not_skip_with_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        result = run_build(tmp_path)
        assert "skipped" not in result or result["skipped"] is not True


class TestCompareBuilds:
    def test_passing_build_unchanged(self):
        build = {"overall_pass": True, "typescript": {"pass": True}, "lint": {"pass": True}}
        result = compare_builds({}, build)
        assert result["overall_pass"] is True

    def test_pre_existing_failures_accepted(self):
        before = {
            "typescript": {"pass": False},
            "lint": {"pass": True},
            "tests": {"failed": 2},
        }
        after = {
            "overall_pass": False,
            "typescript": {"pass": False},
            "lint": {"pass": True},
            "tests": {"failed": 1},
        }
        result = compare_builds(before, after)
        assert result["overall_pass"] is True
        assert result["pre_existing_failures_accepted"] is True

    def test_new_failures_not_accepted(self):
        before = {"typescript": {"pass": True}, "lint": {"pass": True}, "tests": {"failed": 0}}
        after = {
            "overall_pass": False,
            "typescript": {"pass": False},
            "lint": {"pass": True},
            "tests": {"failed": 0},
        }
        result = compare_builds(before, after)
        assert result["overall_pass"] is False

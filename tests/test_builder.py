"""Tests for gate.builder module."""


from gate.builder import (
    _parse_generic,
    _parse_generic_test,
    _parse_lint,
    _parse_pytest,
    _parse_test,
    _parse_tsc,
    compare_builds,
    compile_build,
    run_build,
)


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


class TestParseGeneric:
    def test_passing(self):
        result = _parse_generic("all good", 0)
        assert result["pass"] is True
        assert result["error_count"] == 0

    def test_failing(self):
        result = _parse_generic("error: something broke\nline2", 1)
        assert result["pass"] is False
        assert result["error_count"] == 2


class TestParseGenericTest:
    def test_passing(self):
        result = _parse_generic_test("ok", 0)
        assert result["pass"] is True
        assert result["failed"] == 0

    def test_failing(self):
        result = _parse_generic_test("FAILED", 1)
        assert result["pass"] is False
        assert result["failed"] == 1


class TestRunBuildSkip:
    def test_run_build_skips_without_commands(self, tmp_path):
        result = run_build(tmp_path)
        assert result["overall_pass"] is True
        assert result["skipped"] is True
        assert result["blocking_issues"] == []
        assert result["typecheck"]["pass"] is True
        assert result["lint"]["pass"] is True
        assert result["tests"]["pass"] is True

    def test_run_build_does_not_skip_with_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        result = run_build(tmp_path)
        assert "skipped" not in result or result["skipped"] is not True


class TestCompileBuild:
    def test_node_project_uses_structured_parsers(self):
        result = compile_build(
            typecheck_log="src/foo.ts(1,1): error TS2322: msg",
            typecheck_exit=1,
            lint_log="", lint_exit=0,
            test_log="", test_exit=0,
            project_type="node",
            typecheck_tool="npx",
        )
        assert result["typecheck"]["error_count"] == 1
        assert result["typecheck"]["errors"][0]["code"] == "TS2322"

    def test_python_project_uses_generic_parsers(self):
        result = compile_build(
            typecheck_log="", typecheck_exit=0,
            lint_log="error: something\n", lint_exit=1,
            test_log="", test_exit=0,
            project_type="python",
            lint_tool="ruff",
        )
        assert result["lint"]["pass"] is False
        assert result["lint"]["tool"] == "ruff"

    def test_has_project_type_in_result(self):
        result = compile_build(
            typecheck_log="", typecheck_exit=0,
            lint_log="", lint_exit=0,
            test_log="", test_exit=0,
            project_type="go",
        )
        assert result["project_type"] == "go"
        assert result["overall_pass"] is True


class TestCompareBuilds:
    def test_passing_build_unchanged(self):
        build = {"overall_pass": True, "typecheck": {"pass": True}, "lint": {"pass": True}}
        result = compare_builds({}, build)
        assert result["overall_pass"] is True

    def test_pre_existing_failures_accepted(self):
        before = {
            "typecheck": {"pass": False},
            "lint": {"pass": True},
            "tests": {"failed": 2},
        }
        after = {
            "overall_pass": False,
            "typecheck": {"pass": False},
            "lint": {"pass": True},
            "tests": {"failed": 1},
        }
        result = compare_builds(before, after)
        assert result["overall_pass"] is True
        assert result["pre_existing_failures_accepted"] is True

    def test_new_failures_not_accepted(self):
        before = {"typecheck": {"pass": True}, "lint": {"pass": True}, "tests": {"failed": 0}}
        after = {
            "overall_pass": False,
            "typecheck": {"pass": False},
            "lint": {"pass": True},
            "tests": {"failed": 0},
        }
        result = compare_builds(before, after)
        assert result["overall_pass"] is False

    def test_backward_compat_typescript_key(self):
        """Old build.json files use 'typescript' key; compare_builds handles both."""
        before = {
            "typescript": {"pass": False},
            "lint": {"pass": True},
            "tests": {"failed": 1},
        }
        after = {
            "overall_pass": False,
            "typecheck": {"pass": False},
            "lint": {"pass": True},
            "tests": {"failed": 1},
        }
        result = compare_builds(before, after)
        assert result["overall_pass"] is True
        assert result["pre_existing_failures_accepted"] is True


class TestParsePytest:
    def test_all_passed(self):
        log = "tests passed\n624 passed in 95.89s (0:01:35)\n"
        result = _parse_pytest(log, 0)
        assert result["pass"] is True
        assert result["passed"] == 624
        assert result["failed"] == 0
        assert result["skipped"] == 0
        assert result["total"] == 624

    def test_passed_and_skipped(self):
        log = "624 passed, 6 skipped in 95.89s (0:01:35)\n"
        result = _parse_pytest(log, 0)
        assert result["passed"] == 624
        assert result["skipped"] == 6
        assert result["total"] == 630

    def test_passed_and_failed(self):
        log = "1 failed, 45 passed in 35.69s\n"
        result = _parse_pytest(log, 1)
        assert result["pass"] is False
        assert result["passed"] == 45
        assert result["failed"] == 1
        assert result["total"] == 46

    def test_failed_passed_skipped_all_present(self):
        log = "2 failed, 597 passed, 6 skipped in 100s\n"
        result = _parse_pytest(log, 1)
        assert result["passed"] == 597
        assert result["failed"] == 2
        assert result["skipped"] == 6
        assert result["total"] == 605

    def test_errors_counted_as_failures(self):
        log = "3 errors in 5.0s\n"
        result = _parse_pytest(log, 1)
        # Collection errors treated as failures for pass/fail semantics
        assert result["failed"] == 3
        assert result["total"] == 3

    def test_failed_lines_captured(self):
        log = (
            "FAILED tests/test_foo.py::test_bar - AssertionError\n"
            "FAILED tests/test_baz.py::test_qux - TypeError\n"
            "2 failed, 10 passed in 1s\n"
        )
        result = _parse_pytest(log, 1)
        assert len(result["failures"]) == 2
        assert result["failures"][0]["file"] == "tests/test_foo.py::test_bar"

    def test_empty_log(self):
        result = _parse_pytest("", 0)
        assert result["passed"] == 0
        assert result["failed"] == 0
        assert result["total"] == 0
        assert result["pass"] is True

    def test_pytest_not_installed_output(self):
        # Regression: previously _parse_generic_test returned 0/0 even for
        # successful pytest runs. _parse_pytest should find the counts.
        log = "pytest ran\n\n100 passed in 5.00s\n"
        result = _parse_pytest(log, 0)
        assert result["passed"] == 100


class TestCompileBuildPython:
    def test_python_uses_pytest_parser(self):
        """Regression: Python project should parse real pytest counts,
        not return 0/0 like the old generic test parser did."""
        result = compile_build(
            typecheck_log="", typecheck_exit=0,
            lint_log="", lint_exit=0,
            test_log="624 passed, 6 skipped in 95.89s\n",
            test_exit=0,
            project_type="python",
            test_tool="python",
        )
        assert result["tests"]["passed"] == 624
        assert result["tests"]["total"] == 630
        assert result["tests"]["skipped"] == 6

    def test_python_failed_tests_captured(self):
        result = compile_build(
            typecheck_log="", typecheck_exit=0,
            lint_log="", lint_exit=0,
            test_log="1 failed, 20 passed in 3.5s\n",
            test_exit=1,
            project_type="python",
            test_tool="python",
        )
        assert result["tests"]["pass"] is False
        assert result["tests"]["failed"] == 1
        assert result["tests"]["passed"] == 20

    def test_python_no_tests_configured_still_zero(self):
        """If test_log is empty (no pytest run), counts should be 0."""
        result = compile_build(
            typecheck_log="", typecheck_exit=0,
            lint_log="", lint_exit=0,
            test_log="", test_exit=0,
            project_type="python",
            test_tool="",
        )
        assert result["tests"]["passed"] == 0
        assert result["tests"]["total"] == 0

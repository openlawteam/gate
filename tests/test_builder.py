"""Tests for gate.builder module."""

import subprocess
from unittest.mock import patch

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

# ── Real-world log fixtures ──────────────────────────────────
#
# Captured by running ``npm run lint:check`` (= ``eslint .``) against
# openlawteam/adin-chat at the pre-fix commit ``2a1b470c`` of PR #223 —
# the commit whose Vercel build failed with 5 ESLint errors while Gate
# approved the PR. Embedded verbatim so the parser and the
# compile_build integration can be asserted against the exact bytes
# the real Gate builder consumed for that PR.

# fmt: off
PR223_ESLINT_LOG = (
    "\n"
    "> adin-platform@0.1.0 lint:check\n"
    "> eslint .\n"
    "\n"
    "\n"
    "/private/tmp/adin-chat-audit/src/app/chat/components/ChatDesktopHeaderSection.tsx\n"
    "  114:15  error  Do not use an `<a>` element to navigate to `/login/`. Use `<Link />` from `next/link` instead. See: https://nextjs.org/docs/messages/no-html-link-for-pages  @next/next/no-html-link-for-pages\n"  # noqa: E501
    "  122:15  error  Do not use an `<a>` element to navigate to `/login/`. Use `<Link />` from `next/link` instead. See: https://nextjs.org/docs/messages/no-html-link-for-pages  @next/next/no-html-link-for-pages\n"  # noqa: E501
    "\n"
    "/private/tmp/adin-chat-audit/src/app/chat/components/MobileTopBar.tsx\n"
    "  260:13  error  Do not use an `<a>` element to navigate to `/login/`. Use `<Link />` from `next/link` instead. See: https://nextjs.org/docs/messages/no-html-link-for-pages  @next/next/no-html-link-for-pages\n"  # noqa: E501
    "  268:13  error  Do not use an `<a>` element to navigate to `/login/`. Use `<Link />` from `next/link` instead. See: https://nextjs.org/docs/messages/no-html-link-for-pages  @next/next/no-html-link-for-pages\n"  # noqa: E501
    "\n"
    "/private/tmp/adin-chat-audit/src/app/chat/components/WelcomeAnimation/WelcomeWaitingPhase.tsx\n"
    "  228:13  error  Do not use an `<a>` element to navigate to `/login/`. Use `<Link />` from `next/link` instead. See: https://nextjs.org/docs/messages/no-html-link-for-pages  @next/next/no-html-link-for-pages\n"  # noqa: E501
    "\n"
    "\u2716 5 problems (5 errors, 0 warnings)\n"
    "\n"
)
# fmt: on


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


class TestPr223Regression:
    """PR #223 regression: real-world ``eslint .`` log must parse cleanly.

    Gate approved PR #223 despite Vercel's compile failure. The post-mortem
    (see CHANGELOG, fix/lint-silent-dismissal) showed the regex actually
    does parse this log correctly today; these tests pin that invariant
    so no future change can silently regress it without breaking a test.
    """

    def test_all_five_errors_parsed(self):
        result = _parse_lint(PR223_ESLINT_LOG, 1)
        assert result["pass"] is False
        assert result["error_count"] == 5
        assert result["warning_count"] == 0

    def test_every_finding_has_the_expected_rule(self):
        result = _parse_lint(PR223_ESLINT_LOG, 1)
        rules = {e["rule"] for e in result["errors"]}
        assert rules == {"@next/next/no-html-link-for-pages"}

    def test_file_line_mapping_is_correct(self):
        result = _parse_lint(PR223_ESLINT_LOG, 1)
        coords = {(e["file"].rsplit("/", 1)[-1], e["line"]) for e in result["errors"]}
        assert coords == {
            ("ChatDesktopHeaderSection.tsx", 114),
            ("ChatDesktopHeaderSection.tsx", 122),
            ("MobileTopBar.tsx", 260),
            ("MobileTopBar.tsx", 268),
            ("WelcomeWaitingPhase.tsx", 228),
        }

    def test_compile_build_surfaces_five_lint_errors(self):
        result = compile_build(
            typecheck_log="", typecheck_exit=0,
            lint_log=PR223_ESLINT_LOG, lint_exit=1,
            test_log="", test_exit=0,
            project_type="node",
        )
        assert result["overall_pass"] is False
        assert result["lint"]["error_count"] == 5
        assert result["lint"]["parse_failure"] is False
        assert any("5 lint errors" in b for b in result["blocking_issues"])


class TestParseLintDefenseInDepth:
    """Forward-looking parser coverage for other common lint formats."""

    def test_next_lint_wrapped_format(self):
        log = (
            "./src/app/page.tsx\n"
            "  12:3  Error: Do not use `<a>` here.  @next/next/no-html-link-for-pages\n"
        )
        result = _parse_lint(log, 1)
        assert result["error_count"] == 1
        assert result["errors"][0]["rule"] == "@next/next/no-html-link-for-pages"
        assert result["errors"][0]["severity"] == "error"
        assert result["errors"][0]["file"] == "src/app/page.tsx"

    def test_capitalised_warning_with_colon(self):
        log = (
            "./pkg/util.ts\n"
            "  5:1  Warning: prefer const  prefer-const\n"
        )
        result = _parse_lint(log, 0)
        assert result["warning_count"] == 1
        assert result["warnings"][0]["severity"] == "warning"

    def test_uppercase_severity(self):
        log = (
            "src/foo.ts\n"
            "  7:2  ERROR  bad stuff  no-bad\n"
        )
        result = _parse_lint(log, 1)
        assert result["error_count"] == 1

    def test_mjs_cjs_paths_accepted(self):
        log = (
            "scripts/build.mjs\n"
            "  1:1  error  broken  some-rule\n"
            "scripts/legacy.cjs\n"
            "  2:2  warning  soft  soft-rule\n"
        )
        result = _parse_lint(log, 1)
        assert result["error_count"] == 1
        assert result["warning_count"] == 1

    def test_stylish_format_backwards_compat(self):
        """Baseline stylish ESLint format must still work unchanged."""
        log = "src/bar.ts\n  5:1  error  Missing return  consistent-return\n"
        result = _parse_lint(log, 1)
        assert result["error_count"] == 1
        assert result["errors"][0]["rule"] == "consistent-return"


class TestCompileBuildParseFailure:
    """Non-zero exit with zero parsed findings becomes explicit, blocking."""

    def test_lint_parse_failure_is_flagged(self):
        log = "npm error Exit handler never called!\nnpm error something went wrong\n"
        result = compile_build(
            typecheck_log="", typecheck_exit=0,
            lint_log=log, lint_exit=1,
            test_log="", test_exit=0,
            project_type="node",
        )
        assert result["overall_pass"] is False
        assert result["lint"]["parse_failure"] is True
        # Synthetic error was appended so lint.errors is not misleadingly empty.
        assert result["lint"]["error_count"] >= 1
        assert any(
            e.get("rule") == "gate/unparsed-build-output"
            for e in result["lint"]["errors"]
        )
        # blocking_issues cannot be rationalised as a tooling anomaly.
        joined = " ".join(result["blocking_issues"])
        assert "opaque build failure" in joined
        assert "unknown output format" in joined
        # Raw log preserved for forensic review.
        assert "Exit handler never called" in result["lint"]["raw_output_tail"]

    def test_typecheck_parse_failure_is_flagged(self):
        """Same treatment for typecheck — e.g. tsc wrapped by a broken script."""
        log = "something unrelated printed to stderr\n"
        result = compile_build(
            typecheck_log=log, typecheck_exit=2,
            lint_log="", lint_exit=0,
            test_log="", test_exit=0,
            project_type="node",
            typecheck_tool="npx",
        )
        assert result["typecheck"]["parse_failure"] is True
        assert any(
            e.get("rule") == "gate/unparsed-build-output"
            for e in result["typecheck"]["errors"]
        )
        assert any("opaque build failure" in b for b in result["blocking_issues"])

    def test_tests_parse_failure_is_flagged(self):
        log = "runner crashed with signal 9\n"
        result = compile_build(
            typecheck_log="", typecheck_exit=0,
            lint_log="", lint_exit=0,
            test_log=log, test_exit=137,
            project_type="node",
        )
        assert result["tests"]["parse_failure"] is True
        assert result["tests"]["failed"] >= 1
        assert any("opaque build failure" in b for b in result["blocking_issues"])

    def test_parse_failure_survives_empty_log(self):
        """Empty log + non-zero exit still flags (that's ambiguous too)."""
        result = compile_build(
            typecheck_log="", typecheck_exit=0,
            lint_log="", lint_exit=1,
            test_log="", test_exit=0,
            project_type="node",
        )
        assert result["lint"]["parse_failure"] is True

    def test_no_false_positive_on_successful_lint(self):
        """Clean run: parse_failure stays False, no synthetic error added."""
        result = compile_build(
            typecheck_log="", typecheck_exit=0,
            lint_log="", lint_exit=0,
            test_log="", test_exit=0,
            project_type="node",
        )
        assert result["lint"]["parse_failure"] is False
        assert result["typecheck"]["parse_failure"] is False
        assert result["tests"]["parse_failure"] is False
        assert result["overall_pass"] is True

    def test_no_false_positive_when_findings_exist(self):
        """Real parsed errors: not a parse failure, just a normal failed lint."""
        result = compile_build(
            typecheck_log="", typecheck_exit=0,
            lint_log=PR223_ESLINT_LOG, lint_exit=1,
            test_log="", test_exit=0,
            project_type="node",
        )
        assert result["lint"]["parse_failure"] is False

    def test_raw_output_tail_is_present_on_passing_build(self):
        """Passing build still preserves raw_output_tail for audit."""
        result = compile_build(
            typecheck_log="Compiled cleanly.", typecheck_exit=0,
            lint_log="Lint clean.", lint_exit=0,
            test_log="Tests 3 passed (3)", test_exit=0,
            project_type="node",
        )
        assert "Compiled cleanly" in result["typecheck"]["raw_output_tail"]
        assert "Lint clean" in result["lint"]["raw_output_tail"]
        assert "Tests 3 passed" in result["tests"]["raw_output_tail"]

    def test_raw_output_tail_is_bounded(self):
        """raw_output_tail must not blow up build.json — capped at 2000 chars."""
        huge = "x" * 100_000
        result = compile_build(
            typecheck_log="", typecheck_exit=0,
            lint_log=huge, lint_exit=1,
            test_log="", test_exit=0,
            project_type="node",
        )
        assert len(result["lint"]["raw_output_tail"]) <= 2000

    def test_exit_codes_plumbed_through(self):
        result = compile_build(
            typecheck_log="", typecheck_exit=2,
            lint_log="", lint_exit=1,
            test_log="", test_exit=137,
            project_type="node",
        )
        assert result["typecheck"]["exit_code"] == 2
        assert result["lint"]["exit_code"] == 1
        assert result["tests"]["exit_code"] == 137


class TestRunBuildTimeoutLogging:
    """Silent TimeoutExpired handling was a pre-PR-223 observability gap."""

    def _profile_stub(self, **overrides):
        base = {
            "project_type": "node",
            "typecheck_cmd": "npx tsc --noEmit",
            "lint_cmd": "npm run lint:check",
            "test_cmd": "npm run test:run",
        }
        base.update(overrides)
        return base

    def test_lint_timeout_emits_warning(self, tmp_path, caplog):
        (tmp_path / "package.json").write_text("{}")

        real_run = subprocess.run

        def fake_run(args, **kwargs):
            if "lint" in " ".join(args):
                raise subprocess.TimeoutExpired(cmd=args, timeout=300)
            return real_run(["true"], capture_output=True, text=True)

        with patch("gate.builder.subprocess.run", side_effect=fake_run), \
             patch(
                 "gate.builder.profiles.resolve_profile",
                 return_value=self._profile_stub(),
             ):
            with caplog.at_level("WARNING", logger="gate.builder"):
                result = run_build(tmp_path)

        assert any(
            "lint timed out" in rec.message for rec in caplog.records
        ), "expected a WARNING log when lint hits TimeoutExpired"
        assert result["lint"]["pass"] is False

    def test_typecheck_timeout_emits_warning(self, tmp_path, caplog):
        (tmp_path / "package.json").write_text("{}")
        real_run = subprocess.run

        def fake_run(args, **kwargs):
            if "tsc" in " ".join(args):
                raise subprocess.TimeoutExpired(cmd=args, timeout=300)
            return real_run(["true"], capture_output=True, text=True)

        with patch("gate.builder.subprocess.run", side_effect=fake_run), \
             patch(
                 "gate.builder.profiles.resolve_profile",
                 return_value=self._profile_stub(),
             ):
            with caplog.at_level("WARNING", logger="gate.builder"):
                run_build(tmp_path)

        assert any(
            "typecheck timed out" in rec.message for rec in caplog.records
        )

    def test_tests_timeout_emits_warning(self, tmp_path, caplog):
        (tmp_path / "package.json").write_text("{}")
        real_run = subprocess.run

        def fake_run(args, **kwargs):
            if "test" in " ".join(args):
                raise subprocess.TimeoutExpired(cmd=args, timeout=300)
            return real_run(["true"], capture_output=True, text=True)

        with patch("gate.builder.subprocess.run", side_effect=fake_run), \
             patch(
                 "gate.builder.profiles.resolve_profile",
                 return_value=self._profile_stub(),
             ):
            with caplog.at_level("WARNING", logger="gate.builder"):
                run_build(tmp_path)

        assert any(
            "tests timed out" in rec.message for rec in caplog.records
        )

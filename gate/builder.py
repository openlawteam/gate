"""Build verification (typecheck, lint, tests).

Supports per-project-type build commands via profiles. Node.js projects
use structured parsers; other project types use generic exit-code-based parsing.
"""

import logging
import re
import shlex
import subprocess
from pathlib import Path

from gate import profiles

logger = logging.getLogger(__name__)


def run_build(worktree: Path, config: dict | None = None) -> dict:
    """Run typecheck, lint, and test in the worktree. Return build results dict.

    Uses the project profile to determine which commands to run.
    Falls back to auto-detection if no project_type is configured.
    """
    repo_cfg = (config or {}).get("repo", {})
    profile = profiles.resolve_profile(repo_cfg, worktree)
    project_type = profile.get("project_type", "none")

    typecheck_cmd = profile.get("typecheck_cmd", "")
    lint_cmd = profile.get("lint_cmd", "")
    test_cmd = profile.get("test_cmd", "")

    if not typecheck_cmd and not lint_cmd and not test_cmd:
        logger.info(f"No build commands for {worktree} (project_type={project_type}), skipping")
        return {
            "typecheck": {"pass": True, "errors": [], "error_count": 0, "tool": ""},
            "lint": {
                "pass": True, "warnings": [], "errors": [],
                "warning_count": 0, "error_count": 0, "tool": "",
            },
            "tests": {
                "pass": True, "total": 0, "passed": 0,
                "failed": 0, "skipped": 0, "failures": [], "tool": "",
            },
            "overall_pass": True,
            "blocking_issues": [],
            "skipped": True,
            "skip_reason": f"no build commands (project_type={project_type})",
            "project_type": project_type,
        }

    cwd = str(worktree)
    logger.info(f"Running build in {cwd} (project_type={project_type})")

    build_timeout = 300

    if typecheck_cmd:
        tc_args = shlex.split(typecheck_cmd)
        try:
            tc_result = subprocess.run(
                tc_args,
                capture_output=True, text=True, cwd=cwd, timeout=build_timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                f"typecheck timed out after {build_timeout}s in {cwd} "
                f"(cmd: {typecheck_cmd})"
            )
            tc_result = subprocess.CompletedProcess(
                tc_args, 1, stdout="", stderr=f"typecheck timed out after {build_timeout}s",
            )
    else:
        tc_result = subprocess.CompletedProcess([], 0, stdout="", stderr="")

    if lint_cmd:
        lint_args = shlex.split(lint_cmd)
        try:
            lint_result = subprocess.run(
                lint_args,
                capture_output=True, text=True, cwd=cwd, timeout=build_timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                f"lint timed out after {build_timeout}s in {cwd} "
                f"(cmd: {lint_cmd})"
            )
            lint_result = subprocess.CompletedProcess(
                lint_args, 1, stdout="", stderr=f"lint timed out after {build_timeout}s",
            )
    else:
        lint_result = subprocess.CompletedProcess([], 0, stdout="", stderr="")

    if test_cmd:
        test_args = shlex.split(test_cmd)
        try:
            test_result = subprocess.run(
                test_args,
                capture_output=True, text=True, cwd=cwd, timeout=build_timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                f"tests timed out after {build_timeout}s in {cwd} "
                f"(cmd: {test_cmd})"
            )
            test_result = subprocess.CompletedProcess(
                test_args, 1, stdout="", stderr=f"tests timed out after {build_timeout}s",
            )
    else:
        test_result = subprocess.CompletedProcess([], 0, stdout="", stderr="")

    tc_tool = shlex.split(typecheck_cmd)[0] if typecheck_cmd else ""
    lint_tool = shlex.split(lint_cmd)[0] if lint_cmd else ""
    test_tool = shlex.split(test_cmd)[0] if test_cmd else ""

    return compile_build(
        typecheck_log=tc_result.stdout + tc_result.stderr,
        typecheck_exit=tc_result.returncode,
        lint_log=lint_result.stdout + lint_result.stderr,
        lint_exit=lint_result.returncode,
        test_log=test_result.stdout + test_result.stderr,
        test_exit=test_result.returncode,
        project_type=project_type,
        typecheck_tool=tc_tool,
        lint_tool=lint_tool,
        test_tool=test_tool,
    )


def compile_build(
    typecheck_log: str,
    typecheck_exit: int,
    lint_log: str,
    lint_exit: int,
    test_log: str,
    test_exit: int,
    project_type: str = "node",
    typecheck_tool: str = "",
    lint_tool: str = "",
    test_tool: str = "",
) -> dict:
    """Parse build outputs into structured results.

    Node.js projects use structured parsers for detailed error extraction.
    Python projects parse pytest summary lines for accurate counts.
    Other project types use generic exit-code-based parsing.
    """
    if project_type == "node":
        tc = _parse_tsc(typecheck_log, typecheck_exit)
        lint = _parse_lint(lint_log, lint_exit)
        test = _parse_test(test_log, test_exit)
    elif project_type == "python":
        tc = _parse_generic(typecheck_log, typecheck_exit)
        lint = _parse_generic(lint_log, lint_exit)
        test = _parse_pytest(test_log, test_exit)
    else:
        tc = _parse_generic(typecheck_log, typecheck_exit)
        lint = _parse_generic(lint_log, lint_exit)
        test = _parse_generic_test(test_log, test_exit)

    # Flag non-zero exit with zero parsed findings as an opaque build failure.
    for name, result, count_keys in [
        ("typecheck", tc, ["error_count"]),
        ("lint", lint, ["error_count", "warning_count"]),
        ("tests", test, ["failed"]),
    ]:
        if not result["pass"]:
            total_findings = sum(result.get(k, 0) for k in count_keys)
            if total_findings == 0:
                result["parse_failure"] = True
                raw = (result.get("output") or "").strip()
                tail = "\n".join(raw.splitlines()[-40:]) if raw else "(empty)"
                synthetic = {
                    "message": (
                        f"{name} exited non-zero "
                        f"(exit={result.get('exit_code', '?')}) but parser "
                        f"extracted 0 findings. Treat as opaque build failure; "
                        f"see raw_output_tail. Raw tail:\n{tail}"
                    ),
                    "rule": "gate/unparsed-build-output",
                    "severity": "error",
                }
                if name == "tests":
                    result.setdefault("failures", []).append(
                        {"file": "<unparsed>", "message": synthetic["message"]}
                    )
                    result["failed"] = max(result.get("failed", 0), 1)
                else:
                    result.setdefault("errors", []).append(synthetic)
                    result["error_count"] = max(result.get("error_count", 0), 1)

    overall_pass = tc["pass"] and lint["pass"] and test["pass"]
    blocking_issues = []
    tc_tool_name = typecheck_tool or "typecheck"
    if not tc["pass"]:
        if tc.get("parse_failure"):
            blocking_issues.append(
                f"{tc_tool_name} exited non-zero but parser extracted 0 findings "
                f"(unknown output format — opaque build failure, see raw_output_tail)"
            )
        else:
            blocking_issues.append(f"{tc['error_count']} {tc_tool_name} errors")
    if not lint["pass"]:
        if lint.get("parse_failure"):
            blocking_issues.append(
                "lint exited non-zero but parser extracted 0 findings "
                "(unknown output format — opaque build failure, see raw_output_tail)"
            )
        else:
            blocking_issues.append(
                f"{lint['error_count']} lint errors, {lint.get('warning_count', 0)} warnings"
            )
    if not test["pass"]:
        if test.get("parse_failure"):
            blocking_issues.append(
                "tests exited non-zero but parser extracted 0 findings "
                "(unknown output format — opaque build failure, see raw_output_tail)"
            )
        else:
            blocking_issues.append(f"{test.get('failed', 0)} test failures")

    return {
        "typecheck": {
            "pass": tc["pass"],
            "errors": tc.get("errors", []),
            "error_count": tc["error_count"],
            "tool": typecheck_tool,
            "exit_code": tc.get("exit_code"),
            "parse_failure": tc.get("parse_failure", False),
            # Last 2000 chars of raw output, bounded per parser, for forensic audits.
            "raw_output_tail": tc.get("output", ""),
        },
        "lint": {
            "pass": lint["pass"],
            "warnings": lint.get("warnings", [])[:20],
            "errors": lint.get("errors", [])[:20],
            "warning_count": lint.get("warning_count", 0),
            "error_count": lint["error_count"],
            "tool": lint_tool,
            "exit_code": lint.get("exit_code"),
            "parse_failure": lint.get("parse_failure", False),
            "raw_output_tail": lint.get("output", ""),
        },
        "tests": {
            "pass": test["pass"],
            "total": test.get("total", 0),
            "passed": test.get("passed", 0),
            "failed": test.get("failed", 0),
            "skipped": test.get("skipped", 0),
            "failures": test.get("failures", [])[:10],
            "tool": test_tool,
            "exit_code": test.get("exit_code"),
            "parse_failure": test.get("parse_failure", False),
            "raw_output_tail": test.get("output", ""),
        },
        "overall_pass": overall_pass,
        "blocking_issues": blocking_issues,
        "project_type": project_type,
    }


# ── Node.js parsers (structured error extraction) ────────────


def _parse_tsc(log: str, exit_code: int) -> dict:
    """Parse TypeScript compiler output. Ported from parseTsc()."""
    errors = []
    for m in re.finditer(r"^(.+)\((\d+),(\d+)\):\s+error\s+(TS\d+):\s+(.+)$", log, re.MULTILINE):
        errors.append({
            "file": m.group(1),
            "line": int(m.group(2)),
            "column": int(m.group(3)),
            "code": m.group(4),
            "message": m.group(5),
        })
    return {
        "pass": exit_code == 0,
        "exit_code": exit_code,
        "errors": errors,
        "error_count": len(errors),
        "output": log[-2000:],
    }


def _parse_lint(log: str, exit_code: int) -> dict:
    """Parse ESLint output (stylish and next-lint formats, case-insensitive severity)."""
    warnings = []
    errors = []
    current_file = None
    for line in log.split("\n"):
        # Accept optional ./ prefix (next lint) and mjs/cjs extensions.
        file_match = re.match(
            r"^(?:\./)?([^\s].*\.(?:ts|tsx|js|jsx|mjs|cjs))$", line
        )
        if file_match:
            current_file = file_match.group(1)
            continue
        # Accept optional trailing colon after severity and case-insensitive.
        issue_match = re.match(
            r"^\s*(\d+):(\d+)\s+(warning|error):?\s+(.+?)\s{2,}(\S+)\s*$",
            line,
            re.IGNORECASE,
        )
        if issue_match and current_file:
            severity = issue_match.group(3).lower()
            entry = {
                "file": current_file,
                "line": int(issue_match.group(1)),
                "column": int(issue_match.group(2)),
                "severity": severity,
                "message": issue_match.group(4).strip(),
                "rule": issue_match.group(5),
            }
            if severity == "error":
                errors.append(entry)
            else:
                warnings.append(entry)
    return {
        "pass": exit_code == 0,
        "exit_code": exit_code,
        "warnings": warnings,
        "errors": errors,
        "warning_count": len(warnings),
        "error_count": len(errors),
        "output": log[-2000:],
    }


def _parse_test(log: str, exit_code: int) -> dict:
    """Parse Vitest output. Ported from parseTest()."""
    total = 0
    passed = 0
    failed = 0
    skipped = 0
    failures = []

    summary = re.search(
        r"Tests\s+(\d+)\s+passed\s*(?:\|\s*(\d+)\s+failed)?\s*(?:\|\s*(\d+)\s+skipped)?\s*\((\d+)\)",
        log, re.IGNORECASE,
    )
    if summary:
        passed = int(summary.group(1) or 0)
        failed = int(summary.group(2) or 0)
        skipped = int(summary.group(3) or 0)
        total = int(summary.group(4) or (passed + failed + skipped))
    else:
        alt = re.search(r"(\d+)\s+passed", log)
        if alt:
            passed = int(alt.group(1))
        fail_match = re.search(r"(\d+)\s+failed", log)
        if fail_match:
            failed = int(fail_match.group(1))
        total = passed + failed + skipped

    for m in re.finditer(r"FAIL\s+(.+?)(?:\n|$)", log):
        failures.append({"file": m.group(1).strip()})

    return {
        "pass": exit_code == 0,
        "exit_code": exit_code,
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "failures": failures,
        "output": log[-2000:],
    }


# ── Python parsers ───────────────────────────────────────────


def _parse_pytest(log: str, exit_code: int) -> dict:
    """Parse pytest output into structured counts.

    Handles the standard pytest summary formats:
    - ``624 passed, 6 skipped in 95.89s (0:01:35)``
    - ``472 passed in 29.22s``
    - ``1 failed, 45 passed in 35.69s``
    - ``3 errors`` (e.g. collection errors)

    Each count is matched independently so the order pytest prints them in
    doesn't matter.
    """
    passed = 0
    failed = 0
    skipped = 0
    errors = 0

    passed_match = re.search(r"(\d+)\s+passed\b", log)
    if passed_match:
        passed = int(passed_match.group(1))
    failed_match = re.search(r"(\d+)\s+failed\b", log)
    if failed_match:
        failed = int(failed_match.group(1))
    skipped_match = re.search(r"(\d+)\s+skipped\b", log)
    if skipped_match:
        skipped = int(skipped_match.group(1))
    error_match = re.search(r"(\d+)\s+error(?:s)?\b", log)
    if error_match:
        errors = int(error_match.group(1))

    # Collection errors count as failures for pass/fail semantics.
    failed_total = failed + errors
    total = passed + failed_total + skipped

    failures: list[dict] = []
    for m in re.finditer(r"^FAILED\s+(\S+)", log, re.MULTILINE):
        failures.append({"file": m.group(1)})

    return {
        "pass": exit_code == 0,
        "exit_code": exit_code,
        "total": total,
        "passed": passed,
        "failed": failed_total,
        "skipped": skipped,
        "failures": failures[:10],
        "output": log[-2000:],
    }


# ── Generic parsers (exit-code-based) ────────────────────────


def _parse_generic(log: str, exit_code: int) -> dict:
    """Generic parser: pass/fail based on exit code, raw output as errors."""
    errors = []
    if exit_code != 0 and log.strip():
        errors = [{"message": line} for line in log.strip().split("\n")[:50]]
    return {
        "pass": exit_code == 0,
        "exit_code": exit_code,
        "errors": errors,
        "error_count": len(errors) if exit_code != 0 else 0,
        "warnings": [],
        "warning_count": 0,
        "output": log[-2000:],
    }


def _parse_generic_test(log: str, exit_code: int) -> dict:
    """Generic test parser: exit code determines pass/fail."""
    return {
        "pass": exit_code == 0,
        "exit_code": exit_code,
        "total": 0,
        "passed": 0,
        "failed": 1 if exit_code != 0 else 0,
        "skipped": 0,
        "failures": [{"file": "see output"}] if exit_code != 0 else [],
        "output": log[-2000:],
    }


def compare_builds(before: dict, after: dict) -> dict:
    """Compare pre and post build results for pre-existing failure detection.

    If typecheck/lint pass state is unchanged and test failure count didn't increase,
    accept the build as passing (pre-existing failures).
    Handles both old ("typescript") and new ("typecheck") key names for backward compat.
    """
    if after.get("overall_pass"):
        return after

    tc_before = before.get("typecheck", before.get("typescript", {}))
    tc_after = after.get("typecheck", after.get("typescript", {}))
    tc_same = tc_before.get("pass") == tc_after.get("pass")
    lint_same = before.get("lint", {}).get("pass") == after.get("lint", {}).get("pass")
    test_not_worse = (
        after.get("tests", {}).get("failed", 0)
        <= before.get("tests", {}).get("failed", 0)
    )

    if tc_same and lint_same and test_not_worse:
        after["overall_pass"] = True
        after["pre_existing_failures_accepted"] = True

    return after

"""Build verification (tsc, lint, vitest).

Ported from compile-build.js parseTsc/parseLint/parseTest + GHA workflow
build execution steps.
"""

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def run_build(worktree: Path) -> dict:
    """Run tsc, lint, and vitest in the worktree. Return build results dict.

    Runs each tool independently so partial failures don't block later tools.
    Skips entirely for non-Node.js projects (no package.json).
    """
    if not (worktree / "package.json").exists():
        logger.info(f"No package.json in {worktree}, skipping Node.js build")
        return {
            "typescript": {"pass": True, "errors": [], "error_count": 0},
            "lint": {
                "pass": True, "warnings": [], "errors": [],
                "warning_count": 0, "error_count": 0,
            },
            "tests": {
                "pass": True, "total": 0, "passed": 0,
                "failed": 0, "skipped": 0, "failures": [],
            },
            "overall_pass": True,
            "blocking_issues": [],
            "skipped": True,
            "skip_reason": "no package.json (not a Node.js project)",
        }

    cwd = str(worktree)
    logger.info(f"Running build in {cwd}")

    build_timeout = 300

    # TypeScript
    try:
        tsc_result = subprocess.run(
            ["npx", "tsc", "--noEmit"],
            capture_output=True, text=True, cwd=cwd, timeout=build_timeout,
        )
    except subprocess.TimeoutExpired:
        tsc_result = subprocess.CompletedProcess(
            ["npx", "tsc"], 1, stdout="", stderr="tsc timed out",
        )

    # Lint
    try:
        lint_result = subprocess.run(
            ["npm", "run", "lint:check"],
            capture_output=True, text=True, cwd=cwd, timeout=build_timeout,
        )
    except subprocess.TimeoutExpired:
        lint_result = subprocess.CompletedProcess(
            ["npm", "run", "lint:check"], 1, stdout="", stderr="lint timed out",
        )

    # Tests (with gate test exclusions via env)
    test_env = None
    try:
        test_result = subprocess.run(
            ["npm", "run", "test:run"],
            capture_output=True, text=True, cwd=cwd, env=test_env, timeout=build_timeout,
        )
    except subprocess.TimeoutExpired:
        test_result = subprocess.CompletedProcess(
            ["npm", "run", "test:run"], 1, stdout="", stderr="tests timed out",
        )

    return compile_build(
        tsc_log=tsc_result.stdout + tsc_result.stderr,
        tsc_exit=tsc_result.returncode,
        lint_log=lint_result.stdout + lint_result.stderr,
        lint_exit=lint_result.returncode,
        test_log=test_result.stdout + test_result.stderr,
        test_exit=test_result.returncode,
    )


def compile_build(
    tsc_log: str,
    tsc_exit: int,
    lint_log: str,
    lint_exit: int,
    test_log: str,
    test_exit: int,
) -> dict:
    """Parse build outputs into structured results.

    Ported from compile-build.js.
    """
    tsc = _parse_tsc(tsc_log, tsc_exit)
    lint = _parse_lint(lint_log, lint_exit)
    test = _parse_test(test_log, test_exit)

    overall_pass = tsc["pass"] and lint["pass"] and test["pass"]
    blocking_issues = []
    if not tsc["pass"]:
        blocking_issues.append(f"{tsc['error_count']} TypeScript errors")
    if not lint["pass"]:
        blocking_issues.append(
            f"{lint['error_count']} lint errors, {lint['warning_count']} warnings"
        )
    if not test["pass"]:
        blocking_issues.append(f"{test['failed']} test failures")

    return {
        "typescript": {
            "pass": tsc["pass"],
            "errors": tsc["errors"],
            "error_count": tsc["error_count"],
        },
        "lint": {
            "pass": lint["pass"],
            "warnings": lint["warnings"][:20],
            "errors": lint["errors"][:20],
            "warning_count": lint["warning_count"],
            "error_count": lint["error_count"],
        },
        "tests": {
            "pass": test["pass"],
            "total": test["total"],
            "passed": test["passed"],
            "failed": test["failed"],
            "skipped": test["skipped"],
            "failures": test["failures"][:10],
        },
        "overall_pass": overall_pass,
        "blocking_issues": blocking_issues,
    }


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
    """Parse ESLint output. Ported from parseLint()."""
    warnings = []
    errors = []
    current_file = None
    for line in log.split("\n"):
        file_match = re.match(r"^([^\s].*\.(?:ts|tsx|js|jsx))$", line)
        if file_match:
            current_file = file_match.group(1)
            continue
        issue_match = re.match(
            r"^\s*(\d+):(\d+)\s+(warning|error)\s+(.+?)\s{2,}(\S+)\s*$", line
        )
        if issue_match and current_file:
            entry = {
                "file": current_file,
                "line": int(issue_match.group(1)),
                "column": int(issue_match.group(2)),
                "severity": issue_match.group(3),
                "message": issue_match.group(4).strip(),
                "rule": issue_match.group(5),
            }
            if issue_match.group(3) == "error":
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


def compare_builds(before: dict, after: dict) -> dict:
    """Compare pre and post build results for pre-existing failure detection.

    If tsc/lint pass state is unchanged and test failure count didn't increase,
    accept the build as passing (pre-existing failures).
    """
    if after.get("overall_pass"):
        return after

    tsc_same = before.get("typescript", {}).get("pass") == after.get("typescript", {}).get("pass")
    lint_same = before.get("lint", {}).get("pass") == after.get("lint", {}).get("pass")
    test_not_worse = (
        after.get("tests", {}).get("failed", 0)
        <= before.get("tests", {}).get("failed", 0)
    )

    if tsc_same and lint_same and test_not_worse:
        after["overall_pass"] = True
        after["pre_existing_failures_accepted"] = True

    return after

"""Fix pipeline orchestrator.

Single continuous Claude session with Codex delegation.
"""

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from gate import builder, github, notify, prompt, state
from gate.codex import bootstrap_codex
from gate.config import build_claude_env, gate_dir
from gate.logger import write_live_log
from gate.runner import StructuredRunner, run_with_retry
from gate.schemas import FixResult

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 2
FIX_SESSION_MAX_TURNS = 300
RESUME_MAX_TURNS = 100

SEVERITY_ORDER = {"critical": 0, "error": 1, "warning": 2, "info": 3}

# Gate artifacts written to the worktree during review/fix.  Removed before
# commit as a safety net (the primary defense is the per-worktree git exclude
# configured in workspace._setup_artifact_exclusions).
#
# Keep in sync with GATE_EXCLUDE_PATTERNS in gate/workspace.py.
GATE_ARTIFACT_FILES = {
    "diff.txt", "changed_files.txt", "file_count.txt",
    "diff_stats.txt", "lines_changed.txt",
    "pr-metadata.json", "prior-review.json",
    "build.json", "triage.json", "architecture.json",
    "security.json", "logic.json", "verdict.json",
    "fix-build.json", "fix-diff.txt", "fix-env.json",
    "fix-resume-prompt.md", "fix-rereview.json",
    "no-codex.txt", "fix.json",
}

GATE_ARTIFACT_GLOBS = [
    "*-findings.json", "*-result.json",
    "*-session-id.txt", "*-raw.json",
    "*.in.md", "*.out.md", "*.in.md.tmp",
]


# ── Fix Helpers ──────────────────────────────────────────────


def cleanup_artifacts(workspace: Path) -> list[str]:
    """Remove Gate review/fix artifact files from the worktree.

    Safety net before commit — the primary defense is the per-worktree
    git exclude configured in workspace._setup_artifact_exclusions().
    Returns the list of paths that were removed.
    """
    removed: list[str] = []

    # Static filenames
    for name in GATE_ARTIFACT_FILES:
        p = workspace / name
        if p.exists():
            p.unlink(missing_ok=True)
            removed.append(name)

    # Glob patterns (root-level only — Path.glob without ** does not recurse)
    for pattern in GATE_ARTIFACT_GLOBS:
        for p in workspace.glob(pattern):
            if p.is_file():
                name = p.name
                p.unlink(missing_ok=True)
                if name not in removed:
                    removed.append(name)

    # fix-build/ directory
    build_dir = workspace / "fix-build"
    if build_dir.is_dir():
        shutil.rmtree(build_dir, ignore_errors=True)
        removed.append("fix-build/")

    # Unstage any removed paths still in the git index
    if removed:
        subprocess.run(
            ["git", "reset", "HEAD", "--"] + removed,
            capture_output=True,
            cwd=str(workspace),
        )
        logger.info(f"Cleaned up {len(removed)} artifact files: {removed}")

    return removed


def _match_glob(filepath: str, pattern: str) -> bool:
    """Match a file path against a glob pattern.

    Ported from matchGlob() in fix-loop-helpers.js.
    """
    if "*" not in pattern:
        return filepath == pattern

    if pattern.endswith("/**"):
        dir_prefix = pattern[:-3]
        return filepath == dir_prefix or filepath.startswith(dir_prefix + "/")

    escaped = re.escape(pattern).replace(r"\*", "[^/]*")
    return bool(re.match(f"^{escaped}$", filepath))


def enforce_blocklist(workspace: Path, config: dict | None = None) -> list[str]:
    """Revert changes to blocklisted files.

    Ported from enforceBlocklist() in fix-loop-helpers.js.
    """
    repo_cfg = (config or {}).get("repo", {})
    blocklist_path_str = repo_cfg.get("fix_blocklist", "")
    if blocklist_path_str:
        blocklist_path = Path(blocklist_path_str)
    else:
        blocklist_path = gate_dir() / "config" / "fix-blocklist.txt"
    try:
        content = blocklist_path.read_text()
    except OSError:
        return []

    patterns = [
        line.strip()
        for line in content.split("\n")
        if line.strip() and not line.strip().startswith("#")
    ]
    if not patterns:
        return []

    changed = _get_changed_files(workspace)
    violations = []
    for filepath in changed:
        for pattern in patterns:
            if _match_glob(filepath, pattern):
                violations.append(filepath)
                _revert_file(workspace, filepath)
                break

    if violations:
        logger.info(f"Blocklist reverted {len(violations)} files: {violations}")
    return violations


def cleanup_gate_tests(workspace: Path) -> None:
    """Remove test files created by gate agents.

    Ported from cleanupGateTests() in fix-loop-helpers.js.
    """
    cwd = str(workspace)

    # Remove tests/gate/ directory
    gate_tests = workspace / "tests" / "gate"
    if gate_tests.exists():
        import shutil

        shutil.rmtree(gate_tests, ignore_errors=True)

    # Find and remove __gate_test_* and __gate_fix_test_* files
    for pattern in ("**/__gate_test_*", "**/__gate_fix_test_*"):
        for f in workspace.glob(pattern):
            try:
                f.unlink()
            except OSError:
                pass

    # Unstage any gate test files from the index
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if result.returncode == 0:
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            if "tests/gate/" in line or "__gate_test_" in line or "__gate_fix_test_" in line:
                subprocess.run(
                    ["git", "reset", "HEAD", "--", line],
                    capture_output=True,
                    cwd=cwd,
                )
                try:
                    (workspace / line).unlink(missing_ok=True)
                except OSError:
                    pass


def build_verify(
    workspace: Path,
    original_build: dict | None = None,
    config: dict | None = None,
) -> dict:
    """Run build and compare to original for pre-existing failure detection.

    Uses the project profile to determine which commands to run.
    Returns dict with pass, typecheck_errors, lint_errors, test_failures,
    typecheck_log, lint_log, and typecheck_tool.
    """
    from gate import profiles

    repo_cfg = (config or {}).get("repo", {})
    profile = profiles.resolve_profile(repo_cfg, workspace)
    project_type = profile.get("project_type", "none")

    typecheck_cmd = profile.get("typecheck_cmd", "")
    lint_cmd = profile.get("lint_cmd", "")
    test_cmd = profile.get("test_cmd", "")

    if not typecheck_cmd and not lint_cmd and not test_cmd:
        return {
            "pass": True,
            "typecheck_errors": 0,
            "lint_errors": 0,
            "test_failures": 0,
            "typecheck_log": "",
            "lint_log": "",
            "typecheck_tool": "",
        }

    cwd = str(workspace)
    build_dir = workspace / "fix-build"
    build_dir.mkdir(exist_ok=True)

    typecheck_tool = typecheck_cmd.split()[0] if typecheck_cmd else ""
    lint_tool = lint_cmd.split()[0] if lint_cmd else ""
    test_tool = test_cmd.split()[0] if test_cmd else ""

    if typecheck_cmd:
        tc_out, tc_exit = _run_silent(f"{typecheck_cmd} 2>&1", cwd=cwd)
    else:
        tc_out, tc_exit = "", 0
    (build_dir / "typecheck.log").write_text(tc_out)

    if lint_cmd:
        lint_out, lint_exit = _run_silent(f"{lint_cmd} 2>&1", cwd=cwd)
    else:
        lint_out, lint_exit = "", 0
    (build_dir / "lint.log").write_text(lint_out)

    if test_cmd:
        test_out, test_exit = _run_silent(f"{test_cmd} 2>&1", cwd=cwd)
    else:
        test_out, test_exit = "", 0
    (build_dir / "test.log").write_text(test_out)

    build_result = builder.compile_build(
        typecheck_log=tc_out,
        typecheck_exit=tc_exit,
        lint_log=lint_out,
        lint_exit=lint_exit,
        test_log=test_out,
        test_exit=test_exit,
        project_type=project_type,
        typecheck_tool=typecheck_tool,
        lint_tool=lint_tool,
        test_tool=test_tool,
    )

    fix_build_path = workspace / "fix-build.json"
    fix_build_path.write_text(json.dumps(build_result, indent=2))

    passed = build_result.get("overall_pass", False)

    if not passed and original_build:
        compared = builder.compare_builds(original_build, build_result)
        passed = compared.get("overall_pass", False)
        if passed:
            logger.info("Pre-existing failures accepted")

    return {
        "pass": passed,
        "typecheck_errors": build_result.get("typecheck", {}).get("error_count", 0),
        "lint_errors": build_result.get("lint", {}).get("error_count", 0),
        "test_failures": build_result.get("tests", {}).get("failed", 0),
        "typecheck_log": tc_out[:5000],
        "lint_log": lint_out[:5000],
        "typecheck_tool": typecheck_tool,
    }


def write_diff(workspace: Path) -> None:
    """Write git diff to fix-diff.txt for re-review consumption.

    Ported from writeDiff() in run-fix-phases.js.
    """
    diff, _ = _run_silent("git diff HEAD 2>/dev/null", cwd=str(workspace))
    (workspace / "fix-diff.txt").write_text(diff or "(no changes)")


def sort_findings_by_severity(findings: list[dict]) -> list[dict]:
    """Sort findings by severity (critical first).

    Ported from sortFindingsBySeverity() in fix-loop-helpers.js.
    """
    return sorted(findings, key=lambda f: SEVERITY_ORDER.get(f.get("severity", ""), 99))


def _get_changed_files(workspace: Path) -> list[str]:
    """Get list of changed, staged, and untracked files."""
    result = subprocess.run(
        ["sh", "-c",
         "{ git diff --name-only 2>/dev/null; "
         "git diff --cached --name-only 2>/dev/null; "
         "git ls-files --others --exclude-standard 2>/dev/null; } | sort -u"],
        capture_output=True,
        text=True,
        cwd=str(workspace),
    )
    return [f for f in result.stdout.strip().split("\n") if f]


def _revert_file(workspace: Path, filepath: str) -> None:
    """Revert a single file to its state in HEAD.

    For tracked files: restores the HEAD version (does not delete).
    For untracked files: removes the file from disk.
    """
    cwd = str(workspace)
    tracked = subprocess.run(
        ["git", "cat-file", "-e", f"HEAD:{filepath}"],
        capture_output=True,
        cwd=cwd,
    )
    if tracked.returncode == 0:
        subprocess.run(
            ["git", "checkout", "HEAD", "--", filepath],
            capture_output=True,
            cwd=cwd,
        )
    else:
        full_path = workspace / filepath
        if full_path.exists():
            full_path.unlink(missing_ok=True)


def _revert_all(workspace: Path) -> None:
    """Revert all changes in the workspace."""
    cwd = str(workspace)
    subprocess.run(["git", "checkout", "--", "."], capture_output=True, cwd=cwd)
    subprocess.run(["git", "clean", "-fd"], capture_output=True, cwd=cwd)


def _run_silent(cmd: str, cwd: str | None = None) -> tuple[str, int]:
    """Run a shell command silently, returning (stdout, exit_code). Never raises."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=600,
        )
        return result.stdout, result.returncode
    except (subprocess.SubprocessError, OSError):
        return "", 1


# ── Prompt Builders ──────────────────────────────────────────


def _build_build_error_prompt(build_result: dict) -> str:
    """Build the resume prompt for build errors.

    Ported from buildBuildErrorPrompt() in run-fix-loop.js.
    """
    tool_name = build_result.get("typecheck_tool") or "Type Check"
    parts = [
        "# Build Errors After Fix",
        "",
        "The external build verification found errors after your fix session.",
        "You still have full context of what you changed. Fix these errors.",
        "",
    ]
    if build_result.get("typecheck_errors", 0) > 0:
        parts.append(f"## {tool_name} Errors ({build_result['typecheck_errors']})")
        parts.append("```")
        parts.append(build_result.get("typecheck_log", "(no log captured)"))
        parts.append("```")
        parts.append("")
    if build_result.get("lint_errors", 0) > 0:
        parts.append(f"## Lint Errors ({build_result['lint_errors']})")
        parts.append("```")
        parts.append(build_result.get("lint_log", "(no log captured)"))
        parts.append("```")
        parts.append("")
    parts.append("Use `gate-code implement` to fix these errors. Then run verification again.")
    return "\n".join(parts)


def _build_rereview_feedback_prompt(rereview_result: dict) -> str:
    """Build the resume prompt for re-review feedback.

    Ported from buildRereviewFeedbackPrompt() in run-fix-loop.js.
    """
    parts = [
        "# Re-Review Feedback",
        "",
        "An independent reviewer found issues with your fix. Fix them.",
        "",
        "```json",
        json.dumps(rereview_result, indent=2)[:3000],
        "```",
        "",
        "Address each issue, then output your updated JSON summary.",
    ]
    return "\n".join(parts)


def _build_codex_bootstrap_prompt() -> str:
    """Load the gate-junior.md prompt for Codex bootstrap."""
    try:
        return prompt.load("gate-junior")
    except FileNotFoundError:
        return "You are a junior engineer. Follow the senior's directions precisely."


# ── Fix Pipeline ─────────────────────────────────────────────


class FixPipeline:
    """Run the fix pipeline: senior Claude + junior Codex.

    Single continuous Claude session that delegates to Codex via gate-code.
    """

    def __init__(
        self,
        pr_number: int,
        repo: str,
        workspace: Path,
        verdict: dict,
        build: dict,
        config: dict,
        check_run_id: int | None = None,
        cancelled: threading.Event | None = None,
        socket_path: Path | None = None,
    ):
        self.pr_number = pr_number
        self.repo = repo
        self.workspace = workspace
        self.verdict = verdict
        self.build = build
        self.config = config
        self.check_run_id = check_run_id
        self._cancelled = cancelled or threading.Event()
        self.socket_path = socket_path
        self.session_id: str | None = None
        self.codex_thread_id: str = ""
        self.branch = ""
        self.fix_pane_id: str | None = None
        self.is_polish = verdict.get("decision") == "approve_with_notes"
        self._state_dir = state.get_pr_state_dir(pr_number, repo)

    def run(self) -> FixResult:
        """Execute the fix pipeline."""
        if self._cancelled.is_set():
            return FixResult(success=False, summary="Cancelled before start")

        findings = self.verdict.get("findings", [])
        actionable = [f for f in findings if f.get("severity", "") != "info"]
        finding_count = len(actionable)

        # Check fix attempt limits
        allowed, reason = state.check_fix_limits(self.pr_number, self.config, repo=self.repo)
        if not allowed:
            github.comment_pr(
                self.repo,
                self.pr_number,
                f"**Gate Auto-Fix: Attempt limit reached** — {reason}",
            )
            return FixResult(success=False, reason=reason, summary=reason)

        triage = self._read_json("triage.json") or {}
        risk_level = triage.get("risk_level", "medium")
        notify.fix_started(self.pr_number, finding_count, risk_level, self.repo)
        write_live_log(self.pr_number, "Fix pipeline starting...", prefix="fix", repo=self.repo)

        try:
            # Sort findings by severity (critical first)
            if findings:
                sorted_findings = sort_findings_by_severity(findings)
                self.verdict["findings"] = sorted_findings
                (self.workspace / "verdict.json").write_text(
                    json.dumps(self.verdict, indent=2)
                )

            # Bootstrap Codex session
            write_live_log(self.pr_number, "Bootstrapping Codex...", prefix="fix", repo=self.repo)
            codex_prompt = _build_codex_bootstrap_prompt()
            exit_code, codex_thread_id = bootstrap_codex(
                codex_prompt, str(self.workspace)
            )

            if codex_thread_id:
                self.codex_thread_id = codex_thread_id
                logger.info(f"Codex session bootstrapped: {codex_thread_id[:8]}")
            else:
                logger.warning("Codex bootstrap failed — senior will work without delegation")
                (self.workspace / "no-codex.txt").write_text("codex bootstrap failed")

            # Write env file for tmux-spawned runner (tmux doesn't inherit our env)
            fix_env = {
                "GATE_CODEX_THREAD_ID": self.codex_thread_id,
                "GATE_FIX_WORKSPACE": str(self.workspace),
            }
            (self.workspace / "fix-env.json").write_text(json.dumps(fix_env))

            all_fixed: list[dict] = []
            last_fix_json: dict | None = None

            for iteration in range(1, MAX_ITERATIONS + 1):
                if self._cancelled.is_set():
                    return FixResult(success=False, summary="Cancelled")

                write_live_log(
                    self.pr_number,
                    f"Fix iteration {iteration}/{MAX_ITERATIONS}",
                    prefix="fix",
                    repo=self.repo,
                )

                # Run or resume the fix session
                if iteration == 1:
                    fix_result = self._run_fix_session()
                else:
                    rereview_data = self._read_json("fix-rereview.json") or {}
                    feedback = _build_rereview_feedback_prompt(rereview_data)
                    fix_result = self._resume_fix_session(feedback)

                last_fix_json = fix_result.get("fix_json")
                new_fixed = (last_fix_json or {}).get("fixed", [])
                all_fixed.extend(new_fixed)

                if not fix_result.get("has_changes"):
                    logger.info(f"Iteration {iteration}: no changes")
                    if iteration >= MAX_ITERATIONS:
                        state.record_fix_attempt(self.pr_number, repo=self.repo)
                        return FixResult(
                            success=False,
                            summary=f"No changes after {iteration} iterations",
                        )
                    continue

                # Post-fix verification
                enforce_blocklist(self.workspace, config=self.config)
                cleanup_gate_tests(self.workspace)

                build_result = build_verify(self.workspace, self.build, config=self.config)

                # If build fails, resume session with build error context
                if not build_result["pass"]:
                    write_live_log(
                        self.pr_number,
                        f"Build failed, resuming with errors (typecheck={build_result['typecheck_errors']})",
                        prefix="fix",
                        repo=self.repo,
                    )
                    error_prompt = _build_build_error_prompt(build_result)
                    self._resume_fix_session(error_prompt)

                    enforce_blocklist(self.workspace, config=self.config)
                    cleanup_gate_tests(self.workspace)
                    build_result = build_verify(self.workspace, self.build, config=self.config)

                if not build_result["pass"]:
                    logger.info(f"Build still failing after iteration {iteration}")
                    _revert_all(self.workspace)
                    if iteration >= MAX_ITERATIONS:
                        state.record_fix_attempt(self.pr_number, repo=self.repo)
                        notify.fix_failed(self.pr_number, "build failures", iteration, self.repo)
                        return FixResult(
                            success=False,
                            summary=f"Build failing after {iteration} iterations",
                        )
                    continue

                # Generate diff and run re-review
                write_diff(self.workspace)

                write_live_log(self.pr_number, "Running re-review...", prefix="fix", repo=self.repo)
                rereview_pass = self._run_rereview()

                if rereview_pass:
                    return self._commit_and_finish(
                        iteration, all_fixed, last_fix_json, finding_count
                    )

                logger.info(f"Re-review rejected iteration {iteration}")
                _revert_all(self.workspace)
                if iteration >= MAX_ITERATIONS:
                    state.record_fix_attempt(self.pr_number, repo=self.repo)
                    notify.fix_failed(self.pr_number, "re-review rejected", iteration, self.repo)
                    return FixResult(
                        success=False,
                        summary=f"Re-review rejected after {iteration} iterations",
                    )

            state.record_fix_attempt(self.pr_number, repo=self.repo)
            return FixResult(success=False, summary="All iterations exhausted")

        except FileNotFoundError as e:
            logger.warning(f"Fix pipeline aborted (workspace deleted): {e}")
            return FixResult(success=False, summary="Workspace deleted (cancelled)")
        except Exception as e:
            logger.exception(f"Fix pipeline failed for PR #{self.pr_number}")
            notify.fix_failed(self.pr_number, str(e), 0, self.repo)
            state.record_fix_attempt(self.pr_number, repo=self.repo)
            return FixResult(success=False, error=str(e), summary=f"Crash: {e}")

    def _run_fix_session(self) -> dict:
        """Run the initial fix-senior session in tmux.

        Spawns fix-senior as an agent stage and waits for result.
        Returns {fix_json, has_changes}.
        """
        from gate.claude import spawn_review_stage
        from gate.tmux import kill_window

        result_file = self.workspace / "fix-senior-result.json"
        result_file.unlink(missing_ok=True)

        from gate.config import repo_slug
        review_id = f"{repo_slug(self.repo)}-pr{self.pr_number}" if self.repo else f"pr{self.pr_number}"
        pane_id = spawn_review_stage(
            review_id=review_id,
            stage="fix-senior",
            workspace=str(self.workspace),
            socket_path=str(self.socket_path) if self.socket_path else None,
            repo=self.repo,
        )
        if not pane_id:
            return {"fix_json": None, "has_changes": False}

        self.fix_pane_id = pane_id
        timeout = self.config.get("timeouts", {}).get("fix_session_s", 2400)
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if self._cancelled.is_set():
                kill_window(pane_id)
                return {"fix_json": None, "has_changes": False}
            if result_file.exists():
                try:
                    data = json.loads(result_file.read_text())
                    logger.debug(f"Fix result: success={data.get('success', '?')}")
                    break
                except json.JSONDecodeError:
                    pass
            time.sleep(5)
        else:
            kill_window(pane_id)

        # Read back the session ID that ReviewRunner created so resume works
        session_file = self.workspace / "fix-senior-session-id.txt"
        if session_file.exists():
            self.session_id = session_file.read_text().strip()

        kill_window(pane_id)

        fix_json = self._read_json("fix.json") or self._read_json("fix-senior-findings.json")
        has_changes = len(_get_changed_files(self.workspace)) > 0

        return {"fix_json": fix_json, "has_changes": has_changes}

    def _resume_fix_session(self, resume_prompt: str) -> dict:
        """Resume the fix-senior session with new context.

        Writes a resume prompt and spawns a resumed session.
        Returns {fix_json, has_changes}.
        """
        if not self.session_id:
            logger.warning("No session ID available for resume")
            return {"fix_json": None, "has_changes": False}

        resume_path = self.workspace / "fix-resume-prompt.md"
        resume_path.write_text(resume_prompt)

        env = build_claude_env()
        env["GATE_CODEX_THREAD_ID"] = self.codex_thread_id
        env["GATE_FIX_WORKSPACE"] = str(self.workspace)

        cmd = [
            "claude",
            "--dangerously-skip-permissions",
            "--resume",
            self.session_id,
            "--model",
            self.config.get("models", {}).get("fix_senior", "opus"),
            "--max-turns",
            str(RESUME_MAX_TURNS),
            resume_prompt,
        ]

        try:
            subprocess.run(
                cmd,
                env=env,
                cwd=str(self.workspace),
                timeout=self.config.get("timeouts", {}).get("fix_session_s", 2400),
            )
        except (subprocess.SubprocessError, OSError) as e:
            logger.warning(f"Resume session failed: {e}")

        fix_json = self._read_json("fix.json") or self._read_json("fix-senior-findings.json")
        has_changes = len(_get_changed_files(self.workspace)) > 0

        return {"fix_json": fix_json, "has_changes": has_changes}

    def _run_rereview(self) -> bool:
        """Run the fix re-review (structured, inline).

        Ported from runReReview() in run-fix-phases.js.
        """
        try:
            template = prompt.load("fix-rereview")
        except FileNotFoundError:
            logger.warning("fix-rereview prompt not found, auto-passing")
            return True

        vars_dict = prompt.build_vars(self.workspace, "fix-rereview", {}, self.config)
        assembled = prompt.safe_substitute(template, vars_dict, "fixer-rereview")

        result = run_with_retry(
            lambda: StructuredRunner().run(
                "fix-rereview", assembled, self.workspace, self.config
            ),
            "fix-rereview",
            self.config,
        )

        # Write result
        (self.workspace / "fix-rereview.json").write_text(
            json.dumps(result.data, indent=2)
        )

        passed = result.data.get("pass") is not False
        logger.info(f"Re-review: {'pass' if passed else 'fail'}")
        return passed

    def _commit_and_finish(
        self,
        iteration: int,
        all_fixed: list[dict],
        last_fix_json: dict | None,
        finding_count: int,
    ) -> FixResult:
        """Commit, push, and return success.

        Ported from commitAndPush() in run-fix-loop.js.
        """
        write_live_log(self.pr_number, "Re-review passed, committing...", prefix="fix", repo=self.repo)

        fixed_count = len(all_fixed)
        not_fixed = (last_fix_json or {}).get("not_fixed", [])

        # Build commit message
        commit_msg = f"fix(gate): auto-fix {fixed_count} issues from Gate review"
        fixed_details = "\n".join(
            f"- {f.get('file', '?')} — {f.get('fix_description', 'fixed')}"
            for f in all_fixed
        )
        if fixed_details:
            commit_msg += f"\n\nFindings fixed:\n{fixed_details}"
        not_fixed_details = "\n".join(
            f"- {f.get('reason', '?')}: {f.get('detail', '')}" for f in not_fixed
        )
        if not_fixed_details:
            commit_msg += f"\n\nNot fixed (require human action):\n{not_fixed_details}"

        cleanup_artifacts(self.workspace)
        new_sha = github.commit_and_push(self.workspace, commit_msg, branch=self.branch)
        pushed = new_sha is not None

        if pushed:
            marker = self._state_dir / "fix-rereview-passed.txt"
            marker.write_text(new_sha)

            # Post visible summary on the PR (original review gets dismissed
            # by GitHub when the fix commit arrives, so this is the only
            # on-PR confirmation the user sees).
            fixed_lines = "\n".join(
                f"- `{f.get('file', '?')}` — {f.get('fix_description', 'fixed')}"
                for f in all_fixed
            )
            not_fixed_lines = "\n".join(
                f"- {f.get('reason', '?')}: {f.get('detail', '')}"
                for f in not_fixed
            )
            body = f"## Gate Auto-Fix Applied\n\n"
            body += f"Fixed {fixed_count}/{finding_count} findings "
            body += f"in {iteration} iteration(s) ({new_sha[:8]}).\n\n"
            if fixed_lines:
                body += f"**Fixed:**\n{fixed_lines}\n\n"
            if not_fixed_lines:
                body += f"**Not fixed (require human action):**\n{not_fixed_lines}\n"
            github.comment_pr(self.repo, self.pr_number, body)

        state.record_fix_attempt(self.pr_number, repo=self.repo)
        notify.fix_complete(self.pr_number, fixed_count, finding_count, iteration, self.repo)

        summary = (
            f"Fixed {fixed_count}/{finding_count} findings in {iteration} iteration(s)"
        )
        write_live_log(self.pr_number, summary, prefix="fix", repo=self.repo)

        return FixResult(
            success=True,
            pushed=pushed,
            summary=summary,
        )

    def _read_json(self, filename: str) -> dict | None:
        """Read a JSON file from the workspace."""
        path = self.workspace / filename
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None

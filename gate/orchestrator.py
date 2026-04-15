"""Review pipeline orchestrator.

The central state machine that replaces the 1072-line GHA workflow.
Runs each review as a sequence of gates and stages.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

from gate import builder, github, notify, prompt, state
from gate import quota as quota_mod
from gate import workspace as workspace_mod
from gate.claude import spawn_review_stage
from gate.config import repo_slug
from gate.logger import log_review, read_recent_decisions, write_live_log
from gate.runner import StructuredRunner, run_with_retry
from gate.schemas import StageResult
from gate.tmux import kill_window

logger = logging.getLogger(__name__)


class ReviewOrchestrator:
    """Orchestrates a complete PR review lifecycle.

    Per Architecture Revisions:
    - Check run created FIRST (before gates)
    - No GATE_MODE — always enforcement
    - Review timing tracked from start to verdict
    - Cancel path cleans up check run
    - Agent stages via spawn_review_stage() (tmux)
    - Structured stages run inline (no tmux)
    - Exception handler approves PR fail-open
    """

    def __init__(
        self,
        pr_number: int,
        repo: str,
        head_sha: str,
        event: str,
        branch: str,
        labels: list[str],
        config: dict,
        socket_path: Path | None = None,
    ):
        self.pr_number = pr_number
        self.repo = repo
        self.head_sha = head_sha
        self.event = event
        self.branch = branch
        self.labels = labels
        self.config = config
        self.socket_path = socket_path
        self.workspace: Path | None = None
        self.check_run_id: int | None = None
        self.start_time: float | None = None
        self.pr_title: str = ""
        self.pr_body: str = ""
        self.pr_author: str = ""
        self._cancelled = threading.Event()
        self._active_panes: dict[str, str] = {}
        self._panes_lock = threading.Lock()
        self._connection = None
        if self.socket_path:
            from gate.client import GateConnection

            self._connection = GateConnection(self.socket_path)
            self._connection.start()

    def _review_id(self) -> str:
        slug = repo_slug(self.repo)
        return f"{slug}-pr{self.pr_number}"

    def _clone_path(self) -> Path:
        repo_cfg = self.config.get("repo", {})
        clone = repo_cfg.get("clone_path", "")
        if not clone:
            raise RuntimeError(f"repo.clone_path not configured for {self.repo}")
        return Path(clone).expanduser()

    def _emit(self, msg_type: str, **fields) -> None:
        """Emit a lifecycle event to the server (no-op if no connection)."""
        if self._connection:
            self._connection.emit(msg_type, **fields)

    def cancel(self) -> None:
        """Cancel this review. Called by queue when a re-push arrives."""
        self._cancelled.set()
        self._emit(
            "review_cancelled",
            review_id=self._review_id(),
        )
        with self._panes_lock:
            panes = dict(self._active_panes)
        for stage, pane_id in panes.items():
            kill_window(pane_id)
        if self.check_run_id:
            github.complete_check_run(
                self.repo,
                self.check_run_id,
                conclusion="cancelled",
                output_title="Superseded by newer push",
                output_summary="A newer commit was pushed. This review was cancelled.",
                sha=self.head_sha,
            )
        if self.workspace:
            workspace_mod.remove_worktree(self.workspace)

    def run(self) -> None:
        """Execute the full review pipeline."""
        self.start_time = time.monotonic()
        review_id = self._review_id()
        write_live_log(self.pr_number, f"Review started sha={self.head_sha[:8]}", "orchestrator", repo=self.repo)

        self._emit(
            "review_started",
            review={
                "id": review_id,
                "pr_number": self.pr_number,
                "repo": self.repo,
                "head_sha": self.head_sha,
                "status": "running",
            },
        )

        try:
            # === PRE-FLIGHT: Wait for GitHub connectivity ===
            if not github._wait_for_connectivity(max_wait=60.0):
                logger.warning(f"PR #{self.pr_number}: GitHub unreachable at start, proceeding anyway (fail-open)")

            # === CREATE CHECK RUN FIRST ===
            self.check_run_id = github.create_check_run(
                self.repo, self.head_sha, name="gate-review", status="queued"
            )
            self._write_active_marker()

            # === GATE 1: Label checks ===
            if "gate-skip" in self.labels or "gate-emergency-bypass" in self.labels:
                action = "skip" if "gate-skip" in self.labels else "bypass"
                github.approve_pr(
                    self.repo, self.pr_number,
                    f"**Gate: {action}** — review skipped by label.",
                )
                github.complete_check_run(
                    self.repo, self.check_run_id,
                    conclusion="neutral",
                    output_title=f"Gate: {action}",
                    output_summary="Review skipped by label.",
                    sha=self.head_sha,
                )
                write_live_log(self.pr_number, f"Skipped ({action})", "gate", repo=self.repo)
                self._emit("review_completed", review_id=review_id, decision="skip")
                return

            if self.event == "labeled" and "gate-rerun" in self.labels:
                github.remove_label(self.repo, self.pr_number, "gate-rerun")

            # === GATE 2: Circuit breaker ===
            if self._circuit_breaker_tripped():
                github.approve_pr(
                    self.repo, self.pr_number,
                    "**Gate (circuit breaker)** — last 3 reviews were errors. "
                    "Auto-approving. Investigate the gate machine.",
                )
                github.complete_check_run(
                    self.repo, self.check_run_id,
                    conclusion="neutral",
                    output_title="Gate: circuit breaker",
                    output_summary="Last 3 reviews were errors. Auto-approved.",
                    sha=self.head_sha,
                )
                notify.circuit_breaker(self.pr_number, repo=self.repo)
                write_live_log(self.pr_number, "Circuit breaker tripped", "gate", repo=self.repo)
                self._emit("review_completed", review_id=review_id, decision="skip")
                return

            # === SETUP: Fetch PR metadata ===
            self._update_check("Setting up workspace...")
            pr_info = github.get_pr_info(self.repo, self.pr_number)
            self.pr_title = pr_info.get("title", "")
            self.pr_body = pr_info.get("body") or ""
            self.pr_author = pr_info.get("user", {}).get("login", "unknown")

            # === SETUP: Create worktree ===
            self.workspace = workspace_mod.create_worktree(
                self.config["repo"]["clone_path"],
                self.pr_number,
                self.head_sha,
                self.branch,
                repo=self.repo,
                config=self.config,
            )
            write_live_log(self.pr_number, f"Worktree: {self.workspace}", "setup", repo=self.repo)

            # Write PR metadata for agent stages running in tmux
            meta = {
                "pr_title": self.pr_title,
                "pr_body": self.pr_body,
                "pr_author": self.pr_author,
                "pr_number": str(self.pr_number),
                "repo": self.repo,
                "head_sha": self.head_sha,
            }
            (self.workspace / "pr-metadata.json").write_text(json.dumps(meta))

            # === Fetch prior review state ===
            prior = state.load_prior_review(self.pr_number, self.workspace, repo=self.repo)

            # === GATE 3: Fix re-run detection ===
            is_fix_rerun = self._detect_fix_rerun(prior)

            # === GATE 4: Quota check ===
            quota = quota_mod.check_quota()
            if not quota["quota_ok"]:
                github.comment_pr(
                    self.repo, self.pr_number,
                    f"**Gate: Quota low** — {quota.get('reason', '')}. "
                    "Review deferred. PR is not blocked.",
                )
                github.approve_pr(
                    self.repo, self.pr_number,
                    "**Gate (quota pause)** — auto-approved, quota low.",
                )
                github.complete_check_run(
                    self.repo, self.check_run_id,
                    conclusion="neutral",
                    output_title="Gate: quota pause",
                    output_summary=f"Quota at {quota.get('five_hour_pct', '?')}%. Deferred.",
                    sha=self.head_sha,
                )
                self._emit("review_completed", review_id=review_id, decision="skip")
                return

            # === GATE 5: Review cycle limit ===
            max_cycles = self.config.get("limits", {}).get("max_review_cycles", 5)
            if prior.get("review_count", 0) >= max_cycles:
                github.comment_pr(
                    self.repo, self.pr_number,
                    f"**Gate: Review cycle limit reached** — {max_cycles} reviews on this PR.",
                )
                github.approve_pr(
                    self.repo, self.pr_number,
                    "**Gate (cycle limit)** — auto-approved after max review cycles.",
                )
                github.complete_check_run(
                    self.repo, self.check_run_id,
                    conclusion="neutral",
                    output_title="Gate: cycle limit",
                    output_summary=f"Reached {max_cycles} review cycles.",
                    sha=self.head_sha,
                )
                self._emit("review_completed", review_id=review_id, decision="skip")
                return

            if self._cancelled.is_set():
                self._emit("review_completed", review_id=review_id, decision="skip")
                return

            # === STAGE 1: TRIAGE (structured — inline) ===
            if is_fix_rerun:
                triage = self._load_cached_triage()
            else:
                self._update_check("Stage 1: Triage (Sonnet)...")
                self._emit("review_stage_update", review_id=review_id, stage="triage", status="running")
                write_live_log(self.pr_number, "Triage starting", "stage", repo=self.repo)
                triage = self._run_structured_stage("triage")
                self._save_stage_result("triage", triage)

            # === STAGE 2: BUILD (direct execution) ===
            self._update_check("Stage 2: Build verification...")
            write_live_log(self.pr_number, "Build starting", "stage", repo=self.repo)
            build_result = builder.run_build(self.workspace)
            (self.workspace / "build.json").write_text(json.dumps(build_result, indent=2))

            # === Fast-track check ===
            fast_track = (
                triage.data.get("fast_track_eligible")
                and build_result.get("overall_pass")
            )

            if not fast_track:
                # === STAGE 3: ARCHITECTURE (agent — tmux) ===
                if not is_fix_rerun:
                    if self._cancelled.is_set():
                        self._emit("review_completed", review_id=review_id, decision="skip")
                        return
                    self._update_check("Stage 3: Architecture review (Sonnet)...")
                    self._emit("review_stage_update", review_id=review_id, stage="architecture", status="running")
                    write_live_log(self.pr_number, "Architecture starting", "stage", repo=self.repo)
                    arch_result = self._run_agent_stage("architecture")
                    self._save_stage_result("architecture", arch_result)

                # === STAGE 4: SECURITY (agent — tmux) ===
                if not is_fix_rerun:
                    if self._cancelled.is_set():
                        self._emit("review_completed", review_id=review_id, decision="skip")
                        return
                    self._update_check("Stage 4: Security review (Opus)...")
                    self._emit("review_stage_update", review_id=review_id, stage="security", status="running")
                    write_live_log(self.pr_number, "Security starting", "stage", repo=self.repo)
                    sec_result = self._run_agent_stage("security")
                    self._save_stage_result("security", sec_result)

                # === STAGE 5: LOGIC (agent — tmux) ===
                if self._cancelled.is_set():
                    self._emit("review_completed", review_id=review_id, decision="skip")
                    return
                self._update_check("Stage 5: Logic review (Opus)...")
                self._emit("review_stage_update", review_id=review_id, stage="logic", status="running")
                write_live_log(self.pr_number, "Logic starting", "stage", repo=self.repo)
                logic_result = self._run_agent_stage("logic")
                self._save_stage_result("logic", logic_result)

            # === Cleanup gate test files ===
            self._cleanup_gate_tests()

            # === STAGE 6: VERDICT (structured — inline) ===
            self._update_check("Stage 6: Rendering verdict (Sonnet)...")
            self._emit("review_stage_update", review_id=review_id, stage="verdict", status="running")
            write_live_log(self.pr_number, "Verdict starting", "stage", repo=self.repo)
            elapsed = int(time.monotonic() - self.start_time)
            verdict = self._run_structured_stage("verdict")
            verdict.data["review_time_seconds"] = elapsed
            self._save_stage_result("verdict", verdict)

            # === POST REVIEW ===
            github.post_review(
                self.repo, self.pr_number, verdict.data, build_result, self.head_sha,
                config=self.config,
            )

            # Complete check run
            decision = verdict.data.get("decision", "approve")
            if decision in ("approve", "approve_with_notes"):
                conclusion = "success"
            else:
                conclusion = "action_required"
            findings_count = verdict.data.get("stats", {}).get("total_findings", 0)
            github.complete_check_run(
                self.repo, self.check_run_id, conclusion,
                output_title=f"Gate Review: {decision} ({findings_count} findings)",
                output_summary=verdict.data.get("summary", ""),
                sha=self.head_sha,
            )

            # === LOG + NOTIFY ===
            log_review(
                self.pr_number, verdict.data, build_result,
                elapsed, quota, triage=triage.data, repo=self.repo,
            )
            notify.review_complete(self.pr_number, verdict.data, self.repo)

            # === PERSIST STATE ===
            clone_path = self._clone_path()
            state.persist_review_state(
                self.pr_number, self.head_sha, self.workspace,
                decision, clone_path=clone_path, repo=self.repo,
            )

            write_live_log(
                self.pr_number,
                f"Review complete: {decision} ({findings_count} findings, {elapsed}s)",
                "orchestrator",
                repo=self.repo,
            )

            # === FIX PIPELINE (if warranted) ===
            if self._should_fix(verdict.data):
                self._emit("review_stage_update", review_id=review_id,
                           stage="fix-bootstrap", status="fixing")
                fix_check_id = github.create_check_run(
                    self.repo, self.head_sha, name="Gate Auto-Fix", status="in_progress"
                )
                try:
                    from gate.fixer import FixPipeline
                    from gate.logger import log_fix_result

                    fixer = FixPipeline(
                        self.pr_number, self.repo, self.workspace,
                        verdict.data, build_result, self.config,
                        check_run_id=fix_check_id,
                        cancelled=self._cancelled,
                        socket_path=self.socket_path,
                    )
                    fixer.branch = self.branch

                    self._emit("review_stage_update", review_id=review_id,
                               stage="fix-session", status="fixing")
                    fix_result = fixer.run()

                    if fixer.fix_pane_id:
                        with self._panes_lock:
                            self._active_panes["fix-senior"] = fixer.fix_pane_id

                    fix_conclusion = "success" if fix_result.success else "failure"
                    self._emit("review_stage_update", review_id=review_id,
                               stage="fix-commit", status="fixing")
                    github.complete_check_run(
                        self.repo, fix_check_id, fix_conclusion,
                        output_title=(
                            "Gate Auto-Fix: succeeded"
                            if fix_result.success
                            else "Gate Auto-Fix: failed"
                        ),
                        output_summary=fix_result.summary,
                        sha=self.head_sha,
                    )
                    log_fix_result(
                        self.pr_number, fix_result.success,
                        fix_result.summary, decision, repo=self.repo,
                    )
                except Exception as fix_err:
                    logger.exception(f"Fix pipeline crashed for PR #{self.pr_number}")
                    github.complete_check_run(
                        self.repo, fix_check_id, "failure",
                        output_title="Gate Auto-Fix: crashed",
                        output_summary=str(fix_err),
                        sha=self.head_sha,
                    )
                finally:
                    with self._panes_lock:
                        self._active_panes.pop("fix-senior", None)

            self._emit("review_completed", review_id=review_id, decision=decision)

        except Exception as e:
            # Fail-open
            self._emit("review_completed", review_id=review_id, decision="error")
            logger.exception(f"Review failed for PR #{self.pr_number}")
            github.approve_pr(
                self.repo, self.pr_number,
                f"**Gate (error)** — review failed: {e}. Auto-approving.",
            )
            if self.check_run_id:
                github.complete_check_run(
                    self.repo, self.check_run_id,
                    conclusion="cancelled",
                    output_title="Gate Review: error",
                    output_summary=f"Review crashed: {e}. PR auto-approved (fail-open).",
                    sha=self.head_sha,
                )
            notify.review_failed(self.pr_number, str(e), repo=self.repo)
            write_live_log(self.pr_number, f"FAILED: {e}", "orchestrator", repo=self.repo)
        finally:
            self._remove_active_marker()
            if self.workspace:
                workspace_mod.remove_worktree(self.workspace)
            if self._connection:
                self._connection.stop()

    # ── Stage Runners ────────────────────────────────────────

    def _run_agent_stage(self, stage_name: str) -> StageResult:
        """Spawn an agent stage in tmux and wait for completion."""
        return run_with_retry(
            lambda: self._spawn_and_wait_agent(stage_name),
            stage_name,
            self.config,
        )

    def _spawn_and_wait_agent(self, stage_name: str) -> StageResult:
        """Spawn a single agent attempt and poll for its result file."""
        result_file = self.workspace / f"{stage_name}-result.json"
        result_file.unlink(missing_ok=True)

        pane_id = spawn_review_stage(
            review_id=self._review_id(),
            stage=stage_name,
            workspace=str(self.workspace),
            socket_path=str(self.socket_path) if self.socket_path else None,
            repo=self.repo,
        )
        if not pane_id:
            return StageResult.fallback(stage_name)

        with self._panes_lock:
            self._active_panes[stage_name] = pane_id

        timeout = self.config.get("timeouts", {}).get("agent_stage_s", 900)
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if self._cancelled.is_set():
                kill_window(pane_id)
                return StageResult(stage=stage_name, success=False, data={}, cancelled=True)
            if result_file.exists():
                try:
                    envelope = json.loads(result_file.read_text())
                    with self._panes_lock:
                        self._active_panes.pop(stage_name, None)
                    return StageResult(
                        stage=stage_name,
                        success=envelope.get("success", False),
                        data=envelope.get("data", {}),
                    )
                except json.JSONDecodeError:
                    pass
            time.sleep(5)

        kill_window(pane_id)
        with self._panes_lock:
            self._active_panes.pop(stage_name, None)
        return StageResult.fallback(stage_name)

    def _run_structured_stage(self, stage_name: str) -> StageResult:
        """Run a structured stage inline (no tmux)."""
        template = prompt.load(stage_name)
        vars_dict = prompt.build_vars(self.workspace, stage_name, self._env_vars(), self.config)
        assembled = prompt.safe_substitute(template, vars_dict, f"orchestrator-{stage_name}")
        return run_with_retry(
            lambda: StructuredRunner().run(stage_name, assembled, self.workspace, self.config),
            stage_name,
            self.config,
        )

    # ── Helpers ──────────────────────────────────────────────

    def _env_vars(self) -> dict:
        """Build env vars dict for prompt template substitution."""
        return {
            "pr_number": str(self.pr_number),
            "pr_title": self.pr_title,
            "pr_body": self.pr_body,
            "pr_author": self.pr_author,
            "repo": self.repo,
            "head_sha": self.head_sha,
        }

    def _should_fix(self, verdict: dict) -> bool:
        """Determine if fix pipeline should run. Always unless suppressed by label."""
        if "gate-no-fix" in self.labels:
            return False
        return verdict.get("decision") in ("request_changes", "approve_with_notes")

    def _update_check(self, title: str) -> None:
        """Update check run with stage progress."""
        if self.check_run_id:
            github.update_check_run(
                self.repo, self.check_run_id,
                status="in_progress", output_title=title,
            )

    def _save_stage_result(self, stage_name: str, result: StageResult) -> None:
        """Save stage result JSON to workspace."""
        if self.workspace and result.data:
            path = self.workspace / f"{stage_name}.json"
            path.write_text(json.dumps(result.data, indent=2))

    def _circuit_breaker_tripped(self) -> bool:
        """Check if last 3 reviews were errors."""
        recent = read_recent_decisions(3)
        return len(recent) >= 3 and all(d == "error" for d in recent)

    def _detect_fix_rerun(self, prior: dict) -> bool:
        """Detect if this is a re-review after a fix commit."""
        if not prior.get("has_prior"):
            return False
        prior_decision = prior.get("prior_decision", "")
        return prior_decision in ("request_changes", "approve_with_notes")

    def _load_cached_triage(self) -> StageResult:
        """Load triage from prior review state (for fix reruns)."""
        triage_path = state.get_pr_state_dir(self.pr_number, self.repo) / "triage.json"
        if triage_path.exists():
            try:
                data = json.loads(triage_path.read_text())
                # Also copy to workspace for prompt vars
                if self.workspace:
                    (self.workspace / "triage.json").write_text(json.dumps(data, indent=2))
                return StageResult(stage="triage", success=True, data=data)
            except json.JSONDecodeError:
                pass
        return StageResult.fallback("triage")

    def _cleanup_gate_tests(self) -> None:
        """Remove any gate-specific test files from the worktree."""
        if not self.workspace:
            return
        for pattern in ("**/*.gate-test.*", "**/gate-test-*"):
            for f in self.workspace.glob(pattern):
                try:
                    f.unlink()
                except OSError:
                    pass

    def _write_active_marker(self) -> None:
        """Write marker for orphaned check run recovery."""
        marker = state.get_pr_state_dir(self.pr_number, self.repo) / "active_review.json"
        marker.write_text(json.dumps({
            "check_run_id": self.check_run_id,
            "review_id": self._review_id(),
            "started_at": time.time(),
            "head_sha": self.head_sha,
            "pid": os.getpid(),
            "repo": self.repo,
        }))

    def _remove_active_marker(self) -> None:
        marker = state.get_pr_state_dir(self.pr_number, self.repo) / "active_review.json"
        marker.unlink(missing_ok=True)

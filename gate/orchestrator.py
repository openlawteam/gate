"""Review pipeline orchestrator.

The central state machine that replaces the 1072-line GHA workflow.
Runs each review as a sequence of gates and stages.
"""

import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path

from gate import builder, github, notify, prompt, state
from gate import quota as quota_mod
from gate import workspace as workspace_mod
from gate.claude import spawn_review_stage
from gate.config import repo_slug
from gate.io import atomic_write
from gate.logger import log_review, read_recent_decisions, write_live_log
from gate.runner import StructuredRunner, run_with_retry
from gate.schemas import StageResult, WorkspaceVanishedError
from gate.tmux import kill_window

logger = logging.getLogger(__name__)


# Reason → (conclusion, output_title, output_summary) lookup for ReviewOrchestrator.cancel().
#
# - ``"superseded"`` — default/back-compat: a newer enqueue displaced this
#   review. Reports ``failure`` (via ``_CONCLUSION_TO_STATE``) because the
#   branch really did get abandoned.
# - ``"manual"`` — operator-initiated (``gate cancel`` or socket call).
#   Reports ``neutral`` (green checkmark) — nothing failed, the operator
#   chose to stop the review.
# - ``"timeout"`` — internal watchdog tripped. Reports ``failure`` since
#   the SHA's review didn't complete.
#
# Reasons are looked up in this dict so typos surface immediately as
# ``KeyError`` rather than silently routing to the default.
_CANCEL_REASON_PAYLOADS: dict[str, tuple[str, str, str]] = {
    "superseded": (
        "cancelled",
        "Superseded by newer push",
        "A newer commit was pushed. This review was cancelled.",
    ),
    "manual": (
        "neutral",
        "Review cancelled by operator",
        "Cancelled via `gate cancel`.",
    ),
    "timeout": (
        "cancelled",
        "Review timed out",
        "Internal timeout reached.",
    ),
}


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
        self.check_name: str = "gate-review"
        self.is_post_fix_rereview: bool = False
        self.start_time: float | None = None
        self.pr_title: str = ""
        self.pr_body: str = ""
        self.pr_author: str = ""
        self._cancelled = threading.Event()
        # Dedicated lock that guards the cancel(check-then-set) race so
        # that concurrent cancel callers (e.g. the socket path AND the
        # server.py:299 echo of our own ``review_cancelled`` event) can
        # never each flip ``_cancelled`` from unset → set. Without this,
        # a second cancel with a different ``reason`` would overwrite
        # the first ``complete_check_run`` payload (see Issue #17).
        self._cancel_lock = threading.Lock()
        self._active_panes: dict[str, str] = {}
        self._panes_lock = threading.Lock()
        self._teardown_lock = threading.Lock()
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
        """Emit a lifecycle event to the server (no-op if no connection).

        Every event is stamped with this orchestrator's ``head_sha`` so the
        server can distinguish it from a **stale** event emitted by a
        superseded orchestrator for the same ``review_id`` (which is
        deterministic per-PR — ``{repo_slug}-pr{pr_number}`` — so two
        orchestrators racing on the same PR collide). See
        ``GateServer._head_sha_matches``.
        """
        if self._connection:
            fields.setdefault("head_sha", self.head_sha)
            self._connection.emit(msg_type, **fields)

    def cancel(self, reason: str = "superseded") -> None:
        """Cancel this review. Called by queue/socket callers.

        This is a **signal-only** operation: we set ``_cancelled``, tear
        down tmux panes, and write the final status for the old check
        run. We deliberately **do not** remove the worktree here — that
        would race the still-running ``run()`` thread, which may be
        mid-stage (``_save_stage_result`` writing ``triage.json`` etc.)
        and would crash with ``FileNotFoundError``. Worktree removal is
        owned by ``run()``'s ``finally``, which runs after the stages
        have observed the cancellation flag and exited cleanly (Group 2B).

        ``reason`` routes the GitHub check payload via
        ``_CANCEL_REASON_PAYLOADS`` so the issue/PR author sees an
        accurate status (Issue #17):

        - ``"superseded"`` (default) — new commit displaced this review.
        - ``"manual"`` — operator ran ``gate cancel``; renders neutral.
        - ``"timeout"`` — internal timeout reached.

        Idempotent and concurrency-safe: ``_cancel_lock`` prevents a
        check-then-set race when the socket path AND the
        ``review_cancelled`` echo in ``gate/server.py`` both invoke a
        cancel. Second entries no-op, so the first caller's ``reason``
        wins and ``complete_check_run`` is called at most once per
        review.
        """
        with self._cancel_lock:
            if self._cancelled.is_set():
                return
            self._cancelled.set()

        conclusion, output_title, output_summary = _CANCEL_REASON_PAYLOADS[reason]

        self._emit(
            "review_cancelled",
            review_id=self._review_id(),
        )
        with self._panes_lock:
            panes = dict(self._active_panes)
        for stage, pane_id in panes.items():
            kill_window(pane_id)
        if self.check_run_id:
            # This write is the marker for the OLD review;
            # Group 2C prevents the continuing ``run()`` thread from
            # clobbering it with a post-cancel ``error`` write.
            github.complete_check_run(
                self.repo,
                self.check_run_id,
                conclusion=conclusion,
                output_title=output_title,
                output_summary=output_summary,
                sha=self.head_sha,
            )
        # NOTE: remove_worktree intentionally not called here — see
        # docstring. run()'s finally handles it once the stages yield.

    def run(self) -> None:
        """Execute the full review pipeline."""
        self.start_time = time.monotonic()
        review_id = self._review_id()
        write_live_log(
            self.pr_number, f"Review started sha={self.head_sha[:8]}",
            "orchestrator", repo=self.repo,
        )

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
                logger.warning(
                    f"PR #{self.pr_number}: GitHub unreachable"
                    " at start, proceeding anyway (fail-open)"
                )

            # === DETECT POST-FIX RE-REVIEW ===
            # If the head commit was authored by the bot, this is Gate
            # re-reviewing its own auto-fix. Tag the check status
            # accordingly so humans can tell at a glance.
            bot_account = self.config.get("repo", {}).get("bot_account") or "gate-bot"
            bot_email = f"{bot_account}@users.noreply.github.com"
            author_email = github.get_commit_author_email(self.repo, self.head_sha)
            if author_email and author_email.strip().lower() == bot_email.lower():
                self.is_post_fix_rereview = True
                self.check_name = "gate-review (post-fix)"
                logger.info(
                    f"PR #{self.pr_number}: HEAD {self.head_sha[:8]} "
                    f"authored by bot ({bot_email}) — tagging as post-fix re-review"
                )

            # === CREATE CHECK RUN FIRST ===
            self.check_run_id = github.create_check_run(
                self.repo, self.head_sha, name=self.check_name, status="queued"
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

            # A cancel that arrives while we are still in pre-workspace
            # setup (fetch PR metadata, etc.) would otherwise race a
            # superseding orchestrator on ``git worktree add`` — both pick
            # the same path and the loser fails with exit 128.
            if self._cancelled.is_set():
                self._emit("review_completed", review_id=review_id, decision="skip")
                return

            # === SETUP: Create worktree ===
            try:
                self.workspace = workspace_mod.create_worktree(
                    self.config["repo"]["clone_path"],
                    self.pr_number,
                    self.head_sha,
                    self.branch,
                    repo=self.repo,
                    config=self.config,
                )
            except workspace_mod.BranchNotFoundError as e:
                # PR branch was deleted between webhook and fetch — the
                # PR is effectively gone. Skip quietly; no fail-open
                # approval spam on a branch that no longer exists
                # (Group 3A).
                logger.info(
                    f"PR #{self.pr_number}: branch {self.branch!r} gone, skipping ({e})"
                )
                write_live_log(
                    self.pr_number,
                    f"Branch gone, skipping: {e}",
                    "orchestrator", repo=self.repo,
                )
                if self.check_run_id:
                    github.complete_check_run(
                        self.repo, self.check_run_id,
                        conclusion="neutral",
                        output_title="Gate: branch not found",
                        output_summary="PR branch no longer exists on origin.",
                        sha=self.head_sha,
                    )
                self._emit("review_completed", review_id=review_id, decision="skip")
                return
            except WorkspaceVanishedError as e:
                # Superseded before workspace finished setting up
                # (Group 3B). Treat as a quiet cancel.
                logger.info(
                    f"PR #{self.pr_number}: workspace vanished during setup: {e}"
                )
                self._cancelled.set()
                self._emit("review_completed", review_id=review_id, decision="cancelled")
                return
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
                self._emit(
                    "review_stage_update", review_id=review_id,
                    stage="triage", status="running",
                )
                write_live_log(self.pr_number, "Triage starting", "stage", repo=self.repo)
                triage = self._run_structured_stage("triage")
                self._save_stage_result("triage", triage)

            # === STAGE 2: BUILD (direct execution) ===
            self._update_check("Stage 2: Build verification...")
            write_live_log(self.pr_number, "Build starting", "stage", repo=self.repo)
            build_result = builder.run_build(self.workspace, config=self.config)
            (self.workspace / "build.json").write_text(json.dumps(build_result, indent=2))

            # === Fast-track check ===
            fast_track = (
                triage.data.get("fast_track_eligible")
                and build_result.get("overall_pass")
            )

            if not fast_track:
                # === STAGE 2.5: POSTCONDITIONS (structured — inline) ===
                # Only runs on high/critical risk and only when the repo
                # has not opted out. Fast-tracked PRs skip entirely
                # (we're inside `if not fast_track`). Fix reruns reuse
                # the cached postconditions so Logic sees the same
                # contract on every iteration. The risk-level guard
                # below (``risk in ("high", "critical")``) is the
                # source of truth — keep ``prompts/postconditions.md``
                # and this comment in sync if you change it.
                if is_fix_rerun:
                    cached_pc = self._load_cached_postconditions()
                    if cached_pc is not None:
                        # _load_cached_postconditions already writes
                        # postconditions.json into the workspace.
                        pass
                else:
                    repo_cfg = self.config.get("repo", {})
                    risk = triage.data.get("risk_level")
                    if (
                        repo_cfg.get("enable_postconditions", True)
                        and risk in ("high", "critical")
                    ):
                        if self._cancelled.is_set():
                            self._emit("review_completed", review_id=review_id, decision="skip")
                            return
                        self._update_check("Stage 2.5: Postconditions (Sonnet)...")
                        self._emit(
                            "review_stage_update", review_id=review_id,
                            stage="postconditions", status="running",
                        )
                        write_live_log(
                            self.pr_number, "Postconditions starting",
                            "stage", repo=self.repo,
                        )
                        pc_result = self._run_structured_stage("postconditions")
                        self._save_stage_result("postconditions", pc_result)

                # === STAGE 3: ARCHITECTURE (agent — tmux) ===
                if not is_fix_rerun:
                    if self._cancelled.is_set():
                        self._emit("review_completed", review_id=review_id, decision="skip")
                        return
                    self._update_check("Stage 3: Architecture review (Sonnet)...")
                    self._emit(
                        "review_stage_update", review_id=review_id,
                        stage="architecture", status="running",
                    )
                    write_live_log(self.pr_number, "Architecture starting", "stage", repo=self.repo)
                    arch_result = self._run_agent_stage("architecture")
                    self._save_stage_result("architecture", arch_result)

                # === STAGE 4: SECURITY (agent — tmux) ===
                if not is_fix_rerun:
                    if self._cancelled.is_set():
                        self._emit("review_completed", review_id=review_id, decision="skip")
                        return
                    self._update_check("Stage 4: Security review (Opus)...")
                    self._emit(
                        "review_stage_update", review_id=review_id,
                        stage="security", status="running",
                    )
                    write_live_log(self.pr_number, "Security starting", "stage", repo=self.repo)
                    sec_result = self._run_agent_stage("security")
                    self._save_stage_result("security", sec_result)

                # === STAGE 5: LOGIC (agent — tmux) ===
                if self._cancelled.is_set():
                    self._emit("review_completed", review_id=review_id, decision="skip")
                    return
                self._update_check("Stage 5: Logic review (Opus)...")
                self._emit(
                    "review_stage_update", review_id=review_id,
                    stage="logic", status="running",
                )
                write_live_log(self.pr_number, "Logic starting", "stage", repo=self.repo)
                logic_result = self._run_agent_stage("logic")
                self._save_stage_result("logic", logic_result)

            # === Cleanup gate test files ===
            self._cleanup_gate_tests()

            # === STAGE 6: VERDICT (structured — inline) ===
            self._update_check("Stage 6: Rendering verdict (Sonnet)...")
            self._emit(
                "review_stage_update", review_id=review_id,
                stage="verdict", status="running",
            )
            write_live_log(self.pr_number, "Verdict starting", "stage", repo=self.repo)
            verdict = self._run_structured_stage("verdict")
            elapsed = int(time.monotonic() - self.start_time)
            verdict.data["review_time_seconds"] = elapsed

            # Collapse findings that describe one logical issue at
            # multiple sites (PR A.2). Runs BEFORE finding_id stamping
            # so the hash is computed on the canonical representative
            # (primary location), preserving the existing
            # (file, line, source_stage, message) hash scheme — critical
            # for cross-review matching in post-fix re-reviews.
            from gate.extract import _dedupe_findings
            if isinstance(verdict.data.get("findings"), list):
                verdict.data["findings"] = _dedupe_findings(
                    verdict.data["findings"]
                )

            # Stamp stable finding_ids on every finding BEFORE we persist
            # verdict.json. Without this, the ROI diff in post-fix
            # re-reviews can't match prior→current findings
            # (Group 2D, audit-revealed).
            from gate.finding_id import compute_finding_id as _cfi
            for _f in verdict.data.get("findings") or []:
                if isinstance(_f, dict) and not _f.get("finding_id"):
                    _f["finding_id"] = _cfi(_f)

            self._save_stage_result("verdict", verdict)

            # If a re-push landed while we were rendering the verdict,
            # posting it now would confuse users (PR review/check for a
            # superseded SHA) and stomp on the ``cancelled`` check run
            # that ``cancel()`` already wrote. Bail quietly — the new
            # orchestrator owns the live state (Group 2C).
            if self._cancelled.is_set():
                self._emit("review_completed", review_id=review_id, decision="cancelled")
                return

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
            # Re-review ROI diff (Group 2D): count prior → current finding
            # overlap so we can later ask "how often does post-fix
            # re-review surface NEW findings?". Only populated when we
            # have a prior verdict to compare against.
            roi_kwargs: dict = {}
            if prior and prior.get("has_prior"):
                prior_ids = {
                    f["finding_id"] for f in prior.get("prior_findings", [])
                    if f.get("finding_id")
                }
                current_ids = {
                    f["finding_id"] for f in verdict.data.get("findings", []) or []
                    if f.get("finding_id")
                }
                roi_kwargs = {
                    "is_post_fix_rereview": self.is_post_fix_rereview,
                    "prior_findings_count": len(prior_ids),
                    "new_findings_count": len(current_ids - prior_ids),
                    "persisting_findings_count": len(current_ids & prior_ids),
                    "resolved_since_prior_count": len(prior_ids - current_ids),
                }

            log_review(
                self.pr_number, verdict.data, build_result,
                elapsed, quota, triage=triage.data, repo=self.repo,
                **roi_kwargs,
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

            # === PHASE 5: SPEC-PR PROMOTION (approve path only) ===
            # On the approve path, the fix pipeline never runs, so any
            # surviving __gate_test_* files would linger until worktree
            # teardown. We capture them into state *before* the second
            # (narrower) cleanup scrubs the worktree.
            if decision in ("approve", "approve_with_notes"):
                self._promote_spec_tests(verdict)
                self._cleanup_underscore_gate_tests()

            # === FIX PIPELINE (if warranted) ===
            fix_completed = False
            if self._should_fix(verdict.data):
                # Phase 4: if the author has left a disambig question
                # pending for too many fix-reruns, tell the senior to
                # pick the safest interpretation instead of halting.
                # Re-written to pr-metadata.json so the fix-senior
                # runner picks it up via `build_vars`.
                self._refresh_force_safest_flag()

                self._emit(
                    "review_stage_update", review_id=review_id,
                    stage="fix-bootstrap", status="fixing",
                )
                fix_check_id = github.create_check_run(
                    self.repo, self.head_sha,
                    name="Gate Auto-Fix", status="in_progress",
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
                        review_id=review_id,
                    )
                    fixer.branch = self.branch

                    self._emit(
                        "review_stage_update", review_id=review_id,
                        stage="fix-session", status="fixing",
                    )
                    fix_start = time.monotonic()
                    fix_result = fixer.run()
                    fix_elapsed = int(time.monotonic() - fix_start)

                    if fixer.fix_pane_id:
                        with self._panes_lock:
                            self._active_panes["fix-senior"] = fixer.fix_pane_id

                    # success=True && pushed=False == graceful no-op.
                    # Report as a success ("no mechanical changes needed")
                    # to GitHub but log as ``no_op`` so reviews.jsonl
                    # consumers can tell the difference (audit A10).
                    is_no_op = bool(
                        fix_result.success and not fix_result.pushed
                    )
                    # Distinguish partial success ("pushed but some findings
                    # require human action") from clean success so humans can
                    # see it on the PR without opening the comment. The legacy
                    # statuses API maps ``neutral`` → ``success`` state; the
                    # title/description is the primary visible signal.
                    fix_total = fix_result.fixed_count + fix_result.not_fixed_count
                    if not fix_result.success:
                        fix_conclusion = "failure"
                        check_title = "Gate Auto-Fix: failed"
                    elif is_no_op:
                        fix_conclusion = "success"
                        check_title = "Gate Auto-Fix: skipped (no mechanical changes needed)"
                    elif fix_result.not_fixed_count > 0:
                        fix_conclusion = "neutral"
                        check_title = (
                            f"Gate Auto-Fix: partial "
                            f"({fix_result.fixed_count}/{fix_total}) — "
                            f"{fix_result.not_fixed_count} require human action"
                        )
                    else:
                        fix_conclusion = "success"
                        check_title = "Gate Auto-Fix: all findings resolved"

                    self._emit(
                        "review_stage_update", review_id=review_id,
                        stage="fix-commit", status="fixing",
                    )
                    github.complete_check_run(
                        self.repo, fix_check_id, fix_conclusion,
                        output_title=check_title,
                        output_summary=fix_result.summary,
                        sha=self.head_sha,
                    )
                    self._emit(
                        "review_completed",
                        review_id=review_id, decision=decision,
                    )
                    fix_completed = True
                    log_fix_result(
                        self.pr_number, fix_result.success,
                        fix_result.summary, decision, repo=self.repo,
                        fix_elapsed_seconds=fix_elapsed,
                        status=(
                            "no_op" if is_no_op
                            else ("succeeded" if fix_result.success else "failed")
                        ),
                        pipeline_mode=fix_result.pipeline_mode or None,
                        sub_scope_total=(
                            fix_result.sub_scope_total
                            if fix_result.sub_scope_total
                            else None
                        ),
                        sub_scope_committed=(
                            fix_result.sub_scope_committed
                            if fix_result.sub_scope_total
                            else None
                        ),
                        sub_scope_reverted=(
                            fix_result.sub_scope_reverted
                            if fix_result.sub_scope_total
                            else None
                        ),
                        sub_scope_empty=(
                            fix_result.sub_scope_empty
                            if fix_result.sub_scope_total
                            else None
                        ),
                        wall_clock_seconds=(
                            fix_result.wall_clock_seconds
                            if fix_result.wall_clock_seconds
                            else None
                        ),
                        runaway_guard_hit=(
                            True if fix_result.runaway_guard_hit else None
                        ),
                        fixed_count=(
                            fix_result.fixed_count
                            if fix_result.fixed_count or fix_result.not_fixed_count
                            else None
                        ),
                        not_fixed_count=(
                            fix_result.not_fixed_count
                            if fix_result.fixed_count or fix_result.not_fixed_count
                            else None
                        ),
                        commit_message_source=(
                            fix_result.commit_message_source or None
                        ),
                        commit_message_reject_reason=(
                            fix_result.commit_message_reject_reason or None
                        ),
                    )
                except Exception as fix_err:
                    logger.exception(
                        f"Fix pipeline crashed for PR #{self.pr_number}"
                    )
                    github.complete_check_run(
                        self.repo, fix_check_id, "failure",
                        output_title="Gate Auto-Fix: crashed",
                        output_summary=str(fix_err),
                        sha=self.head_sha,
                    )
                finally:
                    with self._panes_lock:
                        self._active_panes.pop("fix-senior", None)
                    # Phase 5: scrub any lingering underscore gate-test
                    # files so the block-path worktree isn't left dirty.
                    # Idempotent with the fixer's per-iteration cleanup.
                    self._cleanup_underscore_gate_tests()

            if not fix_completed:
                self._emit(
                    "review_completed",
                    review_id=review_id, decision=decision,
                )

        except Exception as e:
            # When the review was cancelled, a crash on the way out is
            # expected (worktree gone, agents killed). Downgrade the log
            # level and skip the ``error`` GitHub write so we don't
            # clobber the ``cancelled`` marker the cancel() wrote
            # (Group 2C).
            if self._cancelled.is_set():
                logger.info(
                    f"Review cancelled mid-stage for PR #{self.pr_number}: {e}"
                )
                self._emit("review_completed", review_id=review_id, decision="cancelled")
            else:
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
            # Single teardown path (Group 2B) — cancel() no longer does
            # this, so we must, regardless of how run() exits.
            if self.workspace:
                with self._teardown_lock:
                    workspace_mod.remove_worktree(self.workspace)
                    self.workspace = None
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
        """Determine if fix pipeline should run.

        Rules (in order):
        - ``gate-no-fix`` label always short-circuits to False.
        - ``request_changes`` always runs the fix pipeline.
        - ``approve_with_notes`` runs the fix pipeline only when the
          per-repo ``fix_on_approve_with_notes`` flag is true
          (default true for back-compat). Repos that prefer to treat
          ``approve_with_notes`` as a success-no-op can flip this off
          per-repo in ``gate.toml`` (Group 1D).
        """
        from gate.config import get_repo_bool
        if "gate-no-fix" in self.labels:
            return False
        decision = verdict.get("decision")
        if decision == "request_changes":
            return True
        if decision == "approve_with_notes":
            return get_repo_bool(self.config, "fix_on_approve_with_notes", True)
        return False

    def _update_check(self, title: str) -> None:
        """Update check run with stage progress."""
        if self.check_run_id:
            github.update_check_run(
                self.repo, self.check_run_id,
                status="in_progress", output_title=title,
            )

    def _save_stage_result(self, stage_name: str, result: StageResult) -> None:
        """Save stage result JSON to workspace (Group 2A).

        Becomes a silent no-op when the review was cancelled AND the
        workspace has been removed (i.e., the cancel tore down the
        worktree between the stage completing and the orchestrator
        getting here). Without the guard, a superseded review's
        ``_save_stage_result`` crashes with ``FileNotFoundError`` on the
        missing parent directory — observed on PRs 196, 204, 208, 211.

        If the workspace is missing but the review was NOT cancelled,
        that is a real bug and we re-raise so it shows up in logs.
        """
        if not self.workspace or not result.data:
            return
        if self._cancelled.is_set() or not self.workspace.exists():
            logger.info(f"Skipping {stage_name}.json write: review cancelled")
            return
        path = self.workspace / f"{stage_name}.json"
        try:
            path.write_text(json.dumps(result.data, indent=2))
        except (FileNotFoundError, OSError) as e:
            if self._cancelled.is_set():
                logger.info(f"{stage_name}.json write failed after cancel: {e}")
                return
            raise

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

    def _refresh_force_safest_flag(self) -> None:
        """Phase 4: flip ``force_safest_interpretation`` once the author
        has left a disambig question pending for too many fix-reruns.

        Reads ``state/<slug>/pr<N>/disambig_stale_count.txt`` (written
        by :func:`FixPipeline._bump_disambig_stale_count`) and mutates
        ``pr-metadata.json`` in the worktree so the fix-senior runner
        sees the flag via :func:`prompt.build_vars`. Fail-open — any
        exception is swallowed so a broken counter never blocks the
        fix pipeline.
        """
        if not self.workspace:
            return
        try:
            count_path = (
                state.get_pr_state_dir(self.pr_number, self.repo)
                / "disambig_stale_count.txt"
            )
            if not count_path.exists():
                return
            try:
                count = int(count_path.read_text().strip() or "0")
            except ValueError:
                return
            threshold = int(
                self.config.get("repo", {}).get(
                    "max_disambig_stale_retries", 3
                )
            )
            if count < threshold:
                return
            meta_path = self.workspace / "pr-metadata.json"
            if not meta_path.exists():
                return
            meta = json.loads(meta_path.read_text())
            meta["force_safest_interpretation"] = True
            atomic_write(meta_path, json.dumps(meta))
            logger.info(
                f"PR #{self.pr_number}: forcing safest interpretation "
                f"after {count} fix-reruns without author response"
            )
        except Exception as e:  # noqa: BLE001
            logger.info(
                f"PR #{self.pr_number}: could not set "
                f"force_safest_interpretation: {e}"
            )

    def _load_cached_postconditions(self) -> StageResult | None:
        """Load postconditions.json from prior review state (for fix reruns).

        Returns ``None`` if no cached postconditions exist — the caller
        should treat that as "stage was skipped" rather than falling
        back to an empty list (which would look like a successful run
        with no postconditions).
        """
        pc_path = state.get_pr_state_dir(self.pr_number, self.repo) / "postconditions.json"
        if not pc_path.exists():
            return None
        try:
            data = json.loads(pc_path.read_text())
            if self.workspace:
                (self.workspace / "postconditions.json").write_text(
                    json.dumps(data, indent=2)
                )
            return StageResult(stage="postconditions", success=True, data=data)
        except (OSError, json.JSONDecodeError):
            return None

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

    def _cleanup_underscore_gate_tests(self) -> None:
        """Phase 5: narrower cleanup for ``__gate_test_*`` /
        ``__gate_fix_test_*`` files left behind by the logic stage.

        Kept separate from :meth:`_cleanup_gate_tests` (which runs
        pre-verdict) because these files must survive long enough for
        :meth:`_promote_spec_tests` to capture them on the approve path.
        Idempotent — running twice is a no-op.
        """
        if not self.workspace:
            return
        for pattern in ("**/__gate_test_*", "**/__gate_fix_test_*"):
            for f in self.workspace.glob(pattern):
                try:
                    f.unlink()
                except OSError:
                    pass

    def _promote_spec_tests(self, verdict: "StageResult") -> None:
        """Phase 5: capture verified logic-stage tests into per-PR
        state and (if ``persist_spec_tests`` is enabled) open a
        follow-up spec PR.

        Fail-open: any error below must be logged and swallowed so the
        original review's success is never tainted. This is the L4
        audit-fix invariant.
        """
        if not self.workspace:
            return
        try:
            repo_cfg = self.config.get("repo", {})
            if not repo_cfg.get("persist_spec_tests", False):
                return
            findings_path = self.workspace / "logic-findings.json"
            if not findings_path.exists():
                return
            try:
                data = json.loads(findings_path.read_text())
            except (json.JSONDecodeError, OSError):
                return
            tests = data.get("tests_written") or []
            qualifying: list[dict] = []
            for t in tests:
                if not isinstance(t, dict):
                    continue
                if t.get("intent_type") != "confirmed_correct":
                    continue
                mc = t.get("mutation_check") or {}
                # A mutant that *fails* the test proves discrimination.
                if mc.get("result") != "fail":
                    continue
                qualifying.append(t)
            if not qualifying:
                return

            max_files = int(repo_cfg.get("spec_pr_max_files", 5))
            qualifying = qualifying[: max(0, max_files)]

            pr_state_dir = state.get_pr_state_dir(self.pr_number, self.repo)
            sidecar = pr_state_dir / "spec_tests"
            sidecar.mkdir(parents=True, exist_ok=True)

            # Audit fix W6 — path-traversal containment. The Logic agent
            # is an LLM whose prompt context includes diff text and could
            # be coerced into emitting a relative path like
            # ``../../../etc/passwd``. We must (a) confirm the resolved
            # source is inside the workspace, (b) restrict the glob
            # fallback to the expected ``__gate_test_*`` family, and (c)
            # sanitise the destination filename so even a controlled
            # basename cannot escape the sidecar dir.
            workspace_root = self.workspace.resolve()
            captured: list[Path] = []
            for t in qualifying:
                rel = t.get("file") or t.get("path") or ""
                if not rel:
                    continue
                rel_name = Path(rel).name
                if not (
                    rel_name.startswith("__gate_test_")
                    or rel_name.startswith("__gate_fix_test_")
                ):
                    logger.info(
                        f"PR #{self.pr_number}: spec test {rel!r} does not "
                        f"match __gate_test_* / __gate_fix_test_* prefix — skipping"
                    )
                    continue
                src = self.workspace / rel
                try:
                    resolved_src = src.resolve()
                except (OSError, RuntimeError):
                    continue
                if not src.exists() or not resolved_src.is_relative_to(workspace_root):
                    matches = [
                        m for m in self.workspace.glob(f"**/{rel_name}")
                        if m.resolve().is_relative_to(workspace_root)
                    ]
                    if matches:
                        src = matches[0]
                        resolved_src = src.resolve()
                    else:
                        continue
                if not resolved_src.is_relative_to(workspace_root):
                    continue
                # Sanitised destination: just the basename, after the
                # prefix check above (Note 3 — never trust an attacker-
                # controlled basename to land outside ``sidecar``).
                dst = sidecar / Path(src.name).name
                try:
                    shutil.copy2(src, dst)
                    captured.append(dst)
                except OSError as e:
                    logger.info(
                        f"PR #{self.pr_number}: spec sidecar copy failed "
                        f"for {src}: {e}"
                    )
            if not captured:
                return

            try:
                from gate import spec_pr
                base_sha = (
                    self.config.get("repo", {}).get("default_branch_sha")
                    or self.head_sha
                )
                pr_num = spec_pr.create_spec_pr(
                    repo=self.repo,
                    pr_number=self.pr_number,
                    spec_files=captured,
                    base_sha=base_sha,
                    clone_path=self._clone_path(),
                    config=self.config,
                )
                if pr_num:
                    logger.info(
                        f"PR #{self.pr_number}: opened spec PR #{pr_num}"
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"PR #{self.pr_number}: spec PR creation failed "
                    f"(original PR unaffected): {e}"
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"PR #{self.pr_number}: _promote_spec_tests crashed: {e}"
            )

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

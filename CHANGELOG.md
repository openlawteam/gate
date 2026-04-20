# Changelog

All notable changes to Gate are recorded here. The system follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) conventions
and [Semantic Versioning](https://semver.org/).

## [Unreleased] — gate-system-hardening

Comprehensive hardening pass addressing failure modes observed on a
large consumer repository across a batch of back-to-back PRs. Grouped by
plan section.

### Added

- **Polish loop (Group 1F, 1G).** Hopper-style per-finding fix loop in
  `gate.fixer_polish` for `approve_with_notes` verdicts. Each finding
  gets a fresh Codex bootstrap, per-finding timeout, build checkpoint,
  and isolated revert on breakage. Feature-flagged via
  `fix_polish_loop_enabled` (default `true`).
- **Stable finding IDs (Audit A2).** SHA-based `finding_id` computed
  before fix-senior runs, used for dedup and cross-iteration tracking.
- **Fixability classification.** Each finding is tagged `trivial`,
  `scoped`, `broad`, or `unknown` so the polish loop can prioritize
  and the senior prompt can be honest about what it will attempt.
- **`fix-polish` self-audit (Group 1G).** End-of-loop structured audit
  run against `fix-polish.md`; results surfaced in PR comments.
- **Graceful no-op (Group 1C, 1D).** Fix pipeline reports success when
  no mechanical changes are needed, preventing "red X for nothing to
  fix" on `approve_with_notes`. Soft fix-attempt counter resets on
  no-op.
- **Auth drift detection (Group 4A).** Quota probe distinguishes 401
  (token expired) from transient failures, fires a once-per-24h ntfy
  alert, and surfaces a `quota_auth` health check.
- **`gate prune` CLI (Group 5D).** Worktree-only cleanup subcommand;
  does not perturb logs or state.
- **Runner log rotation (Group 5D).** `cleanup_logs` now compresses
  runner process logs after 7 days and deletes them after 30.
- **Per-repo fix config.** New `gate.toml` fields:
  `fix_on_approve_with_notes`, `graceful_noop_on_approve_with_notes`,
  `fix_polish_loop_enabled`, `polish_per_finding_timeout_seconds`,
  `polish_loop_total_budget_s`.
- **Workflow concurrency (Audit A20, Group 3C).** GHA workflow
  gains a `concurrency:` key per PR so superseded runs are cancelled
  at the GitHub Actions level instead of piling up.

### Changed

- **`commit_and_push` return type (Group 1A).** Now returns a
  `CommitResult` dataclass distinguishing `pushed` / `no_diff` /
  `push_failed`. Empty pushes and push errors are treated as hard
  failures; `no_diff` feeds the graceful no-op path.
- **`log_fix_result` status (Audit A10).** Accepts explicit `status`
  including `"no_op"`, which writes a `fix_no_op` decision to
  `reviews.jsonl` distinct from `fix_succeeded`.
- **`git fetch` retry (Group 3A).** Exponential backoff (2/4/8/16s)
  instead of linear 5/10/15s. First transient failure also triggers
  `git fetch --prune` to evict stale refs. Missing remote branch now
  raises `BranchNotFoundError`, which the orchestrator handles as a
  quiet skip.
- **`_install_deps_with_retry` (Group 3B).** Raises
  `WorkspaceVanishedError` when the worktree disappears mid-install;
  the orchestrator treats this as a quiet cancel.
- **Prompts marked reserved (Audit A18).** `fix-plan.md`,
  `fix-prep.md`, and `fix-plan-refine.md` carry a reserved-notice
  banner — they are not part of the live fix pipeline.

### Fixed

- **Cancellation races (Group 2A, 2B, 2C).** `_save_stage_result`
  no-ops when the review was cancelled or the worktree vanished;
  `remove_worktree` is no longer called from `cancel()` (lived in
  both cancel and `finally`, causing double-teardown and
  `FileNotFoundError` crashes); the post-cancel `error` GitHub write
  path is suppressed so it does not clobber the `cancelled` marker
  from the preceding `cancel()`.
- **`all_fixed` deduplication (Group 1B).** Cross-iteration dedup
  keyed on `finding_id`; `fixed_count` capped at `finding_count` so
  the commit summary cannot claim more fixes than findings.
- **Fix-senior JSON validation (Group 4B, 4C).** `_validate_fix_json`
  synthesizes missing descriptions / reasons and normalizes
  `fixed[]`/`not_fixed[]` entries, producing richer commit messages
  and PR comments.

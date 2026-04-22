# Changelog

All notable changes to Gate are recorded here. The system follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) conventions
and [Semantic Versioning](https://semver.org/).

## [Unreleased] — gate-a-plus-polish

Four ergonomics + UX fixes pushing Gate from A- to A (first half of the
A+ polish plan; state-history + external-check integration land in a
follow-up PR).

### Added

- **`Finding` dataclass (PR A.1).** `gate.schemas.Finding` +
  `FindingLocation` formalise the canonical review-finding shape with
  `severity` / `file` / `message` required and everything else
  optional. `Finding.from_dict(raw)` validates and preserves unknown
  keys in an `extra` bucket so future stage additions round-trip
  without being dropped. Non-breaking: emitters are unchanged;
  renderers (`github._format_findings`, `fixer_polish`, `gate
  inspect-pr`) route through the dataclass.
- **`gate inspect-pr <N>` CLI (PR A.1).** Pretty-prints persisted
  review state for a PR using `rich.Table`; `--raw` dumps JSON;
  `--stage` filters to one stage. This is the command to reach for
  during post-mortems instead of ad-hoc `python -c 'json.load(...)'`.
- **`docs/finding-schema.md` (PR A.1).** Documents the canonical
  finding shape and the per-stage emitter contract.
- **Finding deduplication (PR A.2).** `extract._dedupe_findings`
  collapses findings with the same
  `(source_stage, rule_source-or-category, normalised_message)` into a
  single finding with a `locations` array. Polish loop runs once per
  finding class instead of once per site. Dedup runs BEFORE
  `compute_finding_id` stamping so the existing hash scheme stays
  stable across reviews.
- **`gate health --since-restart` (PR A.4).** Shows how long any
  latched `quota_auth` auth-drift alert has been unresolved.

### Changed

- **`gate status` always prints a Health section (PR A.4).** Previously
  the empty-dict guard hid silent degradations. Now health is computed
  in-process via `run_health_check()` (same path as `gate health`) and
  a latched `quota_auth` drift surfaces as a dedicated top-line `⛔`
  alert. Root cause was the server-side health cache never being
  populated; switching `cmd_status` to the in-process call bypasses
  the dead cache entirely.
- **`_format_findings` (github comments) renders multi-location
  findings with an "Also at:" list (PR A.2)** and surfaces malformed
  findings (missing required fields) in a dedicated "Malformed
  findings" section instead of silently dropping them.

### Fixed

- **Dev-install flow (PR A.3).** `pip install -e .` (no `[dev]` extra)
  now raises a loud, actionable error at test-collection time via
  `tests/conftest.py` instead of producing 25 opaque async-test
  failures. README Development section documents the correct install
  command.

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
- **`scoped_build_verify` config is required (breaking, #20).**
  `gate.checkpoint.scoped_build_verify(workspace, touched_files,
  config)` now requires `config` positionally. Previously it accepted
  `config=None` and fell back to an internal `load_config()` call,
  which put config resolution deep inside an exported helper and
  hid the actual config source at the call site. External wrappers
  must resolve config themselves (via `gate.config.load_config`) and
  pass it in. In-repo callers (`_cmd_save`) already do this; the
  legacy `_load_config(workspace)` helper was removed.
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

- **Silent approval when a build stage exits non-zero with zero parsed
  findings (PR #223 regression).** `compile_build` now flags any
  stage (`typecheck`, `lint`, `tests`) with `pass=false` and zero
  parsed findings as `parse_failure: true`, appends a synthetic opaque
  error with the raw log tail, and emits an unspinnable
  `blocking_issues` string ("unknown output format — opaque build
  failure, see raw_output_tail"). The verdict prompt's **Request
  Changes** rule now hard-blocks on any `parse_failure: true` with
  explicit language forbidding the "tooling anomaly" rationalisation.
  Root cause could not be recovered because `compile_build` had been
  discarding the raw `output` field — so `build.json` now also carries
  `raw_output_tail` and `exit_code` for each stage so the next such
  case is forensically debuggable. Belt-and-suspenders: `_parse_lint`
  regex now accepts Next.js-wrapped severities (`Error:` / `Warning:`
  case-insensitive), optional `./` file-path prefix, and `mjs`/`cjs`
  extensions. Retro-scan of state found one historical silent approval
  (adin-chat #219); the class is now closed. Also: `run_build` now
  WARN-logs every `subprocess.TimeoutExpired` — previously silent.
- **Stale `origin/<default_branch>` poisoning review diff scope (#15).**
  `create_worktree` now refreshes `origin/<default_branch>` alongside the
  PR branch via a new `_refresh_default_branch_ref` helper. Previously the
  triple-dot diff in `prepare_context_files` could be computed against a
  stale cached ref, pulling ~10s of unrelated files into the review scope.
  Observed on adin-chat PR #217 (6 real → 39 Gate-reported) and PR #220
  (1 real → 48 Gate-reported; ~8 min of speculative fix-pipeline cycles
  before the senior correctly resolved to `no_op`). The helper uses a
  bounded retry budget (`max_retries=2`) and soft-fails with a structured
  warning so transient fetch errors never block a review.
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

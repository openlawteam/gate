## HOPPER MODE — holistic fix pipeline

You are running in **hopper mode**. Instead of one monolithic pass over all findings, you decompose the work into logical sub-scopes, drive the junior through each one, and let Gate checkpoint clean sub-scopes so partial progress is never lost.

### Decompose first

Before dispatching any stage, produce an explicit decomposition of the findings into 1–6 sub-scopes. Each sub-scope should:

- Have a short human name (e.g. "security patch", "eslint cleanups", "file splits", "mediaTools refactors").
- Group findings that share files, abstractions, or a mental model.
- Be sized so the junior can complete it in one `implement` + `audit` round (plus up to 2 retries).

Write the decomposition to `fix-decomposition.json` in the current working directory using this shape:

```
{
  "sub_scopes": [
    {
      "name": "security patch",
      "risk_tier": "trivial",
      "finding_ids": ["abc1234567"]
    },
    ...
  ]
}
```

Gate picks this up automatically and streams it into the live log so operators see the plan. If you cannot produce a clean decomposition, use a single sub-scope covering everything and say so explicitly in its name.

### Priority ordering (mandatory)

Order sub-scopes to maximise banked progress if the senior budget or a build verify fails late in the run:

1. **Trivial patches first** — small security fixes, eslint cleanups, 1–5 line changes. These rarely break the build.
2. **Scoped refactors second** — single-file rewrites, internal helper extractions. Moderate blast radius.
3. **Cross-file / architectural changes last** — file splits, hook rewrites, shared-module refactors. Highest blast radius, so they go last — if they blow up, earlier sub-scopes are already banked as checkpoints.

### Per sub-scope: implement → audit → checkpoint

For each sub-scope, in order:

1. Dispatch `gate-code implement` with specific instructions and the `finding_id`s for this sub-scope only. Tell the junior exactly which files they may touch.
2. Dispatch `gate-code audit` over the junior's changes. Review the audit output. If it raises real problems, re-dispatch `implement` with the audit tail as feedback.
3. When implement + audit look clean, run:

```bash
gate checkpoint save --name "<sub-scope name>" --touched-files "<comma separated list of files the junior modified>"
```

Gate runs a **scoped `build_verify`** on the touched files (tsc `--incremental` + lint on that subset — much faster than a full build) and:

- **exit 0 + SHA on stdout** → sub-scope is banked. Move on.
- **exit non-zero with build errors on stderr** → the scoped build failed. Read the error tail, re-dispatch `implement` with a focused fix, then try `gate checkpoint save` again. You have up to 3 total implement/audit iterations per sub-scope.

If you exhaust 3 iterations on a sub-scope, run:

```bash
gate checkpoint revert --to-last-clean
```

Then record every finding in that sub-scope under `not_fixed[]` with `reason: "subscope_exhausted"` and a specific `detail` explaining the concrete blocker. Move on to the next sub-scope — earlier checkpoints are preserved.

### Before you finish

When every sub-scope is either checkpointed or explicitly deferred:

1. Run one final **full** `build_verify` by asking the junior to run `$typecheck_cmd` and `$lint_cmd` and reading the tails. If both are clean, proceed. If the full build fails on cross-sub-scope interactions, run `gate checkpoint revert --to-last-clean` (drops the most recent sub-scope) and record its findings as `not_fixed` with reason `subscope_exhausted` and detail explaining the full-build break. Then re-run this step.
2. Run:

```bash
gate checkpoint finalize <<'EOF'
<commit message body — the `fix(gate): auto-fix N/M findings...` template>
EOF
```

Gate squashes all `gate-checkpoint:` commits into one final commit on the PR branch ready for push. Do NOT run git commands yourself.

### Extended output schema

Your `fix-senior-findings.json` must include `sub_scope_log[]` and `final_commit_message` in addition to the base `fixed[]` / `not_fixed[]` shape:

```
{
  "fixed": [...],
  "not_fixed": [...],
  "sub_scope_log": [
    {
      "name": "security patch",
      "finding_ids": ["..."],
      "iterations": 1,
      "outcome": "committed",
      "checkpoint_sha": "abc12345"
    },
    {
      "name": "file splits",
      "finding_ids": ["..."],
      "iterations": 3,
      "outcome": "reverted",
      "reason": "subscope_exhausted"
    }
  ],
  "final_commit_message": "<the same body passed to gate checkpoint finalize>",
  "stats": {...}
}
```

`outcome` is one of `"committed"`, `"reverted"`, `"empty"` (Codex made no file changes — record all the sub-scope's findings in `not_fixed` with reason `no_changes`).

### Atomic write

`fix-senior-findings.json` is polled by Gate. To avoid the poller reading a half-written file, write to `fix-senior-findings.json.tmp` first, then `mv` it to the real name. Pseudocode:

```bash
echo "<json>" > fix-senior-findings.json.tmp && mv fix-senior-findings.json.tmp fix-senior-findings.json
```

# External check-run integration

Gate can consult **external CI signals** (Vercel preview builds, GitHub
Actions workflows, Netlify deploys, Render build checks, CircleCI jobs,
…) before it approves a PR. A red external check blocks approval; a
still-pending blocking check makes Gate wait up to a configurable
timeout; a check that flips red **after** approval writes a
contradiction record and fires a notification.

PR B.2 introduced this module. It is off by default — a repo with no
`required_external_checks` sees zero behavioural change.

## The fast path

Add the following to your repo section in `~/.config/gate/gate.toml`
(see [`config/gate.toml`](../config/gate.toml) for the full schema):

```toml
[[repos]]
name = "your-org/your-repo"
# … existing required keys (clone_path, bot_account, …) …

# Wait up to 10 min for blocking checks to resolve before fail-closing.
external_check_wait_seconds = 600

# Post-hoc poll approved SHAs for 30 min looking for flips to failure.
external_check_recheck_minutes = 30

# One entry per external check you care about.
required_external_checks = [
    { name = "Vercel",           policy = "blocking", match = "substring" },
    { name = "build (ubuntu)",   policy = "blocking", match = "exact" },
    { name = "Lighthouse",       policy = "advisory" },
]
```

Behaviour:

- **`policy = "blocking"`** — a red state (`failure`, `timed_out`,
  `cancelled`, `action_required`, `error`) overrides any approve verdict
  with `request_changes`. A `pending` state waits
  `external_check_wait_seconds`; if still pending past that, Gate
  fail-closes (also `request_changes`).
- **`policy = "advisory"`** — verdict is **not** flipped. The check is
  still included in the post-hoc recheck watch list, so a flip to
  failure after approval is recorded and alerted. Pre-approval
  advisory failures are not annotated onto the verdict itself;
  consult `gate audit contradictions` once the post-hoc phase has
  run if you need the historical record.
- **`match = "substring"` (default)** is case-insensitive. `name = "Vercel"`
  matches both `Vercel – Preview` and `Vercel – Production`.
- **`match = "exact"`** requires the check name to equal `name` exactly.
  Use this when your CI emits multiple similarly-named checks and you
  only want one to gate.

### Master kill-switch

To disable all external check logic (e.g. during a GitHub incident):

```toml
[external_checks]
enabled = false
```

This skips the entire fetch / classify / wait pipeline and the post-hoc
recheck thread. No configuration below is consulted.

Defaults that apply when a repo doesn't override them:

```toml
[external_checks]
enabled = true
wait_seconds_default = 600   # 10 minutes
recheck_minutes_default = 30 # 30 minutes
```

## Provider matrix

GitHub exposes CI through two different APIs. Gate queries both and
merges them, preferring the modern endpoint on name collision.

| Provider              | Reports through         | Typical check name(s)                       |
| --------------------- | ----------------------- | ------------------------------------------- |
| GitHub Actions        | `GET /repos/:o/:r/commits/:sha/check-runs` | Job names from your workflow YAML          |
| CircleCI              | `check-runs` (modern)   | `ci/circleci: <job>`                        |
| Vercel                | `GET /repos/:o/:r/commits/:sha/status` (legacy statuses) | `Vercel – Preview`, `Vercel – Production` |
| Netlify               | legacy statuses         | `netlify/<site-name>`                       |
| Render                | legacy statuses         | `render/<service-name>`                     |
| Cloudflare Pages      | legacy statuses         | `Cloudflare Pages — <project>`              |
| Vercel (monorepo)     | legacy statuses         | `Vercel – Preview – <app-name>`             |

### Examples

**Vercel preview must pass before approval** (most common shape):

```toml
required_external_checks = [
    { name = "Vercel – Preview", policy = "blocking", match = "exact" },
]
```

Use `exact` if you have both Preview and Production contexts and only
want one to gate. Use `substring` with `name = "Vercel"` if **any**
Vercel failure should block.

**GitHub Actions matrix build** (one entry per OS):

```toml
required_external_checks = [
    { name = "build (ubuntu-latest)", policy = "blocking", match = "exact" },
    { name = "build (macos-latest)",  policy = "blocking", match = "exact" },
    { name = "lint",                   policy = "blocking", match = "exact" },
]
```

**Advisory Lighthouse score** (watch, don't gate):

```toml
required_external_checks = [
    { name = "Lighthouse", policy = "advisory" },
]
```

An advisory failure does not block approval, but if it flips to red
post-approval the contradiction is recorded and ntfy fires.

**Multi-provider (typical SaaS repo)**:

```toml
required_external_checks = [
    { name = "Vercel – Preview",      policy = "blocking", match = "exact" },
    { name = "build",                  policy = "blocking", match = "substring" },
    { name = "tests",                  policy = "blocking", match = "substring" },
    { name = "Cloudflare Pages",       policy = "blocking", match = "substring" },
    { name = "Lighthouse",             policy = "advisory" },
]
```

## How Gate uses the signal

### 1. Pre-commit gate (between verdict and GitHub post)

After the verdict stage produces a decision, Gate calls
[`external_checks.fetch_check_state()`](../gate/external_checks.py),
merges results from `check-runs` + `status` endpoints, and runs
`classify()` against the configured `required_external_checks`:

- **`blocking_failures`** → verdict flipped to `request_changes`; one
  finding per failing check is injected with
  `source_stage = "external_checks"`.
- **`blocking_pending` / `unknown`** → `wait_for_pending()` polls every
  5 s for up to `external_check_wait_seconds`. Cancellable via the
  existing `gate cancel` path. If the wait exits with anything still
  pending, those become `request_changes` findings too.
- **All green** → verdict untouched; Gate proceeds.

### 2. Post-hoc recheck (after approve)

On `approve` / `approve_with_notes` verdicts, Gate spawns a daemon
thread that polls the same checks every minute for
`external_check_recheck_minutes`. For any required check (blocking or
advisory) that flips to failure and wasn't already failing at approval
time, Gate:

- Writes `state/<repo>/pr<N>/contradictions/<ISO>-<check_name>.json`
  (atomic; verdict snapshot + failure details + seconds-to-flip).
- Appends to `logs/alerts.jsonl`.
- Fires `notify.notify()` (ntfy if `GATE_NTFY_TOPIC` is set; no-op
  otherwise).

**We cannot un-approve on GitHub.** The post-hoc phase is strictly
best-effort — the value is alerting and the audit trail, not
retroactive correction.

## Inspecting contradictions

```bash
# All contradictions across all PRs, newest first.
gate audit contradictions

# Only contradictions in the last 7 days.
gate audit contradictions --since 7d

# Only in the last 24 hours.
gate audit contradictions --since 24h
```

The sibling `gate audit retro-scan` surfaces **silent approvals** —
approved verdicts whose archived `build.json` actually shows a lint /
tests / typecheck failure. Run both weekly during the first month as
a trust-building exercise.

## Troubleshooting

### "Required external check never reported"

Gate waited the full `external_check_wait_seconds` but neither the
`check-runs` nor `status` endpoint ever returned a check matching
`name`. Common causes:

- The CI provider hasn't started yet (cold repo, large queue).
- The check name changed (e.g. Vercel renamed from `Vercel Preview`
  to `Vercel – Preview`). Substring match helps here.
- The provider reports through a third API we don't consult (rare;
  file an issue).

Workaround: raise `external_check_wait_seconds`, or demote to
`policy = "advisory"` until the reporting is reliable.

### "Gate is slow to approve"

The external-check fetch adds one or two `gh api` calls per review and
polls every 5 s while any blocking check is pending. If you're seeing
50+ s added to review latency, the bottleneck is almost always a slow
CI provider; lower `external_check_wait_seconds` if you'd rather
fail-close sooner.

### "Too many API calls"

At the default settings, each approved PR budgets ~30 polls over 30
min for the post-hoc recheck, plus ~1–5 polls during the pre-commit
wait. Even at 50 PRs/day that's well under GitHub's 5000/hour
authenticated limit. If you hit rate pressure, lower
`external_check_recheck_minutes` or flip the kill-switch.

## See also

- [`gate/external_checks.py`](../gate/external_checks.py) — the module
  (fetch, classify, wait).
- [`gate/orchestrator.py`](../gate/orchestrator.py)
  `_consult_external_checks`, `_schedule_post_hoc_recheck`,
  `_run_post_hoc_recheck` — the hook points.
- [`docs/finding-schema.md`](finding-schema.md) — the shape of
  injected `source_stage = "external_checks"` findings.

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

## Fine-grained PAT limitation

**Short version:** if `GATE_PAT` is a fine-grained PAT (`github_pat_…`
prefix), Gate cannot read the Checks API. It detects this on the first
403 response, logs one INFO line, and runs in statuses-only mode
thereafter. No configuration change is required; no log spam is
produced.

### Why

GitHub exposes two separate APIs for "things attached to a commit that
say pass/fail":

| API               | Path                                              | Who writes to it                    | Fine-grained PAT | Classic PAT | GitHub App |
| ----------------- | ------------------------------------------------- | ----------------------------------- | ---------------- | ----------- | ---------- |
| Commit Statuses   | `GET /repos/:r/commits/:sha/status`               | Vercel, Netlify, Render, scripts    | ✅ `Commit statuses: Read` | ✅ `repo` | ✅ `Commit statuses: Read` |
| Check Runs        | `GET /repos/:r/commits/:sha/check-runs`           | GitHub Actions, CircleCI, Buildkite | ❌ **no permission exists** | ✅ `repo` | ✅ `Checks: Read` |

The fine-grained-PAT permission picker has no "Checks" entry, and no
other permission substitutes. GitHub's
[March 2025 GA announcement](https://github.blog/changelog/2025-03-18-fine-grained-pats-are-now-generally-available/)
lists "calling the Checks and Packages APIs" as a known feature gap for
fine-grained PATs. The restriction also applies transitively: check-run
access through GraphQL `statusCheckRollup.contexts` returns
`FORBIDDEN` for `CheckRun` nodes while `StatusContext` nodes come back
fine, and the Actions API (`/actions/runs`) is a separate permission
that doesn't cover the generic Checks endpoint.

This is not something a scope change can fix — it's a platform-level
design decision by GitHub. Community discussion
[#129512](https://github.com/orgs/community/discussions/129512) has
GitHub support confirming the Checks API is intentionally
GitHub-App-only for non-classic tokens.

### What Gate does about it

[`gate/external_checks.py`](../gate/external_checks.py)
`_paginate_check_runs` is the hook point. On the first 403 from the
check-runs endpoint:

1. The offending `gh api` call is issued with `quiet_on_403=True`, so
   the initial 403 logs at **DEBUG** instead of WARNING.
2. A process-global flag (`_check_runs_forbidden`, guarded by a lock)
   flips.
3. One **INFO** line is emitted pointing to this section of the docs.
4. Every subsequent call to `_paginate_check_runs` short-circuits to
   `[]` without a network round-trip.
5. The statuses endpoint continues to run normally on every request —
   you keep full coverage of Vercel, Netlify, Render, Cloudflare
   Pages, and anything else that publishes through legacy statuses.
6. `_schedule_post_hoc_recheck` keeps polling (its statuses half still
   works); the check-runs half is now free.

Restart the gate server (`launchctl kickstart -k gui/$(id -u)
com.gate.server`) to re-detect after a token swap — the flag is
process-scoped on purpose so a token rotation requires an explicit
bounce.

Programmatic introspection is available via
`external_checks.check_runs_available()` (returns `True` until the
first 403, `False` thereafter).

### What signal you lose in statuses-only mode

Anything whose only pass/fail surface is the Checks API. In practice:

- GitHub Actions job results that aren't mirrored onto commit statuses.
- CircleCI results (CircleCI publishes through `check-runs` only).
- Buildkite results.
- Render / Netlify **build** checks *only* when configured to report
  through the modern Checks API (both default to legacy statuses, so
  usually fine).
- Any third-party bot check (Dependabot status, CodeQL, security
  advisories) that uses `POST /repos/:r/check-runs` exclusively.

What you **keep**:

- Vercel deploy status (uses legacy statuses).
- `gate-review` / `Gate Auto-Fix` statuses Gate writes itself (these
  are commit statuses, not check-runs — see
  [`gate/github.py`](../gate/github.py) `create_check_run`, which
  despite the name uses the Statuses API because of this same gap).
- Any CI provider publishing commit statuses.

If a repo's `required_external_checks` entry matches a check-run-only
provider, `classify()` will bucket it as `unknown` every time; with
`policy = "blocking"` + the default `external_check_wait_seconds`,
that means fail-closed after the wait expires. Mis-configuration
surfaces loudly — but if you explicitly need CircleCI / GHA job
results to **not** block, mark them `policy = "advisory"` or drop
them from the list.

### Removing the limitation

Two paths, in increasing "properness":

**Option 1 — Classic PAT (quick).** Replace `GATE_PAT` with a
`ghp_…` classic PAT with the `repo` scope (and `workflow` if you
expect `fix-senior` to edit workflow YAML). Classic PATs hit both
endpoints without ceremony. GitHub has not announced an EOL for
classic PATs and self-hosted bots routinely use them; trade-off is
they carry broader implicit scope than fine-grained PATs.

```bash
# In ~/.zshrc (or your LaunchAgent plist):
export GATE_PAT="ghp_<new_classic_pat>"

# Apply and re-detect:
source ~/.zshrc
launchctl kickstart -k "gui/$(id -u)/com.gate.server"
```

No code changes required — `_paginate_check_runs` will start returning
check-runs on the next poll; the "forbidden" flag resets because the
gate server is a new process.

**Option 2 — GitHub App (proper).** Register a Gate app on the owning
org with `Checks: Read`, `Contents: Write`, `Pull requests: Write`,
`Commit statuses: Write`. Install on each repo that runs through Gate.
Update `gate.github._gh_env` to mint installation tokens on demand
(JWT-sign the app private key, POST
`/app/installations/:id/access_tokens`, cache until expiry). This also
lets `create_check_run` migrate from the Statuses API to real Check
Runs with annotations (file/line-level red squiggles in the GitHub
Files tab) and get rid of the apology comment in `github.py:486-489`.
Material work — maybe a day — but it's the answer GitHub points you
to. Not wired in today.

## See also

- [`gate/external_checks.py`](../gate/external_checks.py) — the module
  (fetch, classify, wait). Look for the "Fine-grained PAT caveat"
  section in the module docstring and the `_check_runs_forbidden` flag
  in `_paginate_check_runs`.
- [`gate/github.py`](../gate/github.py) — `_gh` carries the
  `quiet_on_403` kwarg that suppresses the expected warning;
  `create_check_run` uses the Statuses API (not Checks) and documents
  why at the top of the "Commit Status API" section.
- [`gate/orchestrator.py`](../gate/orchestrator.py)
  `_consult_external_checks`, `_schedule_post_hoc_recheck`,
  `_run_post_hoc_recheck` — the hook points.
- [`docs/finding-schema.md`](finding-schema.md) — the shape of
  injected `source_stage = "external_checks"` findings.
- GitHub's [Fine-grained PAT GA announcement](https://github.blog/changelog/2025-03-18-fine-grained-pats-are-now-generally-available/)
  (listing Checks API as a feature gap).
- [Community discussion #129512](https://github.com/orgs/community/discussions/129512)
  (GitHub support confirming Checks API is App-only).

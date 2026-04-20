# Health & Recovery

Gate runs 13 health checks every 5 minutes via `gate health` (cron). Issues trigger ntfy + Discord alerts with 1-hour dedup.

## Health Checks

| Check | What It Does | Recovery |
|-------|-------------|----------|
| `sleep_disabled` | Verifies macOS sleep is disabled | Auto-runs `pmset disablesleep 1` |
| `runner` | Checks GHA runner service status | Auto-restarts if stopped |
| `github_api` | Pings `api.github.com/zen` | Alerts if unreachable |
| `tailscale` | Verifies Tailscale process is running | Alerts if down |
| `disk` | Checks disk usage (threshold: 85%) | Auto-cleans old worktrees |
| `tmux_session` | Checks `gate` tmux session exists | Manual: `tmux new 'gate up'` |
| `gate_server` | Pings server via Unix socket | Manual: restart `gate up` |
| `stuck_reviews` | Finds reviews exceeding `hard_timeout_s` | Logs warning |
| `orphaned_checks` | Finds GitHub checks stuck `in_progress` with no process | Auto-completes check + auto-approves PR (fail-open) |
| `orphaned_windows` | Finds tmux windows with no active review | Auto-kills windows |
| `circuit_breaker` | Checks if last 3 reviews were errors | Alerts; system auto-approves PRs until a success resets |
| `quota` | Checks Anthropic quota cache freshness | Alerts if stale (>30min) |
| `quota_auth` | Verifies the Claude OAuth token still authenticates (Group 4A) | Alerts once/24h via `quota_auth_drift`; refresh token with `claude auth login` |
| `recent_errors` | Counts errors in last 5 reviews | Alerts if >= 3 |

## Running Health Checks

```bash
# Run all checks
gate health

# Via cron (every 5 min)
*/5 * * * * /path/to/gate health >> <gate-dir>/logs/health.log 2>&1
```

## Orphaned Commit Status Recovery

When the Python process crashes after creating a GitHub commit status but before completing it, the status blocks the PR forever. The orphaned status cleanup handles this:

1. Scans `<gate-dir>/state/prN/active_review.json` files
2. For each: checks if the PID is still alive and the review is >60s old
3. If PID is dead: completes the commit status as "cancelled", approves the PR (fail-open), sends alert
4. If PID is alive but exceeds `hard_timeout_s`: logs a warning

## Circuit Breaker

Triggers when the last 3 consecutive reviews result in errors. When tripped:
- All new PRs are auto-approved with a "circuit breaker" comment
- ntfy alert sent at high priority
- Resets automatically when the next review succeeds

## Alert Dedup

Alert state persisted in `<gate-dir>/logs/.health-alert-state`. Each check type has a 1-hour cooldown — the same alert won't fire more than once per hour.

## Common Failure Scenarios

| Scenario | Symptom | Fix |
|----------|---------|-----|
| Machine rebooted | No tmux session, runner down | `tmux new 'gate up'`, runner auto-starts via LaunchAgent |
| Claude auth expired | All reviews fail immediately | `claude auth login --method browser` |
| GitHub API rate limit | Checks fail to create/complete | Wait for rate limit reset; quota check will defer reviews |
| Disk full | Build failures, worktree creation fails | `gate cleanup` or `gate prune` (worktrees only) |
| Network outage | GitHub API unreachable | Reviews queued; auto-retry on reconnection |
| Claude OAuth drift | Reviews run but quota check silently fail-opens (`quota_auth` health check tripped) | `claude auth login` — alert latches for 24h then clears on next successful probe |
| Superseded review crashed mid-stage | `FileNotFoundError` on stage JSON write in logs | Benign — Group 2A suppresses the crash and Group 2B ensures the worktree is torn down once, not twice |
| Stale worktree after crash | Disk fills up in `/tmp/gate-worktrees/` | `gate prune` (24h default) or `gate prune --aggressive` |

## Fix Pipeline Behavior

The fix pipeline has two execution paths selected per-verdict:

- **Monolithic (`request_changes`):** single `fix-senior` call attempts
  all findings in one go. Used when the reviewer is actually asking for
  changes.
- **Polish loop (`approve_with_notes`):** per-finding hopper-style loop
  in `gate.fixer_polish`. Each finding gets a fresh Codex bootstrap,
  its own timeout, a build checkpoint, and is reverted in isolation on
  breakage. Controlled by `fix_polish_loop_enabled` (default `true`).

When the fix pipeline runs but produces zero mechanical changes (the
classic "nothing to actually fix, reviewer just wants a comment"
case), the pipeline emits a **graceful no-op**: it reports success to
the Gate Auto-Fix check with title "skipped (no mechanical changes
needed)" and logs `fix_no_op` to `reviews.jsonl`. Soft fix-attempt
counters are reset on no-op so we don't exhaust the retry budget.
Toggle with `graceful_noop_on_approve_with_notes` (default `true`).

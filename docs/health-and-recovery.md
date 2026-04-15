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
| Disk full | Build failures, worktree creation fails | `gate cleanup` or manual `rm -rf /tmp/gate-worktrees/*` |
| Network outage | GitHub API unreachable | Reviews queued; auto-retry on reconnection |

# Metrics & Logging

## Review Log (`reviews.jsonl`)

Every completed review appends a JSON line to `<gate-dir>/logs/reviews.jsonl`:

```json
{
  "timestamp": "2024-04-13T10:30:00+00:00",
  "pr": 42,
  "mode": "enforcement",
  "run_id": "abc12345",
  "pipeline": "multi-stage",
  "quota_five_hour_pct": 45.2,
  "quota_seven_day_pct": 12.8,
  "decision": "approve",
  "confidence": "high",
  "risk_level": "medium",
  "change_type": "feature",
  "review_time_seconds": 180,
  "findings": 3,
  "findings_by_severity": {"critical": 0, "error": 1, "warning": 2, "info": 0},
  "finding_categories": ["architecture", "logic"],
  "resolved_count": 0,
  "build_pass": true,
  "fast_track_eligible": false,
  "stages_run": 6
}
```

## TUI Metrics Bar

The TUI dashboard shows a 24-hour metrics summary at the bottom:

```
24h: 12 reviews │ 75% approved │ 8% errors
```

Computed in real-time by reading `reviews.jsonl` entries from the last 24 hours.

## Recent Reviews Panel

The TUI "Recent Reviews" panel shows the last 8 completed reviews with:
- Decision icon (✓ approved, ✗ blocked, ⚠ error)
- PR number
- Decision text
- Finding count
- Elapsed time

## Daily Digest

Sent every morning at 9 AM via `gate digest` (cron):

```
*/0 9 * * * gate digest
```

Sends a summary to ntfy + Discord with:
- Total reviews in last 24h
- Approval rate
- Error count
- Top findings by category

## Live Logs

Per-PR live logs at `<gate-dir>/logs/live/prN.log` track real-time progress:

```
[2024-04-13 10:30:00] [orchestrator] Review started sha=abc12345
[2024-04-13 10:30:02] [setup] Worktree: /tmp/gate-worktrees/pr42
[2024-04-13 10:30:05] [stage] Triage starting
[2024-04-13 10:30:30] [stage] Architecture starting
...
[2024-04-13 10:35:00] [orchestrator] Review complete: approve (3 findings, 300s)
```

The TUI log tail panel auto-follows the most recent active PR's log.

## Activity Log

Server activity log at `<gate-dir>/logs/activity.log`. Captures all server events, client connections, and review lifecycle messages. Used by the TUI log tail as a fallback when no PR is active.

## Log Rotation

```bash
# Run cleanup (cron at 3 AM daily)
gate cleanup

# Cleans:
# - Live logs older than 7 days
# - Worktrees older than 2 hours
# - Rotates reviews.jsonl if > 10MB
```

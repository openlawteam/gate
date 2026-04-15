# Notifications

Gate sends notifications via ntfy.sh and Discord webhooks. All notifications are best-effort — failures never block reviews.

## Setup

### ntfy.sh

Set the `GATE_NTFY_TOPIC` environment variable to your ntfy topic:

```bash
export GATE_NTFY_TOPIC="gate-reviews"
```

Subscribe in the ntfy app (iOS/Android) or at `https://ntfy.sh/gate-reviews`.

### Discord

Set the `GATE_DISCORD_WEBHOOK` environment variable:

```bash
export GATE_DISCORD_WEBHOOK="https://discord.com/api/webhooks/..."
```

## Notification Events

| Event | ntfy Tags | Priority | Discord Color |
|-------|-----------|----------|---------------|
| PR approved | ✅ `white_check_mark` | default | Green (3066993) |
| PR blocked | ❌ `x` | default | Red (15158332) |
| Review failed (error) | 🚨 `rotating_light` | high | Red (15158332) |
| Circuit breaker tripped | 🚨 `rotating_light` | high | Red (15158332) |
| Auto-fix started | 🔧 `wrench` | default | Blue (3447003) |
| Auto-fix complete | ✅ `white_check_mark` | default | Green (3066993) |
| Auto-fix failed | ❌ `x` | high | Red (15158332) |
| Runner down | 🚨 `rotating_light` | high | — |
| Health alert | ⚠️ `warning` | high | — |

## Implementation

Notifications are sent by `gate/notify.py`:

- `review_complete(pr_number, verdict, repo)` — after verdict
- `review_failed(pr_number, error)` — on pipeline crash
- `circuit_breaker(pr_number)` — when tripped
- `fix_started(pr_number, finding_count, risk_level, repo)` — fix pipeline start
- `fix_complete(pr_number, fixed, total, iterations, repo)` — fix pipeline end
- `fix_failed(pr_number, reason, iterations, repo)` — fix pipeline failure
- `runner_down(runner_id)` — from health check

Each function sends to both ntfy and Discord. If the environment variable is not set, the channel is silently skipped.

## Click URLs

ntfy notifications include a click URL linking to the GitHub PR. Tapping the notification on your phone opens the PR directly.

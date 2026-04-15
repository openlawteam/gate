# Gate — Autonomous PR Review System

Gate is a Python/tmux AI-powered code review system that autonomously reviews every PR on your repository. It runs on a self-hosted machine as a persistent server, orchestrating Claude and Codex agents to perform multi-stage code reviews and auto-fixes.

## Architecture

```
GitHub PR push
  └─▶ GHA thin trigger (gate-review.yml)
       └─▶ gate review --pr N --repo ... --head-sha ...
            └─▶ ReviewQueue (concurrent, up to 3 PRs)
                 └─▶ ReviewOrchestrator per PR
                      ├─▶ GitHub commit status created ("gate-review")
                      ├─▶ Gate 1–5 (labels, circuit breaker, fix-rerun, quota, cycles)
                      ├─▶ Stage 1: Triage (structured, Sonnet)
                      ├─▶ Stage 2: Build verification
                      ├─▶ Stage 3: Architecture review (agent, tmux)
                      ├─▶ Stage 4: Security review (agent, tmux)
                      ├─▶ Stage 5: Logic review (agent, tmux)
                      ├─▶ Stage 6: Verdict (structured, Sonnet)
                      ├─▶ Post review (GitHub review)
                      └─▶ Fix pipeline (if blocked — Claude senior + Codex junior)
```

Agent stages run interactively in tmux windows via `gate process`. Structured stages run inline via `subprocess.run`. All communication between the server, orchestrator, and runners uses Unix socket IPC (JSONL protocol).

## Requirements

| Requirement | Details |
|-------------|---------|
| Python | 3.12+ |
| OS | macOS or Linux |
| tmux | 3.3+ |
| GitHub CLI | `gh` authenticated |
| Claude CLI | `claude` authenticated |
| Codex CLI | `codex` (optional, for fix pipeline) |

## Quick Start

```bash
# 1. Clone and install
git clone <gate-repo-url>
cd gate
pip install -e ".[dev]"

# 2. Run interactive setup
gate init

# 3. Start Gate (inside tmux)
tmux new 'gate up'
```

To add additional repositories to an existing setup:

```bash
gate add-repo
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `gate init` | Interactive first-time setup |
| `gate add-repo` | Add a repository to an existing config |
| `gate up` | Start server and TUI dashboard |
| `gate up --headless` | Start server without TUI |
| `gate status` | Print current state (reviews, queue, health) |
| `gate review --pr N ...` | Enqueue a PR review (called by GHA) |
| `gate cancel --pr N` | Cancel an in-progress review |
| `gate health` | Run all health checks |
| `gate doctor` | Verify all prerequisites |
| `gate cleanup` | Run log rotation and worktree pruning |
| `gate digest` | Send daily metrics digest |
| `gate update` | Pull latest code and reinstall |
| `gate cleanup-pr --pr N` | Clean up state for a closed PR |
| `gate process <id> <stage>` | Run a review stage in tmux (internal) |

## Configuration

### `config/gate.toml`

```toml
[repo]
name = "your-org/your-repo"
clone_path = "~/your-repo"
worktree_base = "/tmp/gate-worktrees"

[models]
triage = "sonnet"
architecture = "sonnet"
security = "opus"
logic = "opus"
verdict = "sonnet"
fix_senior = "opus"
fix_rereview = "sonnet"

[timeouts]
agent_stage_s = 900
structured_stage_s = 120
hard_timeout_s = 1200

[limits]
max_diff_bytes = 500000
max_pr_body_bytes = 50000
max_review_cycles = 5
```

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `GATE_PAT` | GitHub Personal Access Token (push, checks, reviews) |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude CLI authentication |
| `OPENAI_API_KEY` | Codex CLI authentication |
| `GATE_NTFY_TOPIC` | ntfy.sh topic for push notifications |
| `GATE_DISCORD_WEBHOOK` | Discord webhook URL (optional) |

## Directory Layout

```
gate/
├── gate/                      # Python package
│   ├── __init__.py
│   ├── cli.py                 # CLI entry point
│   ├── server.py              # Unix socket JSONL server
│   ├── client.py              # Server client + GateConnection
│   ├── orchestrator.py        # Review pipeline state machine
│   ├── queue.py               # Concurrent review queue (ThreadPoolExecutor)
│   ├── runner.py              # ReviewRunner (agent tmux) + StructuredRunner (inline)
│   ├── claude.py              # tmux window spawner
│   ├── tmux.py                # tmux low-level operations
│   ├── fixer.py               # Fix pipeline (Claude senior + Codex junior)
│   ├── github.py              # GitHub API (checks, reviews, comments, labels)
│   ├── prompt.py              # Prompt loading and variable substitution
│   ├── schemas.py             # StageResult, FixResult, fallback builders
│   ├── config.py              # TOML config loader
│   ├── state.py               # Per-PR state persistence
│   ├── notify.py              # ntfy + Discord notifications
│   ├── health.py              # Infrastructure health monitoring
│   ├── cleanup.py             # Log rotation, worktree pruning, digest
│   ├── logger.py              # reviews.jsonl, live logs
│   ├── builder.py             # Build verification (tsc, lint)
│   ├── quota.py               # Anthropic API quota checking
│   ├── workspace.py           # Git worktree management
│   ├── tui.py                 # Textual TUI dashboard
│   └── code.py                # gate-code Codex wrapper
├── config/
│   ├── gate.toml              # Main configuration
│   ├── cursor-rules.md        # Coding standards for review (customize for your project)
│   ├── fix-blocklist.txt      # Files excluded from auto-fix
│   └── com.gate.server.plist.example  # LaunchAgent config template
├── prompts/                   # 18 prompt templates (.md)
├── workflows/
│   └── gate-review.yml        # Thin GHA trigger workflow
├── docs/
│   ├── health-and-recovery.md
│   ├── metrics.md
│   ├── notifications.md
│   └── remote-access.md
├── tests/                     # pytest test suite
├── logs/                      # Runtime logs
│   ├── reviews.jsonl          # Structured review log (append-only)
│   ├── activity.log           # Server activity log
│   └── live/                  # Per-PR live logs
├── state/                     # Per-PR state
│   └── prN/
│       ├── verdict.json
│       ├── triage.json
│       ├── architecture.json
│       ├── security.json
│       ├── logic.json
│       ├── build.json
│       ├── last_sha.txt
│       ├── review_count.txt
│       └── fix_attempts.txt
├── pyproject.toml
├── Makefile
└── README.md
```

## TUI Dashboard

The TUI runs inside tmux via `gate up`. Tokyo Night color scheme.

| Key | Action |
|-----|--------|
| `q` | Quit |
| `r` | Refresh all panels |
| `c` | Cancel selected review |
| `d` | Show tmux pane capture for selected review |
| `l` | Toggle log tail panel |

**Panels:** Active reviews (with status icons), queue, recent reviews (last 8), health checks, live log tail, 24h metrics bar.

## GitHub Labels

| Label | Effect |
|-------|--------|
| `gate-skip` | Skip review, auto-approve |
| `gate-emergency-bypass` | Emergency bypass, auto-approve |
| `gate-rerun` | Re-trigger a review |
| `gate-no-fix` | Block auto-fix for this PR |

## Operations

### Cron Jobs

```
*/5 * * * *  gate health        → health monitoring + alerts
0 9 * * *    gate digest        → daily metrics summary
0 3 * * *    gate cleanup       → log rotation + worktree pruning
```

### LaunchAgent

```bash
# Install (auto-start on login)
cp config/com.gate.server.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.gate.server.plist

# Control
launchctl unload ~/Library/LaunchAgents/com.gate.server.plist  # stop
launchctl list | grep gate                                      # check
```

### Remote Access

```bash
# SSH + tmux
ssh your-gate-host
tmux attach -t gate

# Status check (no tmux needed)
ssh your-gate-host gate status
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Server not running | `tmux new 'gate up'` |
| Runner offline | `cd <runner-dir> && ./svc.sh start` |
| Claude auth expired | `claude auth login --method browser` |
| Circuit breaker tripped | Last 3 reviews were errors. Fix root cause; next success resets. |
| Check stuck in_progress | `gate health` auto-cleans orphaned checks (fail-open) |
| No notifications | Check `echo $GATE_NTFY_TOPIC` |
| Disk filling up | `gate cleanup` |
| Machine sleeping | `sudo pmset -a disablesleep 1` |
| Prerequisites broken | `gate doctor` to diagnose |

## Documentation

- [health-and-recovery.md](docs/health-and-recovery.md) — Health checks, failure scenarios, recovery
- [metrics.md](docs/metrics.md) — Review metrics, log schema, TUI metrics
- [notifications.md](docs/notifications.md) — ntfy + Discord setup
- [remote-access.md](docs/remote-access.md) — SSH, tmux, status commands

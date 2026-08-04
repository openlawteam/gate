# Remote Access

## SSH + tmux

Gate runs in a persistent tmux session. Connect via SSH and attach:

```bash
ssh your-gate-host
tmux attach -t gate
```

The TUI shows active reviews, queue, stage progress, durable history, evidence, health, and logs.

### tmux Keybindings

| Key | Action |
|-----|--------|
| `q` | Quit TUI (stops server) |
| `r` | Refresh all panels |
| `c` | Cancel selected review |
| `d` / `h` | Dashboard / history |
| `Enter` / `Esc` | Open review detail / return |
| `l` | Toggle log tail panel |
| `o` | Open the selected PR |
| `?` | Help |
| `Ctrl-b d` | Detach from tmux (leave running) |

## Quick Status (No tmux)

Check gate status without attaching to tmux:

```bash
ssh your-gate-host gate status
```

Output:

```
gate v0.1.0

Active reviews (1):
  PR #42 — logic (running)

Queued (0):

Health: all OK
```

## Tailscale

Gate uses Tailscale for reliable remote access. The health check monitors Tailscale status and alerts if it goes down.

```bash
# From any Tailscale device
ssh user@your-host.tailnet.ts.net
```

## Phone Access

### View Status

- SSH from Termius/Blink: `ssh your-gate-host` then `gate status`
- ntfy push notifications for all review events

### Control Actions

From GitHub (phone or desktop):

| Action | How |
|--------|-----|
| Re-run a review | Add `gate-rerun` label |
| Skip a review | Add `gate-skip` label |
| Emergency bypass | Add `gate-emergency-bypass` label |
| Block auto-fix | Add `gate-no-fix` label |

### Cancel a Review

```bash
ssh your-gate-host gate cancel --pr 42
```

## Headless Mode

For LaunchAgent or unattended operation:

```bash
gate up --headless
```

Runs the server without TUI. Use `gate status` for monitoring.

## Diagnostics

```bash
# Full health check
gate health

# Prerequisite verification
gate doctor

# Check server logs
tail -f "$HOME/Library/Application Support/gate/logs/activity.log"

# Check specific PR
find "$HOME/Library/Application Support/gate/logs/live" -name 'pr42.log' -print
```

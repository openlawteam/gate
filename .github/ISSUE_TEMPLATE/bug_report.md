---
name: Bug report
about: Report a reproducible problem so we can fix it
title: ''
labels: bug
assignees: ''
---

## Summary

A short description of what went wrong.

## Environment

- Gate version / commit: <!-- `gate --version` or `git rev-parse --short HEAD` -->
- Operating system: <!-- macOS 14.x, Ubuntu 22.04, etc. -->
- Python version: <!-- `python --version` -->
- How Gate is running: <!-- `gate up` TUI, `gate up --headless`, or launched via LaunchAgent / systemd -->

## Steps to Reproduce

1. ...
2. ...
3. ...

## Expected Behavior

What you expected Gate to do.

## Actual Behavior

What Gate actually did. Include full error messages.

## Logs

<details>
<summary>activity.log (last ~50 lines)</summary>

```
# Paste from `~/Library/Application Support/gate/logs/activity.log` on macOS
# or `~/.local/share/gate/logs/activity.log` on Linux.
```

</details>

## Additional Context

Configuration snippets (redact secrets), screenshots, or anything else that helps.

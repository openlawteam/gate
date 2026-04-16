# CLAUDE.md

Development guidelines for Gate, an autonomous PR review system.

## Project Overview

Gate reviews GitHub PRs with a staged AI pipeline (triage -> build -> architecture -> security -> logic -> verdict) and can optionally auto-fix blocking findings via a Claude-senior/Codex-junior pair. It runs on a self-hosted tmux host, talks to GitHub over the `gh` CLI, and broadcasts live state to a Textual TUI over a Unix socket.

## Key Concepts

- **Review**: one end-to-end pass over a PR head SHA, producing a structured `verdict.json`.
- **Stage**: one step in the pipeline (triage, build, architecture, security, logic, verdict). Structured stages run inline; agent stages run in tmux windows.
- **Gate**: a pre-stage check that can short-circuit to approve (labels, circuit breaker, fix-rerun, quota, cycle limit). The five gates live at the top of `orchestrator.run()`.
- **Fix pipeline**: when the verdict is `request_changes`, Gate can spawn a second pass where Claude (senior) directs Codex (junior) through prep -> design -> implement -> audit -> commit.

## Architecture

```
GitHub PR
    |
    +-- GHA thin trigger (.github/workflows/gate-review.yml)
    |
    +-- gate review --pr N --repo ... --head-sha ...    (self-hosted runner)
            |
            +-- Unix socket -> GateServer (always-on, via `gate up`)
                    |
                    +-- ReviewQueue (ThreadPoolExecutor, max 3 concurrent)
                    |       |
                    |       +-- ReviewOrchestrator per PR
                    |               +-- 5 gates (labels, breaker, fix-rerun, quota, cycle)
                    |               +-- Stage dispatch (structured inline, agent via tmux)
                    |               +-- Optional Fixer (Claude senior + Codex junior)
                    |
                    +-- Broadcast -> TUI + other clients
```

**Data flow for one review:** webhook -> GHA -> `gate review` CLI -> server `review_request` event -> queue -> orchestrator spawns stages -> each stage broadcasts `stage_register`/`review_stage_update` -> TUI renders live -> verdict persisted -> `review_completed` broadcast -> GitHub check run + commit status updated.

## Paths and Persistence

Gate splits paths into three roles:

- **`gate_dir()`** (`GATE_DIR`): the package install root. Ships with `prompts/`, `workflows/`, and default `config/` assets. Read-only at runtime.
- **`data_dir()`** (`GATE_DATA_DIR`, via `platformdirs.user_data_dir("gate")`): all mutable runtime state. On macOS this is `~/Library/Application Support/gate`. Contains `state/`, `logs/`, `server.sock`, `npm-cache/`.
- **`config_dir`** for user config: currently `gate_dir() / "config" / "gate.toml"` (set up by `gate init`). Kept under the install tree so the user's edited TOML travels with the checkout.

Both `GATE_DIR` and `GATE_DATA_DIR` are mutable module globals that tests monkeypatch through `tests/conftest.py`'s autouse `isolate_paths` fixture. Because of this, every helper that needs a runtime path (`state_dir()`, `logs_dir()`, `socket_path()`, `reviews_jsonl()`, `live_dir()`, `quota_cache_path()`, `prompts_dir()`) is a **function**, never a module-level constant. Adding a new path? Add a function, not `FOO = data_dir() / "foo"`.

All full-file writes go through `gate.io.atomic_write` / `atomic_write_bytes` (`.tmp` sibling + `os.replace`, with cleanup on failure). JSONL files (`reviews.jsonl`, live logs) are still append-mode `open("a")` because POSIX appends of short lines are atomic; only full rewrites (like `_trim_jsonl`) use `atomic_write`.

## Shared-State Hazards

Contributors need to know these before touching the hot paths:

- **Server reviews list vs filesystem state.** `GateServer.reviews` is in-memory; `state/{slug}/prN/` is on disk. They can diverge if a review crashes mid-flight. `cleanup_orphans()` (run on startup) reconciles them via `active_review.json` markers and PID liveness checks. Never assume one mirrors the other.
- **Fail-open is intentional.** Any exception in `orchestrator.run()` approves the PR with an error message rather than blocking it. This is documented in `docs/security.md`. If you add a new failure path, make sure the `except Exception` at the end of `run()` still catches it.
- **Per-PR counter atomicity is queue-enforced.** `record_fix_attempt` does read-increment-write without file locks. Safe because `ReviewQueue` serialises work for a given `(repo, pr_number)` key. If you ever parallelise per-PR, put the locking back.
- **Check run lifecycle is the safety net.** Revision 4 creates the check run **before** the first gate so that any crash leaves a "gate review failed" check that `cleanup_orphans()` can complete. Never move the `create_check_run` call later in the pipeline.
- **tmux stuck detection is threaded.** The `ReviewRunner` monitor thread can kill a stage pane while the main thread is still reading its output. If you add new pane bookkeeping, guard it with `_panes_lock`.
- **Module-level path constants will silently leak.** A `FOO = data_dir() / ...` at module scope freezes the value at import time and bypasses test isolation. Use a function.

## Where Codex Lives

Codex is the junior engineer in the fix pipeline. It **never runs in its own tmux window** — looking for a `codex` window will always fail. Gate spawns it three different ways, each invisible in a different way:

1. **Bootstrap (`gate.codex.bootstrap_codex`).** `subprocess.run(..., stdout=PIPE)` with `codex exec --json`. Runs on the orchestrator's pool worker thread. Output is captured in-process to extract the thread id — nothing appears on a terminal.
2. **In-pane delegation (`gate-code` during `fix-senior`).** The senior Claude runs `gate-code <stage> <<<EOF ... EOF` via its Bash tool. `gate-code` (entry point in [gate/code.py](gate/code.py)) calls `codex.run_codex`, which invokes `codex exec resume <thread-id>`. The Codex subprocess is a child of `claude`, so its stdio inherits the `fix-senior` tmux pane — you see it there as Bash-tool output, *not* a separate window.
3. **Off-tmux resume (`FixPipeline._resume_fix_session`).** After the first fix iteration, Gate calls `claude --resume` via `subprocess.run` with **no tmux window**. Any further `gate-code` calls run with their stdio attached to the pool worker thread — completely invisible to the TUI.

Where to look for Codex activity:

- **Per-stage output files.** `codex exec` always uses `-o <path>` to write its final message. `gate-code` writes to `{workspace}/{stage}.out.md` (and `.in.md` for the input prompt). These are the canonical "what Codex said" artifacts.
- **Live PR log.** `logs/live/<slug>/pr<N>.log` has high-level markers like `[fix] Bootstrapping Codex...` via `write_live_log`. Not a transcript — just phase boundaries.
- **fix-senior tmux pane.** Bash-tool invocations during the first fix iteration are visible as Claude Code interleaves them with its own output. This is the only place you see live Codex.
- **Orchestrator logs.** `~/Library/Application Support/gate/logs/activity.log` captures any `logger.info/warning` from `codex.py` or `code.py` (e.g. command failures).

Why resume-phase Codex is especially hidden: `_resume_fix_session` calls `claude` with inherited stdio on a background thread, so neither the fix-senior pane nor the TUI sees it. If you need visibility into a long resume session, tail the `.out.md` files in the workspace.

## Commands

```bash
make install    # pip install -e ".[dev]"
make test       # pytest (~1.5 min for ~600 tests)
make ci         # ruff format + ruff check + pytest
make lint       # ruff check
make format     # ruff format
pytest tests/test_orchestrator.py::TestCancel::test_cancel_sets_event_and_kills_panes
```

## Development Principles

- **Atomic writes for full rewrites.** Use `gate.io.atomic_write` rather than `Path.write_text` when the consumer should never see a partial file. JSONL appends are fine to stay append-mode.
- **Env allowlist for subprocesses.** `build_claude_env()` in `config.py` is the single source of truth for what environment Claude subprocesses see. Add new env keys there, don't inherit `os.environ` ad hoc.
- **Fail-open, alert loud.** Approvals on errors are OK; silent failures are not. Log to `activity.log` and emit a notification via `notify.review_failed` on any fail-open path.
- **Test everything, mock everything.** The autouse `isolate_paths` fixture guarantees no test ever writes to real `~/Library/Application Support/gate` or the repo's `logs/`. If a test needs real paths, use the `real_gate_dir` fixture and scope the work carefully.
- **Prompt injection guards.** Every review prompt starts with an "Important: Untrusted Content" block. When adding a new review stage, copy the block verbatim from `prompts/triage.md`.

## File Layout

| Module | Owns |
|--------|------|
| `cli.py` | Argument parsing and command dispatch |
| `server.py` | Unix socket server, broadcast queue, reaper |
| `queue.py` | Review concurrency and dispatch to orchestrator |
| `orchestrator.py` | Per-review state machine (5 gates + stages) |
| `runner.py` | Agent-stage tmux lifecycle + stuck detection |
| `fixer.py` | Fix pipeline (Claude senior + Codex junior) |
| `claude.py` / `tmux.py` | tmux primitives |
| `github.py` | All `gh` CLI calls (statuses, checks, reviews) |
| `prompt.py` | Template loading, variable substitution |
| `schemas.py` | Structured stage result types |
| `state.py` | Per-PR state persistence + fix counters |
| `logger.py` | `reviews.jsonl`, live logs, sidecar metadata |
| `cleanup.py` | Log rotation, worktree pruning, orphan cleanup |
| `health.py` | 13-check health system + alert cooldowns |
| `quota.py` | Anthropic usage API + cache |
| `workspace.py` | Git worktree creation + artifact exclusion |
| `config.py` | TOML config + path roots + env allowlist |
| `io.py` | `atomic_write` / `atomic_write_bytes` helpers |
| `tui.py` | Textual dashboard |
| `notify.py` | ntfy + Discord push notifications |
| `builder.py` | Per-project build/typecheck/lint/test runner |
| `profiles.py` | Language profile detection |

## TUI Conventions

- **Framework**: [Textual](https://textual.textualize.io/) with `DataTable` for review/queue/history lists.
- **Theme**: Tokyo Night (see `GATE_THEME` in `tui.py`). Don't introduce a second theme.
- **Unicode only**: status icons like `●`, `✓`, `✗`, `⊘`, `◷`, `⋯`, `⚒`. No emoji.
- **Incremental updates**: re-render by mutating existing rows, not replacing the table, so cursor position is preserved.
- **Path reads are lazy**: always call `reviews_jsonl()`/`live_dir()` from inside the refresh loop, not at import time, so test isolation works.

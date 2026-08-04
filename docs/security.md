# Gate Security Threat Model

## Overview

Gate is a local AI pull-request review control plane that runs on a trusted macOS host. This document describes its v0.1 security boundary, accepted risks, and future hardening.

## v0.1 Trusted-Source Rule

Gate fetches pull-request metadata through GitHub's API before creating a workspace or invoking any repository or model process. A review is executable only when the head repository exactly equals both the API-reported base repository and the configured repository. Cross-repository pull requests are recorded as `untrusted`, receive an `action_required` commit status, and stop before worktree, tmux, Claude, Codex, build, or script execution. There is no fork bypass in v0.1.

The packaged GitHub workflow repeats the same equality guard. This defense in depth does not replace the orchestrator-side check.

Infrastructure errors, circuit-breaker activation, and orphan cleanup complete neutrally or as errors and never approve. `gate-skip` and `gate-emergency-bypass` are deliberate trusted-operator overrides.

## Trust Boundaries

### Trusted Components
- **The host machine** — physically secured, single-user, operator-controlled
- **Gate source code** — installed from a reviewed distribution or checkout
- **Configuration files** — `gate.toml`, `fix-blocklist.txt`, prompt templates
- **Secrets** — `GATE_PAT`, `CLAUDE_CODE_OAUTH_TOKEN` stored in environment/Keychain; Codex tokens stored in `~/.codex/auth.json`

### Semi-Trusted
- **Same-repository PR content** — still untrusted as data and may contain adversarial prompt injection
- **AI model outputs** — Claude and Codex produce review verdicts and code changes that are validated but not formally verified

### Untrusted
- **External network** — GitHub API responses, package registries (npm, PyPI, etc.), AI API endpoints

## Dangerous Permission Flags

### `--dangerously-skip-permissions` (Claude in fix pipeline)

Used in `fixer.py` when Claude runs the fix-senior stage. This flag allows Claude to read/write files and execute commands without per-action approval.

**Why it's needed:** The fix pipeline must edit source files, run configured build, lint, and test commands, and validate changes — all requiring filesystem and process access.

**Mitigations:**
- **Ephemeral worktrees** — each fix session runs in a disposable `git worktree` under `/tmp/gate-worktrees/`, removed after completion
- **Blocklist** — the packaged `gate/resources/config/fix-blocklist.txt` prevents modifications to schema files, lockfiles, env files, CI config, and review rules
- **Build verification** — `build_verify()` runs configured typecheck/lint/test commands before and after fixes; regressions are rejected
- **Re-review** — a separate Claude session (`fix-rereview`) evaluates the diff independently
- **Env allowlist** — `build_claude_env()` in `config.py` passes only explicitly listed environment variables to Claude, not the full parent environment

### `--dangerously-bypass-approvals-and-sandbox` (Codex)

Used in `codex.py` for delegated sub-tasks. Same rationale as above — Codex needs to edit files and run commands.

**Additional mitigation:** Codex sessions are scoped to specific stages (`gate-prep`, `gate-design`, `gate-implement`, `gate-audit`) with focused prompts.

## Prompt Injection Surface

PR content (title, body, diff, file contents) is injected into prompt templates via variable substitution. A malicious PR could embed instructions in code comments or the PR body attempting to manipulate review outcomes.

**Current mitigations:**
- Review prompts include an "untrusted content" warning instructing the model to ignore embedded instructions
- Template substitution is single-pass (`safe_substitute`) — user content in variable values is not re-parsed as template syntax
- Size limits (`max_diff_bytes`, `max_pr_body_bytes`) prevent prompt flooding
- The verdict stage synthesizes from multiple independent review stages, making single-stage manipulation less effective

**Residual risk:** Sophisticated prompt injection could still influence individual stage findings. This is accepted for a private repo with a known team.

## Secrets Management

| Secret | Storage | Exposure |
|--------|---------|----------|
| `GATE_PAT` | Environment variable | Passed to `gh` CLI and git via `GIT_CONFIG` extraheader; visible to child processes via `build_claude_env()` allowlist |
| `CLAUDE_CODE_OAUTH_TOKEN` | Environment + macOS Keychain | Passed to Claude CLI; read from Keychain for quota checks |
| Codex tokens | `~/.codex/auth.json` (ChatGPT OAuth) | Read by Codex CLI directly; Gate does not pass or read these tokens |

**Note:** `code.py` uses `build_claude_env()` to construct a minimal environment for Codex. Codex authenticates independently via its own `auth.json` file.

## What a Sandbox Would Look Like

If stronger isolation were needed, fix sessions could be wrapped in a container or sandbox:

### Option A: Docker wrapper
- Run each fix session in a Docker container with the worktree bind-mounted
- Share dependency cache via a read-only volume
- Restrict network to only the AI API endpoints
- **Trade-offs:** Adds 5-10s latency per fix session; requires Docker on the host; dependency cache sharing becomes read-only (no writes from container)

### Option B: macOS `sandbox-exec` / `pf` firewall
- Use macOS `sandbox-exec` profiles to restrict filesystem access to the worktree
- Use `pf` packet filter rules to restrict network access during fix sessions
- **Trade-offs:** macOS sandbox profiles are deprecated and poorly documented; `pf` rules are fragile across OS updates

### Recommendation
Use Gate only on repositories whose contributors and branch-write permissions you trust. Public-fork execution remains unsupported until Gate has a real sandbox.

## Unix Socket Security

The Gate server uses a Unix domain socket (`server.sock`) for IPC. Security relies on filesystem permissions — any local user who can open the socket path can send messages. On a single-user machine, this is acceptable. For multi-user hosts, restrict socket permissions to the Gate user.

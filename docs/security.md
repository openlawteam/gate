# Gate Security Threat Model

## Overview

Gate is an autonomous AI-powered PR review system that runs on a trusted self-hosted machine, reviewing PRs on your GitHub repository. This document describes the security boundaries, accepted risks, and potential future hardening.

## Trust Boundaries

### Trusted Components
- **The host machine** — physically secured, single-user, operator-controlled
- **Gate source code** — maintained in a private repo, changes go through review
- **Configuration files** — `gate.toml`, `fix-blocklist.txt`, prompt templates
- **Secrets** — `GATE_PAT`, `CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_API_KEY` stored in environment/Keychain

### Semi-Trusted
- **PR content** — authored by known team members on a private repo, but could contain adversarial patterns (prompt injection via code comments, PR body, file names)
- **AI model outputs** — Claude and Codex produce review verdicts and code changes that are validated but not formally verified

### Untrusted
- **External network** — GitHub API responses, package registries (npm, PyPI, etc.), AI API endpoints

## Dangerous Permission Flags

### `--dangerously-skip-permissions` (Claude in fix pipeline)

Used in `fixer.py` when Claude runs the fix-senior stage. This flag allows Claude to read/write files and execute commands without per-action approval.

**Why it's needed:** The fix pipeline must edit source files, run configured build, lint, and test commands, and validate changes — all requiring filesystem and process access.

**Mitigations:**
- **Ephemeral worktrees** — each fix session runs in a disposable `git worktree` under `/tmp/gate-worktrees/`, removed after completion
- **Blocklist** — `config/fix-blocklist.txt` prevents modifications to schema files, lockfiles, env files, CI config, and Cursor rules
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
| `OPENAI_API_KEY` | Environment variable | Passed to Codex via `code.py` |

**Note:** `code.py` currently uses `build_claude_env()` (after the env leakage fix) to avoid passing the full parent environment to Codex.

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

### Option C: nsjail (Linux) or Firecracker
- Not directly applicable on macOS; would require a Linux VM layer
- **Trade-offs:** Significant complexity; defeats the purpose of a lightweight local service

### Recommendation
For a private repo with a small trusted team, the current mitigations (ephemeral worktrees, blocklist, build verification, re-review) provide adequate protection. Sandboxing should be revisited if Gate is deployed against public or untrusted repositories.

## Unix Socket Security

The Gate server uses a Unix domain socket (`server.sock`) for IPC. Security relies on filesystem permissions — any local user who can open the socket path can send messages. On a single-user machine, this is acceptable. For multi-user hosts, restrict socket permissions to the Gate user.

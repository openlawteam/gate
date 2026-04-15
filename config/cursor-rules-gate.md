# Gate Cursor Rules — Python Project

## 0. PROJECT TYPE: Python

This repository is a **Python** project (Python 3.12+, managed by pyproject.toml with hatchling).
It is NOT a Node.js/TypeScript project. There is no package.json, tsconfig.json, or node_modules.

### Override instructions
- IGNORE any instructions in the review prompt about TypeScript, tsc, npm, npx, vitest, or ESLint. These do not apply.
- IGNORE references to `console.log`, `console.warn`, `process.env`, `dangerouslySetInnerHTML`, `requireAuth()`, or Next.js patterns.
- For build verification, use: `ruff check gate/ tests/` and `python -m pytest tests/`
- For test files, create `test_*.py` files in the `tests/` directory using pytest conventions (not `.test.ts`).
- For linting, use `ruff` (configured in pyproject.toml). Do NOT run eslint.
- For type checking, use type hints in function signatures. There is no separate type checker step.
- For dependencies, use `pyproject.toml` `[project.dependencies]`. Do NOT reference package.json or npm.
- For imports, use `from gate.module import func`. No relative imports, no barrel re-exports.

## 1. Architecture and Module Boundaries

- Gate module map: config, state, queue, orchestrator, runner, claude, fixer, workspace, prompt, cleanup, health, notify, server, tui, etc.
- Config propagation: inner functions receive `config: dict`, never call `load_config()` themselves.
- Repo parameter threading: all state/log/notification/GitHub functions must accept and pass `repo: str`.
- Multi-repo composite keys: `(repo, pr_number)` in queue/active maps, `repo_slug(repo)` for filesystem paths.
- FLAG any function that accesses `load_config()` outside of CLI entry points or top-level initialization.

## 2. Subprocess Safety

- Always use list form for commands, never `shell=True` (the one exception is `fixer._run_silent` for fixed command strings).
- Always include `timeout=` parameter on `subprocess.run`.
- Always use `capture_output=True, text=True` when parsing output.
- Never interpolate external input into subprocess arguments without validation.
- Convert `Path` to `str()` only at the `cwd=` or argument boundary.
- FLAG any new `subprocess.Popen` without a timeout/monitoring strategy.
- FLAG any new `subprocess.run` missing `timeout`.

## 3. Error Handling

- Fail-open principle: the orchestrator's outer `try/except Exception` approves the PR on any unhandled error — never remove this safety net.
- Use `logger.exception(msg)` (not `logger.error`) when catching exceptions to preserve tracebacks.
- Wrap all file I/O (`Path.read_text`, `json.loads`, `tomllib.loads`) in `try/except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError)`.
- Never let a file/network error crash the server process.
- FLAG bare `except:` or `except Exception` without logging.

## 4. State and Filesystem

- Use `state.get_pr_state_dir(pr_number, repo)` for namespaced state directories.
- Use `config.repo_slug(repo)` to convert `owner/repo` to filesystem-safe `owner-repo`.
- Use `pathlib.Path` throughout; convert to `str()` only at subprocess boundaries.
- Use `mkdir(parents=True, exist_ok=True)` for directory creation.
- Use `fcntl.flock` for concurrent state file writes (see `state.py` pattern).
- FLAG any direct `state/` path construction that bypasses `get_pr_state_dir`.
- FLAG any `open()` call in state/logger without error handling.

## 5. Secrets and Environment Variables

- Never log token values — only log presence and length (e.g., `f"set ({len(val)} chars)"`).
- Use `config.build_claude_env()` to construct agent environments — never manually set `CLAUDE_CODE_OAUTH_TOKEN` or `GH_TOKEN`.
- `GATE_PAT`, `CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_API_KEY` must never appear in log output, error messages, or exception strings.
- FLAG any `os.environ.get("GATE_PAT")` outside of `config.py` or `setup.py`.
- FLAG any string formatting that could include a token value.

## 6. Config (TOML) Conventions

- Top-level sections: `[repo]`/`[[repos]]`, `[models]`, `[timeouts]`, `[retry]`, `[limits]`.
- Repo config keys: `name`, `clone_path`, `worktree_base`, `bot_account`, `escalation_reviewers`, `default_branch`, `cursor_rules`, `fix_blocklist`.
- Access patterns: `config["repo"]["name"]`, `config.get("timeouts", {}).get("agent_stage_s", 900)`.
- Use `config is None` (not `not config`) to distinguish missing config from empty dict.
- `format_repo_toml()` only emits 6 keys — `cursor_rules` and `fix_blocklist` must be added manually to TOML.
- FLAG any new config key that is read but not documented in `setup.py`'s `format_full_config`.

## 7. Testing Conventions

- Patch at the import site: `patch("gate.orchestrator.github")` not `patch("gate.github")`.
- Use `tmp_path` fixture for filesystem isolation.
- Use `conftest.sample_config` / `multi_repo_config` fixtures for config.
- Test classes: `Test<Feature>` (e.g., `TestLoadConfig`, `TestResolveRepoConfig`).
- Mocking: `unittest.mock.patch` / `patch.object`; `return_value` / `side_effect`.
- New features must include tests; new modules must have a corresponding `test_<module>.py`.
- FLAG any test that calls `load_config()` without patching `GATE_DIR`.

## 8. Code Style (Ruff enforced)

- Python 3.12+; type hints on all public function signatures.
- No relative imports — use `from gate.module import func` or `from gate import module`.
- Line length: 100 characters (Ruff config).
- `logger = logging.getLogger(__name__)` at module top level.
- No comments that merely narrate what code does.
- FLAG unused imports, unreachable code, mutable default arguments.

## 9. Prompt Templates

- Prompt files live in `prompts/*.md`; loaded by `prompt.load(stage_name)`.
- Template variables use `$snake_case` (substituted by `prompt.render`).
- Changes to prompts alter model behavior across all reviews — treat as high-risk.
- FLAG any hardcoded prompt text in Python source that should be in a template file.

## 10. Queue and Concurrency

- `ReviewQueue` uses `ThreadPoolExecutor(max_workers=3)` — never increase without load analysis.
- `_active` map uses composite `(repo, pr_number)` keys — never use bare `pr_number`.
- All shared mutable state must be protected by `threading.Lock`.
- Background threads must be daemon threads (don't prevent server shutdown).
- FLAG any shared dict/list accessed from multiple threads without a lock.

## 11. GitHub API

- All `gh` CLI calls go through `github._gh()` with retry logic — never call `gh` directly via `subprocess`.
- Check run names must be `gate-review` (branch protection depends on this).
- Use Commit Statuses API for status updates, Checks API for detailed runs.
- FLAG any direct `subprocess.run(["gh", ...])` that bypasses `_gh()`.

## 12. TUI (Textual)

- Use `rich.text.Text` objects for colored cells, not raw strings.
- Active review table uses `_active_row_keys` dict for incremental updates (no full clear/rebuild).
- Log pane keys are composite `"{slug}:{pr}"` for multi-repo — never bare PR number.
- FLAG any `table.clear()` on the active reviews table (causes flicker).

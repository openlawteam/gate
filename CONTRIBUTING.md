# Contributing to Gate

Thanks for your interest in Gate, a self-hosted AI-powered PR review system. This guide covers everything a new contributor needs to build, test, and ship a change.

For deeper technical context and AI-coding-agent-specific guidance, see [CLAUDE.md](CLAUDE.md).

## Prerequisites

- macOS with Python 3.11, 3.12, 3.13, or 3.14
- [`uv`](https://docs.astral.sh/uv/) for the frozen development environment
- [`tmux`](https://github.com/tmux/tmux) -- required for agent stage execution
- [`gh`](https://cli.github.com/) -- GitHub CLI, used for every API call
- [`claude`](https://claude.com/product/claude-code) -- Claude Code CLI, authenticated (OAuth or `CLAUDE_CODE_OAUTH_TOKEN`)
- `codex` CLI -- optional, only needed for the auto-fix pipeline

## Development Setup

```bash
git clone https://github.com/openlawteam/gate.git
cd gate
uv sync --frozen --all-extras
make ci         # format + lint + full mypy + coverage-enforced tests
```

`make install` syncs the checked-in lockfile. `make ci` chains format checking, lint, full mypy, and the coverage-enforced test suite; run it before every push.

## Architecture in One Minute

```
GHA thin trigger -> `gate review` CLI -> Unix socket -> GateServer
                                         -> ReviewQueue (ThreadPoolExecutor)
                                         -> ReviewOrchestrator
                                             -> stage runners (tmux / inline)
                                             -> optional FixPipeline
```

See [CLAUDE.md](CLAUDE.md) for data flow, path conventions, shared-state hazards, and the rationale behind tmux as an execution substrate. See [README.md](README.md) for operator-facing documentation (installation, CLI, config).

## Paths at Runtime

Gate splits paths into two roles (all exposed as **functions**, never constants, so tests can monkeypatch them):

- `gate.config.gate_dir()` -- read-only packaged resources (`prompts/`, `config/`, `workflows/`, and `demo/`)
- `gate.config.config_file()` -- canonical user config, with `GATE_CONFIG_PATH` override
- `gate.config.data_dir()` -- mutable runtime state via `platformdirs` (state, logs, socket, caches)

When adding a new runtime path, add a helper function in `gate/config.py` (or a module that already exposes one), not a module-level `FOO = data_dir() / "foo"`. Module-level evaluation freezes the path at import time and breaks test isolation.

## Running Tests

```bash
make test                                            # full suite, ~2 min
uv run --frozen pytest -o addopts='' tests/test_queue.py -q
uv run --frozen pytest -o addopts='' tests/test_orchestrator.py::TestCancel -v
```

The autouse `isolate_paths` fixture in [`tests/conftest.py`](tests/conftest.py) redirects packaged resources, user configuration, legacy configuration, and runtime data to a per-test directory. Tests never write to the operator's real `~/Library/Application Support/gate`. The complementary environment fixture strips credentials and notification settings.

If a test genuinely needs the real package directory (e.g. to validate a shipped prompt), request the `real_gate_dir` fixture.

## Adding a New Review Stage

1. Write the prompt template as `gate/resources/prompts/<stage>.md` (include the "Untrusted Content" preamble).
2. Register the stage name in `gate/schemas.py`:
   - Add to `ALLOWED_STAGES`.
   - Add to `AGENT_STAGES` or `STRUCTURED_STAGES`.
   - If structured, add a JSON schema entry to `STAGE_SCHEMAS`.
3. Wire the stage into `ReviewOrchestrator.run()` in `gate/orchestrator.py`.
4. Add a test in `tests/test_orchestrator.py` that exercises the happy path and at least one neutral/error path. Infrastructure failures must never approve.

## Adding a Language Profile

Profiles in [`gate/profiles.py`](gate/profiles.py) drive build verification. To add a new language:

1. Add an entry to `PROFILES` with `typecheck_cmd`, `lint_cmd`, `test_cmd`, `dep_install_cmd`, `dep_file`, and the language metadata used by prompts.
2. Add a marker-file check to `detect_project_type()` (e.g. `Package.swift` for Swift).
3. If the test runner's output format is parseable, add a parser to `gate/builder.py` similar to `_parse_tsc` or `_parse_pytest`; otherwise the generic exit-code parser is used.
4. Add tests in `tests/test_profiles.py` and `tests/test_builder.py`.

## Subprocess Safety

When adding a `subprocess.run` call:

- **Always** use list form (or `shlex.split(cmd)` for user-supplied strings). Never `shell=True`.
- **Always** pass `timeout=`.
- **Always** use `capture_output=True, text=True` when parsing output.
- Never interpolate untrusted input into the command.

## Atomic File Writes

Use `gate.io.atomic_write` / `atomic_write_bytes` for any full-file rewrite (JSON blobs, counters, trimmed JSONL). Append-only JSONL (`reviews.jsonl`, live logs) may continue to use `open("a")`.

## PR Guidelines

- Run `make ci` locally before pushing.
- Add tests for new behavior. Every new module must have a corresponding `test_<module>.py`.
- Keep prompt changes small and focused. Prompts affect model behavior across every review.
- Preserve the safety contract in `ReviewOrchestrator.run()`: untrusted forks are refused before execution, and infrastructure errors must never synthesize approval.
- Keep runtime defaults under `gate/resources/`; never make an installed package write into its own `site-packages` directory.

## Continuous Integration

[`.github/workflows/test.yml`](.github/workflows/test.yml) runs formatting, Ruff, full mypy, tests, and coverage on macOS with Python 3.11–3.14. It also installs the wheel outside the checkout and runs the demo smoke test. The Ubuntu 3.12 job is portable unit-test signal only, not a Linux support claim.

## Reporting Issues

Please use the [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/) templates so we have enough context to reproduce.

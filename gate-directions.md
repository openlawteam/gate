# Audit — finding 65f4229bed (OSError guard in codex.py)

Self-review your own just-landed change. Focus ONLY on the following:

## What to verify

1. **`gate/codex.py` around lines 200-210.**
   - The nested `try/except OSError` wraps ONLY the `open(stdout_log, "ab")` call — not the popen_kwargs assignment below.
   - On failure, `logger.warning` is called (not `logger.error` / `logger.exception`) and `stdout_handle` is explicitly set to `None`.
   - The outer `try:` block and the `finally:` close block are both still intact and unchanged.
   - No other lines in `run_codex()` changed.
   - No imports changed.

2. **`tests/test_codex.py` new test `test_stdout_log_open_error_falls_back_to_inherited_stdout`.**
   - It is inside the `TestRunCodex` class, not at module level.
   - It asserts `exit_code == 0` and `"stdout" not in kwargs`.
   - It does not leak real filesystem state outside `tmp_path`.
   - It uses the same `@patch("gate.codex.subprocess.Popen")` decorator pattern as neighbouring tests.

3. **Blocklist compliance.** Confirm no changes to `prompts/`, `config/`, `workflows/`, `pyproject.toml`, `Makefile`, `gate/schemas.py`, `docs/`, `README.md`.

4. **Rerun verification.** Run:

```bash
ruff check . 2>&1 | tail -20
python -m pytest tests/test_codex.py -x -q 2>&1 | tail -20
```

Report both tails.

## Output format

Respond with:

- **CLEAN** — if everything above checks out. Say "audit clean" and paste the two verification tails.
- **ISSUES** — a bulleted list of each concrete problem with file:line. Do NOT attempt to fix them yourself in this stage.

# Example Python Project — Gate Cursor Rules
#
# This is an example cursor rules file for Python projects.
# Copy to config/cursor-rules.md (or set cursor_rules path in gate.toml)
# and customize for your project's standards.

## 1. Architecture and Module Boundaries

- Define your module map and layer diagram here.
- Config propagation: inner functions receive `config: dict`, never call global config loaders themselves.
- FLAG any function that bypasses the config propagation pattern.

## 2. Subprocess Safety

- Always use list form for commands, never `shell=True`.
- Always include `timeout=` parameter on `subprocess.run`.
- Always use `capture_output=True, text=True` when parsing output.
- Never interpolate external input into subprocess arguments without validation.
- FLAG any `subprocess.run` missing `timeout`.

## 3. Error Handling

- Wrap all file I/O in `try/except (OSError, json.JSONDecodeError)`.
- Use `logger.exception(msg)` (not `logger.error`) when catching exceptions to preserve tracebacks.
- Never let a file/network error crash the main process.
- FLAG bare `except:` or `except Exception` without logging.

## 4. State and Filesystem

- Use `pathlib.Path` throughout; convert to `str()` only at subprocess boundaries.
- Use `mkdir(parents=True, exist_ok=True)` for directory creation.
- FLAG any direct path construction that bypasses utility functions.

## 5. Secrets and Environment Variables

- Never log token values — only log presence and length.
- Access secrets through a dedicated config module, not scattered `os.environ.get()` calls.
- FLAG any string formatting that could include a token value.

## 6. Config Conventions

- Document your config format (TOML, YAML, etc.) here.
- Use `config.get("section", {}).get("key", default)` for safe nested access.
- FLAG any new config key that is read but not documented.

## 7. Testing Conventions

- Patch at the import site: `patch("myapp.module.dependency")` not `patch("dependency")`.
- Use `tmp_path` fixture for filesystem isolation.
- Test classes: `Test<Feature>` (e.g., `TestParser`, `TestConfig`).
- New features must include tests; new modules must have a corresponding `test_<module>.py`.

## 8. Code Style

- Python 3.12+; type hints on all public function signatures.
- No relative imports — use `from package.module import func`.
- `logger = logging.getLogger(__name__)` at module top level.
- No comments that merely narrate what code does.
- FLAG unused imports, unreachable code, mutable default arguments.

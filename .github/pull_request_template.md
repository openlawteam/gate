## Summary

<!-- What changed, and why? -->

## Safety impact

- [ ] No change to fork trust, subprocess, credential, worktree, or auto-fix boundaries
- [ ] Safety-impacting changes have direct refusal/error-path tests
- [ ] Backward compatibility for protocol v1 and existing archives is preserved

## Verification

- [ ] `uv run --frozen ruff format --check .`
- [ ] `uv run --frozen ruff check .`
- [ ] `uv run --frozen mypy gate`
- [ ] `uv run --frozen pytest -q`
- [ ] `make package` and installed-wheel demo smoke, when packaging changed

## Evidence

<!-- Paste concise PASS/FAIL/NOT RUN results. Do not include credentials. -->

# Releasing Gate

Gate releases are intentionally manual. The repository prepares artifacts and a trusted-publishing workflow, but no branch, tag, package, or GitHub release may be published without explicit release-owner approval.

## Release candidate checklist

1. Reconfirm that the `gate-review` distribution name is available on PyPI.
2. Confirm the checkout is clean and inspect the complete diff and commit history.
3. From a clean checkout, run:

   ```bash
   uv sync --frozen --all-extras
   uv run --frozen ruff format --check .
   uv run --frozen ruff check .
   uv run --frozen mypy gate
   uv run --frozen pytest -q
   uv build --no-build-isolation --clear
   uv run --frozen twine check dist/*
   ```

4. Install the wheel into an empty virtual environment outside the checkout. Run `gate --version`, `gate --help`, `gate demo --headless --json`, and packaged prompt/workflow checks.
5. Inspect wheel and sdist file lists. Confirm prompts, configuration templates, workflow, sticky template, and demo resources are present.
6. Produce checksums with `shasum -a 256 dist/*` and draft release notes from `CHANGELOG.md`.
7. Reconfirm macOS CI for Python 3.11–3.14 and package smoke tests are green.

## Trusted publishing

`.github/workflows/publish.yml` is `workflow_dispatch` only, requires the exact confirmation text `publish gate-review`, uses the protected `pypi` environment, and publishes via OIDC. No PyPI password or token belongs in repository secrets.

Running that workflow is a publication action. Do not dispatch it until the release owner separately approves publication. Creating a tag or GitHub release is also a separate action and is not performed by the workflow.

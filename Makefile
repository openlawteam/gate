.PHONY: install test lint format typecheck package ci start restart doctor

install:
	uv sync --frozen --all-extras

test:
	uv run --frozen pytest

lint:
	uv run --frozen ruff check .

format:
	uv run --frozen ruff format --check .

format-fix:
	uv run --frozen ruff format .

typecheck:
	uv run --frozen mypy gate

package:
	uv build --no-build-isolation --clear
	uv run --frozen twine check dist/*

ci: format lint typecheck test

start:
	gate up

restart:
	-pkill -f "gate up" 2>/dev/null || true
	gate up

doctor:
	gate doctor

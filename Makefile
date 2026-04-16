.PHONY: install test lint format typecheck ci start restart doctor

install:
	pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check gate/ tests/

format:
	ruff format gate/ tests/

typecheck:
	mypy gate/

ci: format lint typecheck test

start:
	gate up

restart:
	-pkill -f "gate up" 2>/dev/null || true
	gate up

doctor:
	gate doctor

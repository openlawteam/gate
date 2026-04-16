.PHONY: install test lint format ci start restart doctor

install:
	pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check gate/ tests/

format:
	ruff format gate/ tests/

ci: format lint test

start:
	gate up

restart:
	-pkill -f "gate up" 2>/dev/null || true
	gate up

doctor:
	gate doctor

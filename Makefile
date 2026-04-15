.PHONY: install test lint format start restart doctor

install:
	pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check gate/ tests/

format:
	ruff format gate/ tests/

start:
	gate up

restart:
	-gate stop 2>/dev/null || true
	gate up

doctor:
	gate doctor

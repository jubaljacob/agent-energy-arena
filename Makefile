# Feedback loops for the Energy-AI Nexus simulator.
#
# `make check` is the canonical "is it ready to commit?" gate. CI and the
# AFK agent loop should both run this. Individual targets exist for fast
# iteration during development.

# Honor an active virtualenv if present; otherwise fall back to the local
# .venv (created by `make venv`); otherwise system python.
PYTHON ?= $(shell \
	if [ -n "$$VIRTUAL_ENV" ]; then echo "$$VIRTUAL_ENV/bin/python"; \
	elif [ -x ".venv/bin/python" ]; then echo ".venv/bin/python"; \
	else echo python3; fi)

.PHONY: help venv install test typecheck lint format format-check check play clean

help:
	@echo "Targets:"
	@echo "  install       Install package with dev extras into the active env"
	@echo "  venv          Create .venv if it does not already exist"
	@echo "  test          Run pytest"
	@echo "  typecheck     Run mypy"
	@echo "  lint          Run ruff lint (no fixes)"
	@echo "  format        Apply ruff format in-place"
	@echo "  format-check  Verify ruff format without writing"
	@echo "  check         lint + format-check + typecheck + test (commit gate)"
	@echo "  play          Run uvicorn at :8000 against the world"

venv:
	@test -d .venv || python3 -m venv .venv
	@$(PYTHON) -m pip install --upgrade pip >/dev/null

install: venv
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest

typecheck:
	$(PYTHON) -m mypy

lint:
	$(PYTHON) -m ruff check .

format:
	$(PYTHON) -m ruff format .

format-check:
	$(PYTHON) -m ruff format --check .

check: lint format-check typecheck test

play:
	$(PYTHON) -m uvicorn world.api:app --reload --host 0.0.0.0 --port 8000

clean:
	rm -rf .mypy_cache .ruff_cache .pytest_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# CLAUDE.md

Guidance for Claude Code when working in this repo.

## Agent skills

### Issue tracker

Issues live as markdown files under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical five-role vocabulary, no overrides. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Feedback loops

This is a Python project. There is no `npm`. Run the gates via the Makefile:

- `make test` — pytest
- `make typecheck` — mypy (configured in `pyproject.toml`)
- `make lint` — ruff lint
- `make format` — ruff format in-place; `make format-check` to verify
- `make check` — lint + format-check + typecheck + test, in that order. **Run this before every commit.** It is the canonical pre-commit gate.

If any gate fails, fix the underlying issue rather than skipping the gate. Configuration lives in `[tool.ruff]` and `[tool.mypy]` of `pyproject.toml`.

# 07: Arena package — runner + leaderboard + results module + CLI

Status: ready-for-agent

## Parent

`.scratch/open-source-arena/PRD.md`

## What to build

Add a new top-level `arena/` Python package with three modules:

- `arena/results.py` — dataclass for a single `(agent, scenario)` result row (population, treasury delta, renewable share, raw score, run folder id, scenario name, agent name, seed, submission timestamp) plus JSON I/O.
- `arena/runner.py` — orchestrates `(agent, scenario)` pairs as subprocesses. One pair per subprocess for isolation: a misbehaving agent cannot poison the eval. Captures per-pair results to a JSON file. Sequential execution (no `--parallel` in v1). Invokable as `python -m arena.runner`.
- `arena/leaderboard.py` — pure-function aggregator. Takes a list of results and produces a ranked Markdown table via mean-rank across scenarios. Ties break on mean raw score, then submission timestamp. Agents missing from one scenario are excluded from that scenario's rank but included in others. Writes the Markdown table to a file or stdout.

Tests cover the leaderboard as a pure function (known inputs → known rank tables, tie-breaking, missing-agent policy) and one end-to-end integration test runs the scripted agent against the baseline scenario through the runner CLI and asserts the result row matches a committed baseline file (may be marked slow).

## Acceptance criteria

- [ ] `arena/results.py`, `arena/runner.py`, `arena/leaderboard.py` exist with the responsibilities above.
- [ ] `python -m arena.runner` runs configured (agent, scenario) pairs in subprocesses and writes a results JSON file.
- [ ] The leaderboard module produces a Markdown table from a list of results; mean-rank with documented tie-breaking.
- [ ] Pure-function tests for the leaderboard cover known rankings, ties, and missing-agent policy.
- [ ] An end-to-end integration test runs scripted-agent × baseline through the runner CLI and asserts the result matches the committed baseline (may be marked slow).
- [ ] `make check` passes.

## Blocked by

- 05 (`v1 scenarios`)
- 06 (`evaluate.py --scenario flag`)

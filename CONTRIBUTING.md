# Contributing

Thanks for considering a contribution. This repo is a small Python project — most contributions are either a community agent submission, a new stress scenario, or a focused bugfix to the world. The same gates apply to all of them.

## Local development

```bash
make install      # creates .venv and installs the package with dev extras
make serve        # uvicorn on :8000 — try the UI
make check        # the canonical pre-commit gate (run this before every commit)
```

`make check` runs, in order:

1. `make lint` — ruff lint.
2. `make format-check` — ruff format in verify mode (`make format` fixes in place).
3. `make typecheck` — mypy (configured in `pyproject.toml`).
4. `make test` — pytest.

CI runs the same gate. If a step fails, **fix the underlying issue** rather than skipping it.

Other useful targets:

| Target | What it does |
|---|---|
| `make baselines` | Regenerate the per-scenario scripted-agent baselines under `baselines/arena/`. Run this after editing the scripted agent or a v1 scenario. |
| `make leaderboard` | Regenerate `LEADERBOARD.md` from the committed baselines. Run this after `make baselines`. |
| `make score` | Run the scripted agent on seed 42 and print the score. |
| `make play` | `docker compose up` — world + UI at `:8000`. |
| `make eval` | `docker compose --profile eval run agent` — evaluate `submit/agent.py`. |

## Submitting an agent

Community agents live under `agents/community/`. The PR-as-submission flow:

1. **One file per submission.** Place your agent at `agents/community/<your_handle>.py`. If you genuinely need helper modules, drop them in a subdirectory (`agents/community/<your_handle>/`) and import within.
2. **Header docstring.** Top of the file: a one-paragraph summary naming the author, the approach (rule-based / LLM / hybrid / something else), and any external dependencies. Cite the score range you achieved locally on seed 42.
3. **Conform to the `Agent` protocol.** Either subclass `agents.base.BaseAgent` (override `act(state)` and optionally `next_step_days(state)`) or implement `play_game() -> dict` directly. See [`agents/base.py`](agents/base.py) and the reference agents in `agents/scripted/agent.py`, `agents/llm_react/agent.py`.
4. **Stay within the v1 budget.** Soft caps: ≤ 5 minutes wall-clock per evaluation seed, ≤ 500,000 LLM tokens per game if you use an LLM. The runner does not enforce these; the maintainer reviewing your PR will.
5. **No edits outside `agents/community/<you>/`.** Do not modify `world/`, `agents/base.py`, `agents/api_client.py`, `agents/scripted/`, or `arena/`. If your approach requires a world change, open a separate issue first.
6. **Verify locally.** `make check` must pass. If you added a test (recommended), it goes under `agents/tests/community_<you>.py`.
7. **Open a PR.** Include in the description: the score on seed 42 against the committed `baselines/seed_42.json`, and the arena leaderboard rows your agent earned on the three public scenarios (`baseline`, `grid_stress`, `economy_stress`) via `make baselines`-style runs. A maintainer will re-run on merge and regenerate `LEADERBOARD.md`.

The legacy `submit/` directory at the repo root is preserved as a personal-workspace scratchpad — it is **not** where final submissions live. Final, judged submissions go to `agents/community/`.

## Submitting a scenario

Scenarios live as one Python file per scenario under `scenarios/`. See [SCENARIOS.md](SCENARIOS.md) for the authoring protocol and the v1 shipped scenarios as worked examples. Add a regression test under `scenarios/tests/` that drives the world a few days and asserts your overrides fire on the documented days. Open a PR.

## Bugfixes and world changes

For world or arena code changes, open an issue first describing the bug or the design change. The issue should answer:

- What's the current behavior?
- What's the expected behavior?
- What's the smallest change that gets there?

Then open a PR referencing the issue. Keep the diff focused; bundle unrelated cleanups into separate PRs.

## Style notes

- Python 3.11+, type hints on every public function. mypy strict for the source tree (see `pyproject.toml`).
- ruff format (line length 100). No bikeshedding on formatting in code review.
- Tests assert **external behavior** (what callers see), not internal implementation details. Dispatch tests pin per-source kWh given a fixture world, not the loop's iteration order. Determinism tests assert byte-identical replay without inspecting RNG internals. Match this convention.
- Comments explain *why*. Don't restate the code in prose.

## License

By contributing you agree your contribution is licensed under the [MIT License](LICENSE).

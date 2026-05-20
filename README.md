# Agent Energy Game

A small, readable Python simulation of a city's energy economy, wrapped by a FastAPI server. Site renewables, fossil plants, batteries, oil wells, and refineries — keep the grid balanced hour-by-hour and grow population over a multi-year game.

This is **v1** of the environment. The mechanics are deliberately compact (~3000 lines under `world/`) so they fit in one head and extend in one PR. New world components, scenarios, and agents are the point — see [Contributing](#contributing).

You run a city day-by-day. Every day a set of decisions are made by the agent and events occur such as weather, price changes, or population growth. Every simulated hour, supply must match demand or citizens go dark. Every simulated day, the books must close in the black or the treasury dies. An agent's job: build a profitable, populous, reasonably renewable city without bankruptcy and without letting treasury, population, or happiness collapse late. A scenario may be applied to the world to add stress to the game.

`GET /score` returns a single number in `[0, 100]` derived from per-day `states.jsonl` on disk. The formula decomposes treasury, population, and happiness into level / trend / trough triples, then adds a renewable-share term and a solvency term — a peak-and-collapse run cannot outscore a steady, prosperous one. Empty / fresh-reset runs return `{"n_days": 0, "score": 0.0, "components": {}}` so polling clients use one code path.

The world is the single source of truth — a browser UI and AI agents both talk to the same HTTP API. Full mechanics: [RULES.md](RULES.md). Scoring formula and tunable anchors: [`world/scoring.py`](world/scoring.py).

## The EAGE 2026 Hackathon challenge
Each team of 3 has to submit the following:
1. An agent that can play the game autonomosly for 2 simulatedyears. The agent will be evaluated against a set of scenarios. The more days it survives and prospers within the given simulation time budget the better. The agent may be LLM, rule, XGBoost, RL-based, etc -- be creative. 
2. A contribution to the world. This can be a new world component, mechanics, or something that makes the world more interesting (leverage your domain knowledge).
3. A detailed analysis of the agent's behavior and the world balance.

## Quickstart

Launch the world in Docker: `docker compose up`, then open `localhost:8000` in a browser and play the game yourself.

or install in virtual environment and run the server:

```bash
make install                                              # one-time: pip install -e ".[dev]"
make serve                                                # uvicorn on :8000 — open the UI in a browser
python evaluate.py --agent agents.scripted --seed 42      # play the scripted reference agent
make check                                                # lint + format-check + typecheck + test
```

Manual 

## Talking to the world

Every state and every mutation is one HTTP call. A bare-hands agent loop is four lines:

```python
import requests

api = "http://localhost:8000"
requests.post(f"{api}/reset", json={"seed": 42})

for _ in range(365):
    state = requests.get(f"{api}/state").json()
    # ... decide what to build/drill/set, then post actions:
    requests.post(f"{api}/build", json={"x": 14, "y": 16, "type": "solar_farm"})
    requests.post(f"{api}/step", json={"days": 1})

print(requests.get(f"{api}/score").json())
```

Full endpoint list, request/response shapes, and error codes: [API.md](API.md). The `Agent` protocol in [`agents/base.py`](agents/base.py) wraps the same surface in a typed Python class; `agents/scripted/` is the canonical worked example.

## Features

**Scenarios.** Thin overlays that steer weather, prices, or the event mix to stress one part of an agent's policy. Three ship under [`scenarios/`](scenarios/): `baseline` (identity run), `grid_stress` (low-wind + heatwave cluster), `economy_stress` (fuel shock + crude collapse + regulatory tightening). Attach one with `python evaluate.py --agent agents.scripted --scenario scenarios.grid_stress --seed 42`. The browser UI's **Events → Choose scenario** picker attaches one live; the plan + module source render inline. Author guide: [scenarios/SCENARIOS.md](scenarios/SCENARIOS.md).

**LLM agents.** `agents/llm_react/` (ReAct) and `agents/langgraph_agent/` (LangGraph variant) build their client from env vars — `LLM_PROVIDER` ∈ {`openai`, `anthropic`, `ollama`, `nvidia`}, `LLM_MODEL`, `LLM_API_KEY`, `LLM_BASE_URL`. `evaluate.py` calls `load_dotenv()` on a sibling `.env`. Local Ollama needs no key; NVIDIA NIM uses `langchain_nvidia_ai_endpoints.ChatNVIDIA` (requires the `[llm]` extra). The model must support tool calling.

**Determinism + recorded runs.** A game is fully deterministic given the seed: replaying the same `(seed, scenario)` yields byte-identical state — `world/tests/test_determinism.py` pins this. Every API call lands in `runs/{run_id}/actions.jsonl`; every end-of-day state in `runs/{run_id}/states.jsonl`. Score an existing run folder offline with `python evaluate.py --score runs/{run_id}`.

**Browser UI.** `make serve` opens an interactive city builder at `localhost:8000` — build, step, attach scenarios, watch the score evolve.

## Contributing

The environment is v1. The mechanics are small on purpose so that **new world components** are first-class contributions, not friction.

Worth a PR:

- **New world components** — additional plant types, storage tech, demand profiles, weather dynamics, market layers. Drop a module under `world/`, wire it into `world/state.py` and the dispatch loop, add a regression test under `world/tests/`. The dispatch/pricing/population modules are intentionally separable.
- **New scenarios** — a single module under `scenarios/` with an `apply(world, day)` hook on the override taxonomy. Authoring rules, tests, the determinism contract: [scenarios/SCENARIOS.md](scenarios/SCENARIOS.md).
- **New agents** — drop a module exposing an `Agent` class satisfying the protocol in [`agents/base.py`](agents/base.py). `agents/scripted/` is the rule-based reference; `agents/llm_react/` shows the LLM-driven flavour.
- **Mechanics tuning** — scoring anchors, economic constants, RNG draws. Read the [ADRs](docs/adr/) first to understand why a value is where it is.

`make check` is the canonical pre-commit gate (lint + format-check + typecheck + test). Anything that passes it is a candidate for review. Domain glossary: [CONTEXT.md](CONTEXT.md). Architecture decisions: [docs/adr/](docs/adr/).

## Repository layout

```
world/              # the simulation, API, and UI (single source of truth)
agents/             # reference agents and submissions
  base.py             Agent protocol + BaseAgent helper
  llm.py              shared LLM client factory (OpenAI / Anthropic / Ollama / NVIDIA)
  api_client.py       thin HTTP wrapper over the world API
  attach_runtime.py   shared runtime glue for LLM agents
  prompts.py          system + per-turn prompt templates
  state_summary.py    state-dict → LLM-friendly text reducer
  scripted/           rule-based reference (regression-pinned by agents/tests/scripted_seed_42.json)
  llm_react/          ReAct agent
  langgraph_agent/    LangGraph variant (same provider set)
scenarios/          # one Python module per shipped stress scenario
  SCENARIOS.md        author + runner + scoring guide (lives next to the modules)
  baseline.py         null scenario on seed 42
  grid_stress.py      sustained low-wind + heatwave cluster
  economy_stress.py   fuel shock + crude collapse + regulatory tightening
  tests/              one regression test per shipped scenario
docs/               # ADRs (docs/adr/) + agent-skill docs (docs/agents/)
runs/               # gitignored; one folder per recorded game session
evaluate.py         # CLI: play one game, or score a recorded run folder
Dockerfile          # base image used by docker-compose
docker-compose.yml  # `up` for manual play
pyproject.toml      # package metadata, ruff/mypy config, dependency extras
Makefile            # make check, make serve, make install
```

Approximate sizes: `world/` ~3000 lines, `agents/` ~1500 lines, tests ~3000 lines. Every file is meant to be readable in one sitting.

## License

MIT. See [LICENSE](LICENSE).

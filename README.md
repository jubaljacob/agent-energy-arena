# Agent Energy Game

A small, readable Python simulation of a city's energy economy, wrapped by a FastAPI server, used as an arena for autonomous agents. Site renewables, fossil plants, batteries, oil wells, and refineries. Keep the grid balanced hour-by-hour and grow population over a multi-year game.

## Objective

You run a city. Every simulated hour, supply must match demand or citizens go dark; every simulated day, the books must close in the black or the treasury dies. Over a 10-year horizon (3650 days for evaluation, 365 for manual play), an agent's job is to build a profitable, populous, and reasonably renewable city — without going bankrupt or letting any of those three collapse late in the run.

`GET /score` summarises a finished (or in-progress) run as a single number in `[0, 100]` that rewards sustained level, healthy trend, and survivable troughs across treasury, population, and happiness — with bonuses for renewable share and solvency. A peak-and-collapse run cannot outscore a steady, prosperous one. The mechanics live in [RULES.md](RULES.md); the scoring formula and tunable anchors live in [`world/scoring.py`](world/scoring.py).

The world is the single source of truth. Two clients consume the same API: a browser UI for manual play, and AI agents that play autonomously.

## What you came here to do

| You want to… | Read |
|---|---|
| Understand the game so you can build an agent | [RULES.md](RULES.md) |
| Look up an endpoint or response shape | [API.md](API.md) |
| Write a stress scenario | [scenarios/SCENARIOS.md](scenarios/SCENARIOS.md) |
| Submit an agent | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Read the domain glossary | [CONTEXT.md](CONTEXT.md) |

## 60-second tour

The simulator runs in ticks (1 hour) and days (24 ticks). Agents call `POST /step` once per game day after submitting actions for that day. A game is `GAME_DAYS` days (default 3650 = 10 years for evaluation, 365 for manual play).

Each day the world:

1. Expires finite-duration events.
2. Runs the attached scenario's `apply(world, day)` hook (default: no-op).
3. Samples stochastic events (heatwave, fuel shock, plant failure, demand surprise, regulatory tightening).
4. Steps 24 hours: weather, dispatch, grid balance, population, finance.
5. Emits a daily summary; records the end-of-day state.

`GET /score` returns an absolute score in `[0, 100]` derived from the active run's per-day `states.jsonl` on disk. The formula decomposes treasury, population, and happiness into level / trend / trough triples, then adds a renewable-share term and a solvency term — a peak-and-collapse run cannot outscore a steady prosperous one. Empty / fresh-reset / no-recorder runs return `{"n_days": 0, "score": 0.0, "components": {}}` (HTTP 200) so polling clients can use a single code path. See [`world/scoring.py`](world/scoring.py) for the formula and the tunable scale anchors; [RULES.md §Scoring](RULES.md#scoring) spells out the equations.

```bash
curl http://localhost:8000/score
# {
#   "n_days": 365,
#   "score": 42.3,
#   "components": {
#     "level_treasury": 0.58, "trend_treasury": 0.71, "trough_treasury": 0.44, "axis_treasury": 0.58,
#     "level_pop":      0.41, "trend_pop":      0.63, "trough_pop":     0.35, "axis_pop":      0.47,
#     "level_happy":    0.62, "trend_happy":    0.51, "trough_happy":   0.55, "axis_happy":    0.57,
#     "R":              0.27, "solvency":       0.95
#   }
# }
```

## Quickstart

```bash
make install                              # one-time: pip install -e ".[dev]"
make serve                                # uvicorn on :8000 — open the UI in a browser
make score                                # run the scripted reference agent on seed 42
make check                                # lint + format-check + typecheck + test
```

Docker is also supported:

```bash
docker compose up                                   # manual play at :8000
docker compose --profile eval run --rm agent       # evaluate submit/agent.py
```

## Talking to the world

Every game state and every mutation is one HTTP call. A bare-hands agent loop is four lines:

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

The full endpoint list, request/response shapes, and error codes are in [API.md](API.md). The `Agent` protocol in [`agents/base.py`](agents/base.py) wraps the same surface in a typed Python class — `agents/scripted/` is the canonical worked example (rule-based, forms the regression baseline).

## Scenarios

A *scenario* is a thin overlay that steers weather, prices, or the event mix to stress one part of an agent's policy. Three ship under [`scenarios/`](scenarios/): `scenarios.baseline` (identity run), `scenarios.grid_stress` (low-wind + heatwave cluster), `scenarios.economy_stress` (fuel shock + crude collapse + regulatory tightening).

Attach one to an agent run with `--scenario`:

```bash
# Score the scripted reference agent against grid_stress on its declared seed.
python evaluate.py --agent agents.scripted --scenario scenarios.grid_stress --seed 42

# Sweep agent × scenario pairs and dump scores to a JSON results file.
python -m arena.runner \
    --agent agents.scripted --scenario scenarios.baseline \
    --scenario scenarios.grid_stress --scenario scenarios.economy_stress \
    --output results.json

# Regenerate the committed scripted-agent baselines under baselines/arena/.
make baselines
```

`GET /score` returns the same `[0, 100]` summary regardless of which scenario is attached — the score is how the agent held up under the scenario's pressure. The browser UI's **Events → Choose scenario** picker attaches one live; the scenario's plan + module source render inline so you can read what it does before pressing Confirm.

How to write your own (dotted path, `apply(world, day)` protocol, override taxonomy, tests, baselines): [scenarios/SCENARIOS.md](scenarios/SCENARIOS.md).

## Running an LLM agent

`agents/llm_react/` and `agents/langgraph_agent/` build their LLM client from environment variables (see [`agents/llm.py`](agents/llm.py)):

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | `openai` (default), `anthropic`, `ollama`, or `nvidia` |
| `LLM_MODEL` | model id — provider-specific default if unset |
| `LLM_API_KEY` | required for openai/anthropic/nvidia; ignored for ollama |
| `NVIDIA_API_KEY` | accepted as a fallback for `nvidia` when `LLM_API_KEY` is unset (the name `.env` typically uses) |
| `LLM_BASE_URL` | optional override for the vendor's default endpoint |

`evaluate.py` calls `load_dotenv()` on a `.env` next to it, so any of these can be set there instead of in your shell.

Local Ollama (no key, no quota):

```bash
ollama pull gemma4                                # any tool-capable model works
LLM_PROVIDER=ollama LLM_MODEL=gemma4 \
  python evaluate.py --agent agents.llm_react --seed 42
```

Ollama's `/api/chat` is hit at `http://localhost:11434` by default — override with `LLM_BASE_URL` if your daemon lives elsewhere. The model must support tool calling (gemma4, llama3.1+, qwen2.5+); models without tool support will return empty tool calls and the agent will idle.

NVIDIA NIM (via NVIDIA's recommended `langchain_nvidia_ai_endpoints.ChatNVIDIA` client; requires the `[llm]` extra):

```bash
pip install -e ".[llm]"                           # installs langchain-nvidia-ai-endpoints
echo 'NVIDIA_API_KEY=nvapi-...' >> .env           # or export LLM_API_KEY
LLM_PROVIDER=nvidia python evaluate.py --agent agents.llm_react --seed 42
```

Default model is `moonshotai/kimi-k2.6`; override with `LLM_MODEL`. Override `LLM_BASE_URL` to point at a private NIM deployment. Streaming and Kimi-style `reasoning_content` are not surfaced — the agent consumes one full response per turn.

## Submit an agent

1. Fork the repo.
2. Drop your agent under `agents/community/<your_handle>.py` as a single Python file with a class that satisfies the `Agent` protocol (see [agents/base.py](agents/base.py)).
3. Open a PR. CI runs `make check`.

Full submission protocol: [CONTRIBUTING.md](CONTRIBUTING.md).

## Repository layout

```
world/              # the simulation, API, and UI (single source of truth)
agents/             # reference agents + community submissions
  base.py             Agent protocol + BaseAgent helper
  llm.py              shared LLM client factory (OpenAI / Anthropic / Ollama / NVIDIA)
  api_client.py       thin HTTP wrapper over the world API
  attach_runtime.py   shared runtime glue for LLM agents
  prompts.py          system + per-turn prompt templates
  state_summary.py    state-dict → LLM-friendly text reducer
  scripted/           rule-based reference (forms baselines/seed_42.json)
  llm_react/          ReAct agent
  langgraph_agent/    LangGraph variant (same provider set)
  community/          one folder per community submission (created on first PR)
scenarios/          # one Python module per shipped stress scenario
  SCENARIOS.md        author + runner + scoring guide (lives next to the modules)
  baseline.py         null scenario on seed 42
  grid_stress.py      sustained low-wind + heatwave cluster
  economy_stress.py   fuel shock + crude collapse + regulatory tightening
  tests/              one regression test per shipped scenario
arena/              # multi-(agent, scenario) runner
  runner.py           subprocess-isolated runner; `python -m arena.runner`
  baselines.py        regenerates baselines/arena/<scenario>-<seed>.json
  results.py          shared result schema for runner + baselines
baselines/          # committed reference scores (seed_42.json + arena/<scenario>-<seed>.json)
docs/               # ADRs (docs/adr/) + agent-skill docs (docs/agents/)
scripts/            # one-off CLIs (bench_llm.py, score_run.py)
submit/             # docker-compose `eval` entry point (agent.py + WRITEUP.md)
tests/              # top-level replay-determinism test
runs/               # gitignored; one folder per recorded game session
evaluate.py         # CLI: play one game, or replay a run by ID
Dockerfile          # base image used by docker-compose for serve + eval
docker-compose.yml  # `up` for manual play, `--profile eval` for agent runs
pyproject.toml      # package metadata, ruff/mypy config, dependency extras
Makefile            # make check, make baselines, make play, make eval, make score
```

Approximate sizes: `world/` ~3000 lines, `agents/` ~1500 lines, tests ~3000 lines. Every file is meant to be readable in one sitting.

## Determinism and replay

A game is fully deterministic given `(seed, action log)`. Every API call lands in `runs/{run_id}/actions.jsonl`; every end-of-day state lands in `runs/{run_id}/states.jsonl`. Replay a recorded run with `python evaluate.py --replay runs/{run_id}`; it asserts byte-identical final state.

## License

MIT. See [LICENSE](LICENSE).

## History

This project began as a 24-hour hackathon scaffold (EAGE Annual 2026 Energy–AI Nexus Hackathon). Current docs target the new audience: external agent authors arriving cold.

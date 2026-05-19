# Agent Energy Arena

A small, readable Python simulation of a city's energy economy, wrapped by a FastAPI server, used as a benchmark for autonomous agents. Site renewables, fossil plants, batteries, oil wells, and refineries. Keep the grid balanced hour-by-hour and grow population over a multi-year game. Submit an agent, compare it against the community on shared stress scenarios.

The world is the single source of truth. Two clients consume the same API: a browser UI for manual play, and AI agents that play autonomously.

## What you came here to do

| You want to… | Read |
|---|---|
| Understand the game so you can build an agent | [RULES.md](RULES.md) |
| Look up an endpoint or response shape | [API.md](API.md) |
| Write a stress scenario | [SCENARIOS.md](SCENARIOS.md) |
| Submit an agent | [CONTRIBUTING.md](CONTRIBUTING.md) |

## 60-second tour

The simulator runs in ticks (1 hour) and days (24 ticks). Agents call `POST /step` once per game day after submitting actions for that day. A game is `GAME_DAYS` days (default 3650 = 10 years for evaluation, 365 for manual play).

Each day the world:

1. Expires finite-duration events.
2. Runs the attached scenario's `apply(world, day)` hook (default: no-op).
3. Samples stochastic events (heatwave, fuel shock, plant failure, demand surprise, regulatory tightening).
4. Steps 24 hours: weather, dispatch, grid balance, population, finance.
5. Emits a daily summary; records the end-of-day state.

`GET /score` returns an absolute score in `[0, 100]` derived from the active run's per-day `states.jsonl` on disk. The `trend_aware` formula decomposes treasury, population, and happiness into level / trend / trough triples, then adds a renewable-share term and a solvency term — a peak-and-collapse run cannot outscore a steady prosperous one. Empty / fresh-reset / no-recorder runs return `{"n_days": 0, "score": 0.0, "components": {}}` (HTTP 200) so polling clients can use a single code path. The formula and tunable scale anchors live in [`world/scoring_formula.py`](world/scoring_formula.py).

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

The legacy scripted-baseline scoring path (`make score`, `arena/`) still lives in [`world/scoring.py`](world/scoring.py) and is used by the multi-agent runner; see [RULES.md §Scoring](RULES.md#scoring) for that workflow.

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
  scripted.py         rule-based reference (forms baselines/seed_42.json)
  llm_react.py        ReAct agent over OpenAI / Anthropic / Ollama / NVIDIA NIM
  langgraph_agent.py  LangGraph variant (same provider set)
  community/          one .py per community submission (created on first PR)
scenarios/          # one Python file per shipped stress scenario
  baseline.py         null scenario on seed 42
  grid_stress.py      sustained low-wind + heatwave cluster
  economy_stress.py   fuel shock + crude collapse + regulatory tightening
arena/              # multi-(agent, scenario) runner
  runner.py           subprocess-isolated runner; `python -m arena.runner`
  baselines.py        regenerates baselines/arena/<scenario>-<seed>.json
baselines/          # committed reference scores
docs/               # internal agent-skill docs + archived design briefs
runs/               # gitignored; one folder per recorded game session
evaluate.py         # CLI: play one game, or replay a run by ID
Makefile            # make check, make baselines, make play, make eval, make score
```

Approximate sizes: `world/` ~3000 lines, `agents/` ~1500 lines, tests ~3000 lines. Every file is meant to be readable in one sitting.

## Determinism and replay

A game is fully deterministic given `(seed, action log)`. Every API call lands in `runs/{run_id}/actions.jsonl`; every end-of-day state lands in `runs/{run_id}/states.jsonl`. Replay a recorded run with `python evaluate.py --replay runs/{run_id}`; it asserts byte-identical final state.

## License

MIT. See [LICENSE](LICENSE).

## History

This project began as a 24-hour hackathon scaffold (EAGE Annual 2026 Energy–AI Nexus Hackathon). The original design briefs live under [docs/archive/](docs/archive/) for historical reference. Current docs target the new audience: external agent authors arriving cold.

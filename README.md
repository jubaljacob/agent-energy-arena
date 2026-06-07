# Agent Energy Game

You run a city day-by-day. Every day a set of decisions are made by the agent and events occur such as weather, price changes, or population growth. Every simulated hour, supply must match demand or citizens go dark. Every simulated day, the books must close in the black or the treasury dies. An agent's job: build a profitable, populous, reasonably renewable city without bankruptcy and without letting treasury, population, or happiness collapse late. A scenario may be applied to the world to add stress to the game.

The world is the single source of truth — a browser UI and AI agents both talk to the same HTTP API. Full mechanics: [RULES.md](RULES.md) and [API.md](API.md). 

## The EAGE 2026 Hackathon challenge
Each team of 3 has to submit the following:
1. An agent that can play the game autonomosly for 2 simulated years. The agent will be evaluated against a set of scenarios. The more days it survives and prospers within the given simulation time budget the better. The agent may be LLM, rule, XGBoost, RL-based, etc -- be creative. 
2. A contribution to the world. This can be a new world component, mechanics, or something that makes the world more interesting (leverage your domain knowledge).
3. A detailed analysis of the agent's behavior and the world balance.

![gameplay_gif](docs/energy_game_gameplay-optimized.gif)

## Quickstart

Install in virtual environment and run the server, then open `localhost:8000` in a browser:

```bash
make install                                              # one-time: pip install -e ".[dev]"
make serve                                                # uvicorn on :8000 — open the UI in a browser
make check                                                # lint + format-check + typecheck + test
```

## Evaluate

`evaluate.py` plays one agent through a full game (or a bounded slice) and prints a one-line JSON result ending in the `[0, 100]` score.

Your task is to evaluate the agent you submitted, report a score and show the highlights of the agent's behavior.
On Monday we will give you a seed and a set of scenarios to evaluate the agent against.

```
1. Run evaluation with a given seed and each challenge scenario from `scenarios/challenge/` folder with time budget of 600 seconds each, record a score.
2. Load runs/eval-<timestamp> in the UI and watch the highlights of the agent's behavior.
```

### Evaluation options

Against a scenario, with a wall-clock budget:

```bash
.venv/bin/python evaluate.py \
  --agent agents.langgraph_agent.agent \
  --seed 777 \
  --scenario scenarios.grid_stress \
  --time-budget 600
```

Other arguments:
```bash
--days N                      # override the game-day horizon so the agent plays N days instead of the configured game_days
--time-budget SECONDS         # cap wall-clock time
--metrics                     # adds an llm_metrics block to the result line
```

Every run writes a folder under `runs/{run_id}` (named `eval-<timestamp>` from `evaluate.py`). `runs/` is gitignored; one folder per session:

| File | Contents |
|---|---|
| `states.jsonl` | one line per simulated day — the end-of-day `state` snapshot + per-day P&L `summary`. **This is what scoring reads.** |
| `actions.jsonl` | every API mutation (build / drill / control / step) in order |
| `metadata.json` | seed, scenario, session, start time, run id |
| `final_state.json` | the harness-side end-of-game state snapshot |


Re-score any run offline without replaying it — scoring reads `states.jsonl` only:

```bash
.venv/bin/python evaluate.py --score runs/eval-20260605-091814-2
```


## Talking to the world

Every state and every mutation is one HTTP call. A bare-hands agent loop is four lines:

```python
import requests

api = "http://localhost:8000"
requests.post(f"{api}/reset", json={"seed": 42})

for _ in range(365):
    state = requests.get(f"{api}/state").json()
    # ... decide what to build/drill/set, then post actions:
    requests.post(f"{api}/build", json={"tile_type": "solar_farm", "x": 14, "y": 16})
    requests.post(f"{api}/step", json={"days": 1})

print(requests.get(f"{api}/score").json())
```

Full endpoint list, request/response shapes, and error codes: [API.md](API.md). The `Agent` protocol in [`agents/base.py`](agents/base.py) wraps the same surface in a typed Python class; `agents/scripted/` is the canonical worked example.

## Features

**Scenarios.** Thin overlays that steer weather, prices, or the event mix to stress one part of an agent's policy. Three ship under [`scenarios/`](scenarios/): `baseline` (identity run), `grid_stress` (low-wind + heatwave cluster), `economy_stress` (fuel shock + crude collapse + regulatory tightening). Attach one with `python evaluate.py --agent agents.scripted --scenario scenarios.grid_stress --seed 42`. The browser UI's **Events → Choose scenario** picker attaches one live; the plan + module source render inline. Author guide: [scenarios/SCENARIOS.md](scenarios/SCENARIOS.md).

**LLM agents.** `agents/llm_react/` (ReAct) and `agents/langgraph_agent/` (LangGraph variant) build their client from env vars. `LLM_PROVIDER` ∈ {`openai`, `anthropic`, `ollama`, `nvidia`, `nim`} is the only switch; each provider reads its own namespaced `*_API_KEY` / `*_BASE_URL` / `*_MODEL` (e.g. `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`), so switching providers means changing `LLM_PROVIDER` alone. `evaluate.py` calls `load_dotenv()` on a sibling `.env`. Local Ollama needs no key; hosted NVIDIA NIM uses `langchain_nvidia_ai_endpoints.ChatNVIDIA` (requires the `[llm]` extra); self-hosted NIM containers (`LLM_PROVIDER=nim`) speak the OpenAI wire format on `/v1/chat/completions` without auth and read `NIM_BASE_URL` for the endpoint. The model must support tool calling.

**Determinism + recorded runs.** A game is fully deterministic given the seed: replaying the same `(seed, scenario)` yields byte-identical state — `world/tests/test_determinism.py` pins this. Every API call lands in `runs/{run_id}/actions.jsonl`; every end-of-day state in `runs/{run_id}/states.jsonl`.

Running, budgeting, and scoring an agent — with worked examples for `--scenario`, `--time-budget`, `--days`, `--metrics`, and `--score` — is documented under [Evaluating agents](#evaluating-agents) (or `.venv/bin/python evaluate.py --help`). The `--api-url` flag drives a live server instead of the in-process `TestClient`.

**Browser UI.** `make serve` opens an interactive city builder at `localhost:8000` — build, step, attach scenarios, watch the score evolve.

## Contributing

The environment is v1. The mechanics are small on purpose so that **new world components** are first-class contributions, not friction.

Worth a PR:

- **New world components** — additional plant types, storage tech, demand profiles, weather dynamics, market layers. Drop a module under `world/`, wire it into `world/state.py` and the dispatch loop, add a regression test under `world/tests/`. The dispatch/pricing/population modules are intentionally separable.
- **New scenarios** — a single module under `scenarios/` with an `apply(world, day)` hook on the override taxonomy. Authoring rules, tests, the determinism contract: [scenarios/SCENARIOS.md](scenarios/SCENARIOS.md).
- **New agents** — drop a module exposing an `Agent` class satisfying the protocol in [`agents/base.py`](agents/base.py). `agents/scripted/` is the rule-based reference; `agents/llm_react/` shows the LLM-driven flavour.
- **Mechanics tuning** — scoring anchors, economic constants, RNG draws. Read the [ADRs](docs/adr/) first to understand why a value is where it is.

`make check` is the canonical pre-commit gate (lint + format-check + typecheck + test). Anything that passes it is a candidate for review. Domain glossary: [CONTEXT.md](CONTEXT.md). Architecture decisions: [docs/adr/](docs/adr/).


## Credits
The `agent-energy-arena` is constructed by Oleg Ovcharenko for the EAGE 2026 Hackathon organized by EAGE AI Committee. Claude Code with skills by Matt Pocock is the primary development method. 

The idea of the agentic energy challenge in a virtual tile-based world is by Roderick Perez.

## License

MIT. See [LICENSE](LICENSE).

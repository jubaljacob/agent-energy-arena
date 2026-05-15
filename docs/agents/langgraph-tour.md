# LangGraph reference agent — one-turn tour

A worked-example agent at [`agents/langgraph_agent/agent.py`](../../agents/langgraph_agent/agent.py)
that wires every world endpoint into a graph. Sister to the minimal
ReAct loop at [`agents/llm_react/agent.py`](../../agents/llm_react/agent.py); both
ship — pick one in `submit/agent.py`.

Install the optional `langgraph` extra:

```
pip install -e ".[llm]"
```

Run the offline demo (uses `MockLLM` when `LLM_API_KEY` is unset):

```
python -m agents.langgraph_agent.agent --seed 42 --days 30
```

## Graph topology

One turn flows through six node types. Each arrow is a LangGraph
edge; conditional edges are noted explicitly.

```
START → observe → summarise → plan → ┌─ build ──────┐
                                     ├─ demolish ───┤
                                     ├─ survey ─────┤ → step → (loop)
                                     ├─ drill ──────┤        ├→ observe
                                     ├─ set_well_rate           └→ END
                                     ├─ set_refinery_rate
                                     └─ step
```

The `plan` node and every dispatch node share the same conditional
router, so chained calls (e.g. `build → build → step`) revisit the
router after each dispatch instead of being hard-wired in sequence.

## Per-node reference

| Node | Purpose | API calls | Source |
|------|---------|-----------|--------|
| `_observe` | Snapshot the world | `GET /state`, `GET /forecast`, `GET /events`, `GET /reservoirs` | `agents/langgraph_agent/agent.py:182` |
| `_summarise` | Compress for the LLM | (none — pure) | `agents/langgraph_agent/agent.py:196` |
| `_plan` | One LLM call; split into action queue + step size | (none — LLM only) | `agents/langgraph_agent/agent.py:213` |
| `_route_next` | Conditional edge: pick the next dispatch branch | (none) | `agents/langgraph_agent/agent.py:249` |
| `_make_dispatch_node("build")` | Place a tile | `POST /build` | `agents/langgraph_agent/agent.py:261` |
| `_make_dispatch_node("demolish")` | Remove a tile | `POST /demolish` | `agents/langgraph_agent/agent.py:261` |
| `_make_dispatch_node("survey")` | Reveal a voxel column | `POST /survey` | `agents/langgraph_agent/agent.py:261` |
| `_make_dispatch_node("drill")` | Spawn a well | `POST /drill` | `agents/langgraph_agent/agent.py:261` |
| `_make_dispatch_node("set_well_rate")` | Throttle a well | `POST /control/well` | `agents/langgraph_agent/agent.py:261` |
| `_make_dispatch_node("set_refinery_rate")` | Throttle a refinery | `POST /control/refinery` | `agents/langgraph_agent/agent.py:261` |
| `_step` | Advance the simulation | `POST /step` | `agents/langgraph_agent/agent.py:285` |
| `_loop` | Conditional edge: `observe` until `day == game_days`, else `END` | (none) | `agents/langgraph_agent/agent.py:297` |

Two endpoints are read outside the per-turn loop in `play_game` itself:

- `GET /catalog` — read once at startup so the planner knows the legal
  tile vocabulary. Source: `agents/langgraph_agent/agent.py:136`.
- `GET /score` — read once after the loop ends (returns `None` if no
  baseline is committed for the seed). Source: `agents/langgraph_agent/agent.py:136`.

`POST /reset` fires at the top of `play_game` (also `:136`).

## What each node hands the next

The graph state is the `GraphState` TypedDict at
`agents/langgraph_agent/agent.py:58`. The contract:

- `observe` writes `obs`, `forecast`, `events`, `reservoirs`, `day`.
- `summarise` reads them and writes `summary` (the LLM prompt body).
- `plan` reads `summary`, calls the LLM, and writes `pending_calls`
  (the FIFO of mutating actions) plus `step_days` (peeled off the
  `step` tool call or defaulted to 7).
- Each dispatch node pops the head of `pending_calls`, fires the
  matching `ApiClient` method, and stores the envelope on
  `last_envelope` so the next observe round can see the result.
- `step` advances the world and refreshes `obs.day`.
- `loop` re-enters `observe` until `day >= game_days`.

## Fallbacks

- If the LLM omits `step`, `_plan` falls back to `step_days = 7` so the
  graph still terminates.
- If a dispatch raises `RuntimeError` (the world rejected the action,
  e.g. `no_road_adjacency`), the node swallows it and returns
  `last_envelope = None` — the next turn sees the unchanged world.
- If `langgraph` isn't installed, construction raises a clear error
  pointing at `pip install -e ".[llm]"`.

## Scope

This agent is a worked example, not a competitive baseline. The PRD's
>15%-above-scripted target belongs to `agents/llm_react/agent.py`. Use this
file when you want to know how a graph-based agent should be wired
end-to-end against the world.

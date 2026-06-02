# LangGraph reference agent — one-turn tour

A worked-example agent at [`agents/langgraph_agent/agent.py`](../../agents/langgraph_agent/agent.py)
that drives the `World` through an `ApiClient` using a 5-node graph
with a rule-based local critic. Sister to the minimal ReAct loop at
[`agents/llm_react/agent.py`](../../agents/llm_react/agent.py); both ship —
pick one in `submit/agent.py`.

Install the optional `langgraph` extra:

```
pip install -e ".[llm]"
```

Run a short demo (requires `LLM_API_KEY` — there is no offline fallback):

```
python -m agents.langgraph_agent.agent --seed 42 --days 30
```

## Graph topology

One turn flows through five nodes plus two conditional edges. The
back-edge from `critique → plan` is the interesting one: it fires
only when the local critic vetoes **every** proposed mutation, and
it is capped at one retry per turn.

```
START → observe → plan(LLM) → critique(rules) → execute → step → {observe | END}
                     ↑                                  │
                     └──── re-plan once if all dropped ─┘
```

## Per-node reference

| Node | Purpose | Side effects | Source |
|------|---------|--------------|--------|
| `_observe` | Snapshot the `World` for this turn. Resets per-turn rejection state. | `GET /state`, `GET /forecast` | `agents/langgraph_agent/agent.py:249` |
| `_plan` | One LLM call. On a re-plan pass, prepends the previous turn's rejection reasons to the user message. | LLM call only | `agents/langgraph_agent/agent.py:261` |
| `_critique` | Walks each proposed mutator through the `RULES` list, dropping any a rule vetoes. | pure (reads `state_view`) | `agents/langgraph_agent/agent.py:304` |
| `_execute` | Dispatches each survivor through the shared `agents.tool_dispatch.dispatch_tool_call`. Unknown tool names return `None` and are silently skipped; `World`-side `RuntimeError`s are swallowed. | `POST /build`, `/demolish`, `/survey`, `/drill`, `/control/well`, `/control/refinery` | `agents/langgraph_agent/agent.py:342` |
| `_step` | Advance the `World` by `step_days` and refresh `day`. | `POST /step` | `agents/langgraph_agent/agent.py:356` |

Two endpoints are read outside the per-turn loop in `play_game`:

- `POST /reset` fires once at the top of `play_game` to seed the world.
- `GET /score` is read once after the loop ends (returns `None` if no
  baseline is committed for the seed) for the CLI summary.

## The conditional back-edge

`_route_after_critique` (`agents/langgraph_agent/agent.py:329`)
decides between `plan` and `execute`. The branch to `plan` is taken
when:

1. The planner produced at least one mutator call this turn, **and**
2. The critic rejected all of them (no survivors), **and**
3. `replan_retries < MAX_REPLAN_RETRIES` (currently 1).

Otherwise the graph advances to `execute`. The single-retry cap lives
in `MAX_REPLAN_RETRIES` at the top of the module. `_observe` resets
`replan_retries` to 0 every turn; `_plan` increments it whenever it
sees rejections in state.

### How rejection reasons reach the planner

The critic writes the rejection reasons it produced into
`GraphState["rejections"]`. On the re-plan pass `_plan` reads that
list and prepends it to the user message before calling the LLM:

```
Your previous tool calls were ALL rejected by the local critic:
- build(solar_farm,3,7) tile_occupied by house
- drill(8,4) out_of_bounds (world 8x8)

Revise the plan to avoid these failure modes.

<normal summarize_state output…>
```

After the call, `_plan` clears `rejections` so the same reasons don't
leak into the next turn.

## Extension surfaces

Two places in this file are designed to be modified by hackathon
participants:

### 1. The `RULES` list of critic functions

`RULES` at `agents/langgraph_agent/agent.py:125` is a module-level
`list[RuleFn]`. Each rule is a pure function:

```python
def rule(call: ToolCall, state_view: dict[str, Any]) -> str | None: ...
```

Return a one-line rejection reason to veto the call, or `None` to let
it through. The reason string is what the next `_plan` pass will see.
The two shipped rules are `out_of_bounds` and `tile_occupied` — add
your own by appending to the list. Rules run in order; the first
non-`None` return wins.

The critic is a *fast local pre-flight check, not a second source of
truth*. The `World` re-validates and rejects every mutation
server-side (`_execute` swallows those rejections), so the shipped
rules deliberately stay cheap: they read the `/state` payload only and
never re-implement `World` economics or topology. When you add a rule,
keep it in that spirit — a rule that mirrors `World` pricing or the
road graph will silently drift the moment the `World` changes.

### 2. The rejection-reason prompt construction in `_plan`

The block that builds the re-plan user message lives inside `_plan`
at `agents/langgraph_agent/agent.py:268`. Tune the framing the model
sees when it has to re-plan — the bullet style, the lead-in sentence,
the position of `summarize_state(...)` — all live in one place.

## What each node hands the next

The graph state is the `GraphState` TypedDict at
`agents/langgraph_agent/agent.py:65`. The contract:

- `_observe` writes `obs`, `forecast`, `day`, and resets `rejections`
  and `replan_retries`.
- `_plan` reads `obs`, `forecast`, and (on the retry pass)
  `rejections`. Writes `pending_calls` (the FIFO of proposed mutator
  calls), `step_days` (peeled off the `step` tool call or defaulted
  to 7 and clamped to `[1, remaining_days]`), `cumulative_tokens`,
  and bumps `turn`. Clears `rejections`.
- `_critique` reads `pending_calls` and the `state_view` in `obs`.
  Writes `survivors` (the calls that passed all `RULES`) and
  `rejections`.
- `_execute` reads `survivors` and dispatches each via
  `dispatch_tool_call`. Clears `survivors`.
- `_step` advances the world by `step_days` and refreshes `obs` and
  `day`.
- `_route_after_step` re-enters `observe` until `day >= game_days`,
  then routes to `END`.

## Token accounting

`_plan` accumulates `response.usage.total` into `cumulative_tokens`.
`total` already covers Anthropic prompt-cache `cache_creation` and
`cache_read` tokens, so the per-turn counter doesn't undercount on a
cached run.

## Failure modes the graph tolerates

- If the LLM omits a `step` call, `_plan` falls back to
  `DEFAULT_STEP_DAYS_FALLBACK = 7` so the graph still terminates.
- If `_execute` dispatches a survivor that the `World` rejects
  (`RuntimeError` from the 4xx envelope) or that has malformed
  arguments, the exception is swallowed and the next survivor runs.
- If the LLM hallucinates a tool name outside `MUTATOR_TOOLS`, the
  call passes through `_critique` (it's not a mutator, so no rule
  applies) and then `dispatch_tool_call` returns `None` in
  `_execute` — no special case in the node.
- If `langgraph` isn't installed, `_build_graph` raises a clear error
  pointing at `pip install -e ".[llm]"`.

## Scope

This agent is a worked example, not a competitive baseline. The PRD's
>15%-above-scripted target belongs to `agents/llm_react/agent.py`.
Read this file when you want to see how a graph-based agent should
be wired against the `World` end-to-end — and as the starting point
for extending the critic.

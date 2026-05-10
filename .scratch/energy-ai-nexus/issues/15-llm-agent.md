---
Status: needs-triage
---

# 15 — LLM ReAct reference agent

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## What to build

`agents/llm_react.py` implements an OpenAI-compatible chat-completions ReAct agent per the brief's §7.3. Each decision turn:

1. Fetch state, forecast, recent history.
2. Compress with `agents/state_summary.py.summarize_state(obs, forecast, history)`.
3. Send system prompt + summary to LLM with `tools = ACTION_TOOLS`.
4. Dispatch each returned tool call to the corresponding API endpoint.
5. The final tool call must be `step` with a `days` parameter; this advances the world.

`ACTION_TOOLS` is exactly the 7 entries from the PRD: `build`, `demolish`, `survey`, `drill`, `set_well_rate`, `set_refinery_rate`, `step`. **No `skip` tool**; `step` with no other actions is the equivalent. **No `set_plant_rate`** (`/control/plant` was dropped in v1).

`agents/state_summary.py` is the canonical extension point for participants. It compresses the world to ~1000 tokens with:

- Static config cached in system prompt (not re-sent per turn).
- Tile inventory as counts, not per-tile coordinates.
- Wells as a compact table.
- Reservoirs: top-K=30 voxels by current best `oil_estimate × perm_estimate`.
- Power 24-hour profile as compact array.
- Forecast next-24h as compact line per hour.
- Last 4 weekly summaries as deltas.
- Active events with end-day countdown.

`agents/prompts.py` holds the system prompt: brief mechanic primer, scoring objective summary, output format instructions. All extension points are documented.

Token-budget warning: when cumulative tokens exceed 80% of 1M, the agent logs a warning to stderr (does not crash). Per-game token usage is tracked client-side.

This slice is **HITL** — the end-to-end test requires a live LLM API endpoint and credentials (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` env vars). The model choice and prompt-tuning are decisions that benefit from human iteration.

## Acceptance criteria

- [ ] `agents/llm_react.py` implements the `Agent` protocol from `agents/base.py`.
- [ ] `ACTION_TOOLS` schema includes exactly the 7 tools listed in the PRD.
- [ ] Each LLM-emitted tool call dispatches to the correct API endpoint with parameter validation.
- [ ] If the LLM omits `step`, the agent harness emits `step(days=7)` automatically (so the world doesn't hang).
- [ ] `agents/state_summary.py.summarize_state(...)` produces a string that fits within the documented token target (~1000 tokens) for the dev seed at any game day.
- [ ] System prompt explains the six-tool action vocabulary and the `step` final-call requirement.
- [ ] Cumulative token usage is tracked per game; a warning logs to stderr when usage exceeds 800K (80% of 1M).
- [ ] The agent runs to completion within 5 minutes wall-clock on seed 42 with `gpt-4o-mini` or equivalent (HITL verification).
- [ ] Token usage stays under 1M for the dev seed (HITL verification).
- [ ] LLM agent's final score on seed 42 exceeds the scripted baseline by at least 15% (HITL verification — this is the brief's §1.4 success criterion).
- [ ] Documentation in `agents/llm_react.py` lists the four named extension points: `summarize_state`, `system_prompt`, `decide`, `ACTION_TOOLS`.

## Blocked by

- 14 — Scripted agent + baseline regression test

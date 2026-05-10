---
Status: needs-triage
---

# PRD — Energy–AI Nexus Hackathon: World, Reference Agents, and UI (v1)

This PRD operationalizes the design brief at `docs/hackathon-brief.md` plus the design decisions resolved in the grilling session that preceded this document. Where the brief and the grilling decisions disagree, **the grilling decisions win** and are called out below.

## Problem Statement

The EAGE Annual 2026 conference is hosting a 24-hour, in-person hackathon themed on the energy–AI nexus. Participants need a single shared, deterministic simulation of a city's energy economy that they can both *play themselves* (to feel the mechanics) and *write AI agents against* (to compete on a leaderboard). Without a shared artifact:

- Each team would build their own toy world, making submissions impossible to compare.
- Judges would have nothing reproducible to evaluate.
- The 24-hour budget would be consumed re-implementing physics rather than designing agents.

The simulation must be:

1. Rich enough to challenge AI agents over a 10-year game horizon — combining intermittent renewables, ramp-limited fossil dispatch, partially observable subsurface geology, depleting reservoirs, and population dynamics tied to grid reliability.
2. Simple enough that a developer can read the entire world codebase in 30–45 minutes.
3. Reproducible enough that two runs of the same seed produce byte-identical state.
4. Approachable enough that a non-coder human can play a one-year tutorial session and learn the mechanics from the UI alone.

## Solution

A Python 3.11+ FastAPI service that owns the simulation as the single source of truth, plus two clients: a browser UI for manual human play, and reference AI agents (one scripted, one LLM-driven) that consume the same HTTP API. The whole package deploys with `docker compose up`.

The world advances in hourly internal ticks (so dispatch dynamics are real) but the agent and human player only ever observe and act at daily-or-coarser granularity — by default, weekly. Each `/step` advances 1 to 7 days at the caller's choice, allowing crisis-aware step-down during active events. Agents play 10-year games (3650 days); human manual sessions play 1-year tutorial sessions (365 days) against the same world.

Scoring is anchored to a competent scripted-agent baseline: each submission's final population and treasury delta are normalized against the scripted agent's outcomes on the same seed, with a renewable-share tie-breaker. The population term is capped to prevent runaway scores. A held-out evaluation seed disclosed only to organizers determines the final leaderboard.

## User Stories

### Human player (manual play)

1. As a human player, I want to start the game with one shell command, so that I can begin playing without setup friction.
2. As a human player, I want to play a one-year tutorial session by default, so that I can learn all five mechanic categories (build, dispatch, survey, drill, refine) inside a sitting.
3. As a human player, I want to see the surface tile grid as the central view, so that I can place buildings spatially.
4. As a human player, I want a build-menu rail showing every tile type with cost and a one-line description, so that I can decide what to build without reading docs.
5. As a human player, I want immediate visual feedback on whether a tile placement is valid (cash, adjacency), so that I don't waste clicks.
6. As a human player, I want a play/pause button that auto-advances days at adjustable speed, so that I can let the world tick forward while I watch.
7. As a human player, I want the spacebar to toggle play/pause, so that I can fluidly stop-and-go without reaching for the mouse.
8. As a human player, I want to single-step one day at a time while paused (period key), so that I can carefully observe one day's outcome before continuing.
9. As a human player, I want to adjust auto-step speed (0.5x / 1x / 2x / 4x days per second), so that early-game exploration is slow and late-game grinding is fast.
10. As a human player, I want a top bar showing day, treasury, population, happiness, and grid balance state at all times, so that critical state is always visible.
11. As a human player, I want a power tab with a 24-hour supply-vs-demand line chart, so that I can see whether my grid actually balances hour-by-hour.
12. As a human player, I want a finance tab with a treasury line chart and daily P&L breakdown, so that I can see where money flows.
13. As a human player, I want a wells tab with each well's setpoint slider, cumulative production, and estimated remaining oil, so that I can manage production without leaving the screen.
14. As a human player, I want a subsurface cross-section view that lets me pick an axis and slice index, so that I can visualize where reservoirs are after surveying.
15. As a human player, I want unrevealed voxels to render as outlines and revealed voxels as colored cells, so that I can tell exploration apart from confirmed knowledge.
16. As a human player, I want an events tab listing active and recent events, so that I can plan around heatwaves and plant failures.
17. As a human player, I want a history tab with past daily summaries, so that I can debug why my treasury is bleeding.
18. As a human player, I want a bottom-bar action ticker showing the actions queued during this paused turn, so that I can review before resuming.
19. As a human player, I want pending actions to apply on the next day-advance (whether I press play or single-step), so that mid-turn rebalancing feels intentional.

### AI agent author (writes `submit/agent.py`)

20. As an agent author, I want a stable HTTP API I can call from any language, so that I'm not locked into a specific agent framework.
21. As an agent author, I want the world to be deterministic given (seed, action log), so that I can replay and debug failed runs.
22. As an agent author, I want a `GET /state` endpoint that returns the full observable world, so that I can build my own custom state summarizers.
23. As an agent author, I want a compact `GET /state/summary` endpoint, so that I can poll cheaply during steady-state stretches.
24. As an agent author, I want a `GET /forecast` endpoint that returns 24-hour weather and demand predictions with growing noise, so that I can plan dispatch ahead.
25. As an agent author, I want forecast resampling to reduce noise via averaging, so that I can spend compute for prediction quality.
26. As an agent author, I want a six-tool action vocabulary (build, demolish, survey, drill, set_well_rate, set_refinery_rate, step), so that the action space is tractable for an LLM.
27. As an agent author, I want `step` to be an explicit tool with a `days` parameter (1–7), so that I can step finely during crises and weekly during steady state.
28. As an agent author, I want each tool call to commit independently and return its own ok/error, so that one bad action doesn't roll back a whole turn.
29. As an agent author, I want a full action log including rejections, so that replay reproduces my exact sequence.
30. As an agent author, I want survey results to return the full voxel array, so that I can run my own offline reservoir analysis if I want.
31. As an agent author, I want `/state` to return only the top-K most promising revealed voxels with aggregate stats, so that my prompt context doesn't explode after months of surveying.
32. As an agent author, I want a paginated `/reservoirs` endpoint for full subsurface detail when I need it, so that compression is opt-in.
33. As an agent author, I want a reference scripted agent that plays a competent 10-year game, so that I have a strong baseline to study and beat.
34. As an agent author, I want a reference LLM ReAct agent with a clean state-summarization extension point, so that I can iterate on the LLM strategy without touching world code.
35. As an agent author, I want the LLM agent to use ≤1M tokens per game, so that I have headroom inside the organizer-enforced budget.
36. As an agent author, I want extension points clearly named — state summary, system prompt, decision logic, action tools — so that I know where to plug in changes.
37. As an agent author, I want my submitted agent to run inside Docker via a single profile, so that organizers can evaluate it without per-team setup.
38. As an agent author, I want hard constraints (5 minutes wall-clock, 1M LLM tokens, no outbound network besides the configured LLM endpoint) enforced by the harness, so that fairness is mechanical not negotiated.

### Organizer / judge

39. As an organizer, I want a held-out evaluation seed that produces a different reservoir field, weather profile, and event roll than the dev seed, so that submissions can't memorize.
40. As an organizer, I want the scripted baseline computed automatically per seed, so that I'm not hand-tuning numbers.
41. As an organizer, I want each submission's score broken down into population, treasury, and renewable-share components, so that I can explain the leaderboard to participants.
42. As an organizer, I want the scoring formula to bound each component's contribution, so that one runaway dimension doesn't dominate.
43. As an organizer, I want bankruptcy to be heavily penalized but not game-terminating, so that strategically-failed agents still produce comparable end-state metrics.
44. As an organizer, I want a `make score` command that runs all dev seeds and prints a one-line summary, so that I can sanity-check submissions before the eval seed.

### Implementer

45. As an implementer, I want every formula in §4 of the brief to map 1:1 to a named function in the corresponding module, so that the spec and code are interlocked.
46. As an implementer, I want the world codebase to stay around 2000 lines, so that the "30-minute reading tour" promise holds.
47. As an implementer, I want all tunable constants centralized in one module, so that playtest-driven retuning is a one-line change per parameter.
48. As an implementer, I want a single seeded RNG threaded through the simulator and a *separate* RNG for forecast noise, so that calling `/forecast` doesn't perturb simulation determinism.
49. As an implementer, I want a step-size invariance test that proves `step(days=7)` ≡ `step(days=1) × 7`, so that variable step size doesn't break reproducibility.

## Implementation Decisions

This section captures the design choices resolved during the grilling session that override or supplement the design brief. The brief's §4 equations and §5 API contracts remain authoritative *except* where listed below.

### Time and cadence

- **`GAME_DAYS = 3650`** (10 years) for agent sessions. **`MANUAL_GAME_DAYS = 365`** (1 year) for human manual sessions. Same world generation otherwise — manual sessions just stop earlier.
- **`TICKS_PER_DAY = 24`**, internal-only. Agents and humans never observe individual hours.
- **`DAYS_PER_DECISION = 7`** is the default cadence. The `/step` endpoint accepts a `days` parameter in `[1, 7]`.
- `/step` always advances the full requested number of days. Events that fire mid-step are reported in the returned summary, not as early termination. Agents step `days=1` when they want crisis-response cadence.

### Scoring

- The population term is capped: `p_term = 0.5 · min(P / P_ref, 3.0)`. The treasury and renewable terms are unchanged from the brief's §8.1.
- The renewable-share `R` is a **lifetime** average computed as `Σ renewable_served_kwh / Σ total_served_kwh`, weighted by served kWh and excluding curtailed kWh from both numerator and denominator. All loads (residential, commercial, industrial, refinery, injection) count.
- Bankruptcy never terminates the game. Treasury can go negative via passive bleeding (OPEX, fuel, carbon, blackout penalty). The `tanh` in the treasury term naturally drives `t_term → 0` for very negative T. Population scoring continues to reward survivors, but the cap prevents pop-only winning.
- The `/build` action rejects with `"insufficient_funds"` when `treasury < CAPEX`. Passive bleeding has no such guard.

### Power economics

- **Retail-billing model.** All civilian, commercial, and industrial loads pay `GRID_PRICE_RETAIL = $0.08/kWh` to the agent for served kWh. Curtailed kWh sell to the external grid at `GRID_PRICE_EXPORT = $0.04/kWh`. Plants now have explicit ROI; portfolio decisions become meaningful.
- Process loads (refineries and injection wells) are unbilled — the agent's own equipment doesn't pay the agent. Refinery margin is the refined-vs-crude price spread; injection's value is reservoir pressure boost.

### Demand formula (revises brief §4.3)

The brief's bottom-line multipliers are ambiguous and physically inconsistent. The corrected scopes are:

- Heatwave multiplier (1.4 when active) applies to **residential demand only**. People run A/C; factories don't.
- Demand-surprise multiplier (1.3 when active) applies to **commercial + industrial demand only**. Economy-driven surge.
- Process loads (refinery and injection) are unaffected by either event.

### Demand-response on injection wells (replaces battery storage)

The brief lists "no batteries" as a core design pillar. To preserve renewable value without introducing a battery tile, injection wells behave as automatic demand-response:

- During brownout or blackout: injection well power drops to 0 (sheds load to free up power for civilian demand).
- During curtailment: injection well power ramps to 2× baseline, capped at the well's hardware maximum (absorbs surplus renewables and converts it to reservoir pressure).
- During balanced state: well runs at the agent-set baseline.
- The hour's balance state is computed from the *previous* hour's dispatch, breaking the otherwise-circular dependency between injection power (a load) and dispatch.
- `cumulative_injected_bbl` reflects DR-adjusted actual injection, not setpoint times days.

### Industrial CO₂ (revises brief §4.7)

The brief's per-MWh-consumed industrial CO₂ term double-counts emissions already charged at the generation side. Replace with `INDUSTRIAL_PROCESS_CO2_T_PER_DAY = 2 t/day` flat per industrial tile, modeling process emissions independent of electricity input. Refinery CO₂ per refined barrel is unchanged — it represents real process emissions distinct from the refinery's electrical load.

### Events (revises brief §4.11)

- **Regulatory tightening is capped at 3 occurrences per game.** Without the cap, the permanent-and-stacking nature of the event creates extreme cross-seed variance in carbon price. With the cap, end-of-game carbon price is bounded at `25 × 1.5³ = $84.4/ton`.
- All other event probabilities and durations are unchanged. With a 10-year horizon, expected occurrences scale linearly: ~11 heatwaves, ~7 fuel price shocks, ~11 demand surprises per game.

### Subsurface

- **Survey cost scales quadratically with size**: `cost = 15_000 × (n / 8)²`. Size is bounded at `n ∈ [4, 16]`. The brief's flat $15k cost would let an agent reveal the entire field for $15k, defeating the exploration mechanic.
- Resurvey is allowed and costs the full amount. Each resurvey draws independent noise per voxel. The cost scaling makes resurvey-spam economically self-limiting.
- The chebyshev distance (max of |dx|, |dy|) is the canonical metric for "houses within 3 of a coal plant" in the happiness penalty.

### API and action vocabulary

- The reference LLM agent's action tool list is exactly six tools: `build`, `demolish`, `survey`, `drill`, `set_well_rate`, `set_refinery_rate`, `step`. Plus `step` for time advance, totalling seven.
- The brief's `/control/plant` endpoint and `skip` action tool are dropped from v1. Per-plant dispatch override was the only sub-daily-meaningful control; the auto-dispatcher handles peakers correctly. `step(days=7)` with no other tool calls is equivalent to skip.
- Wells are exclusively created via `drill` — not via `build` with `tile_type="oil_well"`. The build catalog `oil_well` and `injection_well` entries are accessed only through the drill action, which combines placement, target-depth specification, and CAPEX deduction in one call.
- Action ordering is **submission order, best-effort, per-tool result**. Each mutation endpoint commits independently and returns its own `{ok, error?, treasury_after, result}`. Failures don't block subsequent tool calls. The full action log records every attempt including rejections.
- The action log is the ground-truth replay artifact. Combined with seed, it deterministically reproduces world state.

### Subsurface data presentation

Three layers of compression to keep LLM context tractable:

1. **`/survey` returns full voxel data** (the full `n × n × WORLD_D` array). The agent's own code can filter before passing anything to the LLM. This preserves optionality for agents that want offline Bayesian analysis of survey noise.
2. **`/state.reservoirs_revealed` returns top-K=10 voxels** by current best estimate of `oil × perm`, plus aggregate stats (number of revealed voxels, total estimated remaining oil, number of explored columns). Bounded size regardless of game progress.
3. **`/reservoirs?min_oil=N&top_k=M`** is a separate paginated endpoint for agents that want detail on demand.
4. **`agents/state_summary.py`** produces top-K=30 voxels for inclusion in the LLM prompt, plus the rest of the compressed state (~1000 tokens total). This is documented as the canonical extension point for agent authors.

### Reference agents

- **Scripted agent** is rebuilt for the 10-year horizon as a competent baseline (not a deliberately-mediocre one). Five phases by week — Bootstrap (1–4), Buildout (5–26), Diversify (27–104), Mature (105–260), Late (261–521) — plus an always-on Crisis Response policy. Heuristics use strict, deterministic priority ordering (starvation triage → blackout response → reserve-margin → capacity → carbon-driven coal demolition → reservoir re-exploration → drilling → refinery → DR-injection siting). The scripted agent uses variable step size, dropping to `days=1` when `events_active` is non-empty.
- **LLM ReAct agent** uses the six-tool action vocabulary plus `step`. Token budget per game is 1M (assumed unlimited locally, accounting bound at 1M for safety). The state-summary boundary, system prompt, decision logic, and action tools are all named extension points.
- The brief's prohibition on participants modifying `world/`, `agents/base.py`, and `scoring.py` stands. Submissions live under `submit/` only.

### Determinism

- **Two RNG streams**, both children of the master seed via `numpy.random.SeedSequence`:
  - `sim_rng` drives world dynamics: weather noise per hour, event rolls per day, reservoir generation at reset.
  - `forecast_rng` drives forecast noise per `/forecast` call. Independent so that calling forecasts has no effect on simulation reproducibility.
- The simulation RNG advances **per simulated day**, not per `/step` call. This makes `step(days=7)` byte-identical to `step(days=1) × 7`. The determinism test suite verifies this invariance.
- Every action submitted to a mutating endpoint — successful or rejected — is appended to the action log with timestamp, parameters, ok/error result. The action log plus seed is sufficient to byte-replay any game.

### Manual play UI

- A play/pause button (▶/⏸) drives auto-step at adjustable speed (0.5x, 1x, 2x, 4x days per second).
- The spacebar toggles play/pause.
- The period key single-steps one day, only while paused.
- Pending actions are queued client-side only — each POST commits server-side immediately. The "pending actions" panel is purely UI affordance for human players who want to plan a turn before resuming.
- Subsurface cross-sections show revealed voxels colored by oil estimate; unrevealed voxels render as outlines.

### Repository layout

The brief's §10 layout is preserved verbatim, with two additions:
- A new module producing the LLM state summary (`agents/state_summary.py`) — already in the brief's layout.
- The action log location and format (`runs/{run_id}/actions.jsonl`) — already in the brief's §15.
- A new test file for step-size invariance lives alongside the existing determinism tests.

## Testing Decisions

### What makes a good test here

Tests in this codebase verify **external behavior**, not implementation details. A good test:

- Exercises a module's public function with concrete inputs and asserts on its return value or on observable state changes.
- Does not mock internal collaborators of the module under test. The world is small and fast — real instances are cheap.
- Names the formula or behavior under test in plain English (e.g., `test_solar_irradiance_zero_before_sunrise`, `test_dispatch_must_run_coal_minimum`).
- Survives refactors that preserve behavior. A test that breaks every time you rename an internal variable is a bad test.

### Modules that must have unit tests

Per your selection, all deep modules in `world/` plus determinism, smoke, and scripted-baseline regression:

- **`world/weather.py`** — solar irradiance shape (zero before sunrise, peaks at midday, scales with cloud factor); wind power curve at boundary speeds (3, 12, 25 m/s); forecast noise sigma growing with horizon; seasonal modulation of `sunrise(D)` and `sunset(D)`.
- **`world/power.py`** — demand formula scope correctness (heatwave affects residential only, demand surprise affects I+C only, process loads unaffected); dispatch merit order (renewables first, coal must-run, coal ramp by cost, gas peakers); ramp limits enforced (coal at 10%/h, gas at 50%/h); balance-state thresholds (curtailment at R≥1.15, brownout at 0.70≤R<0.95, blackout at R<0.70); blackout penalty applied per blackout hour.
- **`world/subsurface.py`** — reservoir generation reproducible from seed; voxel pool clipping at grid edges; well production formula (zero when `V_init=0`, scales with `k_eff` and `effective_fraction`); injection-well DR behavior (sheds at brownout/blackout, ramps at curtailment, baseline at balanced); drainage weighted by `permeability × oil_remaining`; survey cost scaling `15_000 × (n/8)²`.
- **`world/grid.py`** — road-adjacency 4-connected flood-fill correctness; town hall counts as road; tile-type road-adjacency requirements (house/commercial/industrial/refinery require connectivity; plants and wells do not); demolition refunds 25% of CAPEX; cannot place over existing tile.
- **`world/economy.py`** — refinery yield (0.85 × actual throughput); refinery routes crude to highest-throughput refineries first; carbon cost computed against current (post-regulatory-tightening) carbon price; industrial process CO₂ flat 2 t/day per tile; retail vs export power revenue split.
- **`world/population.py`** — growth when `jobs ≥ pop AND capacity > pop AND happiness ≥ 0.5`; housing exodus when `capacity < pop`; job-driven decline when `jobs < 0.7 × pop`; happiness-driven decline when `happiness < 0.5`; happiness clipped to [0, 1.5]; chebyshev-distance proximity to coal plants reduces happiness.
- **`world/events.py`** — daily probabilities respected over many trials within seeded RNG; durations sampled from spec ranges; regulatory tightening capped at 3 cumulative occurrences; carbon price multiplied by 1.5 each tightening; heatwave/demand-surprise multipliers applied to correct demand scopes; plant failure removes affected plant's output for sampled duration.

### Cross-cutting tests

- **`test_determinism`** — running the scripted agent twice on the same seed produces byte-identical world state at every checkpoint. Plus a step-size invariance test: stepping `days=7` once equals stepping `days=1` seven times. Plus a forecast-RNG isolation test: calling `/forecast` an arbitrary number of times does not perturb simulation state.
- **`test_api_smoke`** — full game end-to-end. Reset to seed 42, run scripted agent for 3650 days, assert no crashes, assert score within bounds, assert action log is replayable.
- **`test_scripted_baseline_regression`** — running the scripted agent on dev seed 42 produces a final score within 5% of the committed baseline. This test gates the scoring contract: if scripted's behavior changes meaningfully, the baseline file must be regenerated and checked in.

### Prior art

This is a green-field project — no existing tests. Standard pytest conventions apply. Each module's tests live in `world/tests/test_<module>.py`. Determinism, smoke, and regression tests live alongside the unit tests in the same directory. Run the full suite with `pytest world/tests`.

## Out of Scope

The following items are **explicitly deferred to v2 or later**, per the brief's §16 and confirmed by the grilling session:

- **Battery storage tiles.** The DR-on-injection mechanic replaces the renewable-storage role. A clean v2 hook for batteries is documented but not implemented.
- **Per-plant hourly dispatch override (`/control/plant`).** Dropped from v1; auto-dispatcher handles all hourly decisions.
- **Hourly transmission topology, line losses, congestion.** The grid is implicit; supply meets demand globally.
- **Pipeline transport cost or capacity.** Pipelines are aesthetic/connectivity tiles only.
- **Water resource constraints on injection.**
- **Drill-through-rock cost variation by depth.** Drilling cost is the well's catalog CAPEX, regardless of `target_z`.
- **Multi-agent / multi-player interaction.** Single-agent, single-tenant.
- **WebSocket streaming.** UI polls `/state` every 500ms.
- **Persistence across container restarts.** Game is in-memory; reset on restart. No save/load mid-game.
- **Authentication or rate-limiting.** Single-tenant local deployment trusts the local agent.
- **Forecast resampling caps.** Resampling is intended (variance reduction is a strategic feature). The cost-scaling on surveys handles the analogous exploit there; forecasts are free but parsing-them-usefully takes agent code.
- **Tool schemas for participants other than the six listed.** Participants can extend `ACTION_TOOLS` in their submission, but the reference LLM agent ships with exactly the six.

## Further Notes

### Tunables flagged for playtest

The grilling session resolved most spec ambiguities, but several numerical magnitudes are best left to playtesting once the world is running:

- Survey top-K cutoffs (currently 10 in `/state`, 30 in LLM summary).
- Industrial process CO₂ rate (currently 2 t/day flat per industrial tile).
- DR ramp factor on injection wells during curtailment (currently 2× baseline).
- LLM state summary token target (currently ~1000 tokens; the budget can absorb more if needed).
- Scripted agent thresholds (treasury triggers, reserve-margin triggers, oil/perm filter values for drill candidates).

These should be exposed via `world/config.py` so retuning is a one-line change.

### Personas

Per the brief's §17, four personas consume this PRD:

1. The implementer agents who build the world, reference agents, and UI from this PRD.
2. Hackathon participants who write `submit/agent.py` against the resulting code.
3. The human players who play the manual UI.
4. The judges who run scoring at the end of the event.

The PRD prioritizes implementers and agent-author personas in the user-stories section. Human-player and judge stories are captured but secondary.

### Relationship to the brief

The brief at `docs/hackathon-brief.md` remains the canonical reference for:

- §3.5 reservoir generation parameters
- §4.1, §4.2 weather formulas (unchanged)
- §4.5 well production equation (with DR adjustment from this PRD)
- §4.8 population dynamics (unchanged)
- §4.9 forecast formula (unchanged; just runs on a separate RNG stream)
- §4.12 build catalog CAPEX/OPEX (unchanged)
- §5.3 `/state` schema (with subsurface compression from this PRD)
- §9 environment variable list (with additions from this PRD: `MANUAL_GAME_DAYS`, `GRID_PRICE_RETAIL`, `GRID_PRICE_EXPORT`, `INDUSTRIAL_PROCESS_CO2_T_PER_DAY`)
- §10 repository layout
- §13 24-hour participant schedule
- §14 code reading tour

Where this PRD is silent, the brief governs. Where this PRD speaks, it overrides the brief.

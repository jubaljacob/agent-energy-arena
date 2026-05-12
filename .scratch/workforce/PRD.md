---
Status: needs-triage
---

# PRD — World v2: Workforce & Per-Facility Staffing

This PRD introduces a workforce mechanic to the v1 simulation defined in `.scratch/energy-ai-nexus/PRD.md`. Where this PRD and the v1 PRD disagree, **this PRD wins for the touched surface** (population dynamics, producer-tile output, CO2 attribution, UI population display). Everything else from v1 stands unchanged.

The design captured here is the synthesis of a grilling session that resolved 10 questions across tax base, efficiency formula, town-hall handling, CO2 scaling, drain order, operational-flag semantics, demolish flow, test migration, plant/well job counts, and UI granularity.

## Problem Statement

In the v1 simulation, `jobs` is a capacity number that gates population growth (`jobs >= pop`) but has no other effect. Two consequences are felt by players:

1. **Facilities run at full output regardless of workforce.** A refinery built when the city has no spare labor still produces oil at 100% throughput. A coal plant with zero workers still emits CO2 and serves load. This makes the act of building a facility a one-shot CAPEX decision — there is no ongoing labor cost or scarcity to manage.
2. **Population is a single scalar.** Players see `population: 100` but cannot tell who is working where, who is idle, or which facility is dragging because of understaffing. The strategic question "do I have labor headroom?" cannot be answered from the UI.

Both gaps undercut the strategic depth of the build phase: the player's only economic decision is "do I have cash?" — not "do I have the people?"

## Solution

Every facility that produces something (commercial, industrial, refinery, all power plants, all wells, town hall) requires workers to operate. Each facility declares a `jobs` count (its hiring need) and tracks `staffed_jobs` (its current headcount). Efficiency is `staffed_jobs / jobs` and multiplies every capacity-derived behavior of that facility: power output, fuel burn, CO2 emissions, refinery throughput, well production, and the facility's own power demand.

Workers are drawn from the city's unemployed pool. At every build, drill, demolish, and end-of-day population update, an allocator runs that fills vacancies starting with the **oldest** facility (by built/drilled day, then id-ascending tiebreak). When the city loses population through unhappiness, the housing shortage, or job shortage, **unemployed people leave first**; only when the unemployed pool is exhausted do employed workers depart from their facilities, starting with the **newest** facility.

The UI surfaces both views: a top-line `{unemployed}/{population}` counter (e.g. `34/100`) and per-facility staffing badges (e.g. `5/8`) coloured by efficiency band.

Construction itself remains laborless and instantaneous — workforce only matters for *operation*, not building.

## User Stories

### Macro — population & treasury

1. As a player, I want to see my population as `{unemployed}/{total}` (e.g. `34/100`) in the top bar, so that I know at a glance how much labor I have available to staff new facilities.
2. As a player, I want my tax revenue to remain `$4 × total population` (employed and unemployed both contribute), so that growing my city is unambiguously good for the treasury — workforce mechanics do not double-penalize me.
3. As a player, I want unemployed citizens to draw residential power like any other resident, so that idle population has a real cost (load + happiness drain) even without changing the tax model.
4. As a player, when my city's population grows on a happy day, I want the new arrivals to automatically fill any open vacancies (oldest facility first), so that I do not have to micromanage hiring.
5. As a player, when my city's population shrinks (housing, jobs, or unhappiness), I want unemployed citizens to leave the city before any employed worker is forced out of their job, so that my productive infrastructure stays staffed for as long as possible.
6. As a player, when the unemployed pool is exhausted and my city is still shrinking, I want the most recently built facility to lose staff first, so that my long-running infrastructure is the last to break.

### Build flow

7. As a player, when I build a facility with available unemployed labor, I want it to staff itself immediately from the unemployed pool, so that it starts operating at full efficiency on day one.
8. As a player, when I build a facility but there is not enough unemployed labor to fully staff it, I want it to be built and operate at fractional efficiency (e.g. 3 hired out of 4 needed → 75%), so that I can still construct ahead of my workforce and have it ramp up as my city grows.
9. As a player, when I build a facility and zero workers are available, I want it to be built as a fully idle facility (0% efficiency, no output, no fuel burn, no CO2, no power demand), so that an unstaffed plant has zero economic footprint and is not silently bleeding fuel/emissions.
10. As a player, I do not want to need any workforce to *construct* a facility — only to *operate* it, so that my building decisions remain about cash and placement, not about pausing my city while a build queue drains labor.

### Demolish flow

11. As a player, when I demolish a staffed facility, I want its workers to re-enter the unemployed pool and then immediately backfill any under-staffed facility (oldest first), so that demolishing a building does not strand its labor force in limbo.
12. As a player, I want the 25% CAPEX refund on demolish to remain unaffected by staffing level, so that demolish economics stay predictable.

### Per-facility behavior

13. As a player, I want each producer facility on the map to show a `{staffed}/{jobs}` badge, so that I can spot which facility is dragging on output.
14. As a player, I want each facility badge to be colored — green at 100%, yellow at 50-99%, red below 50%, fully red/empty at 0% — so that I can scan the map for problems in a second.
15. As a player, I want a half-staffed coal plant to behave like a smaller coal plant — half the capacity ceiling, half the must-run minimum, half the ramp room, half the fuel burn, half the CO2 — so that efficiency degrades gracefully and matches my mental model of a "partially active plant".
16. As a player, I want a half-staffed refinery's max throughput to scale with efficiency (e.g. 250 bbl/day instead of 500 bbl/day at 50% staff), so that crude routing prioritizes refineries that are actually capable of processing their share.
17. As a player, I want a half-staffed oil well's production ceiling to scale with efficiency, so that under-staffed wells produce less even if I leave the setpoint at max.
18. As a player, I want an idle industrial tile to produce zero CO2 (the flat 2 t/day process emission scales with efficiency), so that "idle = zero footprint" is a uniform rule with no carve-outs.
19. As a player, I want an idle industrial or commercial tile to draw zero electric demand, so that an unstaffed factory does not silently waste power.

### Plant failure interaction

20. As a player, when a plant fails (operational=False), I want its workers to stay assigned to it for the duration of the failure, so that the same crew resumes operation when the plant is restored — no labor-market jitter on every plant_failure event.
21. As a player, when a failed plant restores (operational=True again), I want it to come back online with its original staffing intact, so that I do not have to re-hire after every event.

### Determinism & forecasts

22. As a player or AI agent, I want hire and fire order to be deterministic (built_day ascending for hires, descending for fires, with id-string tiebreak), so that the same seed produces byte-identical staffing decisions across runs.
23. As an AI agent, I want `/forecast` to use the current staffing snapshot for its simulated future days (since `/forecast` does not model future builds), so that forecast output reflects the labor situation I am actually in.

### Day-zero behavior

24. As a player, I want day 0 to start with the town hall staffed at full (30/30) from my starting population of 100, leaving 70 unemployed, so that the early game is visibly "you have lots of people, build things to employ them" instead of "everyone is at work, you cannot build."
25. As a player, I want the town hall to never lose staff to unhappiness while any other facility has staff, since it is the oldest building and the fire order goes newest-first, so that civic function never collapses.

### State API & data plane

26. As an AI agent, I want `/state` to expose `employed` and `unemployed` totals alongside `population`, so that I can plan builds against my available labor without re-summing per-tile staffing myself.
27. As an AI agent, I want each tile and well in `/state` to carry its `staffed_jobs` and `jobs` numbers, so that I can compute per-facility efficiency and identify which facilities to prioritize when labor is scarce.
28. As an AI agent, I want the `/catalog` endpoint to report `jobs` for every staffable tile and well type, so that I can plan workforce headroom before committing to a build.

### Workforce scope

29. As a player, I want passive tiles (road, house, park, pipeline) to require no workers, so that my infrastructure tiles do not eat my labor budget.
30. As a player, I want renewable plants (solar, wind) to require a small but non-zero workforce (2 jobs each), so that even a fully-renewable grid has a labor cost.
31. As a player, I want wells (oil and injection) to require workers (3 and 2 respectively), so that drilling a well has an ongoing labor commitment, not just a CAPEX hit.

## Implementation Decisions

### Deep module — `world/workforce.py`

A new module that owns the entire workforce calculus behind a small interface:

- `efficiency(tile_or_well)` — returns `staffed_jobs / spec.jobs` in `[0, 1]`, or `1.0` if `spec.jobs == 0` (passive tile)
- `employed(state)` — returns `sum(staffed_jobs)` across all tiles and wells
- `unemployed(state)` — returns `max(0, state.population - employed(state))`
- `producers(state)` — yields all tiles and wells with `spec.jobs > 0`, sorted by `(creation_day, id_string)` ascending, where creation_day is `built_day` for tiles or `drilled_day` for wells
- `hire_to_fill(state)` — walks producers oldest-first; each fills `min(spec.jobs - staffed_jobs, unemployed_pool)` until the pool is empty
- `drain_n(state, n)` — drains `n` people from the city: first decrements `population` by `min(n, unemployed(state))` (the unemployed leave silently); if `n` is still positive, walks producers newest-first decrementing `staffed_jobs` and `population` together until `n` is exhausted

All functions are pure (read-only or surgical mutation) — no I/O, no time, no RNG. This is the only module that decides how labor flows.

### State

- `Tile.staffed_jobs: int = 0` and `Well.staffed_jobs: int = 0` added to the dataclasses in `world/state.py`. Default `0` — no staff until allocator runs.

### Catalog

- `coal_plant: jobs=8`
- `gas_peaker: jobs=4`
- `solar_farm: jobs=2`
- `wind_turbine: jobs=2`
- `oil_well: jobs=3`
- `injection_well: jobs=2`

Existing job counts (`town_hall=30, commercial=12, industrial=30, refinery=25`) are unchanged. Passive tiles (`road, house, park, pipeline`) remain at `jobs=0`.

### Hook points (when the allocator runs)

| Event | Behaviour |
| --- | --- |
| `reset` (including town hall placement) | `hire_to_fill` runs after the town hall is appended |
| `build` | After the new tile is appended to `state.tiles`, `hire_to_fill` runs |
| `drill` | After the new well is appended to `state.wells`, `hire_to_fill` runs |
| `demolish` | After the tile is removed (and its `staffed_jobs` are dropped, returning workers to the unemployed pool), `hire_to_fill` runs to backfill other under-staffed facilities |
| `update_population` end-of-day | Growth branch: after `pop += growth`, `hire_to_fill` runs. Decline branches (exodus/job-decline/happiness-decline): after computing `target_pop`, `drain_n(state, pop_before - target_pop)` runs |
| Per-hour during `/step` | Staffing is fixed for the duration of the day. No automatic re-allocation inside the hourly loop |
| `operational=False` (plant failure event) | Staffing untouched. Output gated by `operational` flag in dispatch as today |
| `operational=True` (failure restore) | Staffing untouched. Same workers resume |

### Uniform efficiency rule — "scales capacity"

Every consumer of a producer's catalog capacity multiplies by `efficiency(t)`:

- **Coal plants and gas peakers** — `effective_capacity_kw = catalog.capacity_kw × efficiency`. The must-run floor (25% of capacity for coal) and the ramp-room per hour (10%/h coal, 50%/h gas) are derived from `effective_capacity_kw`, so a half-staffed plant has half the floor, half the ceiling, and half the ramp room. Fuel burn and CO2 scale with the actual kWh dispatched (already linear in output, so this is automatic).
- **Solar and wind** — output `× efficiency`. A solar farm with 0 staff produces 0 kW even on a sunny day.
- **Oil wells** — `effective_q_max = Q_MAX_WELL_BBL_DAY × efficiency` enters both the setpoint clamp and the `q_potential = effective_q_max × k_eff × effective_fraction` formula. A half-staffed well's geological cap is halved.
- **Injection wells** — the baseline kW and the curtailment-mode double-baseline cap both scale with efficiency.
- **Refineries** — `route_crude` uses `effective_max_bbl_day = REFINERY_MAX_BBL_DAY × efficiency` as the per-refinery cap. Process load `× efficiency`.
- **Commercial and industrial tiles** — `demand_kw × efficiency` in `total_demand_kw`. Industrial's flat 2 t/day CO2 process emission `× efficiency`.

The principle: an N%-staffed facility behaves like an N%-sized version of itself in every observable way.

### Population dynamics

The four-branch cascade in `update_population` stays as today (status quo growth/exodus/job-decline/happiness-decline gates with the same thresholds and rates). The math computes a `target_pop`; the implementation then routes the delta through the workforce module:

```
delta = pop_before - target_pop
if delta > 0:                 # any decline branch
    drain_n(state, delta)     # unemployed first, then newest-first fire
elif delta < 0:               # growth branch
    state.population = target_pop
    hire_to_fill(state)       # oldest-first
```

Tax revenue is then accrued on the final `state.population` exactly as today: `$4 × population`. No change to the tax base.

The growth gate's `jobs` term continues to use `sum(spec.jobs)` (catalog capacity), not `employed`. Workforce only changes what happens to the new arrivals (auto-hire) and to those who leave (departure order).

### API & state surface

- `/state` adds `employed: int` and `unemployed: int` to the top-level dict, computed from the workforce module.
- Each entry in `/state.tiles` and `/state.wells` adds `staffed_jobs: int`.
- `/catalog` already exposes `jobs` per tile/well; the new fields on plants and wells flow through automatically.

### UI

- **Top bar** — population reads `{unemployed}/{total}` (e.g. `34/100`).
- **Facility badges** — every producer tile and well on the map renders a small badge showing `{staffed_jobs}/{spec.jobs}`. Colour: green at 100%, yellow 50–99%, red below 50%, fully red at 0%.

### Test migration — hybrid strategy

- The test injection helpers (`_inject_tile` in `test_population.py` and similar in `test_economy.py`, `test_demand.py`, etc.) default `staffed_jobs = spec.jobs` (i.e. inject fully-staffed) so tests that do not care about workforce remain unchanged.
- Workforce-specific tests bypass the helper or override `staffed_jobs` explicitly to verify the new invariants (idle = zero output, partial = fractional, drain order, hire order).
- Tests that manipulate `state.population` directly after injection may produce `employed > pop`. The workforce module's `unemployed = max(0, pop - employed)` clamp keeps this benign.
- One known existing test must be rewritten: `test_industrial_pays_flat_co2_even_when_no_grid` (`test_economy.py:622`) currently asserts an industrial tile emits 2 t/day flat when `pop=0`. Under the uniform efficiency rule this becomes 0. Rewrite to assert against a manually-staffed industrial tile, preserving the test's actual intent (the flat term is independent of grid dispatch).

### Determinism

- All hire/fire passes are deterministic: producers are sorted by `(creation_day, id_string)` ascending for hiring and reversed for firing. No RNG draws.
- The skeleton's per-day RNG-budget contract (one `sim_rng.standard_normal()` draw per simulated day, anchored in the determinism tests) is unaffected — the workforce module does not consume RNG.
- Build/drill/demolish are called between `/step` invocations, so the in-step state is stable and the daily loop sees a fixed staffing snapshot for the duration of one simulated day.

## Testing Decisions

### Principle

Tests assert on observable contract — what a player or agent reads from `/state`, `/catalog`, and the daily summary — not on internals of the workforce module. The workforce module's pure functions can be tested directly, but the integration tests must drive the behaviour through `world.build`, `world.drill`, `world.demolish`, and `world.step` calls.

### Modules covered

- **`workforce.py` (unit)** — direct function tests for `hire_to_fill` (oldest-first ordering, fills until pool empty, leaves stragglers unemployed), `drain_n` (unemployed first then newest-first fire, decrements both `staffed_jobs` and `population`), `efficiency` (boundary cases: zero jobs, zero staffed, full staffed), `producers` (correct ordering, includes both tiles and wells, excludes passive tiles).
- **`population.py` (integration)** — drain order through each of the three decline branches (exodus, job-decline, happiness-decline) verifies unemployed-first then newest-first fire. Growth branch verifies new arrivals auto-fill oldest vacancies. Day-0 starting state (pop=100, town hall 30/30, 70 unemployed).
- **`sim.py` build/drill/demolish (integration)** — building a facility with available labor immediately staffs it. Building with insufficient labor leaves it at fractional efficiency. Demolishing a staffed facility returns workers to the unemployed pool and rebalances.
- **`power.py` and dispatch (integration)** — a half-staffed coal plant has half the must-run, half the ramp room, half the ceiling. An idle commercial or industrial tile draws zero power.
- **`economy.py` (integration)** — refinery routing uses efficiency-scaled cap. Idle industrial emits zero CO2. Idle refinery emits zero CO2 even with crude available.
- **`subsurface.py` (integration)** — a half-staffed oil well produces at most half of `Q_MAX_WELL_BBL_DAY`. Injection well baseline kW and DR cap both scale with efficiency.
- **`/state` schema (smoke)** — `employed` and `unemployed` keys present and consistent; per-tile and per-well `staffed_jobs` present.

### Prior art

The existing tests under `world/tests/` already establish the patterns to follow:

- `test_population.py` exercises each population-cascade branch in isolation by injecting tiles directly — that pattern is the model for the new drain-order tests.
- `test_economy.py` mixes pure-function tests (`refine_one`, `route_crude`) with end-to-end `world.step()` flows — the workforce tests mirror this split between unit-level `workforce.py` tests and integration tests through `sim.py`.
- `test_dispatch.py` already isolates dispatch with synthetic plant lists — the new efficiency-scales-capacity tests follow the same pattern, injecting plants with chosen `staffed_jobs` values and asserting output.
- `test_determinism.py` asserts step-size invariance (`step(7) ≡ step(1)×7`). The workforce module is deterministic by construction, but a regression test that builds and runs a city under both step cadences and asserts identical staffing remains valuable.

### What we explicitly do not test

- The exact integer values of plant/well job counts (those are tuning parameters; tests use the catalog values, not hard-coded literals).
- The exact colour thresholds on UI badges (visual styling, not contract).
- The exact ordering when two facilities share `built_day` and the id-string tiebreak fires (deterministic by construction; one regression test is enough, no need to exhaustively enumerate).

## Out of Scope

- **Manual labor reallocation by the player.** Allocation is fully automatic (oldest-first hire, newest-first fire). There is no `/hire` or `/transfer` endpoint, no slider, no priority list — the player cannot say "fire industrial first, refinery last". If this becomes a strategic gap, it is a v3 conversation.
- **Per-worker tracking or named citizens.** Workers are an integer count per facility, not individuals. No biographies, no skills, no aging.
- **Wages or labor-cost OPEX.** OPEX remains a per-tile-per-day flat cost as in v1. Workers do not have a salary line separate from OPEX.
- **Education / skill levels / training time.** Anyone can do any job instantly. A coal plant operator and a refinery technician are interchangeable.
- **Commute, transportation, or geographic constraints on labor.** A well in the corner of the map and a refinery in the city centre draw from the same pool with no friction.
- **Strikes, unions, hiring freezes, or labor events.** No event in `events.py` adds to or subtracts from the labor pool directly.
- **Construction crews / time-to-build.** Building is instantaneous and laborless, as in v1.
- **Well decommissioning.** Wells still cannot be demolished. Their workers remain bound for the life of the game.
- **Differential drain rates per branch.** All three decline branches use the same uniform drain order. No "exodus drains proportionally" or "job-decline fires employed first" variants.
- **Tax base changes.** Tax stays `$4 × total population`. We did not adopt the `$4 × employed` alternative.

## Further Notes

### Relationship to v1 PRD

This PRD does not deprecate v1. The simulation contract (deterministic seeds, hourly ticks, daily steps, 10-year agent horizon, 1-year manual session, scoring formula, event mechanics, weather model, subsurface generation, dispatch merit order) all stand. The workforce mechanic adds a multiplicative scalar to producer behaviour and a new state field; it does not change the shape of the simulation.

The skeleton-slice determinism contract (`sim_rng` advances per simulated day, `forecast_rng` independent) is preserved by construction — the workforce module is RNG-free.

### Design rationale captured in the grilling session

Each of the 10 design choices in this PRD was the result of a one-question-at-a-time grilling pass:

1. **Tax base** stays at `$4 × population` (status quo). Unemployment already has indirect costs (residential demand, happiness drain, idle OPEX); we did not stack a tax cliff on top.
2. **Efficiency formula** is the uniform "scales capacity" rule — one mental model, one helper, no per-system carve-outs. An N%-staffed facility behaves like an N%-sized facility everywhere.
3. **Town hall** is a normal facility in the workforce model. Built_day=0 makes it the oldest, so it is effectively protected from unhappiness firing without a special case.
4. **Industrial flat CO2** scales with efficiency. Idle industrial = 0 t/day. Consistency with the uniform rule.
5. **Drain order** is uniform across all three decline branches: unemployed first, then newest-first fire. One helper (`drain_n`), one rule.
6. **Non-operational tiles** keep their workers. Plant failures are short events; re-recruiting on every restore would create labor jitter.
7. **Demolish** frees workers and immediately rebalances oldest-first. Mirrors the build path.
8. **Test migration** is the hybrid: helpers auto-fill, workforce-specific tests stay explicit. Minimises churn.
9. **Plant/well jobs** are coal=8, gas=4, solar=2, wind=2, oil=3, inj=2 — moderate values so partial staffing is meaningful but energy labor is not the dominant constraint.
10. **UI** is population top-line `34/100` plus per-facility badges with colour bands.

### Risk: labour-starved blackout spiral

A pathological starting condition would be a player who builds a refinery and an industrial complex on day 1, soaking up all 70 unemployed, then immediately demolishes a house — population can no longer grow (no housing) and any unhappiness fires the only workers staffing power-relevant facilities, cascading into blackouts. This is a designed failure mode, not a bug: it reflects the genuine trade-off of over-committing labor before growing the city. The UI badges make it diagnosable; the player can demolish to recover capex and rebalance.

### Migration path for existing test suite

Step 1: introduce `staffed_jobs` field on `Tile` and `Well` with default `0`. Run `make test` — many tests will fail because injected producer tiles are now idle. Step 2: update test helpers to default `staffed_jobs = spec.jobs`. Re-run; most failures clear. Step 3: identify the remaining individually-broken tests (those manipulating `state.population` after injection, or the flat-CO2 industrial test) and fix by hand. Step 4: add workforce-specific tests for the new invariants.

This is the order I propose to implement, with a `make check` gate at each step.

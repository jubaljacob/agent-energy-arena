---
Status: needs-triage
---

# 04 — Power dispatch scales with efficiency (coal/gas/solar/wind)

## Parent

[PRD: World v2 — Workforce & Per-Facility Staffing](../PRD.md)

## What to build

Every power plant's effective capacity scales with its staffing efficiency. An N%-staffed plant behaves exactly like an N%-sized plant: half the ceiling, half the must-run floor (coal), half the ramp room, half the fuel burn, half the CO2.

The PRD's uniform rule: `effective_capacity_kw = spec.capacity_kw × efficiency(t)`. Every downstream derivation — must-run, ramp limit, intermittent output cap — uses `effective_capacity_kw` rather than the catalog `capacity_kw`.

### Implementation details

**`world/power.py` — `dispatch()`**:

The current dispatch in `world/power.py:126` reads `t.jobs` and the catalog's `capacity_kw` directly. Replace `capacity_kw` reads with `effective_capacity_kw = spec.capacity_kw × workforce.efficiency(t)` everywhere they appear in the dispatch path:

- **Coal plant**:
  - Ceiling for this hour: `effective_capacity_kw`.
  - Must-run floor: `0.25 × effective_capacity_kw` (the existing coefficient stays; the *base* changes).
  - Per-hour ramp room: `0.10 × effective_capacity_kw` (existing 10%/h coal ramp; same coefficient, new base).
  - At `efficiency=0`, ceiling, floor, and ramp are all 0 → an unstaffed coal plant produces 0 kW, burns 0 fuel, emits 0 CO2. Make sure the dispatch code does not divide by `capacity_kw` somewhere; if it does, the zero-capacity case needs an explicit early-out.
- **Gas peaker**: same treatment. Ceiling = `effective_capacity_kw`, ramp room = `0.50 × effective_capacity_kw`. Gas has no must-run.
- **Solar farm**: weather-derived output is capped at `effective_capacity_kw`. A 0%-staffed solar farm produces 0 kW even on a sunny day.
- **Wind turbine**: same — weather-derived output capped at `effective_capacity_kw`.

Fuel burn (`fuel_cost_per_mwh × actual_kwh / 1000`) and CO2 emissions (`co2_t_per_mwh × actual_kwh / 1000`) are already linear in dispatched output, so they scale automatically once the dispatched kWh reflects efficiency.

**`world/power.py` — operational flag interaction**:

`operational=False` plants already contribute 0 to dispatch via the existing `operational` guard. The workforce module does not filter on `operational`, so a failed plant's `staffed_jobs` stays bound (PRD stories 20–21). When the plant restores, dispatch picks up the existing `staffed_jobs` and the same crew resumes — no labor jitter on `plant_failure` events. Verify with an explicit regression test.

**`world/sim.py` per-hour update**:

`world/sim.py:560` currently writes `current_output_kw` per tile after dispatch. No change needed if dispatch returns per-tile kW correctly under efficiency scaling, but verify that `current_output_kw` reads as 0 for unstaffed plants.

**Renewable share scoring** (`state.cumulative_renewable_served_kwh` and `state.cumulative_total_served_kwh`): no change needed — both are derived from dispatched kWh, which already scales.

### Tests to add in this slice

`world/tests/test_dispatch.py` and `test_power.py` (existing modules — follow their pattern of injecting synthetic plant lists):

- **Half-staffed coal plant**: catalog capacity 1000 kW, must-run floor 250 kW, ramp 100 kW/h. Inject a coal plant with `staffed_jobs=4` (`jobs=8`, efficiency=0.5). Dispatch with demand at 600 kW. Effective capacity 500 kW, effective floor 125 kW, effective ramp 50 kW/h. Assert the dispatched output is capped at 500 kW.
- **Idle coal plant produces no output**: same plant with `staffed_jobs=0`. Dispatch with demand high enough to call on coal — assert `current_output_kw=0`, fuel burn = 0, CO2 emission = 0.
- **Idle solar farm produces no output**: `staffed_jobs=0` solar farm under full irradiance — assert output = 0.
- **Half-staffed wind turbine caps at half**: catalog 200 kW, `staffed_jobs=1` (jobs=2, efficiency=0.5). Wind conditions that would produce 200 kW at full staffing → cap at 100 kW.
- **Ramp room scales**: cold-start coal at `staffed_jobs=4`. Hour 0 output 0 kW. Hour 1 demand spike — output cannot exceed `min(effective_floor + effective_ramp, effective_cap) = 125 + 50 = 175 kW`. (The current ramp implementation may bootstrap from must-run rather than 0; align the assertion with whichever semantics the existing test_dispatch.py establishes.)
- **Plant-failure preserves staffing**: build a coal plant, run `/step` to staff it, fire an event that flips `operational=False`. After the event, the tile's `staffed_jobs` is unchanged. Restore `operational=True`. The next dispatch hour produces normally.
- **Determinism**: `test_determinism.py` step-size invariance still holds — same seed, build the same set of plants, `step(7) == step(1)×7` for `current_output_kw` traces.

## Acceptance criteria

- [ ] Coal plant ceiling, must-run floor, and ramp room all scale with `workforce.efficiency(t)` in `dispatch()`.
- [ ] Gas peaker ceiling and ramp room scale with efficiency.
- [ ] Solar and wind output caps scale with efficiency; idle (0%) plants produce 0 kW.
- [ ] Fuel cost and CO2 emissions scale correctly through the dispatched-kWh path (no double-multiplication).
- [ ] A `plant_failure` event does not perturb `staffed_jobs`; restoration resumes with the original crew.
- [ ] `test_determinism.py` step-size invariance is preserved.
- [ ] `make check` is green.

## Blocked by

- 01 — Workforce foundation (provides `workforce.efficiency` and `staffed_jobs` field)

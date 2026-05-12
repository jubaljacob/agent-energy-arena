---
Status: needs-triage
---

# 07 — Wells scale with efficiency (oil + injection)

## Parent

[PRD: World v2 — Workforce & Per-Facility Staffing](../PRD.md)

## What to build

Both well types scale with staffing efficiency. A half-staffed oil well's production ceiling is halved; a half-staffed injection well's baseline kW and curtailment-mode (DR) cap are both halved. An idle well produces nothing, injects nothing, and draws no power.

### Implementation details

**`world/subsurface.py` — oil wells**:

`Q_MAX_WELL_BBL_DAY = 200.0` at line 45 stays as the catalog constant. Per-well effective max becomes:

- `effective_q_max = Q_MAX_WELL_BBL_DAY × workforce.efficiency(well)`
- In `well_production_bbl_day` at line 288, the `q_potential = Q_MAX_WELL_BBL_DAY × k_eff × effective_fraction` formula at line 318 becomes `q_potential = effective_q_max × k_eff × effective_fraction`.
- The setpoint clamp (where `setpoint_rate_bbl_day` is bounded by `Q_MAX_WELL_BBL_DAY` aka `WELL_SETPOINT_MAX` at line 49) is **not** efficiency-scaled. The player can still set the setpoint to 200. The *actual* production is `min(setpoint, q_potential)` where `q_potential` is now efficiency-scaled.
- A 0%-staffed oil well has `q_potential = 0` regardless of setpoint or reservoir; it produces 0 bbl/day.

**`world/power.py` and / or `world/sim.py` — injection wells**:

Injection wells contribute to electric demand through:

- A baseline kW (the steady-state energy to run the pump) — this is the "always on when the well exists" load.
- A curtailment-mode DR cap (the demand-response upper bound where injection can be doubled during low-demand hours to soak surplus power).

Both scale linearly with efficiency:

- `effective_baseline_kw = baseline_kw × workforce.efficiency(well)`
- `effective_dr_cap_kw = 2 × baseline_kw × workforce.efficiency(well)` (the "double the baseline" DR rule still holds; the base doubles)

A 0%-staffed injection well draws 0 baseline kW and has 0 DR headroom. A half-staffed injection well draws half the baseline and offers half the DR headroom.

Locate the existing injection-well baseline + DR computation (likely in `world/power.py` or `world/sim.py` — check `_process_loads_kw` and the dispatch path that handles injection curtailment). Thread `workforce.efficiency(well)` through those formulas.

**`world/state.py` — cumulative production/injection**:

`Well.cumulative_produced_bbl` and `Well.cumulative_injected_bbl` accumulate the actual hourly production/injection, which is already efficiency-scaled at source. No change needed; cumulative values just track reality.

### Tests to add in this slice

`world/tests/test_production.py`:

- **Half-staffed oil well caps at half Q_MAX**: drill an oil well with `staffed_jobs=1` (jobs=3, efficiency≈0.33). Place it on a voxel with `k_eff=1.0` and `effective_fraction=1.0`. Setpoint at 200. `well_production_bbl_day` returns `200 × 0.33 × 1.0 × 1.0 ≈ 67 bbl/day`. Not 200.
- **Idle oil well produces nothing**: oil well with `staffed_jobs=0`. Setpoint at 200, rich voxel. Production = 0 bbl/day.
- **Fully-staffed oil well matches v1 baseline**: oil well `staffed_jobs=3`, rich voxel, setpoint 200. Production = `Q_MAX_WELL_BBL_DAY × k_eff × effective_fraction` (existing v1 formula).
- **Setpoint not auto-clamped by efficiency**: oil well `setpoint_rate_bbl_day=200`, mutate `staffed_jobs` to 1. Assert setpoint stays 200; only actual production reflects efficiency.

`world/tests/test_injection.py`:

- **Half-staffed injection baseline halves**: inject one injection well `staffed_jobs=1` (jobs=2, efficiency=0.5). Compute `_process_loads_kw` (or whichever function emits injection baseline). Injection contribution to total demand = half the v1 baseline.
- **Idle injection draws zero baseline**: injection well `staffed_jobs=0`. No injection contribution to demand. No injection happens at any hour.
- **Half-staffed DR cap halves**: under curtailment conditions (surplus available, DR active), a half-staffed injection well's max draw is `2 × baseline × 0.5 = baseline`. A 0%-staffed well offers zero DR headroom.
- **Pool-intersection still works**: oil well's reservoir pressure boost from a fully-staffed injection well sharing its 3×3×3 pool is unchanged (that effect is geological, not workforce). Add a regression case: half-staffed injection well still establishes pool intersection, but injects half the bbl, so the pressure-recovery effect is proportionally reduced. (Match this assertion to whatever pressure-coupling logic exists in `well_production_bbl_day` — if injection rate enters `effective_fraction` directly, the test asserts the proportional reduction; if there is a binary "intersection / no intersection" flag, the test asserts the intersection is still detected.)

## Acceptance criteria

- [ ] `well_production_bbl_day` for an oil well caps `q_potential` at `Q_MAX_WELL_BBL_DAY × workforce.efficiency(well)`.
- [ ] Oil well `setpoint_rate_bbl_day` is not auto-clamped by efficiency (only actual production is).
- [ ] Injection well baseline kW scales with efficiency in `total_demand_kw` (via `_process_loads_kw` or its equivalent).
- [ ] Injection well DR / curtailment cap scales with efficiency (max = `2 × baseline × efficiency`).
- [ ] Idle wells produce 0 bbl/day, inject 0 bbl/day, and draw 0 kW.
- [ ] New tests cover idle / half-staffed / fully-staffed cases for both well types.
- [ ] `make check` is green.

## Blocked by

- 01 — Workforce foundation (provides `workforce.efficiency` and `staffed_jobs` field)

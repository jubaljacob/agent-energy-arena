---
Status: needs-triage
---

# 07 — Production wells + crude revenue

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## What to build

`POST /drill { "x", "y", "target_z", "well_type": "production" }` builds-and-drills a production well in one call, deducting CAPEX from the brief's catalog. Wells are exclusively created via `/drill` — `/build` does not accept `oil_well` or `injection_well` types. Validity rules from §4.12: two wells cannot share the same (x, y), but different wells may target different `z`. The tile is recorded as a "drilled well" and cannot be re-targeted (demolish and re-drill if needed).

`world/subsurface.py.well_production_bbl_day(w, world)` implements the brief's §4.5 production formula: 3×3×3 voxel pool clipped to grid, `q_potential = Q_MAX_WELL_BBL_DAY × k_eff × effective_fraction`, `q_actual = min(setpoint, q_potential)`. In this slice, `effective_fraction` equals `fraction` (no injection support yet — that lands in slice 08). Drainage weights pool voxels by `permeability × oil_remaining`.

`POST /control/well { "well_id", "rate_bbl_day" }` sets the well's setpoint in [0, 200]. Wells produce daily; `cumulative_produced_bbl` and `current_rate_bbl_day` accumulate.

Crude is sold daily at `CRUDE_PRICE = $40/bbl`. Refinery routing comes in slice 09; for now all crude is sold raw. `/state.wells` lists every well with id, type, target_z, drilled_day, setpoint_rate_bbl_day, current_rate_bbl_day, cumulative_produced_bbl. UI gains a wells tab with a table and rate sliders.

## Acceptance criteria

- [ ] `POST /drill { "well_type": "production", ... }` deducts $50,000 and creates a well; world state reflects it within the same tick.
- [ ] `POST /drill` rejects with `"tile_occupied"` when an existing well shares the same (x, y).
- [ ] `POST /drill` rejects with `"voxel_out_of_bounds"` when target_z is outside [0, WORLD_D).
- [ ] `POST /drill` rejects with `"insufficient_funds"` when treasury < CAPEX.
- [ ] `POST /control/well { "well_id": ..., "rate_bbl_day": 150 }` sets the setpoint; `current_rate_bbl_day` reflects the actual produced rate (≤ setpoint, ≤ q_potential).
- [ ] A well targeting a voxel with no hydrocarbon (V_init = 0) produces 0 bbl/day indefinitely.
- [ ] Two wells targeting overlapping pools share the resource: each runs the production formula against the *current* `oil_remaining`; ordering is deterministic by `well.id`.
- [ ] Drainage weights voxels by `permeability × oil_remaining`; total drained equals q_actual.
- [ ] Daily crude revenue equals `Σ q_actual × CRUDE_PRICE`; reflected in the daily summary.
- [ ] `/state.wells` exposes id, type, target_z, drilled_day, setpoint, current rate, cumulative produced.
- [ ] UI wells tab shows a table with each well's id, type, depth, current rate, cumulative production. Each row has a rate slider that calls `/control/well`.
- [ ] Tests in `world/tests/test_production.py` cover: production formula edge cases (V_init=0, full pool, partial pool clipped at edges), drainage weighting, two-wells-overlap-same-pool deterministic ordering.

## Blocked by

- 06 — Subsurface generation + seismic survey + cross-section UI

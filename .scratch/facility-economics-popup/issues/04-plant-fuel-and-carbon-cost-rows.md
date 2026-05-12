# 04 — Plant fuel cost + carbon cost popup rows; accurate CO₂ row

Status: needs-triage

## Parent

`.scratch/facility-economics-popup/PRD.md`

## What to build

Fossil plants (coal, gas peaker) show explicit dollar-denominated `Fuel cost / day` and `Carbon cost / day` rows in the hover popup, computed from yesterday's served kWh. The existing CO₂ row switches from `current_output_kw × 24 × intensity` to a kWh-served-based daily total so the displayed tonnage matches the cost. The `Net / day` row updates to subtract fuel and carbon, so Net reconciles with the rows above it for fossil plants. Renewables show $0 fuel and $0 carbon explicitly to make the contrast visible. `GET /state` carries the same per-tile cost fields.

## Acceptance criteria

- [ ] `world.pricing` gains `plant_fuel_cost_for_tile(tile, spec)` returning `kwh_served_yesterday / 1000 × spec.fuel_cost_per_mwh`.
- [ ] `world.pricing` gains `plant_carbon_cost_for_tile(state, tile, spec)` returning `(kwh_served_yesterday / 1000) × spec.co2_t_per_mwh × state.carbon_price`.
- [ ] `_tile_to_dict` for plants emits `estimated_fuel_cost_per_day` and `estimated_carbon_cost_per_day`. For renewables both are 0.0; for fossils they reflect the helpers above.
- [ ] `_tile_to_dict` for plants emits an updated `estimated_co2_per_day` derived from `kwh_served_yesterday`, not `current_output_kw × 24`.
- [ ] `_tile_to_dict` for plants updates `estimated_net_per_day` to `revenue − opex_per_day − estimated_fuel_cost_per_day − estimated_carbon_cost_per_day`, replacing the simpler `revenue − opex` from Issue 03.
- [ ] Hover popup for fossil plants adds `Fuel cost / day` and `Carbon cost / day` $-rows.
- [ ] Hover popup for renewables adds `Fuel cost / day` and `Carbon cost / day` rows showing `$0` (so the renewable advantage is explicit, not implicit).
- [ ] The existing CO₂ row on plant popups now displays `kwh_served_yesterday`-based daily tonnage (consistent with the cost-row math).
- [ ] Unit tests cover: `plant_fuel_cost_for_tile` returns 0 when no kWh served, scales with served kWh and the catalog's `fuel_cost_per_mwh`; `plant_carbon_cost_for_tile` tracks `state.carbon_price` (regulatory-tightening events flow through immediately); renewables return 0 from both helpers.
- [ ] Integration test: a coal plant tile dict satisfies `estimated_net_per_day == estimated_revenue_per_day − opex_per_day − estimated_fuel_cost_per_day − estimated_carbon_cost_per_day` exactly.
- [ ] `make check` passes. Replay/determinism tests still pass.

## Blocked by

- Issue 03 — depends on `kwh_served_yesterday` being populated and on the existing plant revenue + Net fields.

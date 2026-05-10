---
Status: needs-triage
---

# 05 — Plants + dispatch + balance state + power revenue

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## What to build

Power generation comes online. Four plant types are buildable via `/build`: `solar_farm`, `wind_turbine`, `gas_peaker`, `coal_plant` (per brief §4.12). Plants do not require road adjacency. `world/power.py` implements the `dispatch(plants, demand_kw, prev_outputs, weather)` function from §4.4 with merit order:

1. Must-take renewables (solar + wind use available_kw from weather)
2. Coal must-run minimum (25% of capacity), then ramp upward by cost (10%/h)
3. Gas peakers ramp by cost (50%/h) up to demand

Balance state is computed from `R = supply / max(demand, 1)`:

- `R ≥ 1.15` → curtailment (excess sold at `GRID_PRICE_EXPORT = $0.04/kWh`)
- `0.95 ≤ R < 1.15` → balanced (all demand served)
- `0.70 ≤ R < 0.95` → brownout (happiness penalty)
- `R < 0.70` → blackout (`BLACKOUT_PENALTY_HOUR = $5,000` deducted, happiness penalty)

Power revenue follows **Model 2** from the PRD:

- `served_kwh × GRID_PRICE_RETAIL ($0.08)` paid to the agent for civilian + commercial + industrial loads.
- `excess_kwh × GRID_PRICE_EXPORT ($0.04)` paid to the agent for curtailed kWh.
- Process loads (refinery, injection) are unbilled — appear in slice 08+.

Population happiness is updated for blackout hours and coal-plant proximity (chebyshev distance ≤ 3 from any house). Coal proximity penalty of `0.05 × houses_within_3 / max(1, house_count)` from §4.8.

UI gains a power tab with a 24-hour supply-vs-demand line chart and a list of plants with current-output bars. The top-bar balance-state badge shows the latest hour's state.

## Acceptance criteria

- [ ] `POST /build { "tile_type": "solar_farm", ... }`, `wind_turbine`, `gas_peaker`, `coal_plant` succeed and create plants with correct capacities (150 kW, 200 kW, 500 kW, 800 kW respectively).
- [ ] Plants do not require road adjacency.
- [ ] During `/step`, hourly dispatch runs the merit order: renewables first, then coal must-run + ramp, then gas peakers.
- [ ] Coal output never exceeds previous hour's output by more than `COAL_RAMP_PER_HOUR × capacity_kw` (10%).
- [ ] Coal output never falls below `COAL_MIN_RUN × capacity_kw` (25%) when the plant is operational.
- [ ] Gas peaker output never exceeds previous hour's output by more than `GAS_RAMP_PER_HOUR × capacity_kw` (50%).
- [ ] Balance state thresholds are enforced exactly (curtailment at R≥1.15, brownout at 0.70≤R<0.95, blackout at R<0.70).
- [ ] During curtailment hours, excess kWh is sold at `GRID_PRICE_EXPORT`. The daily summary `power_revenue` includes both retail-served and export-curtailment components.
- [ ] During blackout hours, treasury is decremented by `BLACKOUT_PENALTY_HOUR` per hour. Happiness drops by 0.20 per blackout hour (clipped to [0, 1.5]).
- [ ] During brownout hours, happiness drops by `0.05 × (1 - R)`.
- [ ] Houses within chebyshev distance 3 of any coal plant reduce happiness by `0.05 × houses_within_3 / max(1, house_count)`.
- [ ] `/state.power_now.by_source_kw` reports current-hour supply by source.
- [ ] UI power tab renders a 24-hour line chart of yesterday's supply vs demand and per-plant output bars.
- [ ] Top-bar balance-state badge shows the most recent hour's state.
- [ ] Tests in `world/tests/test_dispatch.py` cover: merit order ordering, coal must-run, coal ramp limit, gas ramp limit, balance-state thresholds, blackout penalty accrual, retail+export revenue split.

## Blocked by

- 04 — Hourly clock + weather + demand formula

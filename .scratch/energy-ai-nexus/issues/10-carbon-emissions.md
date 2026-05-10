---
Status: needs-triage
---

# 10 — Carbon and emissions

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## What to build

`world/economy.py.daily_emissions_t(world)` computes total CO₂ per day from the **PRD-revised** sources:

- `Σ coal_mwh_today × COAL_CO2_T_PER_MWH` (0.90)
- `Σ gas_mwh_today × GAS_CO2_T_PER_MWH` (0.40)
- `n_industrial_tiles × INDUSTRIAL_PROCESS_CO2_T_PER_DAY` (2 t/day flat — replaces the brief's per-MWh-consumed double-counting term)
- `Σ refined_bbl × REFINERY_CO2_PER_BBL` (0.30)

Carbon cost equals `daily_emissions_t × CARBON_PRICE_USD_PER_TON` and is deducted from treasury daily. The carbon price is a tracked stateful value initialized at $25/ton; it will be mutated by regulatory tightening events in slice 11 (with a cap of 3 cumulative occurrences per game).

The daily summary exposes `co2_emitted_t` and `carbon_cost`. The state's `today_summary_so_far` accumulates these intra-day. UI: the finance tab P&L breakdown shows the carbon-cost line.

## Acceptance criteria

- [ ] Daily emissions sum across the four sources per the PRD formula. The brief's per-MWh-consumed industrial term is **not** present.
- [ ] Each industrial tile contributes exactly 2 t/day flat regardless of electricity input.
- [ ] Carbon cost = `daily_emissions_t × current_carbon_price`; deducted from treasury during `/step`.
- [ ] On `/reset`, carbon price is initialized to `CARBON_PRICE_USD_PER_TON = $25`.
- [ ] Daily summary `co2_emitted_t` matches the sum from the formula.
- [ ] Daily summary `carbon_cost` matches `co2_emitted_t × current_carbon_price`.
- [ ] `today_summary_so_far.carbon_cost` accumulates intra-day during a multi-day `/step`.
- [ ] UI finance tab shows carbon cost line in the P&L breakdown.
- [ ] Tests in `world/tests/test_economy.py` (extended) cover: no double-counting (industrial doesn't pay both for grid emissions and for itself), per-tile-day flat industrial CO₂, refinery CO₂ scales with refined bbl, carbon price reads current value.

## Blocked by

- 05 — Plants + dispatch + balance state + power revenue
- 09 — Refinery + refined-oil revenue

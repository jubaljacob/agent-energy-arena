---
Status: needs-triage
---

# 09 — Refinery + refined-oil revenue

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## What to build

`POST /build { "tile_type": "refinery", ... }` creates a refinery tile. The brief's catalog: $150,000 CAPEX, $300/day OPEX, +25 jobs, max throughput 500 bbl/day, 200 kWh/bbl power, requires road adjacency.

`POST /control/refinery { "refinery_id", "rate_bbl_day" }` sets the throughput setpoint. `world/economy.py.refine(refinery, available_crude)` returns refined bbl and updates the refinery's power and CO₂ accounting per the brief's §4.6 formulas.

Crude routing per day: the simulator computes total crude from all production wells, then routes it across refineries **preferring higher-throughput refineries first** (deterministic ordering by setpoint descending, then by id ascending to break ties). Each refinery refines up to its setpoint; surplus crude is sold raw at `CRUDE_PRICE = $40/bbl`. Refined oil is sold at `REFINED_PRICE = $90/bbl`.

The refinery's process load (`actual_throughput × REFINERY_KWH_PER_BBL / 24`) shows up in the daily demand calculation but is **unbilled** to the agent (per Model 2 in the PRD — process loads don't pay the agent). It still counts toward total demand for dispatch purposes and toward fuel-burn / carbon emissions on whichever plants serve it.

UI: the wells tab gains a refinery row with a throughput slider. Finance tab shows oil revenue split into crude-direct and refined.

## Acceptance criteria

- [ ] `POST /build { "tile_type": "refinery", ... }` deducts $150,000 and creates a refinery (requires road adjacency, rejects without).
- [ ] `POST /control/refinery` sets the throughput setpoint in [0, 500].
- [ ] Daily refining: `actual = min(setpoint, available_crude, REFINERY_MAX_BBL_DAY)`; refined output = `actual × 0.85`.
- [ ] Crude routing: with multiple refineries, higher-throughput refineries get crude first; ties broken by id ascending.
- [ ] Surplus crude (after all refineries refine to their setpoints) sells at $40/bbl.
- [ ] Refined oil sells at $90/bbl.
- [ ] Refinery's process load contributes to total demand but is unbilled (no retail revenue from refinery).
- [ ] Daily summary `oil_revenue` correctly sums crude_direct × $40 + refined × $90.
- [ ] UI wells tab displays a refinery section with throughput slider and current refined-bbl/day output.
- [ ] UI finance tab daily breakdown distinguishes crude vs. refined revenue.
- [ ] Tests in `world/tests/test_economy.py` cover: refinery yield (0.85), crude routing priority, single-refinery throughput limit, surplus crude direct-sale, no-double-billing of process load.

## Blocked by

- 07 — Production wells + crude revenue

# 05 — Refinery revenue + carbon cost popup rows

Status: needs-triage

## Parent

`.scratch/facility-economics-popup/PRD.md`

## What to build

Refineries surface yesterday's revenue from refined product and the carbon cost of refining in the hover popup, ending with a `Net / day` row. `GET /state` carries the same per-tile fields, and `GET /catalog` exposes refined price, refinery yield, and refinery CO₂ per bbl in the `economics` block. No new state fields are needed — `current_throughput_bbl_day` already exists on the refinery tile.

## Acceptance criteria

- [ ] `world.pricing` gains `refinery_revenue_for_tile(tile)` returning `current_throughput_bbl_day × refinery_yield × refined_price_usd_per_bbl`.
- [ ] `world.pricing` gains `refinery_carbon_cost_for_tile(state, tile)` returning `current_throughput_bbl_day × 0.30 × state.carbon_price`.
- [ ] `_tile_to_dict` for refinery tiles emits `estimated_revenue_per_day`, `estimated_carbon_cost_per_day`, `estimated_co2_per_day`, and `estimated_net_per_day = revenue − opex − carbon_cost`.
- [ ] `GET /catalog` `economics` block adds `refined_price_usd_per_bbl`, `refinery_yield`, and `refinery_co2_t_per_bbl`.
- [ ] Hover popup for refinery shows the existing throughput/refined-yield rows plus new `CO₂ / day`, `Carbon cost / day`, `Revenue / day`, and `Net / day` rows. Net reads from the server.
- [ ] Unit tests cover: refinery revenue uses yesterday's pinned `current_throughput_bbl_day`, scales linearly with throughput, returns 0 when throughput is 0; refinery carbon cost tracks `state.carbon_price`.
- [ ] Integration test: build a refinery + supplying production wells, step one day with non-zero throughput, assert the refinery tile dict's `estimated_revenue_per_day` matches `throughput × yield × refined_price`.
- [ ] Catalog test confirms `economics.refined_price_usd_per_bbl`, `economics.refinery_yield`, and `economics.refinery_co2_t_per_bbl` are present.
- [ ] `make check` passes. Replay/determinism tests still pass.

## Blocked by

- Issue 01 — depends on the `world.pricing` module, `_tile_to_dict(t, world)` signature, and `/catalog` economics block.

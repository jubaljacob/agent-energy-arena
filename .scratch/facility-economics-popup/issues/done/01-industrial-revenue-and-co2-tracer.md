# 01 — Industrial revenue + CO₂ visible end-to-end (tracer)

Status: needs-triage

## Parent

`.scratch/facility-economics-popup/PRD.md`

## What to build

Industrial tiles become net-positive economic actors: they generate $500/day × workforce efficiency × operational, and their existing 2 t CO₂/day emission becomes visible. The hover popup for industrial shows OPEX, CO₂ tonnes, carbon cost ($), revenue ($), and a final `Net / day` row. The finance panel on the Wells tab gets a new `Industrial revenue` line. A `GET /catalog` exposes the industrial rate in a new `economics` block, and `GET /state` carries the same per-tile economic numbers the popup displays.

This is the tracer slice that establishes the foundation other facility slices reuse: the new `world.pricing` module, the `update_civic_revenue` step in the daily routine, the `_tile_to_dict(t, world)` signature change, and the `/catalog` economics block.

## Acceptance criteria

- [ ] A new `world.pricing` module exists, containing `industrial_revenue_for_tile(tile)` and `industrial_co2_for_tile(tile)` plus the rate constants. Pure functions; no I/O.
- [ ] `world.economy.daily_emissions_t` delegates the industrial CO₂ term to `world.pricing.industrial_co2_for_tile` without changing its external return value or signature.
- [ ] `WorldState.today_summary_so_far` includes a new `industrial_revenue` key, defaulting to 0.0 on `/reset`.
- [ ] A new `update_civic_revenue(world)` function in `world.pricing` accrues industrial revenue into `today_summary_so_far["industrial_revenue"]` and credits `state.treasury`. It is called from `_advance_one_day` before `update_population`.
- [ ] `_tile_to_dict` accepts a `world` parameter and emits, for industrial tiles, the keys: `estimated_revenue_per_day`, `estimated_co2_per_day`, `estimated_carbon_cost_per_day`, `estimated_net_per_day`. Non-industrial tiles get 0.0 for all four (real values land in later slices).
- [ ] `_tile_to_dict` call sites (`/build` response and `state_dict`) are updated to pass `self`.
- [ ] `GET /catalog` payload includes a new top-level `economics` block. The block contains at least `industrial_revenue_per_day` and the current `carbon_price`. Existing `tiles`, `wells`, `subsurface` blocks are unchanged in shape.
- [ ] The industrial-tile catalog entry's `description` mentions the new revenue/CO₂ behavior.
- [ ] Hover popup for industrial in the UI shows rows: `CO₂ / day`, `Carbon cost / day`, `Revenue / day`, `Net / day`. Net is read from the server field, not computed client-side.
- [ ] Wells-tab finance panel includes a new `Industrial revenue` row that reflects `today_summary_so_far["industrial_revenue"]` after each `/step`.
- [ ] Unit tests in `world/tests/test_pricing.py` (new) cover: industrial revenue at full staffing, scaling with workforce efficiency, zero when not operational.
- [ ] Integration test confirms a single `/step` with one industrial tile credits `state.treasury` and `today_summary_so_far["industrial_revenue"]` by the expected amount.
- [ ] Catalog test confirms `economics.industrial_revenue_per_day` is present and matches the constant.
- [ ] `make check` passes (lint, format-check, mypy, full pytest).
- [ ] Existing replay/determinism tests still pass.

## Blocked by

None — can start immediately.

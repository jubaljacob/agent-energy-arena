# 02 — Commercial revenue with 5×5 chebyshev

Status: needs-triage

## Parent

`.scratch/facility-economics-popup/PRD.md`

## What to build

Commercial tiles earn daily revenue proportional to housing capacity within a 5×5 chebyshev square centered on them, scaled by city-wide occupancy and the commercial's workforce efficiency. The hover popup for commercial shows `Residents served`, `Revenue / day`, and `Net / day` rows. The finance panel gets a `Commercial revenue` line. `GET /state` exposes the same per-tile numbers, and `GET /catalog` exposes the commercial rate plus radius in the `economics` block. Civic-revenue accrual runs before population update so commercial earnings use the population that lived through today, not tomorrow's survivors.

## Acceptance criteria

- [ ] `world.pricing` gains `_occupancy_ratio(state)` (returns `min(1.0, population / max(1, total_housing_capacity))`) and `commercial_revenue_for_tile(state, tile)`.
- [ ] Commercial revenue sums `housing_capacity` over every tile within chebyshev distance ≤ 2 (rule: `tile.housing_capacity > 0`, so town hall counts), multiplies by occupancy × `$1.00/resident/day` × `workforce.efficiency(tile)`, and returns 0 when `not tile.operational`.
- [ ] Overlapping commercials independently full-count residents (v1 convention; documented in the helper docstring).
- [ ] `WorldState.today_summary_so_far` includes `commercial_revenue`, defaulting to 0.0.
- [ ] `update_civic_revenue(world)` extended to accrue commercial revenue alongside industrial. The civic call's position in `_advance_one_day` is immediately before `update_population` — explicitly pinned by a test.
- [ ] `_tile_to_dict` for commercial tiles emits `residents_in_radius` (raw capacity × occupancy as a float) plus the existing economic fields (`estimated_revenue_per_day`, `estimated_net_per_day` reflect commercial earnings).
- [ ] `GET /catalog` `economics` block adds `commercial_revenue_per_resident_per_day` and `commercial_radius`.
- [ ] Commercial catalog entry's `description` mentions the new "earns ~$1/resident/day from houses within 5×5" behavior.
- [ ] Hover popup for commercial shows `Residents served`, `Revenue / day`, `Net / day` rows. Net comes from the server.
- [ ] Wells-tab finance panel includes a `Commercial revenue` row reading `today_summary_so_far["commercial_revenue"]`.
- [ ] Unit tests cover: zero when no houses in radius, sums housing in 5×5 chebyshev (inside vs outside boundary), clips at grid edges (commercial at (0,0)), scales with occupancy ratio, scales with workforce efficiency, zero when not operational, includes town-hall capacity, overlapping commercials double-count.
- [ ] Order-pinning test: civic revenue uses pre-update population, not post-update. Construct a state where `update_population` would change population materially and assert commercial revenue used the pre-update value.
- [ ] Integration test: build commercials adjacent to houses, `/step` once, confirm `today_summary_so_far["commercial_revenue"]` and `state.treasury` reflect the expected amount.
- [ ] Catalog test confirms `economics.commercial_revenue_per_resident_per_day` and `economics.commercial_radius` are present.
- [ ] `make check` passes. Replay/determinism tests still pass.

## Blocked by

- Issue 01 — depends on the `world.pricing` module, `update_civic_revenue` step, `_tile_to_dict(t, world)` signature, and `/catalog` economics block.

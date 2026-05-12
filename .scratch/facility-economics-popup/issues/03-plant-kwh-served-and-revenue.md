# 03 — Plant kwh_served accumulator + per-plant revenue and Net

Status: needs-triage

## Parent

`.scratch/facility-economics-popup/PRD.md`

## What to build

Plants gain a daily kWh-served accumulator so per-plant revenue can be computed from yesterday's actual served energy (not from the last-hour snapshot × 24). The hover popup for every plant type — solar, wind, coal, gas peaker — shows an honest `Revenue / day (est.)` row and a `Net / day` row. Renewables show a positive Net (revenue − OPEX) at a glance; fossil plants' Net in this slice is still `revenue − OPEX` and is intentionally **overstated** until Issue 04 lands the fuel/carbon cost rows. `GET /state` carries the same numbers, and `GET /catalog` exposes the grid retail price in the `economics` block.

## Acceptance criteria

- [ ] `Tile` dataclass gains two new fields: `kwh_served_today: float = 0.0` and `kwh_served_yesterday: float = 0.0`.
- [ ] The hourly dispatch loop in `_advance_one_day` accumulates each plant tile's output into `kwh_served_today` (one line per plant per hour).
- [ ] At the start of each day, `kwh_served_today` is reset to 0 on every plant tile.
- [ ] At end-of-day (after dispatch, before population update), `kwh_served_today` is copied to `kwh_served_yesterday` on every plant tile.
- [ ] `world.pricing` gains `plant_revenue_for_tile(state, tile, spec)` returning `kwh_served_yesterday × config.grid_price_retail`.
- [ ] `_tile_to_dict` for plants (solar/wind/coal/gas) emits `estimated_revenue_per_day` using the new helper and `estimated_net_per_day` as `revenue − opex_per_day` (fuel/carbon land in Issue 04). All other plant fields (`current_output_kw`, `capacity_kw`) keep their existing semantics.
- [ ] `GET /catalog` `economics` block adds `grid_price_retail` and `grid_price_export`.
- [ ] Hover popup for each plant type shows a `Revenue / day (est.)` row (labelled "(est.)") and a `Net / day` row reading server-stamped values.
- [ ] Renewables (solar, wind) display $0 fuel and $0 carbon implicitly (those rows land in Issue 04; for now they have no fuel/carbon row at all).
- [ ] Unit tests cover: `kwh_served_today` accumulates across hours, resets at start of each day, and is copied to `kwh_served_yesterday` at end of day; `plant_revenue_for_tile` returns yesterday's actual × retail price; a freshly-built plant has 0 revenue until the next `/step`.
- [ ] Integration test: build a coal plant, step a day with non-zero dispatch, assert the tile dict's `estimated_revenue_per_day` matches `kwh_served_yesterday × grid_price_retail`.
- [ ] Catalog test confirms `economics.grid_price_retail` and `economics.grid_price_export` are present.
- [ ] `make check` passes. Replay/determinism tests still pass.

## Blocked by

- Issue 01 — depends on the `world.pricing` module, `_tile_to_dict(t, world)` signature, and `/catalog` economics block.

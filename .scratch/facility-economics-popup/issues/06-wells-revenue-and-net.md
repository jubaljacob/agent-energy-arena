# 06 — Wells revenue + Net rows

Status: needs-triage

## Parent

`.scratch/facility-economics-popup/PRD.md`

## What to build

Production wells show estimated daily gross crude value and Net in the hover popup. Injection wells show daily power consumed (kWh, informational, not a $-cost because power is internalized through your own plants) and Net. `GET /state` carries the same fields on each well. `GET /catalog` exposes the crude price and the injection kWh-per-bbl constant in the `economics` block. The `_well_to_dict` signature changes to take `world` to mirror `_tile_to_dict`.

## Acceptance criteria

- [ ] `world.pricing` gains `well_gross_crude_value_for_tile(well)` returning `current_rate_bbl_day × crude_price_usd_per_bbl` for production wells, 0 for injection.
- [ ] `world.pricing` gains `well_injection_kwh_per_day(well)` returning `current_rate_bbl_day × injection_kwh_per_bbl` for injection wells, 0 for production.
- [ ] `_well_to_dict` accepts a `world` parameter. Both call sites (`/drill` response and `state_dict`) are updated to pass `self`.
- [ ] `_well_to_dict` emits `estimated_revenue_per_day`, `injection_power_kwh_per_day`, and `estimated_net_per_day` on every well dict.
- [ ] Production wells: `estimated_net_per_day = estimated_revenue_per_day − opex_per_day`.
- [ ] Injection wells: `estimated_net_per_day = −opex_per_day` (no $-cost from power consumption — that cost is internalized through plants).
- [ ] `GET /catalog` `economics` block adds `crude_price_usd_per_bbl` and `injection_kwh_per_bbl`.
- [ ] Hover popup for production wells shows `Gross crude value (est.) / day` and `Net / day` rows.
- [ ] Hover popup for injection wells shows `Power consumed / day` (in kWh, informational) and `Net / day` rows.
- [ ] Unit tests cover: production well gross value uses `current_rate_bbl_day` and the catalog crude price; injection well returns 0 from the revenue helper; injection kWh helper scales with rate; both helpers return 0 when the well type doesn't match.
- [ ] Integration test: drill a production well, set non-zero setpoint, `/step` once, assert the well dict's `estimated_revenue_per_day` matches `current_rate_bbl_day × crude_price`.
- [ ] Catalog test confirms `economics.crude_price_usd_per_bbl` and `economics.injection_kwh_per_bbl` are present.
- [ ] `make check` passes. Replay/determinism tests still pass.

## Blocked by

- Issue 01 — depends on the `world.pricing` module and `/catalog` economics block. Inherits the serializer-signature-change pattern but applies it to `_well_to_dict` (a different code path).

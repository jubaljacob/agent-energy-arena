---
Status: needs-triage
---

# 05 — Demand-side scales with efficiency (commercial + industrial + flat CO2)

## Parent

[PRD: World v2 — Workforce & Per-Facility Staffing](../PRD.md)

## What to build

Commercial and industrial tile demand multiplies by `workforce.efficiency(t)`. The industrial tile's flat 2 t/day CO2 process emission also multiplies by efficiency, so an idle factory has a zero footprint (no demand, no emissions).

This is the slice that finally rewrites the existing `test_industrial_pays_flat_co2_even_when_no_grid` test (the only existing test the PRD calls out as needing manual migration).

### Implementation details

**`world/power.py` — `total_demand_kw(state, h)`**:

The function aggregates several contributions at `world/power.py:114`:

- Residential: `residential_kw(h, pop)` — unchanged; depends on population, not staffing.
- Commercial: `_commercial_peak_kw(state) × commercial_factor(h)` at `world/power.py:102`. Inside `_commercial_peak_kw`, the sum is over commercial tiles of `t.demand_kw`. Change to `sum(t.demand_kw × workforce.efficiency(t) for t in commercial_tiles)`.
- Industrial: `_industrial_kw(state)` at `world/power.py:98`. Currently sums `t.demand_kw` over industrial tiles. Change to `sum(t.demand_kw × workforce.efficiency(t) for t in industrial_tiles)`. Industrial demand is continuous (no hourly factor), so the only adjustment is the efficiency multiplier.
- Process loads (refinery + injection): handled in slices 06 and 07 respectively. **Do not** modify `_process_loads_kw` in this slice — the per-producer scaling for refinery and injection wells belongs to their own slices.
- Heatwave / demand_surprise multipliers: unchanged, they multiply the aggregate.

Note that `Tile.demand_kw` on each tile is a stored snapshot of `spec.demand_kw` from build time (mirrors how `capex_paid` and `opex_per_day` are snapshotted on the tile). Read the per-tile value, not the catalog, so retunes do not affect existing tiles. Efficiency is computed live against the catalog `spec.jobs`.

**`world/economy.py` — `daily_emissions_t(world)`**:

The flat industrial CO2 contribution (2 t/day per industrial tile) lives in this function. Change the industrial-flat-CO2 sum from `2.0 × industrial_count` to `sum(2.0 × workforce.efficiency(t) for t in industrial_tiles)`. An idle industrial tile contributes 0 t/day; a half-staffed industrial contributes 1.0 t/day.

The grid-derived industrial CO2 (the part that follows from the kWh consumed by industrial tiles routed through fossil plants) is automatic — `total_demand_kw` already scales by efficiency, so the dispatched kWh and the per-MWh CO2 cascade through.

### Test migration

**Rewrite `world/tests/test_economy.py:622` — `test_industrial_pays_flat_co2_even_when_no_grid`**:

The current test asserts an industrial tile emits 2 t/day flat when `pop=0`. Under the uniform efficiency rule, an industrial with `staffed_jobs=0` emits 0 t/day. The test's *real* intent is "the flat term is independent of grid dispatch" — that the 2 t/day arrives regardless of whether the industrial's electricity demand was served, blacked out, or curtailed.

Rewrite: keep `pop=0` so residential demand is 0; **manually staff** the industrial to full (`staffed_jobs=30`) via the helper override; assert the daily CO2 contribution includes `2.0 t/day` from that industrial regardless of whether any coal plant exists. Add a second assertion that at `staffed_jobs=15` the contribution is `1.0 t/day`. Add a third that at `staffed_jobs=0` it is `0.0 t/day`.

### Tests to add in this slice

`world/tests/test_demand.py`:

- **Idle commercial draws zero demand**: inject a commercial tile (`demand_kw=50`) with `staffed_jobs=0`. At each hour 0–23, `_commercial_peak_kw` returns 0 (the only commercial tile contributes 0). `total_demand_kw` reflects this (no commercial bump in the 8–20h window).
- **Half-staffed commercial draws half peak**: commercial with `staffed_jobs=6` (jobs=12, efficiency=0.5). At hour 12 (peak): commercial contribution = `25 kW × commercial_factor(12) = 25 × 1.0 = 25 kW` (half of catalog 50). At hour 22 (off-peak): contribution = `25 × 0.2 = 5 kW`.
- **Idle industrial draws zero demand**: industrial with `staffed_jobs=0`. `_industrial_kw(state)` returns 0. `total_demand_kw` has no industrial contribution at any hour.
- **Half-staffed industrial draws half**: industrial `staffed_jobs=15`, `_industrial_kw` returns 150 kW (half of 300).

`world/tests/test_economy.py`:

- **Rewritten `test_industrial_pays_flat_co2_even_when_no_grid`** as described above.
- **Idle industrial emits zero flat CO2**: build/inject an industrial with `staffed_jobs=0`. `daily_emissions_t(world)` returns 0 for the industrial contribution; if no other CO2 source exists, total daily emissions = 0.
- **Half-staffed industrial emits half flat CO2**: `staffed_jobs=15` → contributes 1.0 t/day.

## Acceptance criteria

- [ ] `_commercial_peak_kw(state)` sums `t.demand_kw × workforce.efficiency(t)` over commercial tiles.
- [ ] `_industrial_kw(state)` sums `t.demand_kw × workforce.efficiency(t)` over industrial tiles.
- [ ] `daily_emissions_t(world)` scales the flat industrial 2 t/day term by `workforce.efficiency(t)` per industrial tile.
- [ ] `test_industrial_pays_flat_co2_even_when_no_grid` is rewritten to manually staff the tile and assert the flat term scales with efficiency.
- [ ] New tests cover idle / half-staffed / fully-staffed commercial demand, industrial demand, and industrial flat CO2.
- [ ] If slice 03 (forecast snapshot) gated a test on this slice landing, that gate is removed and the test passes.
- [ ] `make check` is green.

## Blocked by

- 01 — Workforce foundation (provides `workforce.efficiency` and `staffed_jobs` field)

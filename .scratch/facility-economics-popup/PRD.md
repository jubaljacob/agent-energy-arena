# PRD: Per-facility daily economics in popup + API parity

Status: needs-triage

## Problem Statement

A player hovering a built tile or well in the map UI sees physical state (OPEX, jobs, demand kW, CO₂ intensity) but cannot answer the only question that matters at build time: **"is this facility net-positive, and by how much per day?"** Worse:

- Commercial tiles currently earn **zero revenue** — they exist as pure $50/day cost sinks. A player asks "why would I ever build a shop?" and the answer is "you wouldn't."
- Industrial tiles emit 2 t CO₂/day (already real, via `daily_emissions_t`) for zero revenue. A player has no reason to build them. The CO₂ is invisible in the popup, so the player doesn't even know they're emitting.
- Plants, refineries, and wells generate revenue end-of-day, but the player can't attribute revenue to a specific tile from the UI or from the API. `today_summary_so_far` only carries aggregates (`power_revenue`, `crude_revenue`, `refined_revenue`).

The popup added in the previous session promised "economic impact" but only delivered cost components. The promise needs to be kept: every revenue-bearing facility must show what it earns, what it costs, and net-of-everything. Both the human UI and any agent calling the API must see the same per-facility numbers.

## Solution

Compute per-tile and per-well daily economics server-side, stamp the values on every facility in `/state` (and `/build` and `/drill` responses), and render them in the hover popup. Introduce two new civilian revenue streams (commercial earnings, industrial output) and expose all pricing constants through `/catalog` so API consumers can replicate the math.

Every facility popup ends with a single **Net / day** row, server-computed as `revenue − opex − fuel_cost − carbon_cost`, color-coded positive or negative. Every component contributing to Net appears as its own labelled row above it, so the math reconciles by eye.

Commercial revenue is proportional to housing capacity within a 5×5 chebyshev square centered on the commercial tile, scaled by city-wide occupancy ratio and the commercial's workforce efficiency. Industrial revenue is a flat daily rate scaled by efficiency. Both rates are calibrated against oil-asset economics (oil remains the strategic windfall at ~100× the per-tile profitability of civilian assets).

## User Stories

1. As a player, I want to hover any built tile and see its estimated daily revenue, so I can tell whether building it was a good decision without doing arithmetic in my head.
2. As a player, I want to see industrial tiles' CO₂ emissions surfaced in the popup, so I understand that "industrial" is not free — it carries a carbon cost that scales with the carbon price.
3. As a player, I want commercial tiles to earn money from nearby housing, so building shops near residential blocks is a strategy rather than a tax on me.
4. As a player, I want commercial revenue to fall when nearby houses are unoccupied, so a depopulating city stops paying commercial rent it doesn't earn.
5. As a player, I want commercial revenue to fall when the commercial tile is understaffed, so workforce shortages have visible economic consequences.
6. As a player, I want industrial tiles to earn money proportional to their staffing level, so unemployment hurts industrial output the same way it hurts other staffed facilities.
7. As a player, I want a **Net / day** row on every facility popup, so a single number answers "is this tile making me money right now?"
8. As a player, I want fossil plants to show explicit `Fuel cost / day` and `Carbon cost / day` dollar rows, so I can audit Net by adding up the components I see.
9. As a player, I want renewable plants to show $0 fuel and $0 carbon explicitly, so the contrast with fossils is visible at a glance.
10. As a player, I want production wells to show estimated gross crude revenue in the popup, so I see why drilling dwarfs civilian assets — and which wells are pulling their weight.
11. As a player, I want injection wells to show daily power consumption (in kWh), so the hidden cost of pressure maintenance is visible.
12. As a player, I want refineries to show daily revenue from refined oil and the carbon cost of refining, so the refinery decision (build vs. sell crude direct) is informed.
13. As a player, I want the finance panel under the Wells tab to include line items for **Commercial revenue** and **Industrial revenue**, so daily aggregates match per-tile sums.
14. As a player, I want the build menu's tile descriptions to mention the new revenue and CO₂ behaviors, so I understand the trade-offs before I place a tile, not only after hovering it.
15. As an API agent, I want every economic value visible in the popup also present in the `/state` response, so my agent can replicate the player's view without scraping the UI.
16. As an API agent, I want the pricing constants (commercial $/resident/day, industrial $/day, refinery $/bbl, grid retail price) exposed in `/catalog`, so I can compute marginal revenue for hypothetical placements without round-tripping `/build`.
17. As an API agent, I want the `/build` response for a new tile to include the same economic fields as `/state`, so I can validate a placement decision atomically.
18. As an API agent, I want each well's `/drill` response to include estimated revenue fields, so I can score a drill candidate using the same numbers a human would see.
19. As an API agent, I want today's `commercial_revenue` and `industrial_revenue` aggregates in `today_summary_so_far`, so my reward signal can use them without recomputing.
20. As an API agent, I want the per-tile `estimated_net_per_day` to be reproducible from `(revenue, opex, fuel_cost, carbon_cost)` rows in the same response, so I can verify the server-side math is self-consistent.
21. As a developer, I want all new revenue/cost helpers in a new pure module that takes only `state` and a tile, so the unit tests don't need a full `World`.
22. As a developer, I want commercial-revenue tests to cover boundary clipping (commercial at grid edges), zero-housing isolation, occupancy scaling, efficiency scaling, operational gating, and town-hall inclusion as explicit independent cases.
23. As a developer, I want a test pinning the order of operations (civic revenue must accrue **before** population update), so a future refactor that swaps the order is caught by CI rather than by a player noticing tomorrow's residents shopped today.
24. As a developer, I want a test that confirms `/catalog` exposes the new rate constants, so the API contract for agent consumers is locked.
25. As a developer, I want a full end-to-end test that calls `world.step()` and asserts both `state.treasury` and `today_summary_so_far` reflect the new revenue streams.
26. As a maintainer, I want the existing `world.economy.daily_emissions_t` aggregator to keep working unchanged externally, so the determinism contract held by `tests/test_replay.py` is not broken.
27. As a maintainer, I want `baselines/seed_42.json` regenerated in a separate atomic commit, so a bisect can isolate the scoring shift from the feature commit.

## Implementation Decisions

### Modules

- **New module `world.pricing`** — pure helpers, takes `state` (and optionally `config`/`carbon_price`) plus a tile/well, returns floats. No I/O, no RNG. Houses: occupancy ratio, commercial revenue per tile, industrial revenue per tile, plant fuel cost per tile, plant carbon cost per tile, refinery carbon cost per tile, well gross-crude value per tile, well injection-energy kWh per tile, net-per-day computation. Also houses the new constants (`COMMERCIAL_RADIUS`, `COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY`, `INDUSTRIAL_REVENUE_PER_DAY`) so the pricing surface lives in one place.
- **`world.economy`** — left structurally untouched. Its `daily_emissions_t` still owns the aggregate CO₂ sum. Where the aggregator inlines the industrial CO₂ formula, it now calls a thin pricing helper (`industrial_co2_for_tile`) to keep DRY without moving the aggregator.
- **`world.state`** — schema extension. Two new fields on `Tile` (`kwh_served_today`, `kwh_served_yesterday`). Two new keys (`commercial_revenue`, `industrial_revenue`) in the `today_summary_so_far` default-dict factory.
- **`world.sim`** — orchestration changes only. Per-hour dispatch loop accumulates `kwh_served_today` on each plant tile. `_advance_one_day` resets `kwh_served_today` at the start of the day, copies to `kwh_served_yesterday` at end-of-day, and calls `update_civic_revenue(self)` immediately before `update_population(self)`. The tile/well serializers (`_tile_to_dict`, `_well_to_dict`) gain a `world` parameter and emit the new economic fields.
- **`world.catalog`** — `build_catalog()` payload gains an `economics` block exposing every constant the popup uses to compute Net: commercial rate + radius, industrial rate, refinery yield + refined price + refinery CO₂ per bbl, crude price, grid retail/export prices, injection kWh/bbl, fuel cost per MWh and CO₂ intensity for each fossil plant type (already present on tile entries), and current carbon price.
- **`world/ui/app.js`** — `buildTilePopup` and `buildWellPopup` read the new server-stamped fields and render them. No client-side math. `renderFinance` gains two rows. No new code paths in the canvas/render loop.

### Interfaces and contracts

- `_tile_to_dict(tile, world)` returns the existing dict plus: `estimated_revenue_per_day`, `estimated_co2_per_day`, `estimated_fuel_cost_per_day`, `estimated_carbon_cost_per_day`, `estimated_net_per_day`. For commercial tiles only, also `residents_in_radius`.
- `_well_to_dict(well, world)` returns the existing dict plus: `estimated_revenue_per_day`, `injection_power_kwh_per_day`, `estimated_net_per_day`. Injection wells get `estimated_revenue_per_day = 0` and `estimated_net_per_day = −opex`.
- `today_summary_so_far` adds two keys; existing keys are unchanged in name and semantics.
- `/catalog` payload gains an `economics` block; the existing `tiles`, `wells`, and `subsurface` blocks keep their current shape — this is purely additive so existing agents are unaffected.

### Pricing decisions (calibrated against oil)

- Commercial rate: $1.00/resident/day × workforce efficiency × occupancy ratio. Town hall counts as a housing source (`tile.housing_capacity > 0`).
- Industrial rate: flat $500/day × workforce efficiency. CO₂ stays at the existing 2.0 t/day × efficiency.
- Plant revenue: `kwh_served_yesterday × grid_retail_price` (honest yesterday's actual, not last-hour × 24).
- Refinery revenue: `current_throughput_bbl_day × refinery_yield × refined_price_per_bbl`. Refinery has no fuel cost; its carbon cost is `throughput × 0.30 × carbon_price`.
- Wells: production `current_rate_bbl_day × $50/bbl` (conservative; refining can lift this). Injection: 0 revenue, daily power load shown in kWh.
- Overlapping commercials double-count residents in v1.
- Civic revenue runs before `update_population` so commercial earnings use today's lived-population, not tomorrow's survivors.

### Order of operations in `_advance_one_day`

1. Reset `today_summary_so_far` and per-tile `kwh_served_today`.
2. Roll events.
3. Hourly dispatch (accumulates `kwh_served_today` per plant tile).
4. OPEX, fuel cost, power revenue, oil revenue, carbon accounting (unchanged).
5. Copy `kwh_served_today → kwh_served_yesterday` on each plant tile.
6. **`update_civic_revenue(world)` — new step.**
7. `update_population(world)` (includes tax accrual).
8. Day increment.

### API parity (new requirement explicit)

Every value the popup reads comes from a tile or well dict or from `today_summary_so_far`. No value is computed only on the client. Rate constants the popup might want to display (e.g., `$X/resident`) are surfaced through `/catalog`. An agent that GETs `/state` + `/catalog` can reproduce every popup row, including the Net computation, byte-for-byte.

### Schema additions summary

- `Tile.kwh_served_today: float = 0.0`
- `Tile.kwh_served_yesterday: float = 0.0`
- `today_summary_so_far["commercial_revenue"]: float = 0.0`
- `today_summary_so_far["industrial_revenue"]: float = 0.0`
- Per-tile dict: 5 new keys (6 for commercial).
- Per-well dict: 3 new keys.
- `/catalog`: 1 new top-level `economics` block.

## Testing Decisions

Tests assert external behavior — what the public functions and endpoints return given a constructed state — not implementation details. No mocking of internal collaborators; the pricing module is pure so tests pass in real `WorldState` objects.

### Test areas

- **`world.pricing` helpers (unit, pattern: `world/tests/test_economy.py`)**
  - Commercial: zero-when-no-houses, sums-housing-in-5×5-chebyshev, clips-at-grid-boundary, scales-with-occupancy-ratio, scales-with-workforce-efficiency, zero-when-not-operational, includes-town-hall-capacity, overlapping-commercials-double-count.
  - Industrial: flat-rate-at-full-staffing, scales-with-efficiency, zero-when-not-operational.
  - Plant fuel/carbon: cost-is-zero-when-no-kwh-served, fuel-cost-uses-yesterday-not-today, carbon-cost-tracks-carbon-price.
  - Refinery: carbon-cost-from-throughput-bbl.
  - Wells: production-gross-uses-current-rate-and-crude-price, injection-energy-kwh-scales-with-rate.

- **Sim ordering + serializers (integration, pattern: `world/tests/test_sim.py`)**
  - `test_civic_revenue_accrues_before_population_update` — construct a state where population would change mid-day; assert commercial revenue used pre-update population.
  - `test_plant_kwh_served_accumulates_across_hours_and_resets_each_day`.
  - `test_tile_to_dict_includes_all_estimated_fields`.
  - `test_well_to_dict_includes_all_estimated_fields`.
  - `test_net_per_day_reconciles_with_component_rows` — for a coal plant, assert `net == revenue − opex − fuel − carbon` exactly.

- **`world.catalog` payload (unit, pattern: `world/tests/test_catalog.py` if present, else direct call test)**
  - `test_catalog_exposes_economics_block`.
  - `test_catalog_economics_contains_commercial_rate_and_radius`.
  - `test_catalog_economics_contains_industrial_rate`.
  - `test_catalog_economics_contains_grid_prices_and_crude_price`.

- **End-to-end `/step` accrual (integration, pattern: `world/tests/test_economy.py`'s end-to-end style)**
  - `test_step_accrues_commercial_revenue_into_summary_and_treasury` — build commercials near houses, step one day, assert `today_summary_so_far["commercial_revenue"] > 0` and treasury reflects it.
  - `test_step_accrues_industrial_revenue_into_summary_and_treasury` — same shape, industrial tile.
  - `test_step_unchanged_when_no_commercial_or_industrial` — regression guard: existing baseline-style cities keep their pre-feature behavior modulo the two new zero buckets.

What makes a good test in this codebase:
- Construct `WorldState` directly via dataclass instances; bypass `/build` road-adjacency checks.
- Read return values, not internal attributes.
- Pin order-of-operations with explicit assertions, not by reading log output.
- Use the existing `_refinery_tile`-style fixtures in `test_economy.py` for setup boilerplate.

## Out of Scope

- **Per-plant historical revenue series**: the new `kwh_served_yesterday` is a one-day window. No multi-day aggregate per tile. Lifetime-cumulative kWh per plant is deferred.
- **Per-well refining attribution**: a production well's revenue display uses the conservative direct-crude price ($50/bbl). The actual refined-yield lift is not redistributed back to source wells.
- **Split-coverage commercial revenue**: overlapping commercials double-count v1. A divisor-by-coverage pass is deferred unless balance breaks.
- **Industrial revenue coupling to power supply**: industrial revenue is flat × efficiency. It does *not* drop during brownouts. The brownout penalty already discourages outages via the existing path.
- **Demand-side coupling for commercial**: commercial revenue uses housing capacity (not actual purchasing trips). No "served-by-power" gate; an unpowered commercial still earns nominally (it has 0 power demand if the grid blacks out, but the revenue model is decoupled).
- **Build-palette revenue preview math**: the build menu shows textual descriptions only ("earns ~$1/resident/day"). It does not preview the exact revenue at a hovered grid cell pre-build.
- **Scoring formula changes**: the scoring formula remains as-is; only the deterministic `t_ref` in `baselines/seed_42.json` shifts because treasury accrues new revenue.

## Further Notes

- ADR check: no ADRs in `docs/adr/` (directory does not exist). No conflicts to flag.
- Glossary: existing terms (`tile`, `well`, `OPEX`, `dispatch`, `today_summary_so_far`, `workforce efficiency`) are reused verbatim. The new term **"Net / day"** is introduced — short for "estimated net contribution per day at current operating state."
- Baseline regen workflow: after merging the feature commit, run `make score` on seed 42, read `(population, treasury)` from the resulting JSON, update `baselines/seed_42.json`, and commit as an atomic follow-up so `git bisect` can isolate the scoring shift.
- Determinism: all new logic is deterministic. No RNG draws are introduced. The existing replay test must continue to pass.
- Mypy ripple: changing `_tile_to_dict(t)` to `_tile_to_dict(t, world)` (and similarly for wells) is a typed signature change; both call sites and the `state_dict` aggregator need updating in the same commit.
- Carbon price coupling: per-tile carbon-cost rows read `state.carbon_price` at compute time, so regulatory-tightening events automatically reflect in the Net row the same day they fire.

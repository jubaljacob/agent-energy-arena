# TODO

Gaps and follow-ups noticed during play/diagnosis. Group by surface so a contributor can grab one cluster at a time.

## Batteries

The battery exists end-to-end in the sim/catalog/API, but several telemetry and control hooks are missing from the player- and agent-facing surfaces.

- **No manual setpoint widget in UI.** `POST /control/battery` is documented but the UI has no slider/toggle. Wells and refineries got control rows in the Wells tab — batteries deserve the same. Suggested: a "Batteries" table on the Wells tab (or a new "Storage" tab) with rows for SoC bar, mode toggle (`auto` / `charge` / `discharge`), and a kW slider.
- **No per-hour charge/discharge rate stamped on the tile.** `current_output_kw` is always 0 for batteries because dispatch applies the result to bus supply, not to the tile. Result: the plant list and popup can't show "currently charging at 120 kW" — only the static SoC. Suggested: stamp `current_charge_kw` and `current_discharge_kw` on the tile in `world/sim.py` after `battery_charge_step` / `battery_discharge_step`.
- **No SoC trajectory in `next_24h_preview`.** `world/preview.py` projects supply/demand by hour but not battery SoC. Agents and the chart can't anticipate when storage will fill or drain. Suggested: add `battery_soc_kwh_by_hour: list[float]` (aggregated) and per-battery in a sibling field.
- **Catalog description over-claims.** "Grid-scale battery" at 800 kWh is C&I scale (~1/5 of a Tesla Megapack). Rename in `world/catalog.py` to "Utility battery" / "Community battery" to match the rest of the small-town framing.
- **Steady-state arbitrage is negative.** $40/day opex vs ~$27/day best-case arbitrage at the current retail/export spread means batteries only earn their keep on outage avoidance and scoring. If that's intentional, leave it; if not, drop opex to ~$10/day, drop capex to ~$30k, or widen the retail/export spread.

## Telemetry / API

- **No `by_source_kw_by_hour` in `last_day_*`.** `last_day_supply_kw_by_hour` and `last_day_demand_kw_by_hour` exist but the per-source breakdown is only on `power_now` (instantaneous) and `next_24h_preview.by_source_kw_by_hour` (forecast). Yesterday's source mix is invisible. Add `last_day_by_source_kw_by_hour: {solar, wind, coal, gas}` so agents can audit the realized mix.
- **No daily P&L breakdown stream.** `today_summary_so_far` is per-day and gets overwritten. A `last_day_summary` mirror (and history persisted to `states.jsonl`) would let agents and the UI plot revenue/opex/fuel/carbon over time without scraping `states.jsonl`.
- **Population float not surfaced.** `state.population` is a float internally but `/state` casts to int, so growth between integer ticks is invisible. Expose `population_float` (or `population_fractional_day`) for diagnosis when growth stalls — would have saved an hour of grepping today.
- **No "happiness contributions" breakdown.** `state.happiness` is a scalar but is built from park benefit / noise / blackout hours / coal proximity. Surface the components on `/state` (`happiness_breakdown: {park: +0.10, noise: -0.05, ...}`) so players can see which terms are dragging.
- **No `growth_rate_per_day` derived field.** `happiness_velocity` is computable client-side from existing fields but it's the easiest way to diagnose stalled growth (today's session); a server-stamped value would shortcut that.

## UI

- **No daily Net widget in topbar.** Treasury is shown but day-over-day delta (the actual "am I making money" answer) lives in the Wells tab finance list. Add a topbar "Net/day" metric, or at least a sparkline next to treasury.
- **No renewable-share badge.** Score-relevant but only visible via `/score`. Add to the topbar or the Supply panel.
- **No CO₂/day badge.** Same as renewable-share — relevant to scoring + scenarios, only visible per-facility today.
- **Plant tooltip "Current output 0 kW" for batteries was misleading** — now suppressed, but the underlying cause is the missing per-hour charge/discharge stamp (see Batteries section).
- **No build palette tooltip showing job/housing/demand impact.** A "this tile adds +12 jobs" preview on hover (before clicking) would help new players plan the city without opening the catalog endpoint by hand.
- **`hover-popup` is for tiles only, not for empty cells.** Hovering an empty cell could show "buildable" / "needs road adjacency" / "out of bounds for current build mode" — would prevent the trial-and-error of dropping a tile and seeing `no_road_adjacency` come back.
- **Replay UI doesn't render forecast/preview.** Recorded states include `next_24h_preview`, but in replay mode the projection lines may go stale or differ from what was shown live. Worth verifying.

## Scenarios / Events

- **Scenario detach doesn't surface in `/state.active_events`.** A scenario can inject events; detaching reattaches `NullScenario` but there's no obvious record. Add `last_scenario_change_day` or similar.
- **No scenario library doc in-UI.** The events tab has a free-text "dotted_path" field, but the player has to know which scenarios exist. Pull the list from `scenarios/` and offer a dropdown.

## Docs

- **`GET /state/history?day=N` is implemented but undocumented.** Lives in `world/api.py:140` and powers the UI's "peek backward" mode, but not in `API.md`'s endpoint index. Document the shape (404 on missing day, 404 on no recorder) so agent authors can use it.

## Domain docs

- **`CONTEXT.md` / `docs/adr/` should reflect the battery role.** The current model (renewable-only charging, post-gas discharge, RTE accounting) is a design decision worth ADR'ing so future contributors don't "fix" it by letting batteries charge from coal.

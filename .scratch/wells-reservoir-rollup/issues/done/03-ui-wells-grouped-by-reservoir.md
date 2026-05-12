---
Status: needs-triage
---

# UI: wells table grouped by reservoir + Unaffiliated group

## Parent

`.scratch/wells-reservoir-rollup/PRD.md`

## What to build

Rewrite the Wells-tab table in `world/ui/app.js:renderWells()` so it presents wells grouped by `reservoir_id` instead of as a flat list. The grouping is driven by the new `obs.reservoirs_summary` array from `/state` (added in #01).

For each entry in `reservoirs_summary` (ascending `reservoir_id`):

- Emit a group-header row spanning the table width. The header displays the reservoir id, estimated bbl, remaining bbl, revealed-voxel count, and well-type counts. Example text: `Reservoir R3 — est 8.2M bbl · remaining 7.1M · 42 revealed vox · 2P + 1I`.
- Under the header, emit the producers in this reservoir first (ascending well id), then the injectors (ascending well id). Wells are looked up by id in the existing `wells` array.
- If a reservoir has no wells, the header still renders, followed by no rows (or a single italicized placeholder row reading "no wells — drill here?").

After all reservoir groups, if any wells have `reservoir_id == null` (drilled into rock), emit an "Unaffiliated (drilled into rock)" group at the bottom with those wells listed beneath. The Unaffiliated group is hidden when there are zero null-reservoir wells.

Existing row content (id, type, coordinate, setpoint slider, current rate, cumulative bbl) is preserved. The wells-stats summary line above the table keeps its current behaviour (counts across all wells, including unaffiliated).

This slice does NOT add new columns to the rows themselves — that's #04.

## Acceptance criteria

- [ ] `renderWells()` reads `obs.reservoirs_summary` and emits one group section per entry, in ascending `reservoir_id` order.
- [ ] Each group section starts with a header row (CSS class `reservoir-group-header` or equivalent) that spans the table width and shows `R{id}`, estimated bbl, remaining bbl, revealed-voxel count, and well-type counts (e.g. `2P + 1I`).
- [ ] Within each group, producers appear first (ascending well id), then injectors (ascending well id). Looked up by id from the existing `wells` array.
- [ ] A reservoir with no wells in it still gets its group header; the body shows either no rows or a single italicized "no wells" placeholder.
- [ ] An "Unaffiliated" group appears at the bottom containing all wells with `reservoir_id == null` (or missing). When there are zero such wells, the group is omitted entirely.
- [ ] Orphan-well badges (existing) still render on producer rows where `orphanWellIds.has(w.id)`.
- [ ] The existing `wells-stats` summary line above the table is unchanged (still aggregates across all wells).
- [ ] The setpoint slider on each well row still calls `setWellRate(w.id, ...)` on change.
- [ ] CSS class `reservoir-group-header` is defined in `world/ui/style.css` for visual separation (background tint, padding, monospace numeric column).
- [ ] Manual visual verification (dev-server browser session): groups appear in ascending reservoir order; producer→injector order within group; resurveying via `POST /survey` grows the header's estimated bbl on the next tick; running production via `step` lowers the header's remaining bbl; drilling into rock produces a row that lands under the Unaffiliated group; demolishing the last null-reservoir well removes the Unaffiliated header.
- [ ] `make check` passes (no Python changes expected; gate stays green).

## Blocked by

- #01 (needs `reservoirs_summary` in `/state` to drive grouping).

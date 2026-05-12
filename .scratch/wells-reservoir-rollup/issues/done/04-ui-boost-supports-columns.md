---
Status: needs-triage
---

# UI: `boost` + `supports` columns on well rows

## Parent

`.scratch/wells-reservoir-rollup/PRD.md`

## What to build

Add two new columns to the Wells-tab table, layering on top of #03's grouped layout:

- **`boost`** — populated on production-well rows from the existing `pressure_boost` field already in the well payload. Displayed as a number with three decimal places (e.g. `0.421`) or `—` when zero. Empty cell for injectors.
- **`supports`** — populated on injection-well rows from the new `supports_producer_ids` field added in #02. Displayed as a compact comma-separated list of producer well ids (e.g. `W1, W3`). When the list is empty, the cell shows `—` with a hover-tooltip explaining "no qualifying producers in this reservoir at Chebyshev distance > 1." Empty cell for producers.

The `<thead>` row in `world/ui/index.html` gains two new `<th>` elements (`boost`, `supports`). Existing columns stay in their current order; the new columns are appended after `cum bbl`. The group-header row from #03 widens its `colspan` to cover the new columns. CSS may be added to `world/ui/style.css` for the new column types if needed (e.g. monospace for the boost number).

This slice is the final piece of the wells-reservoir-rollup feature: once it lands, the player can read per-row pressure support directly from the table without consulting hover popups or doing manual Chebyshev arithmetic.

## Acceptance criteria

- [ ] Two new `<th>` elements (`boost`, `supports`) in the wells table header in `world/ui/index.html`, appended after the `cum bbl` column.
- [ ] `renderWells()` emits a `boost` `<td>` on every well row: producers show `w.pressure_boost.toFixed(3)` (or `—` when ≤ 0); injectors show an empty cell.
- [ ] `renderWells()` emits a `supports` `<td>` on every well row: injectors show a comma-separated list of `w.supports_producer_ids`, or `—` with a hover-tooltip when the list is empty; producers show an empty cell.
- [ ] Group-header row's `colspan` is updated to cover all columns including the two new ones.
- [ ] When a producer's `pressure_boost` reaches the cap (0.5), the cell displays the cap value (no special styling required, just numerical correctness).
- [ ] An injector with an empty `supports_producer_ids` array shows `—` plus the explanatory hover-tooltip; an injector with one or more entries shows them comma-separated, ascending.
- [ ] Manual visual verification (dev-server browser session): drilling an injector adjacent (Chebyshev 1) to a producer in the same reservoir yields `supports = —` (qualification fails); moving to Chebyshev ≥ 2 yields `supports = [producer id]`; producer boost rises after one step once injection runs; producer boost is `0.500` when injection rate ≥ 50% of producer's yesterday rate.
- [ ] `make check` passes (no Python changes expected; gate stays green).

## Blocked by

- #02 (needs `supports_producer_ids` on injection wells).
- #03 (needs the grouped renderer to extend; both edit `renderWells()` and the `<thead>`, so this slice layers on top of #03 to avoid merge conflicts).

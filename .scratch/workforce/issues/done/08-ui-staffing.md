---
Status: needs-triage
---

# 08 — UI: top-bar population counter + per-facility staffing badges

## Parent

[PRD: World v2 — Workforce & Per-Facility Staffing](../PRD.md)

## What to build

Two UI changes that surface the workforce model to the player:

1. **Top-bar population display** changes from `{population}` to `{unemployed}/{population}` (e.g. `34/100`).
2. **Per-facility badges** render on every producer tile and well on the map showing `{staffed_jobs}/{spec.jobs}`. The badge is colour-coded by efficiency band:
   - Green at 100% (fully staffed)
   - Yellow at 50–99% (partially staffed but functional)
   - Red below 50% (heavily under-staffed)
   - Solid red / empty at 0% (idle)

Passive tiles (road, house, park, pipeline) do not render a badge.

UI-only slice. No backend changes; the data already flows through `/state` from slice 01.

### Implementation details

**`world/ui/app.js`**:

- Top-bar population (`els.population` at line 23, updated at line 1220): change the render to `${s.unemployed}/${s.population}`. Read `s.employed` and `s.unemployed` from `/state` (added in slice 01).
- Per-facility badges: in the map render loop (wherever tiles are drawn), for each tile with `spec.jobs > 0` (or, equivalently, where `staffed_jobs` is meaningful), render a small overlay badge with `${tile.staffed_jobs}/${spec.jobs}`. Mirror for wells. Pull the `spec.jobs` value from the cached `/catalog` response (already fetched at app load for the build panel) so the UI does not need to round-trip per render.
- Colour bands: compute `efficiency = tile.staffed_jobs / spec.jobs`. Apply a CSS class:
  - `efficiency === 1.0` → `.badge-staffing-full` (green)
  - `0.5 <= efficiency < 1.0` → `.badge-staffing-partial` (yellow)
  - `0.0 < efficiency < 0.5` → `.badge-staffing-low` (red)
  - `efficiency === 0.0` → `.badge-staffing-idle` (solid red / empty)

**`world/ui/style.css`**:

- Add the four `.badge-staffing-*` classes. Pick legible foreground/background colours that contrast with the existing tile sprites. Existing badge styles (if any — check the current tile-overlay system) are the prior art for sizing and positioning.
- Badge size and position should not overflow the tile bounding box and should not obscure the tile's primary art. Position above the tile (top-right corner of the tile rect) or below (bottom-right) — match existing tile-overlay conventions if there is one.

**`world/ui/index.html`**:

- Likely no structural change — the badges are dynamic DOM nodes injected by `app.js`. If the existing tile DOM is a `<canvas>` rather than per-tile elements, the badges may need to render via canvas text. Match the existing strategy.

### Manual verification (no automated tests)

UI-only slices in this repo follow the precedent of issue 16 (UI play/pause): manual verification in a browser, no test requirement.

Verification checklist:

- [ ] Fresh game (`/reset`): top bar reads `70/100`. The town hall renders a green `30/30` badge.
- [ ] Build a coal plant: badge appears at `8/8` green; top bar updates to `62/100`.
- [ ] Drain unemployed (e.g., build several industrials in succession): build a final industrial when only 10 unemployed remain — badge appears at `10/30` red.
- [ ] Build an industrial with zero unemployed: badge appears at `0/30` solid red / empty.
- [ ] Demolish a tile: workers return; older under-staffed facility's badge updates to a higher number (and possibly upgrades colour band).
- [ ] Advance several days with population growth: badges on previously-under-staffed facilities tick up oldest-first.
- [ ] Trigger a happiness decline (force blackouts via a coal-only grid + plant failure event): badges tick down on the newest facility first.
- [ ] Solar / wind / oil well / injection well all render badges (small jobs values like 2 / 3).
- [ ] Roads, houses, parks, pipelines do **not** render badges.
- [ ] Failed plant (`operational=False`) keeps its badge displayed and unchanged.

### Notes

- The PRD explicitly does not test the exact colour thresholds (visual styling, not contract). Threshold values are 50% and 100% as defined here; small tweaks at review time are fine.
- The badge is purely informational. There is no click-to-hire or transfer UI — allocation remains fully automatic (PRD's "Out of Scope" section).

## Acceptance criteria

- [ ] Top bar reads `{unemployed}/{population}` (e.g. `70/100` on a fresh game).
- [ ] Every producer tile (commercial, industrial, refinery, town hall, coal, gas, solar, wind) and every well (oil, injection) renders a `{staffed_jobs}/{jobs}` badge on the map.
- [ ] Passive tiles (road, house, park, pipeline) render no badge.
- [ ] Badge colour reflects efficiency: green=100%, yellow=50–99%, red=1–49%, solid red/empty=0%.
- [ ] Badges update live after `/build`, `/demolish`, `/drill`, and `/step`.
- [ ] Failed plants (`operational=False`) keep their badge displayed.
- [ ] Manual verification checklist above passes in a browser.

## Blocked by

- 01 — Workforce foundation (provides `/state.employed`, `/state.unemployed`, per-tile `staffed_jobs`, per-well `staffed_jobs`, and `/catalog` job counts)

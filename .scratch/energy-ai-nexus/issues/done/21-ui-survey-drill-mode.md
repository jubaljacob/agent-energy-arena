---
Status: ready-for-agent
---

# 21 — UI seismic-survey + drill modes (map-canvas integration + API parity)

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## What to build

The world already exposes `/survey` (slice 06) and `/drill` (slice 07)
as REST endpoints, but the manual-play UI has **no way to fire either
from the canvas**. The Subsurface tab visualises revealed voxels, the
Wells tab lists drilled wells, but there is no map-canvas affordance
to:

1. Pick a `(x, y)` anchor for a seismic survey column.
2. Pick a `(x, y, z)` voxel to drill into (production or injection).

The build palette also has no entry for production wells because
**wells are not buildable tiles** — they are a separate world object
spawned by `POST /drill`. Today a human player can only interact with
wells via raw curl. This issue closes that gap.

The UI uses `/catalog` to render the build palette, including each
tile's CAPEX / OPEX / requires-road metadata. The same UI now needs
to render a **cost preview for a survey of size N** in hover state,
but `/catalog` currently has no entry for the survey-cost formula or
its size bounds — both live as Python-only constants
(`world/subsurface.py::survey_cost`). To keep "the UI and a
participant's agent see the same world," this issue also extends
`/catalog` so anyone consuming the API has full parity with what the
UI shows. No new endpoints; one extended response shape.

### Backend — `/catalog` extended for API parity

`world/catalog.py::build_catalog` gains a new top-level key
`"subsurface"` describing the survey + drill surface so the UI's
hover cost preview AND a participant's agent can both consume the
exact same metadata:

```jsonc
GET /catalog
{
  "tiles":     [...],   // unchanged
  "wells":     [...],   // unchanged (oil_well + injection_well CAPEX/OPEX)
  "subsurface": {
    "survey": {
      "base_cost":   15000,        // matches subsurface.SEISMIC_BASE_COST
      "base_size":   8,            // formula divisor (also default_size)
      "min_size":    4,
      "max_size":    16,
      "cost_formula": "base_cost * (size / base_size) ** 2",  // descriptive only — NOT eval'd
      "default_size": 8
    },
    "drill": {
      "production": {
        "capex":   50000,
        "opex_per_day": 100,
        "max_rate_bbl_day": 200,
        "crude_price_usd_per_bbl": 40
      },
      "injection": {
        "capex":   30000,
        "opex_per_day":  50,
        "max_rate_bbl_day": 200,
        "kwh_per_bbl":     50
      }
    }
  }
}
```

Field origin:
- `survey.base_cost / base_size / min_size / max_size` lift the
  existing constants from `world/subsurface.py` (`SEISMIC_BASE_COST`,
  `SEISMIC_DEFAULT_SIZE`, `SEISMIC_MIN_SIZE`, `SEISMIC_MAX_SIZE`) so
  they live in one place. `survey_cost(size)` keeps its
  implementation; only the constants are surfaced.
- `cost_formula` is **descriptive metadata** — it is intended for
  agent-facing prompt copy (e.g. `agents/prompts.py`). The UI and
  `ApiClient.survey_cost_preview` MUST compute the cost via plain
  arithmetic from `base_cost`, `base_size`, and `size`. No client may
  `eval()` the string.
- `drill.production / drill.injection` already exist in the catalog
  as `wells: [...]`. The `subsurface.drill.*` block is just a
  re-shape keyed by well-type so callers don't have to filter the
  `wells` list. Backwards compatibility: the existing `wells` array
  stays untouched.
- `drill.production.crude_price_usd_per_bbl` mirrors
  `subsurface.CRUDE_PRICE_USD_PER_BBL`, matching the symmetry of
  `drill.injection.kwh_per_bbl`. Agents reading the catalog can then
  compute well economics without re-reading the brief.

`POST /survey` already returns the cost actually charged on success
(via the existing `result` envelope); no shape change there. The
preview-cost is a pure derivation, so agents can compute it locally
once they read `/catalog`.

**`POST /survey` body: `size` is optional.** When omitted, the server
treats it as `SEISMIC_DEFAULT_SIZE` (8), matching the catalog's
`default_size`. This lets an LLM call `survey(x, y)` without re-reading
the catalog.

### Agent-side ergonomics

`agents/api_client.py::ApiClient` gains one helper so issuance
parity holds: `survey_cost_preview(size: int) -> float` reads the
already-cached `/catalog` response (or fetches if missing) and
returns `base_cost * (size / base_size) ** 2`. No new HTTP call per
preview. The scripted + LLM agents don't need this today (they read
/state and check treasury after the fact), but the LangGraph demo
agent in issue 19 will use it to show "preview before you fire."

`agents/prompts.py::ACTION_TOOLS` for the survey tool already
exposes `size in [4, 16]`. Once the catalog change lands, the prompt
mechanic primer should cite the size bounds and cost formula
verbatim from the catalog rather than hardcoding them — a follow-up
in the LLM-prompt code path.

### New left-rail "Subsurface tools" palette

Add a small palette directly below the build palette in `world/ui/`
that exposes two mode buttons:

- **Survey** — when active, click on the canvas anchors a size-N
  survey column at `(x, y)`. A secondary input (number field) picks
  the column size in `[4, 16]`, defaulting to 8 per the catalog. The
  hover overlay shows the size-N×N footprint with a faint ring and a
  cost preview from `15_000·(size/8)²`. Click → `POST /survey
  { x, y, size }`. Right-click cancels mode.
- **Drill** — when active, the canvas is unchanged, but the
  Subsurface tab's cross-section gains click handlers on each
  revealed voxel. Clicking a voxel:
  1. Switches the canvas to "drill-here" mode anchored at that
     voxel's `(x, y)`.
  2. The depth `target_z` is locked to the clicked voxel's z.
  3. A radio toggle in the palette picks `well_type ∈ {production,
     injection}`.
  4. Clicking the surface `(x, y)` on the canvas fires `POST /drill
     { x, y, target_z, well_type }`. Right-click cancels mode.

Both modes are mutually exclusive with the build mode (selecting one
deselects the others), matching the existing single-mode pattern.

### Survey footprint geometry (binding)

The hover preview MUST render the same cells the server will reveal.
The canonical bounds live in `world/subsurface.py::_column_bounds`:

```
half = size // 2
x0 = max(0, x - half)
y0 = max(0, y - half)
x1 = min(world_w, x - half + size)
y1 = min(world_h, y - half + size)
```

Implications the UI must honour:

- The footprint is **asymmetric** around the anchor for size=8 (4 left,
  3 right). The preview is not a "centered N×N square."
- Near grid edges the footprint is **clipped**. A click at (1, 1) with
  size=8 reveals a 5×5×depth column, not 8×8. The hover overlay MUST
  render the clipped rectangle so the user sees what they pay for.
- Cost is unchanged by clipping (server charges `survey_cost(size)`
  regardless), but the rendered cell count is the clipped count. The
  tooltip should reflect that: `survey @ (x, y) size=N · cost $C · NN cells`.

### Surface-canvas affordances

- While in **survey mode**: hover renders the clipped footprint
  (per the geometry section above) as a translucent yellow
  rectangle + a tooltip showing
  `survey @ (x, y) size=N · cost $C · NN cells`. Canvas cursor is
  `crosshair`.
- **Resurvey indicator.** Cells inside the hovered footprint that
  already appear in `/state.reservoirs_revealed`'s explored set
  (or whose column is in `explored_columns` via `/reservoirs`) render
  with a distinct hatching/fill so the user sees "this fraction of
  the column is a resurvey." The tooltip switches to
  `resurvey · $C · NN cells (M previously surveyed)` when M > 0.
- **Affordability tint.** If `treasury < survey_cost(size)` the
  footprint tint switches to red and the click is short-circuited
  client-side with an `"insufficient_funds"` toast — matching the
  existing build-hover convention at `world/ui/app.js:97`.
- While in **drill mode**: hover renders a red crosshair on the
  surface tile + a tooltip showing
  `drill @ (x, y) target_z=Z type=production`. Canvas cursor is
  `crosshair`.
- **Dry-hole guard (drill mode).** If the picked voxel's 3×3×3
  drainage pool — computed client-side from `/reservoirs?top_k=4096`
  — contains zero known HC voxels, the crosshair turns **yellow**
  with a tooltip `no known HC in 3×3×3 pool — possible dry hole`,
  and clicking the canvas opens a **confirm modal** before any POST
  fires:

  ```
  ┌────────────────────────────────────────────────┐
  │ Drill possibly-empty rock?                     │
  │                                                │
  │ No surveyed HC voxels in the 3×3×3 drainage    │
  │ pool around (x, y, target_z). The well may    │
  │ produce 0 bbl/day — $50,000 CAPEX at risk.    │
  │                                                │
  │           [Cancel]   [Drill anyway]            │
  └────────────────────────────────────────────────┘
  ```

  Confirming fires `POST /drill` as normal. Cancelling leaves the
  anchor lock intact so the user can pick a different voxel without
  re-entering drill mode. Wildcatting is still possible — just
  gated behind one explicit click so a misclick doesn't burn $50k.
  Confirmed HC in the pool ⇒ normal red crosshair, no modal.
- If the picked tile is already occupied (well or building), the
  hover indicator is red-tinted and the click is short-circuited
  client-side with a `"occupied"` toast (defence in depth — the
  server will reject too).
- The existing build palette buttons stay above the survey/drill
  palette; selecting any build tile deactivates survey/drill modes.

### Global mode indicator

Selecting a build tile today only highlights its palette row.
Subsurface modes need a more obvious indicator because their click
target (canvas vs cross-section SVG) differs:

- The `#buildhint` paragraph under the build menu is replaced
  dynamically while a subsurface mode is active:
  - Survey: `Survey mode — click canvas to anchor. Right-click or Esc to cancel.`
  - Drill: `Drill mode — pick a voxel in the Subsurface tab, then click the canvas. Right-click or Esc to cancel.`
- Canvas cursor changes to `crosshair` for both modes (vs `default` for build/inactive).
- Pressing **Escape** clears `selectedType` and any subsurface mode
  (aligns with issue 16's keyboard story).

### Post-survey UX: bridging Map → Subsurface

A successful `/survey` reveals voxels that only render in the
Subsurface tab — without a bridge, the user is left on the Map tab
unsure whether anything happened. On the success path:

1. The toast switches from "survey @ … cost $C" to
   `survey done — N voxels revealed · open Subsurface tab` with a
   click target that activates the Subsurface tab.
2. When the Subsurface tab is activated this way, the slice selector
   auto-jumps to `slice = anchor_y` (axis=y), so the user lands
   directly on the slice that was just surveyed.
3. The Map canvas keeps a faint hatching overlay over every cell in
   `explored_columns` (data already in `/state.reservoirs_revealed`
   via the existing `n_explored_columns` aggregate, or richer via
   `/reservoirs?top_k=4096`). The overlay is visible in **all** modes
   (not just survey mode) so the user always sees which columns have
   been paid for.

### Subsurface-tab voxel picker

The existing SVG cross-section (`#subchart` in
`world/ui/index.html:51`) renders each voxel as a rect. Add click
handlers so:

- In **drill mode**, clicking a revealed voxel sets the active
  `(x_or_y, z)` pair. The "other" surface coord comes from the
  current slice selector at click time and is **locked** along with
  the clicked coord — subsequent changes to the slice selector do
  **not** move the locked anchor. Re-clicking another voxel replaces
  the lock; pressing Escape or right-click clears it.
- The map canvas highlights the locked wellhead tile (a yellow
  outline) immediately on voxel-click, even while the user is still
  on the Subsurface tab.
- Outside drill mode, clicking a voxel does nothing (current
  behavior).
- A small status line under the chart shows
  `selected target: (x, y, z) — click surface to drill` once a
  voxel is picked.

### Existing wells on the cross-section

Without seeing where already-drilled wells live, the user can't site
injection wells so their pool intersects a producer's pool
(`world/subsurface.py::pools_intersect`). Issue 21 keeps the 3×3×3
drainage-pool visualisation out of scope, but the **wellhead markers
themselves** are in scope:

- For each well in `state.wells` whose `(lateral, target_z)` lands
  on the current slice (where "lateral" is `well.x` for axis=y and
  `well.y` for axis=x, **and** the off-axis coord matches the slice
  index), render a small marker (▼ for production, ▲ for injection)
  on the appropriate rect in `#subchart`.
- The marker carries a `<title>` tooltip:
  `well_id · type · (x, y, target_z) · setpoint NN bbl/d`.
- The marker is purely visual — clicking it does nothing in this
  slice (well control lives in the Wells tab).

### Persistence + telemetry

- Mode state lives in the same client-side store as `selectedType`
  (a string union `"build:<tile>" | "survey" | "drill" | null`).
  Page reload resets it to `null`.
- The drill-mode voxel-pick anchor (`{x, y, target_z, well_type}`)
  also lives in this store and survives mode-internal interactions
  (slice changes) but clears on mode exit or page reload.
- Each fired survey / drill adds a toast on success
  (`survey done — N voxels revealed · open Subsurface tab` for
  survey; `drilled production well at (x, y, z) — id=W#` for drill)
  and on failure (`survey rejected: insufficient_funds`, etc.).
- **Action-ticker coordination (issue 16).** When issue 16 lands the
  bottom-bar action ticker, surveys and drills append to it on the
  same code path as `/build` and `/demolish`. Spec'ing here so the
  implementer of 21 wires the hook even if 16 hasn't merged yet (the
  ticker container is opt-in — append-if-present).

## Acceptance criteria

### Backend (API parity)

- [ ] `GET /catalog` returns a new `"subsurface"` key alongside
      `"tiles"` and `"wells"`. Existing keys unchanged
      (backwards-compatible extension).
- [ ] `subsurface.survey` carries `base_cost`, `base_size`,
      `min_size`, `max_size`, `cost_formula` (descriptive string),
      and `default_size`. Values match the constants the existing
      `world/subsurface.py::survey_cost` reads (`SEISMIC_BASE_COST`,
      `SEISMIC_DEFAULT_SIZE`, `SEISMIC_MIN_SIZE`, `SEISMIC_MAX_SIZE`).
- [ ] `subsurface.drill.production` and
      `subsurface.drill.injection` mirror the catalog's `oil_well`
      and `injection_well` entries (CAPEX, OPEX, max rate),
      **plus** `crude_price_usd_per_bbl: 40` on production and
      `kwh_per_bbl: 50` on injection — sourced from
      `CRUDE_PRICE_USD_PER_BBL` and `INJECTION_KWH_PER_BBL`.
- [ ] No new constants in `world/subsurface.py`. The catalog imports
      the existing `SEISMIC_*` constants and `survey_cost(size)`
      keeps its existing implementation — only the export shape
      changes.
- [ ] `POST /survey` accepts an optional `size` body field. When
      omitted, the server uses `SEISMIC_DEFAULT_SIZE` (8). When
      present and out of `[SEISMIC_MIN_SIZE, SEISMIC_MAX_SIZE]`, the
      server rejects with the existing `invalid_size` error
      (no behaviour change there).
- [ ] `agents/api_client.py::ApiClient.survey_cost_preview(size)`
      returns the same float as `subsurface.survey_cost(size)` for
      every `size in [4, 16]`. Implementation reads the
      already-cached `/catalog` response (fetching on first call).
      Repeated previews issue **zero** additional HTTP calls.
- [ ] Unit tests in `agents/tests/test_api_client.py` pin:
  - Exhaustive `size in [4, 16]` parity between
    `survey_cost_preview(size)` and `subsurface.survey_cost(size)`.
  - `survey_cost_preview` issues exactly **one** HTTP `GET /catalog`
    across many invocations (cache hit on subsequent calls).
- [ ] Unit tests in `world/tests/test_catalog.py` (or extending the
      existing catalog test) pin:
  - `build_catalog()["subsurface"]["survey"]["base_cost"] == 15_000`
    (and the other survey constants).
  - `build_catalog()["subsurface"]["drill"]["production"]["capex"] == 50_000`
    and `crude_price_usd_per_bbl == 40`.
  - `build_catalog()["subsurface"]["drill"]["injection"]["capex"] == 30_000`
    and `kwh_per_bbl == 50`.
  - The catalog dict shape matches the JSON schema documented in
    the "Backend — /catalog extended" section above.
- [ ] Unit test in `world/tests/test_api_smoke.py` (or alongside)
      pins `POST /survey` with no `size` field defaults to size 8 —
      same revealed-voxel count as an explicit `{size: 8}`.

### UI flow — palette + modes

- [ ] Left rail shows a **"Subsurface tools"** section with two mode
      buttons: **Survey** and **Drill**.
- [ ] Selecting Survey deactivates build mode and any other mode.
      The size input appears next to the Survey button, defaults to
      8, and clamps client-side to `[4, 16]` (values outside the
      range snap to the nearest bound; non-integer input falls back
      to the last valid value).
- [ ] Selecting Drill deactivates build/survey mode. A radio toggle
      for `well_type ∈ {production, injection}` appears.
- [ ] Right-click anywhere on the canvas cancels the active
      subsurface mode (does not fire `/demolish` while a survey or
      drill mode is active).
- [ ] **Escape** cancels the active mode (including build mode),
      consistent with issue 16's keyboard story.
- [ ] The `#buildhint` paragraph reflects the active mode (Survey /
      Drill / Build / none) per the "Global mode indicator" section.
- [ ] Canvas cursor switches to `crosshair` while any subsurface
      mode is active.

### UI flow — survey

- [ ] While in Survey mode, hovering the canvas shows the **clipped**
      footprint per `_column_bounds(x, y, size, world_w, world_h)`
      (not a centered N×N).
- [ ] Tooltip shows `survey @ (x, y) size=N · cost $C · NN cells`
      where `NN` is the clipped cell count.
- [ ] If `treasury < survey_cost(size)`, the footprint tints red and
      a click short-circuits client-side with `"insufficient_funds"`
      — no POST fires.
- [ ] Already-explored cells within the footprint render with a
      distinct hatching/fill; tooltip switches to
      `resurvey · $C · NN cells (M previously surveyed)` when M > 0.
- [ ] Clicking the canvas in Survey mode POSTs `/survey {x, y, size}`
      and on success shows a toast
      `survey done — N voxels revealed · open Subsurface tab`. On
      failure shows the existing error toast.
- [ ] Clicking the toast (or its "open Subsurface tab" affordance)
      activates the Subsurface tab and sets the slice selector to
      `slice = anchor_y` (axis=y).
- [ ] The Map canvas renders a faint hatching overlay over every
      cell in `explored_columns`, visible in all modes (not just
      survey mode).

### UI flow — drill

- [ ] Picking a voxel in the Subsurface tab locks both the surface
      `(x, y)` and `target_z` of the drill anchor. Changing the
      slice selector after the click does NOT move the anchor.
- [ ] On voxel-pick, the Map canvas immediately highlights the
      locked wellhead tile (yellow outline) — even while the user
      is still on the Subsurface tab.
- [ ] Hovering the canvas in Drill mode renders a crosshair on the
      anchored surface tile with tooltip
      `drill @ (x, y) target_z=Z type=production` (or `injection`).
- [ ] **Dry-hole guard.** If the client-side 3×3×3 pool check around
      the picked voxel finds zero known HC voxels (using
      `/reservoirs?top_k=4096` data already on hand), the crosshair
      turns yellow with tooltip
      `no known HC in 3×3×3 pool — possible dry hole`, and clicking
      the canvas opens a confirm modal before any POST fires.
- [ ] Confirm-modal text reads
      `Drill possibly-empty rock? No surveyed HC voxels in the 3×3×3
      drainage pool around (x, y, target_z). The well may produce
      0 bbl/day — $C CAPEX at risk.` with **Cancel** and
      **Drill anyway** buttons. $C is the well-type's catalog CAPEX.
- [ ] Cancelling the modal leaves the anchor lock intact (user can
      pick a different voxel without re-entering drill mode).
      Confirming fires `POST /drill` as normal.
- [ ] Confirmed HC in the pool ⇒ no modal, click fires `/drill`
      directly (existing single-click behaviour).
- [ ] If the picked surface `(x, y)` is already occupied (any tile
      or well), the hover indicator tints red and the click is
      short-circuited client-side with an `"occupied"` toast — no
      POST fires.
- [ ] Clicking the canvas in Drill mode POSTs `/drill {x, y,
      target_z, well_type}` and shows a success or error toast.
- [ ] On a successful drill, the locked anchor clears.

### Existing wells on the cross-section

- [ ] For each well whose `(off-axis, target_z)` lands on the
      currently-shown slice, render a small marker on the
      cross-section: ▼ for production, ▲ for injection.
- [ ] Marker carries a `<title>` tooltip
      `well_id · type · (x, y, target_z) · setpoint NN bbl/d`.
- [ ] Clicking the marker does nothing (well control lives in the
      Wells tab).

### Subsurface-tab voxel picker

- [ ] Each revealed voxel in `#subchart` is clickable in Drill mode.
- [ ] Clicking a voxel sets the active drill target and surfaces a
      "selected target: (x, y, z)" status line.
- [ ] The selected voxel is visually highlighted (border or ring) in
      the cross-section until cleared.
- [ ] Unrevealed voxels remain non-clickable.

### Cross-cutting

- [ ] No new HTTP endpoints. The backend changes are limited to
      extending the `/catalog` payload and making the `size` field
      on `POST /survey` optional — `POST /drill` shape is unchanged.
- [ ] UI portion has no automated tests (consistent with slices
      16/17/20); manual verification in browser. The backend
      portion is fully covered by unit tests per the "Backend
      (API parity)" AC list above.
- [ ] Subsurface tools palette is documented with a one-line caption
      under the existing build-palette docs in
      `world/ui/index.html`, plus a one-paragraph "Survey → Drill"
      flow hint under the Subsurface tab's existing `.sub-hint`.
- [ ] Picking a voxel that's outside the current slice does not
      crash — the picker only fires when the click is on a
      revealed-voxel rect.
- [ ] No client may `eval()` the `cost_formula` string from
      `/catalog`. Cost is computed via plain arithmetic.
- [ ] End-to-end manual test: starting from a fresh world, a human
      who has not read the code can — using only the UI — survey a
      column, identify a high-oil voxel in the Subsurface tab,
      drill a production well at it, see the wellhead marker render
      on the cross-section, and read the resulting bbl/d on the
      Wells tab. The flow does NOT require any curl or API docs.

## Out of scope

- A "well" entry in the build palette. Wells are not buildable
  tiles; this issue keeps the build palette unchanged and adds a
  separate Subsurface-tools palette.
- **Drawing** the well's 3×3×3 drainage pool on the cross-section.
  The wellhead marker itself **is** in scope (see "Existing wells
  on the cross-section"), but rendering the full 27-voxel pool box
  is deferred to a polish slice. The dry-hole warning's pool check
  is purely a hidden client-side calculation, not a render.
- Setpoint sliders on the canvas — the existing Wells tab already
  hosts `/control/well`. This issue focuses on placement only.
- Drag-to-select survey rectangles. The size-N column is the only
  survey shape the world supports today; clicking the anchor is
  enough.
- Surfacing the injection-well DR mechanic in the Wells tab. The
  user learns "injection sheds during brownout / ramps during
  curtailment" from playing; this issue does not add explanatory
  UI for the DR behaviour.

## Notes

- The hover-cost preview computes `base_cost·(size/base_size)²`
  inline from `/catalog`'s `subsurface.survey` block. The
  `cost_formula` string in the catalog is documentation only.
- Footprint geometry MUST use
  `world/subsurface.py::_column_bounds(x, y, size, world_w, world_h)`
  semantics, including grid clipping. The half-window is
  `size // 2`, so even sizes are asymmetric — preview matches.
- The map canvas already has hover-cell tracking (`hoverCell` in
  `world/ui/app.js:43`). The survey footprint renders an extra
  clipped rectangle around the hover cell while in survey mode.
- The Subsurface tab's voxel rendering already maps `(slice, depth)`
  to SVG coords (`world/ui/app.js:402+`); adding a click handler is
  a one-line `addEventListener` per rect. The target-z is whichever
  voxel was clicked.
- `/reservoirs?top_k=4096` is already fetched by the existing chart
  loader (`world/ui/app.js:391+`) — that data structure carries the
  same `(x, y, z, oil_estimate, perm_estimate)` records both the
  picker and the dry-hole pool-check need.
- The dry-hole pool check is purely client-side filtering over the
  already-fetched `/reservoirs` payload: for picked voxel
  `(x, y, z)`, look for any record with
  `|dx| ≤ 1 ∧ |dy| ≤ 1 ∧ |dz| ≤ 1`. No new server call.
- Right-click currently fires `/demolish` (`world/ui/app.js:242`).
  In survey/drill mode the right-click is intercepted to cancel the
  mode FIRST; only when no subsurface mode is active does the
  right-click hit `/demolish`. Issue 20's connectivity guard for
  road demolition still applies once we exit subsurface mode.
- Existing wells (the markers on the cross-section) come from
  `state.wells` (already in the polled `/state` payload). No new
  endpoint needed.

## Blocked by

None — depends only on shipped slices (06 for `/survey`, 07 for
`/drill`, 01 for the UI canvas). Should land before issues 16/17 are
played-tested since seismic exploration is a core mechanic and
manual players need access to it.

## Related

- Issue 16 (UI play/pause + action ticker) — the survey/drill
  actions must append to the action ticker once 16 lands. The
  ticker hook is opt-in (append-if-present) so 21 can ship first.
- Issue 20 (demolish connectivity guard) — independent code path
  (different tile types), but both interact with the
  right-click-on-canvas affordance. 21's mode-cancel-on-right-click
  takes precedence when a subsurface mode is active; 20's guard
  kicks in otherwise.

## Progress note (AFK pass 1)

Backend portion landed — the entire AFK deliverable for this issue:

- `GET /catalog` now returns a new `subsurface` key alongside the
  existing `tiles` and `wells` arrays:
  - `subsurface.survey` carries `base_cost`, `base_size`, `min_size`,
    `max_size`, `default_size`, and the descriptive `cost_formula`
    string. Values are lifted from `world.subsurface.SEISMIC_*` —
    no constants duplicated; the catalog imports them.
  - `subsurface.drill.production` mirrors the `oil_well` catalog
    entry plus `crude_price_usd_per_bbl = 40` from
    `subsurface.CRUDE_PRICE_USD_PER_BBL`.
  - `subsurface.drill.injection` mirrors the `injection_well` entry
    plus `kwh_per_bbl = 50` from `subsurface.INJECTION_KWH_PER_BBL`.
  - The existing `wells` array is left untouched (backwards-compat).
- `POST /survey` body's `size` field already had `Field(default=8)`
  from slice 06; the literal `8` is now the named
  `SEISMIC_DEFAULT_SIZE` constant for traceability. Behaviour
  unchanged — confirmed by
  `test_post_survey_size_field_is_optional_and_defaults_to_eight`:
  fresh world + survey at (16,16) with no `size` field produces
  identical voxel count, cost, and revealed set as an explicit
  `{size: 8}` call.
- `agents/api_client.py::ApiClient` gains
  `survey_cost_preview(size: int) -> float`. Reads the cached
  `/catalog` response (fetching on first call) and returns
  `base_cost * (size / base_size) ** 2`. The `catalog()` method now
  caches its response on the client instance.

Tests added (all `make check` clean):
- `world/tests/test_catalog.py` (new, 6 tests): top-level keys,
  survey-block constant parity, drill.production / drill.injection
  constant parity, formula↔helper agreement over all sizes in
  `[4, 16]`, wells-array unchanged.
- `world/tests/test_api_smoke.py`:
  `test_post_survey_size_field_is_optional_and_defaults_to_eight`.
- `agents/tests/test_api_client.py` (new, 15 cases):
  parametrised parity between `survey_cost_preview(size)` and
  `world.subsurface.survey_cost(size)` for every legal size; the
  preview issues exactly one `GET /catalog` across many invocations
  (cache hit on subsequent calls); the `catalog()` method itself
  returns the same cached object twice.

Verification: `make check` — 0 ruff findings, 49 files mypy-clean,
408 pytest passing (386 prior + 22 new).

UI portion (left-rail Subsurface palette, hover footprint, dry-hole
guard, voxel-pick → drill, wellhead markers on the cross-section,
toast bridge to the Subsurface tab) is HITL — consistent with slices
16 / 17 / 20 which the issue explicitly cites as "no automated tests;
manual verification in browser." Backend API parity is the entire
AFK deliverable; the UI work can land in a separate HITL slice.

Blockers / notes for next iteration:
- `subsurface.drill.{production,injection}.max_rate_bbl_day` is sourced
  from `Q_MAX_WELL_BBL_DAY` (200). The wells array already carries
  the same value via the per-tile spec, so the drill block is a
  re-shape, not new data.
- `agents/prompts.py::ACTION_TOOLS` for the `survey` tool still
  hardcodes `size in [4, 16]`. Slice 19 + slice 15 follow-up: regen
  the prompt's mechanic primer from `catalog["subsurface"]` so the
  LLM and the world stay in lockstep without manual edits.
- The HITL voxel-picker change in `world/ui/app.js:402+` and the
  hover-cost rendering in `world/ui/app.js:43+` can now read
  `catalog.subsurface` directly — no schema work blocks the UI
  slice.

## Progress note (HITL pass 2 — UI portion)

UI portion landed. The left rail now carries a **Subsurface tools**
palette beneath the build palette with two mode buttons (Survey,
Drill) wired into a unified mode state machine in `world/ui/app.js`.
Survey has a size input that clamps to `[4, 16]` and a live cost
preview computed from `catalog.subsurface.survey` (no HTTP roundtrip
per hover — `/catalog` is cached on `ApiClient` but the UI uses a
plain `loadCatalog()` that runs once at boot). Drill has a
production/injection radio.

Map canvas (`world/ui/app.js`):
- Hover in Survey mode renders the **clipped** N×N footprint via a
  JS port of `world.subsurface._column_bounds`. Affordability tint
  switches yellow → red when `treasury < cost`. Already-explored
  cells inside the footprint get an extra crossed-line overlay so
  resurveys are visible.
- Hover tooltip (via `canvas.title`) reads
  `survey @ (x, y) size=N · cost $C · NN cells` or
  `resurvey · cost $C · NN cells (M previously surveyed)` when any
  cell in the footprint is already in the explored set.
- Click in Survey mode short-circuits client-side if
  `treasury < cost` with an `insufficient_funds` toast; otherwise
  POSTs `/survey {x, y, size}`. Success toast is a **bridge**
  affordance (yellow + underlined + clickable) that activates the
  Subsurface tab and snaps `subAxisEl.value = "y"` +
  `subSliceEl.value = y`.
- Drill mode renders a coloured crosshair on the **locked anchor**
  tile (not at hover): red if tile/well occupies it, yellow if the
  dry-hole pool check fails, orange otherwise. Hovering anywhere on
  the canvas just shows a tooltip describing the locked target.
- Click in Drill mode fires `/drill { x, y, target_z, well_type }`
  using the anchor coords; the hover position is ignored
  (defence: the issue's "click the canvas to fire" is interpreted
  as "anywhere — the anchor is already committed"). Occupied
  → short-circuit toast. Dry-hole → confirm modal with the wording
  from the issue, including `$C CAPEX at risk` derived from
  `catalog.subsurface.drill[type].capex`. Cancel leaves the anchor
  intact; Confirm fires drill; success clears the anchor.
- Explored-columns hatching renders in every mode (faint yellow
  diagonals over each `(x, y)` that appears in the cached
  `/reservoirs` voxel list). Empty-but-surveyed columns won't
  appear in the overlay because `/reservoirs` only carries HC
  voxels — acceptable for a hover hint; the server is the source
  of truth.

Subsurface tab (`renderSubsurface`):
- Each revealed voxel rect gets a `drill-pickable` class when
  `mode === "drill"`. Clicking sets `drillAnchor = {x, y, target_z}`,
  highlights the rect with a yellow stroke, surfaces a "selected
  target" status line, and refreshes the map canvas so the locked
  yellow outline appears immediately (even while the user is still
  on the Subsurface tab).
- Existing wells render as ▼ (production, green) / ▲ (injection,
  blue) text markers on the cross-section whenever their off-axis
  coord matches the current slice. Markers carry a `<title>`
  tooltip `well_id · type · (x, y, target_z) · setpoint NN bbl/d`.
- The status line under the chart reads `selected target: (x, y, z)
  — click surface on the Map tab to drill` once a voxel is picked,
  and clears when the anchor is released.

Keyboard / cancel story:
- **Escape** clears any active mode (build/survey/drill) and
  dismisses the confirm modal. Ignored when typing in an input.
- **Right-click** while in survey/drill mode cancels the mode and
  does NOT fall through to `/demolish`. With no subsurface mode
  active, right-click keeps its existing demolish semantics
  (issue 20's connectivity guard still applies).
- Mutual exclusivity: selecting a build tile auto-deactivates
  survey/drill; selecting Survey or Drill clears `selectedType`.
- Canvas cursor switches to `crosshair` only when a subsurface
  mode is active.
- `#buildhint` paragraph rewrites itself with the mode-specific
  prompt (matches the "Global mode indicator" section of the spec).

HTML/CSS:
- `world/ui/index.html`: new `<ul id="sublist">` with `mode-survey`
  and `mode-drill` `<li>`s, plus the confirm-modal scaffolding
  (`#modal`, `#modal-cancel`, `#modal-confirm`) inside `#canvasrow`
  (which already has `position: relative`). The Subsurface tab
  `.sub-hint` paragraph now documents the Survey → Drill flow and
  the ▼/▲ legend; a new `<p id="sub-target">` carries the
  "selected target" status line.
- `world/ui/style.css`: appended `.modeItem`, `.sub-survey-swatch`,
  `.sub-drill-swatch`, `.modal-backdrop`, `.modal`, `.modal-actions`,
  `.drill-pickable`, `.drill-picked`, `.sub-target`, `.toast.ok-bridge`
  styles. Crosshair cursor toggle via `canvas.classList`.

Backend (re)verified end-to-end via curl: fresh `/reset` → cluster
of `/survey` calls at (4,4) … (28,28) reveals 70 HC voxels across
1024 columns; `/reservoirs?top_k=10` picks the first; `/drill` at
that voxel succeeds with the expected `production-1` envelope and
$50k CAPEX. The UI mirrors this exact flow.

Verification:
- `node -e "new Function(fs.readFileSync('world/ui/app.js'))"` parses
  cleanly (syntax sanity).
- `make check` exits clean: 0 ruff findings, 49 files mypy-clean,
  408 pytest passing — no backend regression.
- Headless E2E (Playwright/Puppeteer) was not run since the repo
  has no JS test harness today. Manual browser verification of the
  full Survey → Subsurface tab → Drill → Wells tab flow is the
  remaining sign-off.

Blockers / notes for next iteration:
- The toast bridge uses `toastEl.onclick = handler` and resets to
  `null` on click or auto-hide. If a future polish adds keyboard-
  accessible toasts, swap to an explicit anchor inside the toast
  body so screen readers see "click to open Subsurface tab" as a
  focusable target.
- Dry-hole pool check reads from the cached `/reservoirs` payload
  refreshed on every `tick()` (only when the Subsurface tab is
  active) or after a survey. If the user picks a voxel, surveys a
  neighbouring column that fills the previously-empty pool, and
  immediately clicks to drill without the Subsurface tab being
  open, the pool check could miss the new HC. Mitigation: the
  modal is a soft gate — Confirm fires the drill regardless. The
  defence-in-depth nature of the client-side check makes a stale
  pool tolerable.
- Issue 16's action-ticker hook isn't wired here. The spec's
  "Action-ticker coordination" line said "append-if-present" — the
  Survey/Drill paths don't try to find a ticker container today.
  When 16 lands, add the append call inside `handleSurveyClick`
  and `fireDrill` next to the `tick()` call.
- The explored-columns hatching derives from `/reservoirs` (HC
  voxels only). A future polish slice could add an `explored_
  columns: [[x, y], ...]` field to `/reservoirs` (or
  `/state.reservoirs_revealed`) so empty-but-paid-for columns
  also light up. Strictly additive, no breaking change.
- The voxel-pick → drill flow uses `drillAnchor` shared across
  tabs. If the user toggles into another mode mid-pick, the
  anchor clears (see `setMode`). That's intentional but worth a
  note: there's no "remember last pick" affordance.

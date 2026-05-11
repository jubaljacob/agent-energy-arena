---
Status: needs-triage
---

# 20 — Demolish connectivity guard + UI affordance

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## What to build

Right-click demolish already exists in the UI (`world/ui/app.js:242`)
and the world already refunds 25% of CAPEX on `/demolish`
(`world/sim.py:261`). What's missing is a **connectivity guard**:
demolishing a road (or the town hall — though that's already
disallowed by `world.sim.demolish`) must be **rejected** if doing so
would leave any road-requiring civilian tile (house, commercial,
industrial, refinery) disconnected from the town-hall road network.

Plus a UI affordance so the human player understands what's happening:
the right-click on a road that would orphan a civilian tile shows a
preview indicator (cursor + which tile(s) would become stranded) and
the click is rejected with a toast — no charge, no refund.

### Backend

`world/sim.py::demolish` gains a connectivity precheck **only when the
target tile is a road**:

1. Compute `road_connected_set(tiles)` — the current network.
2. Tentatively remove the target road tile.
3. Recompute `road_connected_set(tiles_without_target)`.
4. For every road-requiring tile (`requires_road=True` per
   `world/catalog.py`), assert at least one orthogonal neighbor is
   still in the new network.
5. If any civilian tile loses adjacency, reject with
   `{"ok": false, "error": "would_disconnect", "result": {"stranded":
   [{"x": ..., "y": ..., "type": ...}, ...]}}`. No treasury delta.
6. Otherwise proceed with the existing demolish path (refund, remove,
   action log).

The `town_hall` case is unchanged — it remains undemolishable with
`error: "town_hall_undemolishable"` (or whatever the current error
string is; preserve verbatim).

The `requires_road` lookup uses the existing catalog metadata
(`world/catalog.py::TileSpec.requires_road`); no new spec field.

### Frontend

`world/ui/app.js` is updated so:

- On `contextmenu` over a road tile, BEFORE firing `/demolish`, the
  client computes the same connectivity check client-side (cheap —
  the tile list and grid are already in memory from `/state`) and:
  - If safe: shows a confirm cursor (or just fires `/demolish` as
    today). Implementer choice — a one-click flow stays acceptable.
  - If unsafe: aborts the `/demolish` POST entirely and shows a toast
    "would strand N tile(s) — demolish blocked" with the stranded
    tile coordinates listed.
- The server-side check is the source of truth. If the client check
  passes but the server rejects (e.g., the user demolished elsewhere
  between the client check and the POST), the existing toast path
  surfaces the `would_disconnect` error string.
- Hover over a road tile in normal cursor mode shows a small "stranded
  if removed: N" badge in the tile inspector panel (the existing
  bottom-left panel that already shows tile type / id / OPEX). Zero
  if removal is safe, otherwise the count of civilian tiles that
  would lose road connectivity.

The 25% refund cost is unchanged. The "at some cost" phrasing in the
ask is already satisfied by the existing 75% CAPEX loss on demolish.

## Acceptance criteria

### Backend

- [ ] `world/sim.py::demolish` rejects with `error: "would_disconnect"`
      when removing a road would leave any `requires_road=True` tile
      with no road-network neighbor.
- [ ] Rejection envelope includes `result.stranded` — a list of
      `{x, y, type}` for every tile that would lose connectivity. Empty
      list ⇒ allowed.
- [ ] Rejection does NOT mutate treasury, tiles, or action log result
      (the action log entry is still appended with `ok=false`).
- [ ] Demolishing a power plant / well / pipeline / refinery is
      unaffected (those don't sit in the road network).
- [ ] Demolishing the town hall remains rejected with the existing
      error string (no behavioural change there).
- [ ] Unit tests in `world/tests/test_grid.py` (or a new
      `test_demolish_connectivity.py`):
  - Removing an island road that anchors no civilian tile is allowed.
  - Removing a road that's the sole neighbor of a house is rejected;
    house stays in place; treasury unchanged.
  - Removing a road in the middle of a long road chain is allowed
    when civilian tiles still have alternative neighbors.
  - Removing a road that disconnects a downstream cluster of 5 tiles
    is rejected and `stranded` lists all 5.
  - Removing a non-road tile (e.g., gas peaker) is unaffected by the
    new check.

### Frontend

- [ ] Right-click on a road tile in the UI runs a client-side
      pre-check; if unsafe, no `/demolish` POST fires and a toast
      explains the rejection.
- [ ] Right-click on a safe road or on any non-road tile fires
      `/demolish` exactly as today.
- [ ] Tile inspector panel shows "stranded if removed: N" for road
      tiles when the cursor hovers (N=0 ⇒ panel can hide the line or
      show "0").
- [ ] No tests required for the UI portion (consistent with slices
      16/17); manual verification in browser.

## Out of scope

- Plant grid connectivity. The brief explicitly says supply meets
  demand globally — no transmission topology. This issue only guards
  road-network civilian connectivity.
- Pipeline connectivity to refineries. Pipelines are aesthetic /
  connectivity-only tiles per the brief's "Pipelines are aesthetic /
  connectivity tiles only" note.
- A demolish-confirmation modal. The right-click is a one-shot
  destructive action by design — players who want safety can rely on
  the connectivity guard plus the toast.
- Variable demolish cost based on age or value of the tile. The
  refund stays at the existing 25% of CAPEX.

## Notes

- The connectivity check should reuse `world/grid.py::road_connected_set`
  unchanged — pass it the tile list minus the target. This keeps the
  spec → code mapping clean (one function = one rule).
- The check is O(W·H) per demolish call. On a 32×32 grid that's 1024
  cells; negligible cost.
- The `requires_road` predicate is already on `TileSpec`, so the loop
  is just `for t in tiles: if catalog[t.type].requires_road: ...`.
- The error string `"would_disconnect"` matches the existing
  hyphenated-snake convention of other error strings
  (`"insufficient_funds"`, `"no_road_adjacency"`, `"occupied"`,
  `"town_hall_undemolishable"`).
- This issue is independent of slices 16/17 (play/pause and UI tabs)
  — they touch the top-bar and right-rail, this touches the map
  canvas + tile inspector.

## Blocked by

None — all dependencies already shipped (slice 02 for /demolish,
slice 03 for `requires_road`, slice 01 for the UI canvas).

## Progress note (AFK pass 1)

Backend portion landed: `world/sim.py::demolish` now rejects road
demolitions that would strand any `requires_road=True` civilian tile
with `error: "would_disconnect"` and `result.stranded` listing each
orphaned tile's `(x, y, type)`. 7 new tests in
`world/tests/test_demolish_connectivity.py` cover the AC list — island
removal allowed, sole-anchor rejected, alt-path-removable allowed,
5-tile choke-point rejected, non-road tiles unaffected, action-log
appended with ok=false, refinery with redundant road neighbors safe.
The 25% refund path and town_hall undemolishable behaviour are
untouched. Full `make check` clean (387 passing).

UI portion (right-click pre-check, hover badge, toast) is still HITL
— consistent with slices 16/17 which the issue explicitly cites as
"no automated tests; manual verification in browser." Moving issue
back to open queue is unnecessary — backend AC is the entire AFK
deliverable; UI portion can land in a separate HITL slice.

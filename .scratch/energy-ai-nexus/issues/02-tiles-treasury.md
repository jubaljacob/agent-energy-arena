---
Status: needs-triage
---

# 02 — Surface tiles, treasury, town hall, adjacency

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## What to build

A human can build and demolish civilian tiles (road, house, commercial, industrial, park, pipeline) on the 32×32 surface grid. Treasury accounting works correctly — CAPEX is deducted on build, 25% of CAPEX is refunded on demolish, daily OPEX accrues during `/step`. The town hall is placed at the world center on `/reset` and cannot be demolished.

Adjacency rules from the brief's §4.12 are enforced: house, commercial, industrial, and refinery tiles must be 4-connected via roads (or town hall, which counts as a road for adjacency). Power plants and wells are exempt. The road-connected check is a flood-fill from any road tile.

The UI's left-rail build menu lists each tile type with CAPEX and a one-line description. Selecting a tile type enters "build mode"; clicking on the grid attempts placement. The cursor shows valid/invalid feedback before the click commits (cash check, adjacency check, occupancy check). Failed attempts return a human-readable error string from the API and surface as a UI toast.

Action ordering is submission-order, best-effort: each `/build` and `/demolish` POST commits independently and returns its own `{ok, error?, treasury_after, result}`. The action log records every attempt including rejections.

## Acceptance criteria

- [ ] On `/reset`, a town hall is placed at `(WORLD_W/2, WORLD_H/2)` providing 100 housing capacity and 30 jobs.
- [ ] `POST /build { "tile_type": "road", "x": ..., "y": ... }` succeeds when the tile is empty and treasury covers CAPEX; treasury is decremented immediately.
- [ ] `POST /build { "tile_type": "house", ... }` requires road adjacency (4-connected from any road or town hall) and rejects with `"no_road_adjacency"` otherwise.
- [ ] `POST /build` rejects with `"insufficient_funds"` when treasury < CAPEX. World state is unchanged.
- [ ] `POST /build` rejects with `"tile_occupied"` when the target (x, y) already has a tile. Town hall is treated as an occupied tile.
- [ ] `POST /demolish { "x": ..., "y": ... }` removes the tile and refunds 25% of CAPEX. Town hall demolish is rejected with `"cannot_demolish_townhall"`.
- [ ] Daily OPEX for each placed tile accrues during `/step`; treasury reflects the deduction in the daily summary.
- [ ] `/state.tiles` lists every placed tile with id, type, position, built_day, operational status.
- [ ] The UI build menu shows each civilian tile type (road, house, commercial, industrial, park, pipeline) with CAPEX label.
- [ ] Selecting a tile type and hovering over the grid shows a valid (green) or invalid (red) overlay.
- [ ] Clicking a valid cell places the tile; the UI updates within 500ms.
- [ ] Tests in `world/tests/test_grid.py` cover: road-adjacency flood-fill correctness; town hall counts as road; demolish refund math; insufficient_funds path; tile_occupied path.

## Blocked by

- 01 — Server skeleton + determinism foundation

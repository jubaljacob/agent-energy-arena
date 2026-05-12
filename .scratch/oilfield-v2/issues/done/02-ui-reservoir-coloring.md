# 02 — UI: cross-section colors voxels by `reservoir_id`

Status: needs-triage

## Parent

`.scratch/oilfield-v2/PRD.md`

## What to build

The subsurface cross-section colors each revealed HC voxel by its `reservoir_id` using a small palette rotation, so a player can recognize a reservoir at a glance and infer reservoir identity from a survey at the edge. Unrevealed voxels remain outlined as today. The well popup shows `reservoir_id`.

## Acceptance criteria

- [ ] Cross-section render in `world/ui/app.js` reads `reservoir_id` from `/reservoirs` voxel rows and colors HC voxels via an 8-color rotation (`reservoir_id % 8`). Hue choices are visually distinct on the existing canvas background.
- [ ] Sub-legend updates: replace "color = oil estimate" with "color = reservoir_id" (or equivalent). The estimate stays accessible via hover/popup.
- [ ] Well popup shows `Reservoir: R{reservoir_id}` (or `—` if `None`).
- [ ] Manual verification: build seed 42, run a couple of surveys, confirm voxels in the same connected blob share a color and the well popup reservoir id matches the cross-section.
- [ ] `make check` passes (no test changes expected for this UI-only slice).

## Blocked by

- `.scratch/oilfield-v2/issues/01-bfs-reservoirs.md`

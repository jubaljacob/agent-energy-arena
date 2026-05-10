---
Status: needs-triage
---

# 06 — Subsurface generation + seismic survey + cross-section UI

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## What to build

`world/subsurface.py` generates the 32×32×16 voxel grid on `/reset`. Per the brief's §3.5, 3–7 reservoir blobs are placed at random centers (`z ∈ [4, WORLD_D-2]`) with radius `r ∈ [3, 6]`. For each voxel within Manhattan distance r of a blob center, with probability `0.6 × (1 - dist/r)`, the voxel is marked hydrocarbon-bearing with porosity, permeability, and oil saturation per the brief's distributions. Total OOIP across all reservoirs is in the 5–15M bbl range expected from the brief.

`POST /survey { "x", "y", "size" }` reveals a `size × size` column at all depths and returns a list of voxel records `{x, y, z, oil_estimate_bbl, perm_estimate_md}` with noise per the brief's §4.10. **Cost scales quadratically** per the PRD: `cost = 15_000 × (size / 8) ** 2`. Size is bounded at `[4, 16]`.

`/state.reservoirs_revealed` returns top-K=10 voxels by current best estimate of `oil × perm`, plus aggregate stats (n_revealed_voxels, total_estimated_oil_remaining_bbl, n_explored_columns). `GET /reservoirs?min_oil=N&top_k=M` returns paginated detail. The full per-voxel survey history (one entry per resurvey) is preserved server-side; resurveys produce independent noise samples on `sim_rng`.

UI gains a subsurface tab with a cross-section view. The user picks an axis (X or Y) and a slice index; the perpendicular plane renders revealed voxels colored by oil estimate and unrevealed voxels as outlines.

## Acceptance criteria

- [ ] On `/reset` with seed 42, between 3 and 7 reservoir blobs are generated; total OOIP across all reservoirs falls in [5M, 15M] bbl.
- [ ] Reservoir generation is reproducible: two `/reset` calls with the same seed produce byte-identical voxel grids.
- [ ] `POST /survey { "x": 16, "y": 16, "size": 8 }` deducts $15,000 and returns a list of 8×8×WORLD_D = 1024 voxel records.
- [ ] `POST /survey { "x": 16, "y": 16, "size": 4 }` deducts $3,750.
- [ ] `POST /survey { "x": 16, "y": 16, "size": 16 }` deducts $60,000.
- [ ] `POST /survey` rejects with `"invalid_size"` when size < 4 or size > 16.
- [ ] Resurveying the same column returns independent noise samples (different `oil_estimate_bbl` values for the same voxel across two calls).
- [ ] Server-side, every survey appends a new estimate entry per voxel to that voxel's history.
- [ ] `/state.reservoirs_revealed` returns top-K=10 voxels plus aggregate stats; size is bounded regardless of game progress.
- [ ] `GET /reservoirs?min_oil=5000&top_k=20` returns up to 20 voxels with current best `oil_estimate_bbl ≥ 5000`.
- [ ] UI subsurface tab renders a cross-section with axis/slice selectors.
- [ ] Revealed voxels render colored by oil estimate; unrevealed voxels render as outlines.
- [ ] Tests in `world/tests/test_subsurface.py` cover: reservoir generation reproducibility, survey cost scaling, voxel pool clipping at grid edges, independent noise on resurvey.

## Blocked by

- 01 — Server skeleton + determinism foundation

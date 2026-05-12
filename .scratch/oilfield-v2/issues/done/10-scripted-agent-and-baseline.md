# 10 — Scripted agent + `seed_42.json` regen

Status: needs-triage

## Parent

`.scratch/oilfield-v2/PRD.md`

## What to build

Update `agents/scripted.py` so the scripted baseline plays the new oilfield mechanics correctly: smaller surveys, pipeline-laying, same-reservoir injector siting at safe distance, balanced injection/production rates. Regenerate `baselines/seed_42.json` from the updated agent so scoring tests pin to the new contract.

## Acceptance criteria

- [ ] Scripted agent surveys at `size = 4` (was 8).
- [ ] After drilling a producer, scripted agent lays pipeline along an L-shaped path to the nearest existing refinery (or queues the path as part of the refinery-build flow if no refinery exists yet). Resulting network connects the producer's adjacent tile to the refinery's adjacent tile.
- [ ] Scripted agent drills injectors in the same `reservoir_id` as their producer, Chebyshev distance ≥ 2 from the producer's target. Read `reservoir_id` from `/state.wells` (do not recompute connectivity client-side).
- [ ] Scripted agent sets injection setpoint roughly equal to producer setpoint so `pressure_boost > 0` is achievable.
- [ ] `baselines/seed_42.json` regenerated from a fresh end-to-end scripted run on seed 42.
- [ ] `world/tests/test_determinism.py` updated and passes: two `reset(seed=42)` + identical action sequence produce byte-identical `state.tiles`, `state.wells`, `subsurface.voxels` (with `reservoir_id`), `treasury`, `population`.
- [ ] Any scoring test that reads `baselines/seed_42.json` passes against the regenerated baseline.
- [ ] `make check` passes.

## Blocked by

- `.scratch/oilfield-v2/issues/02-ui-reservoir-coloring.md`
- `.scratch/oilfield-v2/issues/04-rate-pressure-observability.md`
- `.scratch/oilfield-v2/issues/05-quadratic-drill-cost.md`
- `.scratch/oilfield-v2/issues/06-survey-cost-rescale.md`
- `.scratch/oilfield-v2/issues/09-pipelines-ui.md`

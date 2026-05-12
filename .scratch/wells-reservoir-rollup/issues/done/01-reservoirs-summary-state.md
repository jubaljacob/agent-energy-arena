---
Status: needs-triage
---

# `reservoirs_summary` in `/state` + LLM `RESERVOIRS` block

## Parent

`.scratch/wells-reservoir-rollup/PRD.md`

## What to build

A new top-level `reservoirs_summary` array on the `/state` payload, one entry per reservoir that has at least one revealed (surveyed-at-least-once) voxel. Each entry carries the player-facing rollup the Wells tab will later render: noisy estimated bbl, remaining bbl (`estimated − cumulative_produced`, allowed to go negative), revealed-voxel count, cumulative produced + injected sums, and sorted lists of the producer/injector well ids in that reservoir. Unsurveyed reservoirs are omitted entirely (no information leak).

The aggregation is a new pure helper in `world/subsurface.py` called from `world/sim.py:state_dict()`. The existing voxel-top-K helper currently named `reservoirs_summary` is renamed to `reservoirs_voxel_summary` to free the name for the new rollup helper; its single caller in `sim.py` is updated.

The LLM `state_summary.py` gains a `RESERVOIRS (N):` block immediately above the existing per-voxel block, one line per entry in the new array. The existing per-voxel block label is renamed from `RESERVOIRS top-30 revealed voxels:` to `RESERVOIRS_VOXELS_TOP-30 revealed voxels:` so the two labels are unambiguous.

## Acceptance criteria

- [ ] New pure helper `reservoirs_summary(grid, wells) -> list[dict]` in `world/subsurface.py`. Returns one entry per `reservoir_id` that has ≥1 revealed voxel.
- [ ] Each entry has exactly these keys: `reservoir_id`, `estimated_bbl`, `remaining_bbl`, `n_revealed_voxels`, `cumulative_produced_bbl`, `cumulative_injected_bbl`, `producer_ids`, `injector_ids`.
- [ ] `estimated_bbl == Σ latest oil_estimate_bbl over revealed voxels of this reservoir_id`.
- [ ] `remaining_bbl == estimated_bbl − cumulative_produced_bbl` exactly. Negative values are allowed and not clamped.
- [ ] `cumulative_produced_bbl == Σ Well.cumulative_produced_bbl over production wells with matching reservoir_id`. Null-reservoir wells contribute to NO reservoir.
- [ ] `cumulative_injected_bbl == Σ Well.cumulative_injected_bbl over injection wells with matching reservoir_id`. Null-reservoir wells contribute to NO reservoir.
- [ ] `producer_ids` and `injector_ids` are ascending-sorted lists of well-id strings.
- [ ] Reservoirs with revealed voxels but zero wells appear with empty `producer_ids` and `injector_ids`.
- [ ] Reservoirs with zero revealed voxels do NOT appear in the output.
- [ ] Entries are sorted by ascending `reservoir_id`.
- [ ] Existing `reservoirs_summary` helper renamed to `reservoirs_voxel_summary` in `world/subsurface.py`; its single caller in `world/sim.py` updated.
- [ ] `world/sim.py:state_dict()` calls the new helper and exposes the result as the top-level `reservoirs_summary` key.
- [ ] `agents/state_summary.py:summarize_state()` emits a `RESERVOIRS (N):` block above the existing voxel block. Each line carries `R{id} est=… remain=… revealed=…vox wells={P}P+{I}I produced=… injected=…`. Numbers compressed with the existing `_fmt`/`_round` helpers.
- [ ] Existing per-voxel block label renamed in `agents/state_summary.py` from `RESERVOIRS top-30 revealed voxels:` to `RESERVOIRS_VOXELS_TOP-30 revealed voxels:`.
- [ ] New unit tests in `world/tests/test_subsurface.py` cover: estimated/remaining math, resurvey grows estimated, negative-remaining is allowed, empty-id-lists case, reservoir-omission case, ascending sort of entries and of id lists.
- [ ] `/state` smoke assertion in `world/tests/test_api_smoke.py`: top-level `reservoirs_summary` key is present in the response (may be `[]`).
- [ ] LLM state-summary test pin in `agents/tests/test_llm_react.py`: existing voxel-block test updated to expect the renamed label; new test asserts the `RESERVOIRS (` block exists, contains `R{id}`, `est=`, `remain=`, `wells=` substrings, and sits above the voxel block.
- [ ] `make check` passes (ruff, format, mypy, pytest).

## Blocked by

None — can start immediately.

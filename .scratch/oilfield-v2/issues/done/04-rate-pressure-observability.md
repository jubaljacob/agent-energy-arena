# 04 — Rate-based pressure: state + UI observability

Status: needs-triage

## Parent

`.scratch/oilfield-v2/PRD.md`

## What to build

Expose the rate-based pressure inputs and result through `/state` and the well popup so a player (and an agent author) can audit why a producer is or isn't getting boost. No physics changes — this slice is read-only telemetry on top of issue 03.

## Acceptance criteria

- [ ] `/state.wells[*]` adds `yesterday_rate_bbl_day` on every well.
- [ ] `/state.wells[*]` for producers adds `yesterday_inj_rate_bbl_day` (sum over qualifying injectors) and `pressure_boost` (the value used in today's production calc).
- [ ] Well popup for producers shows: `Reservoir`, `Pressure boost`, `Yesterday prod rate`, `Yesterday inj rate (qualifying)`.
- [ ] Well popup for injectors shows: `Reservoir`, `Yesterday inj rate`.
- [ ] Test: extend `world/tests/test_production.py` to assert `state_dict()` reports `pressure_boost` and `yesterday_inj_rate_bbl_day` consistent with the value used in the production calc for a same-reservoir Chebyshev-2 pair.
- [ ] `make check` passes.

## Blocked by

- `.scratch/oilfield-v2/issues/03-rate-pressure-physics.md`

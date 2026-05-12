# 07 — Regenerate baseline for seed 42

Status: needs-triage

## Parent

`.scratch/facility-economics-popup/PRD.md`

## What to build

Run the scripted agent on seed 42 for the full game horizon, capture the new `(population, treasury)` reference values, and commit the updated `baselines/seed_42.json`. Required because Issues 01–06 add two new revenue streams (commercial + industrial) that materially shift the scripted agent's end-of-game treasury. The current `t_ref = -896034.27` is no longer correct.

Marked **HITL**: a maintainer needs to eyeball the regen output, confirm the scoring shift is acceptable (city still survives 365 days; renewable share term still scores sanely), and decide whether the new baseline should be merged or whether pricing constants need re-tuning first.

## Acceptance criteria

- [ ] `make score` runs successfully on seed 42 with the scripted agent and completes the full game horizon.
- [ ] `baselines/seed_42.json` is updated with new `p_ref` and `t_ref` values derived from the scripted-agent run.
- [ ] The regen is committed as a separate atomic commit so `git bisect` can isolate the scoring shift from the feature commits.
- [ ] Maintainer has confirmed the scripted city still survives 365 days (no premature treasury bankruptcy or population collapse).
- [ ] The `/score` endpoint returns finite, non-negative scores against the new baseline on a fresh `/reset` + replay.
- [ ] Existing scoring tests (`world/tests/test_scoring.py` or equivalent) pass against the new baseline.
- [ ] `make check` passes on the regen commit.

## Blocked by

- Issue 01 — industrial revenue
- Issue 02 — commercial revenue
- Issue 03 — plant kwh_served (changes per-plant accounting, may shift dispatch-driven treasury delta)
- Issue 04 — plant fuel/carbon rows (display only; included for safety in case the refactor touches the same code paths)
- Issue 05 — refinery revenue display (no behavior change but bundled for the full feature)
- Issue 06 — well revenue display (no behavior change but bundled for the full feature)

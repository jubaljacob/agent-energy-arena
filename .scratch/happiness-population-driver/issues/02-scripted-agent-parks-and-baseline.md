# 02 — Scripted-agent park rule + baseline regen

Status: needs-triage
Type: AFK

## Parent

`.scratch/happiness-population-driver/PRD.md`

## What to build

Teach the scripted reference agent to build parks during its Bootstrap phase so that the seed-42 baseline produces a city with happiness > 1.0 and meaningful growth under the new velocity model. Regenerate `baselines/seed_42.json` and commit it alongside this slice so the on-disk reference is consistent with the shipped rules.

The park-placement rule: when the agent has just placed a house (or a cluster of houses) in a Bootstrap step, find a road-adjacent buildable tile within Chebyshev radius 2 of the cluster and place a `park` there if the treasury threshold permits. The rule is deterministic and runs in the same decision-priority slot as other Bootstrap building actions. After Bootstrap completes, every house cluster has at least one park inside its Chebyshev-2 noise window.

## Acceptance criteria

- [ ] `agents/scripted.py` Bootstrap phase places at least one `park` tile within Chebyshev radius 2 of every house cluster it builds, on a road-adjacent buildable square, respecting the existing `MIN_TREASURY_BUILD` threshold.
- [ ] The rule is deterministic: two runs of `python -m agents.scripted --seed 42` produce byte-identical action logs.
- [ ] `python -m agents.scripted --seed 42 --output baselines/seed_42.json` runs to completion under the new world rules.
- [ ] The regenerated `baselines/seed_42.json` is committed in this slice. The new `P_ref` reflects positive population growth over the 10-year game (i.e. final pop > starting pop = 100).
- [ ] A smoke test in `agents/tests/` asserts that `ScriptedAgent` on seed 42 reaches `pop ≥ 0.8 · committed_P_ref` at game end, to guard against regression.
- [ ] A determinism test asserts that two consecutive baseline-regeneration runs produce byte-identical JSON content.
- [ ] `make check` is green.

## Blocked by

Slice 01 (`01-velocity-population-model.md`) — the new velocity model must be in place before the scripted-agent run produces a meaningful baseline.

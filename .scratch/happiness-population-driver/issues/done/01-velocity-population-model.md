# 01 — Velocity-driven population model

Status: needs-triage
Type: AFK

## Parent

`.scratch/happiness-population-driver/PRD.md`

## What to build

Replace the four-branch cascade in `update_population` with a continuous signed-velocity model around `h_neutral = 1.0`. All world-side changes ship end-to-end in this slice: the new formula, the pure helpers, the float-typed population, the API serializer cast, the workforce wiring, and the full test suite.

After this slice lands:

- A vanilla city (no parks, no penalties) holds its population indefinitely (happiness 1.0 → zero velocity).
- An unhappy city (any happiness < 1.0) actively bleeds population at a rate proportional to the deficit.
- A happy city (happiness > 1.0 via parks) grows at a rate proportional to the surplus, clamped by available jobs and housing.
- Fractional sub-1/day deltas accumulate across days instead of being truncated.
- The `/state` and `/step` API responses still report an integer population.
- `make check` is green; the determinism test continues to pass.

The committed `baselines/seed_42.json` will be *stale* after this slice — that is expected and addressed in slice 02.

## Acceptance criteria

- [ ] `happiness_velocity(pop, happiness, capacity, jobs) -> float` exists as a pure module-level function in `world/population.py` with no world reference and no mutation.
- [ ] `apply_structural_clamps(pop, capacity, jobs) -> float` exists as a pure module-level function in `world/population.py` encapsulating the housing exodus (`pop = max(capacity, pop − 5)`) and jobs floor (`pop = max(jobs/0.7, pop · 0.99)`) logic.
- [ ] `update_population(world)` reads as a thin orchestrator: compute happiness (existing logic preserved), call `happiness_velocity`, call `apply_structural_clamps`, persist to state, trigger workforce churn on integer transitions, accrue daily tax.
- [ ] The four-branch `if jobs >= pop … elif capacity < pop … elif jobs < 0.7·pop … elif happiness < 0.3 …` cascade no longer appears in the source.
- [ ] `WorldState.population` is typed `float` (was `int`).
- [ ] The `/state` and `/step` (daily summary) endpoints emit `int(state.population)` on the wire — no external consumer changes.
- [ ] `workforce.drain_n` and `workforce.hire_to_fill` are invoked from `update_population` based on `int(pop_after) − int(pop_before)`; fractional updates do not trigger workforce hooks.
- [ ] Unit tests for `happiness_velocity` cover: neutral fixed-point at h=1.0; positive delta proportional to `pop·(h−1)` for h>1; negative delta proportional for h<1; maximum-magnitude negative at h=0.0 (`−0.012·pop`); maximum-magnitude positive at h=1.5 (`+0.006·pop`); upward clamp to `min(jobs−pop, capacity−pop)`; no downward clamp on structural state; documented asymmetry (max emigration is 2× max growth at the cap edges).
- [ ] Unit tests for `apply_structural_clamps` cover: no-op when `pop ≤ capacity` and `jobs ≥ 0.7·pop`; housing-exodus drain bounded by 5/day; jobs-floor combination of `max(jobs/0.7, pop·0.99)`; both clamps interacting when both conditions hold.
- [ ] Integration tests for `update_population` cover: happy city (h=1.2) grows monotonically over 30 ticks; neutral city (h=1.0) holds within fractional drift; unhappy city (h=0.7) bleeds along the closed-form `pop · (1 + 0.012·(h−1))^N` trajectory; small-pop fractional accumulation crosses integer boundaries on schedule (regression test for the truncation bug); workforce hooks fire on and only on integer transitions; daily tax accrual matches post-update integer population.
- [ ] The legacy tests in `world/tests/test_population.py` that assert on the old gate semantics (`happiness ≥ 0.5 → grow`, `happiness < 0.5 → decline`) are removed. Tests targeting the happiness *number* itself (park benefit, noise penalty, coal proximity, blackout/brownout penalties) are preserved.
- [ ] `world/tests/test_determinism.py` continues to pass without modification.
- [ ] `make check` (lint + format-check + typecheck + test) is green.

## Blocked by

None — can start immediately.

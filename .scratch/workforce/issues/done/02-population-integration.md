---
Status: needs-triage
---

# 02 — Population integration: drain order + growth-branch auto-hire

## Parent

[PRD: World v2 — Workforce & Per-Facility Staffing](../PRD.md)

## What to build

Wires `workforce.hire_to_fill` and `workforce.drain_n` into `world/population.update_population` so that day-over-day population changes route through the workforce model. After this slice, growing population auto-fills vacancies oldest-first, and shrinking population removes unemployed citizens first then fires workers newest-first.

The four-branch cascade in `update_population` keeps its current gates and rates unchanged (status quo growth threshold, exodus rule, 0.7× job-decline threshold, 0.5 happiness threshold). Only the path that applies the population delta changes.

### Implementation details

**`world/population.py`** — modify `update_population`:

The branches currently mutate a local `pop` float and write `state.population = max(0, int(pop))` at the end. Refactor so that:

1. Compute `pop_before = state.population` (int, before any branch runs).
2. Run the same four-branch cascade to derive `target_pop` (the integer population the branch dictates). Keep the existing happiness calculation, jobs/capacity sums, and branch gates byte-identical.
3. Compute `delta = pop_before - target_pop`:
   - `delta > 0` (any decline branch): call `workforce.drain_n(state, delta)`. `drain_n` mutates `state.population` and per-producer `staffed_jobs` together. **Do not** also assign `state.population = target_pop` — `drain_n` already does the population mutation.
   - `delta < 0` (growth branch): assign `state.population = target_pop` first, then call `workforce.hire_to_fill(state)` so the new arrivals fill vacancies oldest-first.
   - `delta == 0`: no-op on workforce side; still write `state.happiness` and accrue tax.
4. Tax accrual stays unchanged: `tax = DAILY_TAX_PER_CAPITA × state.population`. The PRD explicitly keeps the `$4 × total_population` rule — both employed and unemployed contribute. **Do not** switch to `$4 × employed`.

The growth gate continues to use catalog jobs sum (`jobs = sum(t.jobs for t in state.tiles)`), not `employed`. Workforce changes what happens to the new arrivals (auto-hire) and to those who leave (drain order), not what triggers growth/decline.

**Town hall protection** falls out naturally: the town hall has the smallest `(built_day, id_string)` tuple, so it is the last facility to be drained newest-first. It is never demolishable, so its `staffed_jobs` only drops when the population has fallen below 30 with no younger producer left to fire.

**Failed-plant interaction**: `operational=False` plants are still listed in `workforce.producers(state)` (the PRD pins this: workers stay assigned through the failure). Slice 01 already gets this right because `producers` filters on `spec.jobs > 0`, not on `operational`. No change needed here, but verify with a regression test.

### Tests to add in this slice

Add to `world/tests/test_population.py` (existing module already exercises each cascade branch by injecting tiles directly — follow the existing pattern):

- **Growth branch auto-hire**: pop=100 with town_hall 30/30 and a freshly-injected industrial at `staffed_jobs=0` (30 vacancies). Inject enough houses to lift capacity above 100, ensure jobs sum is above pop, ensure happiness >= 0.5. Call `update_population`. New `state.population` reflects the growth-rate formula; new arrivals are auto-hired into industrial oldest-first; verify `industrial.staffed_jobs == min(growth, 30)` and `employed = 30 + industrial.staffed_jobs`.
- **Exodus branch (capacity < pop)**: pop=60, town_hall=30/30, industrial=30/30 (unemployed=0). Demolish housing (or never build any) so `capacity < pop`. After `update_population`, `state.population` drops by 5 (the existing exodus rate). All 5 leavers come from `drain_n`: since unemployed=0, the youngest producer (industrial) loses 5 staff → `industrial.staffed_jobs=25`, town_hall untouched.
- **Exodus branch with unemployed buffer**: same setup but pop=80, town_hall=30/30, industrial=30/30, unemployed=20, `capacity=70`. After update_population, pop drops to 75 (exodus rule = max(capacity, pop-5) = 75). The 5 leavers come from the unemployed pool — both staffing levels stay at 30/30, new unemployed=15.
- **Job-decline branch (jobs < 0.7×pop)**: pop=100, town_hall=30/30, no other producers (jobs sum=30, 30 < 0.7×100=70). After update_population, `pop` drops to `max(jobs/0.7, pop*0.99) = max(42.85, 99) = 99` so `delta=1`. Unemployed=70 absorbs the 1 leaver; town_hall stays at 30/30. (If the rule yields a larger drop in a different setup, the drain still goes unemployed-first.)
- **Happiness-decline branch (happiness < 0.5)**: force happiness below 0.5 via `yesterday_blackout_hours` (e.g., 12h × 0.05 = 0.6 drop, leaving happiness around 0.4). pop=60 with town_hall=30/30 and industrial=30/30, unemployed=0. `pop_after = pop * 0.99 = 59.4 → 59` so `delta=1`. Since unemployed=0, industrial loses 1 → staffed=29/30, town_hall untouched.
- **Newest-first fire order with multiple young producers**: pop=90, town_hall (day 0) 30/30, coal_plant (day 5) 8/8, industrial (day 10) 30/30, refinery (day 15) 22/25, unemployed=0. Trigger a decline that drains 10 people. Refinery is youngest with 22 staff; drain takes 10 from refinery → refinery 12/25. Industrial/coal/town_hall untouched.
- **Mixed drain (unemployed + fire)**: pop=50, town_hall 30/30 and industrial 15/30 (built later), unemployed=5. Trigger a decline of delta=10. First 5 leave from unemployed (pop=45), remaining 5 fire from youngest = industrial → industrial 10/30 and pop=40.
- **Tax base regression**: pop=100 with employed=50 and unemployed=50. After a decline branch fires that reduces pop to 95, assert tax accrued = $4 × 95 = $380 (not `$4 × employed`).
- **Failed-plant retains workers across day boundary**: build a coal plant, manually flip `operational=False`, run `update_population` through a happiness-decline branch with delta=1. The fire order still considers the failed plant in the newest-first list (so its staff may be drained), but a regression case where only the failed plant is younger than the town hall verifies that `operational` is **not** a filter for `producers`.

### Determinism

- `update_population` still consumes no RNG; the per-day RNG-budget contract is preserved.
- The drain order is deterministic by `(creation_day, id_string)` from slice 01.

## Acceptance criteria

- [ ] `update_population` routes its population delta through `workforce.drain_n` for all three decline branches and through `workforce.hire_to_fill` for the growth branch.
- [ ] Tax accrual remains `$4 × state.population` after the workforce mutation completes.
- [ ] All cascade-branch tests from `test_population.py` continue to pass for total `state.population`; new staffing-level assertions land alongside them.
- [ ] Drain order is unemployed-first; once unemployed is exhausted, the newest producer (by `(creation_day, id_string)` descending) loses staff one at a time.
- [ ] Growth-branch new arrivals are auto-hired oldest-first into open vacancies.
- [ ] Failed (`operational=False`) plants are not excluded from the producer set.
- [ ] `make check` is green.

## Blocked by

- 01 — Workforce foundation: module, state field, catalog, allocator hooks, API surface

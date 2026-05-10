---
Status: needs-triage
---

# 22 — Blackout doesn't actually punish population

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## Problem

A grid that blacks out for hours every day should bleed pop. In
practice, it doesn't — population stays flat under any blackout
intensity. Two compounding bugs in the happiness/population pipeline:

1. **Hourly happiness writes are clobbered.** `world/sim.py:537-548`
   decrements `state.happiness` in-place during blackout/brownout
   hours (-0.20 per blackout hour, -0.05·(1−R) per brownout hour).
   Then `world/population.py:47-51` *reassigns* happiness from
   scratch each end-of-day:

       happiness = 1.0
       happiness += 0.05 * max(0, park_count - 1)
       happiness -= 0.10 * (state.yesterday_blackout_hours / 24.0)
       happiness -= 0.05 * coal_houses_within_3 / max(1, house_count)

   The hourly decrements never reach `update_population` — they're
   overwritten the moment the day ends.

2. **The end-of-day formula caps blackout damage at −0.10.** Even a
   24/24-hour blackout day produces `happiness ≥ 0.90`, which is
   well above the `< 0.5` threshold that triggers the
   happiness-decline branch (`pop = pop * 0.99`). So the decline
   path never fires from blackouts alone.

Net effect: an agent (or human) can run with continuous blackouts
indefinitely without losing population. The treasury still bleeds via
`BLACKOUT_PENALTY_HOUR` ($5k/h), but pop is invariant — contradicting
PRD §"Demand" / brief §4.4 which describe blackouts as costing
happiness and (through happiness) population.

## What to build

Pick one of two fixes (the second is simpler and matches the brief
more directly; the first preserves the hourly-resolution intent of
sim.py's existing decrements):

### Option A — Persist hourly decrements

Stop overwriting `state.happiness` in `update_population`. Instead:

- The hourly path stays as today (decrement on blackout/brownout).
- `update_population` *adjusts* happiness rather than reassigning:
  add park bonus, add coal-proximity penalty, then clip to [0, 1.5]
  WITHOUT zeroing the hourly accumulation.
- Add a daily *recovery* term so happiness regenerates on a
  blackout-free day (e.g., `happiness = min(1.5, happiness + 0.05)`
  if no blackout/brownout that day). Without recovery, one bad day
  permanently anchors happiness low.

### Option B — Bigger blackout coefficient + use `today_blackout_hours`

Keep the reassignment shape but make blackouts actually bite:

- Bump `-0.10 * (yesterday_blackout_hours / 24.0)` to
  `-0.50 * (yesterday_blackout_hours / 24.0)` (full-day blackout
  drops happiness by 0.5 → 1.0 − 0.5 = 0.5, exactly on the decline
  threshold; >12h/day blackouts cross into decline).
- Optionally also include brownout hours at a lighter weight, e.g.,
  `-0.20 * (yesterday_brownout_hours / 24.0)` (requires a new
  `state.yesterday_brownout_hours` field; mirror the existing
  `yesterday_blackout_hours` plumbing).

Option B is the recommended starting point — minimal surface area,
preserves the existing reassignment idiom, no recovery dynamics to
tune.

Either way, the priority is: *blackouts must produce a measurable pop
decline within a week of sustained occurrence on default tunables.*

## Acceptance criteria

- [ ] After 7 simulated days with `≥ 12 blackout hours/day`,
      `state.population < starting_pop` by at least 1 person on a
      world otherwise configured to support that population (jobs ≥
      pop, capacity > pop, no other decline branches firing).
- [ ] On a world with zero blackout hours, happiness stabilises at
      `>= 1.0` and population grows per the existing rules — no
      regression on the happy-path.
- [ ] `state.happiness` exposed via `/state` reflects the chosen
      design (either the hourly-accumulated value, or the daily
      reassignment with the new coefficient). Pin the value via a
      unit test on a fixture.
- [ ] Unit tests in `world/tests/test_population.py`:
  - `test_full_day_blackout_drops_happiness_below_threshold` — 24h
    of blackout in a single day puts `happiness < 0.5`.
  - `test_sustained_blackout_declines_population` — 7 days × 12h
    blackout produces `pop < starting_pop`.
  - `test_zero_blackout_no_pop_decline` — no blackout, jobs/capacity
    sufficient → pop grows or stays put.
  - `test_blackout_penalty_treasury_unchanged` — the existing
    `BLACKOUT_PENALTY_HOUR` treasury debit still fires per blackout
    hour (no double-counting from the new path).
- [ ] No regression on `world/tests/test_dispatch.py` blackout-state
      assertions — those test the `R` thresholds, not the happiness
      consequences.
- [ ] No new RNG draws (the bug fix is deterministic; sim_rng /
      forecast_rng / event_rng draw budgets unchanged so step-size
      invariance + same-seed replay still hold).

## Notes

- The existing `BLACKOUT_HAPPINESS_PENALTY = 0.20` constant in
  `world/power.py` (the per-hour decrement) becomes either dead code
  (Option B) or the live path (Option A). If Option B is chosen,
  delete the constant + the `state.happiness -= …` lines in
  `sim.py:537-548` to avoid silent surprise on a future reader.
- The brief's §4.4 lists the per-hour `-0.20` decrement on blackout
  and `-0.05·(1−R)` on brownout. The PRD's §"Demand" doesn't
  override either. So *strictly* the brief favours Option A. But the
  brief also caps happiness at [0, 1.5] each hour, so consecutive
  blackout hours in the same day saturate happiness at 0 by hour 5
  — which the end-of-day update would then reset to ~0.9 anyway.
  The two specifications are inconsistent; this issue is the place
  to pick a coherent answer.
- Slices 14/15 (scripted/LLM agents) currently see `pop` invariant
  under blackouts on stress runs, which makes "blackout response"
  in the scripted agent's priority list a noop except for the
  treasury bleed. After this fix, the existing scripted agent's
  emergency-gas-peaker-on-blackout heuristic becomes meaningful;
  no agent code change required.

## Blocked by

None.

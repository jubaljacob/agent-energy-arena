---
Status: needs-triage
---

# 11 — Events (heatwave, plant failure, fuel price shock, demand surprise, regulatory tightening)

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## What to build

`world/events.py` samples and applies the five event types from the brief's §4.11. Sampling happens once at the start of each simulated day, before the day's hourly ticks. Probabilities and durations come from the brief; **regulatory tightening is capped at 3 cumulative occurrences per game** per the PRD.

| Event | Daily probability | Duration | Effect |
|---|---|---|---|
| Heatwave | 0.003 | 5 days | residential demand × 1.40 |
| Plant failure | 0.001 per fossil plant | 3–7 days uniform | affected plant outputs 0 |
| Fuel price shock | 0.002 | 30 days | gas & coal fuel cost × 2 |
| Demand surprise | 0.003 | 10 days | I+C demand × 1.30 |
| Regulatory tightening | 0.001 | permanent (capped at 3) | carbon price × 1.5 |

Event multipliers wire into the (already-implemented) demand formula, dispatch fuel cost, and carbon cost computations. Plant failures select a specific fossil plant by id (deterministic via `sim_rng`) and zero its output for the duration.

`/state.active_events` lists currently-active events with `started_day`, `ends_day` (for finite-duration), `severity`. `GET /events` returns active + historical events.

UI: events tab shows active events with countdown to expiry and a scrollable history.

## Acceptance criteria

- [ ] At the start of each simulated day, each event type is rolled against its probability using `sim_rng`.
- [ ] Heatwave: when active, residential demand is multiplied by 1.4 (per the split-scope formula from slice 04). Duration always 5 days.
- [ ] Plant failure: probability is `0.001 × n_fossil_plants` per day; on fire, a specific fossil plant is selected deterministically; the plant outputs 0 kW for the sampled duration (3-7 days uniform).
- [ ] Fuel price shock: when active, both gas and coal fuel costs are multiplied by 2 in the dispatch's fuel-cost computation. Duration always 30 days.
- [ ] Demand surprise: when active, industrial+commercial demand is multiplied by 1.3. Duration always 10 days.
- [ ] Regulatory tightening: when fired, current carbon price is multiplied by 1.5; effect is permanent. **The event will not fire more than 3 times per game** (additional rolls are skipped after the cap is reached).
- [ ] At most one of each finite-duration event type is active at a time (additional rolls are skipped while one is active). Plant failure is not subject to this limit (multiple plants can fail concurrently).
- [ ] `/state.active_events` lists currently-active events.
- [ ] `GET /events` returns active and historical events with their started_day and ends_day.
- [ ] UI events tab displays each active event with countdown and lists recent past events.
- [ ] Tests in `world/tests/test_events.py` cover: probability respected over many trials with seeded RNG, durations sampled from spec range, regulatory-tightening cap (4th roll silently skipped), event multipliers correctly wired into demand/dispatch/cost.

## Blocked by

- 05 — Plants + dispatch + balance state + power revenue
- 10 — Carbon and emissions

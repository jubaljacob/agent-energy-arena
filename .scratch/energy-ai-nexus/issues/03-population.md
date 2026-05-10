---
Status: needs-triage
---

# 03 — Population dynamics + tax revenue

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## What to build

`world/population.py` implements the brief's §4.8 population dynamics. Each day during `/step`, the simulator computes housing capacity (sum of all `housing_capacity` from town hall and houses), jobs (sum from town hall, commercial, industrial, refinery), and a happiness score. Population grows when `jobs ≥ pop AND capacity > pop AND happiness ≥ 0.5`, with growth bounded by base rate, capacity headroom, and job headroom. Otherwise population declines per the four cascading rules in the brief.

Happiness in this slice uses placeholder coal-proximity (returns 0 since plants don't exist yet) and placeholder blackout penalty (returns 0 since power doesn't exist yet). The park-count bonus is wired since parks exist after slice 02. Coal proximity will be implemented when plants land in slice 05; blackout penalty in slice 05.

Tax revenue accrues each day at `DAILY_TAX_PER_CAPITA = $4` per resident. The treasury reflects the revenue in the daily summary. Population and happiness are exposed in the top bar of the UI and in `/state.population` / `/state.happiness`.

## Acceptance criteria

- [ ] On a fresh world (pop = 100, town hall jobs = 30, no other tiles), advancing one day applies the job-driven decline rule: `pop = max(jobs/0.7, pop × 0.99)`. After 70 days the population approaches the equilibrium near 30/0.7 ≈ 43.
- [ ] Building enough commercial tiles to bring jobs ≥ pop with happiness ≥ 0.5 causes population to grow at `BASE_GROWTH_RATE × pop × happiness` per day, capped by capacity and jobs headroom.
- [ ] When capacity drops below pop (e.g., demolishing a house), the housing-exodus rule applies: `pop = max(capacity, pop − 5)`.
- [ ] When happiness < 0.5, pop declines at 1%/day.
- [ ] Tax revenue equals `DAILY_TAX_PER_CAPITA × population` per day, accruing to treasury.
- [ ] `/state.population` and `/state.happiness` are exposed and update every `/step`.
- [ ] UI top bar displays population and happiness (e.g., as "Pop: 1,230" and "Happy: 0.85").
- [ ] Tests in `world/tests/test_population.py` cover all four cascading branches (grow, exodus, job-decline, happiness-decline) with explicit input/output cases.

## Blocked by

- 02 — Surface tiles, treasury, town hall, adjacency

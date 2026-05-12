---
Status: needs-triage
---

# 03 — Forecast uses current staffing snapshot

## Parent

[PRD: World v2 — Workforce & Per-Facility Staffing](../PRD.md)

## What to build

A verification + regression-test microslice. The PRD's user story 23 promises that `/forecast` reflects the current labor situation: when an AI agent calls `/forecast`, the simulated future hours use the city's *current* staffing snapshot rather than a re-computed or full-staffed projection.

In practice the current `world/forecast.py` already consumes `world.state` directly (via `total_demand_kw(world.state, h)` and the irradiance/wind projections), so once demand-side efficiency scaling lands in slice 05, forecast output will automatically reflect current staffing through `total_demand_kw`. This slice locks that behaviour in with explicit tests and adds a guard against future regressions (e.g. someone adding a "project future builds" loop into `forecast_records`).

This slice does **not** make `/forecast` simulate future workforce changes (day-over-day hires/fires). The PRD is explicit: forecast uses the snapshot, period. There is no future-population projection.

### Implementation details

Most of this slice is test-only. The likely code touch is zero lines in `world/forecast.py`; if any reviewer reads the slice and finds an internal call that re-derives demand without going through `world.state`, this is the slice to fix it.

**`world/tests/test_forecast.py` (extend)**:

- **Snapshot regression**: Build a city with a fully-staffed industrial tile. Snapshot `forecast_records(world, hours=24)`. Then mutate the industrial's `staffed_jobs` to half its `jobs` count. Snapshot a second forecast. Once slice 05 has landed, the second forecast's `demand_factor` values are strictly lower than the first across the hours where the industrial demand contributes. (Until slice 05 lands, this test will produce identical forecasts because `total_demand_kw` does not yet scale by efficiency — guard with a comment or a `pytest.mark.skip` keyed to a `WORKFORCE_DEMAND_EFFICIENCY` feature flag, and remove the skip in slice 05.)
- **No future-staffing simulation**: Build a city near the growth gate (jobs slightly above pop, capacity well above pop, happiness=1.0). Set the population such that running `update_population` would grow it by ~5 and auto-hire those 5 into a partially-staffed industrial. Without calling `/step`, call `/forecast` for 168 hours. Forecast must use today's staffing (5 vacancies remain), not project that those vacancies will be filled tomorrow. Verify by comparing the forecast demand to a direct `total_demand_kw(state, h)` computation against the *current* (pre-step) state — they must match within the noise envelope (`sigma_at(i, hours) * SIGMA_DEMAND_SCALE`).
- **Determinism contract**: `forecast_records` continues to consume exactly 3 `forecast_rng.standard_normal()` draws per hour. The workforce module consumes no RNG, so this contract is automatic, but assert that two `/forecast` calls with the same `forecast_rng` state yield identical staffing-derived demand.

### Where to look

- `world/forecast.py:58–72` — `_project_truth` calls `total_demand_kw(world.state, h)`. This is the line that picks up efficiency-scaled demand once slice 05 wires `× efficiency` into `total_demand_kw`.
- `world/forecast.py:75–104` — `forecast_records` loop. No staffing logic here today; the slice's job is to keep it that way.

## Acceptance criteria

- [ ] A test exists asserting that two `/forecast` calls — one with full staffing, one with half staffing on the same producer set — produce different demand projections (skipped or xfailed until slice 05; un-skipped in slice 05).
- [ ] A test exists asserting that `/forecast` uses the snapshot at call-time and does not project future hires/fires.
- [ ] A test exists asserting `/forecast` is staffing-snapshot deterministic: same `forecast_rng` state + same staffing snapshot → byte-identical output.
- [ ] No code changes to `world/forecast.py` introduce a re-computation of `staffed_jobs` or a simulated future build loop.
- [ ] `make check` is green.

## Blocked by

- 01 — Workforce foundation (provides `staffed_jobs` field and `/state` surface that tests need)

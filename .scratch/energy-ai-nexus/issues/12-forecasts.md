---
Status: needs-triage
---

# 12 — Forecasts

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## What to build

`GET /forecast?hours=24` returns a list of 24 forecast records per the brief's §4.9. For each future hour `i`, the forecast returns:

- `hour_offset`: i
- `solar_irradiance`: `clip(true_solar × (1 + N(0, σ)), 0, 1)`
- `wind_speed_mps`: `max(0, true_wind + N(0, σ × 5))`
- `demand_factor`: `true_demand × (1 + N(0, σ × 0.3))`

where `σ = 0.05 + 0.25 × (i / hours)`, growing from 0.05 at the next hour to 0.30 at the 24-hour horizon.

**The forecast uses `forecast_rng`, not `sim_rng`.** Resampling is allowed and produces independent noise samples. Calling `/forecast` an arbitrary number of times does not perturb simulation reproducibility.

## Acceptance criteria

- [ ] `GET /forecast` returns a list of 24 records (default `hours=24`).
- [ ] `GET /forecast?hours=12` returns 12 records.
- [ ] Noise sigma grows from 0.05 (i=0) to 0.30 (i=23) for default hours=24.
- [ ] Two consecutive `/forecast` calls return *different* records for the same future hour (independent noise samples).
- [ ] Calling `/forecast` 100 times in a row does not change the simulation state — `/step` after these calls produces the same next-day state as if no `/forecast` calls had been made. Verified by test.
- [ ] The mean of N independent forecast samples for a given hour converges to the true value as N grows (within statistical bounds).
- [ ] Tests in `world/tests/test_forecast.py` cover: noise sigma growth shape, forecast-RNG isolation from simulation state, mean-converges-to-truth across many resamples.

## Blocked by

- 04 — Hourly clock + weather + demand formula

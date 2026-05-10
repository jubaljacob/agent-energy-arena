---
Status: needs-triage
---

# 04 — Hourly clock + weather + demand formula

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## What to build

The internal hourly clock starts running. Each `/step` day expands into 24 internal ticks. `world/weather.py` implements solar irradiance (with `cloud_factor` AR(1) process), wind speed, wind direction, and the per-panel `P_solar_kw` and `turbine_kw` formulas from the brief's §4.1 and §4.2. Weather state is consumed from `sim_rng` per hour.

`world/power.py` implements `total_demand_kw(h, world)` with the **revised split-scope event multipliers** from the PRD:

- Heatwave (1.4 when active) multiplies residential demand only.
- Demand surprise (1.3 when active) multiplies industrial + commercial demand only.
- Process loads (refinery, injection well power) are unaffected.

Events themselves are stubbed for this slice — the multipliers exist but always return 1.0 because no events have fired yet. Slice 11 lights up the event flags.

`/state.weather_now` exposes solar irradiance, wind speed, wind direction, cloud factor for the start-of-day hour. `/state.power_now.demand_kw` shows current-hour demand.

## Acceptance criteria

- [ ] One `/step { "days": 1 }` call advances 24 internal hourly ticks; weather updates per tick using `sim_rng`.
- [ ] Solar irradiance is zero when `h < sunrise(D)` or `h > sunset(D)`; non-zero otherwise; peaks near solar noon.
- [ ] `sunrise(D)` is in [4, 8] and `sunset(D)` is in [16, 20] across the year.
- [ ] `cloud_factor(t)` clipped to [0.1, 1.0]; AR(1) recurrence matches §4.1.
- [ ] Wind power curve: returns 0 below 3 m/s and above 25 m/s; returns `WIND_RATED_KW` at v ≥ 12; cubic interpolation between.
- [ ] `total_demand_kw` accounts for residential (with hourly factor), industrial (continuous), commercial (full daytime, 20% otherwise), and process loads. Heatwave/demand-surprise stubs return 1.0 in this slice.
- [ ] `/state.weather_now` returns solar_irradiance, wind_speed_mps, wind_direction_deg, cloud_factor.
- [ ] `/state.power_now.demand_kw` returns current-hour total demand.
- [ ] Tests in `world/tests/test_weather.py` cover: solar shape (zero at midnight, non-zero at noon), wind power curve at boundary speeds, seasonal sunrise/sunset modulation, cloud_factor clipping.
- [ ] Tests in `world/tests/test_demand.py` verify split-scope multipliers (forced via direct event-flag injection in test): heatwave only changes residential; demand surprise only changes I+C; process loads always pass through.

## Blocked by

- 03 — Population dynamics + tax revenue

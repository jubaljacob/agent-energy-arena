Status: needs-triage

## Parent

PRD: `.scratch/balance-upgrade-p0/PRD.md`

## What to build

Make heatwaves actually challenge a solar-heavy fleet by derating solar 20% on top of the existing residential demand spike, so the event's stated counter (batteries + wind) is correct.

- New helper `weather.solar_derate_multiplier(state) -> float`: returns `0.8` when a heatwave is active in `state.active_events`, else `1.0`.
- Applied in `power.dispatch()` step 1 by multiplying the solar tile output cap:
  ```
  eff_cap = TILE_CATALOG[p.type].capacity_kw * efficiency(p) * solar_derate_multiplier(state)
  ```
  Solar only — wind unchanged.

## Acceptance criteria

- [ ] `test_solar_derate_during_heatwave` in `test_dispatch.py`: solar output drops 20% when heatwave is in `state.active_events`; unchanged otherwise.
- [ ] Wind output unaffected during heatwave.
- [ ] `make check` passes.

## Blocked by

None - can start immediately.

# 02: Scenario protocol + day-loop hook + weather overrides

Status: ready-for-agent

## Parent

`.scratch/open-source-arena/PRD.md`

## What to build

Introduce the scenario contract that every shipped and user-authored scenario will obey.

Add `world/scenario.py` with:
- A `Scenario` protocol declaring one method: `apply(world, day) -> None`.
- A `NullScenario` class (the default; `apply` is a no-op).
- A `load_scenario(dotted_path) -> Scenario` helper that imports the module and returns its `Scenario` subclass, raising clear errors on invalid paths or modules with no subclass.

Wire the hook into the day loop: call `world.scenario.apply(world, day)` exactly once per day, after the expiry pass over finite-duration events and before the stochastic event sampler. Store the active scenario on the world as `self.scenario`; default to `NullScenario`. World reset accepts an optional scenario instance.

Add two new fields to `WorldState`:
- `weather_overrides: dict[str, float]` — transient per-day overrides for wind/cloud/irradiance observables, cleared on the next AR(1) update unless re-written.
- `scenario_trace: list[...]` — appended to by scenarios so the recorder can show what fired and when.

The weather module, after computing its AR(1) update for cloud factor and wind speed, consults `state.weather_overrides`; if a key for the observable is present, the override wins. Override values are visible to the recorder.

Determinism must be preserved: with `NullScenario` attached, the byte trace of a baseline-seed run is unchanged.

## Acceptance criteria

- [ ] `world/scenario.py` exposes `Scenario`, `NullScenario`, `load_scenario`.
- [ ] `load_scenario` raises a clear error for invalid dotted paths and for modules lacking a `Scenario` subclass.
- [ ] Day loop calls `self.scenario.apply(self, day)` once per day, after expiry, before stochastic sampling.
- [ ] World reset accepts an optional scenario instance; defaults to `NullScenario`.
- [ ] `WorldState` has `weather_overrides: dict[str, float]` and `scenario_trace: list`.
- [ ] Weather module honors `weather_overrides` when present.
- [ ] Determinism test still passes with `NullScenario` attached.
- [ ] Unit tests cover the loader (valid path, invalid path, missing subclass) and the null scenario (no state mutation).
- [ ] A test asserts a fixture scenario writing a weather override observably changes the next hour's wind/cloud value.
- [ ] `make check` passes.

## Blocked by

None — can start immediately.

# Scenarios

A *scenario* is a thin overlay on the simulation. Same map, same starting cash, same agents — but the weather, prices, or event mix can be steered to stress one part of the agent's policy. Scenarios are how the arena evaluates an agent across diverse pressure profiles instead of a single seed-42 run.

This guide lives next to the scenario modules it documents. If you're writing or playing one, you're in the right place. For endpoints see [`../API.md`](../API.md); for the underlying mechanics see [`../RULES.md`](../RULES.md).

## Table of contents

- [The `Scenario.apply` protocol](#the-scenarioapply-protocol)
- [Override taxonomy](#override-taxonomy)
  - [Weather overrides](#weather-overrides)
  - [Mutable pricing fields](#mutable-pricing-fields)
  - [Injected named events](#injected-named-events)
  - [`scenario_trace`](#scenario_trace)
- [Running an agent against a scenario](#running-an-agent-against-a-scenario)
- [Scoring a scenario run](#scoring-a-scenario-run)
- [Discovery and attachment](#discovery-and-attachment)
- [Determinism](#determinism)
- [v1 shipped scenarios](#v1-shipped-scenarios)
  - [`scenarios.baseline`](#scenariosbaseline)
  - [`scenarios.grid_stress`](#scenariosgrid_stress)
  - [`scenarios.economy_stress`](#scenarioseconomy_stress)
- [Authoring a new scenario](#authoring-a-new-scenario)
- [Submitting a scenario](#submitting-a-scenario)

## The `Scenario.apply` protocol

A scenario is a Python class with one method:

```python
# scenarios/my_scenario.py
from world.scenario import Scenario


class MyScenario(Scenario):
    seed = 42

    def apply(self, world, day):
        # mutate world.state — overrides, prices, active events, trace.
        ...
```

`apply(world, day)` is called once per simulated day by `world.sim.World._advance_one_day`, in this order:

1. `expire_finite_events` — yesterday's finite-duration events expire.
2. **`scenario.apply(world, day)`** — this hook.
3. `sample_and_apply_events` — the daily stochastic event roll.
4. 24-hour weather/dispatch/finance loop.
5. End-of-day summary; recorder appends to `states.jsonl`.

The order matters:

- Running BEFORE the stochastic sampler means a scenario-injected event (e.g. a forced heatwave) suppresses a same-day stochastic roll of the same type, because the sampler skips event types that are already active. So you can inject without double-counting.
- Running AFTER expiry means an injected event with `ends_day == day` does not get expired the same tick it was added.

Source: [`../world/sim.py`](../world/sim.py), [`../world/scenario.py`](../world/scenario.py).

The default scenario is `world.scenario.NullScenario` — a subclass whose `apply` is a no-op. Every fresh `World` attaches it. Introducing the scenario hook does not change the byte trace of a baseline-seed run.

## Override taxonomy

A scenario has four supported levers. Anything beyond them risks breaking the determinism contract (see below).

### Weather overrides

`state.weather_overrides: dict[str, float]` is a transient per-hour dict consulted by `world.weather.step_weather_one_hour` *after* the AR(1) updates. Any key present wins for the current hour:

| Key | Effect | Source |
|---|---|---|
| `cloud_factor` | Overrides the smoothed cloud value in `[0.1, 1.0]`. | [`../world/weather.py`](../world/weather.py) |
| `solar_irradiance` | Overrides the per-hour irradiance, post-`cloud × sin`. | [`../world/weather.py`](../world/weather.py) |
| `wind_speed_mps` | Overrides the AR(1) wind value. Below 3 m/s clips wind output to 0. | [`../world/weather.py`](../world/weather.py) |
| `wind_direction_deg` | Overrides the wind heading (cosmetic; turbines auto-yaw). | [`../world/weather.py`](../world/weather.py) |

The AR(1) draws happen unconditionally — overrides only pin the observed value, not the underlying RNG stream. Determinism is preserved.

If you want a sustained clip, re-write the key on every `apply` call inside the window. Outside the window, `pop(key, None)` so the AR(1) draw flows through. See [`scenarios.grid_stress`](#scenariosgrid_stress) for a worked example.

### Mutable pricing fields

These live on `WorldState` (defaults shown). A scenario can write directly:

| Field | Default | What it controls |
|---|---|---|
| `state.crude_price_usd_per_bbl` | 40.0 | Unrouted crude sale price ($/bbl). |
| `state.refined_price_usd_per_bbl` | 90.0 | Refined product sale price ($/bbl). |
| `state.grid_price_retail` | 0.08 | Local kWh-served price ($/kWh). |
| `state.grid_price_export` | 0.04 | Curtailment export price ($/kWh). |
| `state.industrial_revenue_per_day` | 500.0 | Per staffed industrial slot. |
| `state.commercial_revenue_per_resident_per_day` | 1.0 | Per nearby resident × occupancy. |
| `state.daily_tax_per_capita` | 4.0 | Per-resident daily tax. |
| `state.blackout_penalty_hour` | 5000.0 | $/hour deducted while balance_state = blackout. |
| `state.carbon_price` | 25.0 | $/t CO₂. `regulatory_tightening` events also touch this. |
| `state.plant_fuel_cost_per_mwh` | `{coal_plant: 12, gas_peaker: 30}` | Per-plant fuel cost ($/MWh). |

These are consumed live each hour/day; mutate at any time. Restore on the window's closing day (`elif day == END_DAY: state.crude_price = DEFAULT`) so a mid-window `/reset` followed by a fresh attach still snaps back. See [`scenarios.economy_stress`](#scenarioseconomy_stress) for the start/clear pattern.

### Injected named events

`state.active_events: list[dict]` is the live event queue. The day-loop's stochastic sampler appends to it; a scenario can too. The expected dict shape (matches the sampler):

```python
state.active_events.append({
    "type": "heatwave",        # one of: heatwave, plant_failure, fuel_price_shock,
                                #          demand_surprise, regulatory_tightening
    "started_day": day,
    "ends_day": day + 5,        # consumed by expire_finite_events
    "severity": 1.4,             # event-specific
})
```

Effects by type:

| Type | Effect while active | Notes |
|---|---|---|
| `heatwave` | Residential demand × 1.40. | 5-day stock duration; the sampler refuses to roll one if one is already active. |
| `plant_failure` | Affected `tile.operational = False`. | Carries `tile_id`; the sampler picks a random fossil plant. |
| `fuel_price_shock` | gas fuel × 2.5, coal fuel × 1.3. | Sampler-driven; modify `state.plant_fuel_cost_per_mwh` directly for a custom shock. |
| `demand_surprise` | industrial+commercial demand × 1.30. | 10-day stock duration. |
| `regulatory_tightening` | `carbon_price` × 1.5 cumulative; `regulatory_tightenings_applied += 1`. | Effect is permanent (the marker only carries the duration for the run log). |

Expired events move to `state.historical_events`. Read [`../world/events.py`](../world/events.py) for the sampler's full semantics; mirror its shape so your injected event behaves identically to a stochastic one downstream.

### `scenario_trace`

`state.scenario_trace: list[dict]` is an append-only log of what your scenario did and when. Structure is owned by the scenario author — the recorder writes it through to `states.jsonl` end-of-day so a replay viewer can show "low-wind window opened at day 5, ended at day 25" alongside the game state.

Convention: each entry carries `{"day": day, "kind": "<event_name>", ...}` with whatever extra fields you want surfaced. See [`grid_stress.py`](grid_stress.py) for examples.

## Running an agent against a scenario

Every scenario is addressed by its **dotted path**. The same path works from the CLI, the API, and the browser UI.

### CLI — one agent, one scenario

```bash
# Score the scripted reference agent against grid_stress on seed 42.
python evaluate.py --agent agents.scripted --scenario scenarios.grid_stress --seed 42

# `make score` is shorthand for the no-scenario default run on submit/agent.py.
make score
```

`evaluate.py` attaches the scenario before the agent's first `/reset`, plays the full game, prints a JSON line on `stdout`, and writes a run folder under `runs/<run_id>/` containing `actions.jsonl`, `states.jsonl`, and `metadata.json`. `metadata.json` records the scenario dotted path so the run is reproducible.

### Browser UI — pick a scenario interactively

`make serve` launches the UI on `localhost:8000`. Open the **Events** tab and click **Choose scenario**: the modal lists every scenario discovered under this package via `GET /scenarios`. Confirm to attach (`POST /scenario`), and the readout + plan + module source appear inline. Detach re-attaches `scenarios.baseline`.

The seed for each scenario is read from the class's `seed` attribute (`Scenario.seed: int = 42` by default; subclasses override).

## Scoring a scenario run

`GET /score` returns the absolute `[0, 100]` score for the active run, derived from the per-day `states.jsonl` on disk. Same shape regardless of which scenario is attached — the score reflects how the **agent** held up under the **scenario**'s pressure profile.

```bash
curl http://localhost:8000/score
# {"n_days": 365, "score": 42.3, "components": {...}}
```

The score decomposes treasury / population / happiness into level + trend + trough, plus a renewable-share term and a solvency term — a peak-and-collapse run cannot outscore a steady prosperous one. See [`../world/scoring.py`](../world/scoring.py) for the formula and tunable scale anchors, and [`../RULES.md#scoring`](../RULES.md#scoring) for the equations.

Three ways to read a score:

- **`evaluate.py`** — the final JSON line on stdout carries `{"score": ..., "components": {...}}` alongside the run ID. Pipe to `jq` to extract.
- **`evaluate.py --score <run_dir>`** — score an existing run folder (reads `states.jsonl`) and prints the same payload.
- **`GET /score` (UI / API)** — poll mid-run for live scoring; empty or fresh-reset runs return `{"n_days": 0, "score": 0.0, "components": {}}`.

## Discovery and attachment

`world.scenario.load_scenario(dotted_path)` imports the module and walks its top-level attributes for a concrete `Scenario` subclass (skipping `Scenario` and `NullScenario`). The first match is instantiated and returned. There is no decorator or registry — drop a class in a module and it is discoverable.

The companion `discover_scenarios(scenarios_root)` walks the package for every importable module that contains a Scenario subclass and returns their dotted paths; `GET /scenarios` is the HTTP wrapper that powers the UI picker.

Three ways to attach mid-session:

1. **`evaluate.py --scenario <path>`** — attaches before `play_game` runs; the run folder's `metadata.json.scenario` records the dotted path.
2. **`POST /scenario {"dotted_path": "..."}`** — attaches against the running world. `GET /scenario` returns the currently-attached dotted path, the class docstring (`description`), and the module source (`source`), or `null` on all three for `NullScenario`.
3. **`POST /reset {"seed": 42, "scenario": "..."}`** — resets the world AND attaches in one call. The recorder's metadata.json picks up the dotted path.

Source: [`../world/api.py`](../world/api.py), [`../world/scenario.py`](../world/scenario.py).

## Determinism

The world is fully deterministic given `(seed, action log)`. Scenarios participate in that contract:

- Consume **zero** RNG draws inside `apply`. The four RNG streams (`sim_rng`, `event_rng`, `forecast_rng`, `reservoir_rng`) are reserved for world dynamics; pulling from them inside a scenario re-orders downstream draws.
- The v1 shipped scenarios all satisfy this — given `(world, day)` their effect is pure. If your scenario needs randomness, derive it from `day` (e.g. `if day % 30 == 0`), not a Generator.
- Overrides apply *after* the AR(1) draw, so the draw still consumes one element from `sim_rng` regardless of whether the override is set. Behavior outside the override window is unchanged.
- A baseline-seed run with `--scenario scenarios.baseline` and one with no `--scenario` flag at all produce the same byte trace, because `Baseline` inherits `NullScenario.apply` (a no-op).

[`../world/tests/test_determinism.py`](../world/tests/test_determinism.py) pins the same-seed reproduction contract; [`tests/test_grid_stress.py`](tests/test_grid_stress.py) and its siblings pin each shipped scenario's effects.

## v1 shipped scenarios

All three are seed-42 by default and ship as sibling modules in this package.

### `scenarios.baseline`

Identity scenario. No overrides, no event injections — runs the world on its default seed-42 trajectory. Exists so the CLI surface is uniform (every arena row has a `--scenario` dotted path, even the baseline one).

Source: [`baseline.py`](baseline.py).

### `scenarios.grid_stress`

Stresses the **dispatch surface**. Combines two pressure sources:

- **Sustained low-wind windows.** Three windows at days 5–25, 180–200, 730–750. Wind speed is clipped via `weather_overrides["wind_speed_mps"]` for the whole window, below the turbine cut-in (3 m/s), so wind output is zero. AR(1) draws still fire, only the observed value is pinned.
- **Heatwave cluster.** A heatwave (severity 1.4, 5-day duration) is injected on days 10, 40, 80, 200, 400. Residential demand × 1.40 and solar derate × 0.8 both kick in. The first heatwave overlaps the first low-wind window — the worst-case start for an agent leaning hard on renewables.

Source: [`grid_stress.py`](grid_stress.py). Tuning values are class attributes; retune in review without touching `apply`.

### `scenarios.economy_stress`

Stresses the **economic surface**. Three layered pressure sources:

- **Fuel-price shock.** Days 7–90: coal $30/MWh (2.5×), gas $75/MWh (2.5×). Merit order can flip toward renewables/idle live; restored on day 90.
- **Crude-price collapse.** Days 14–365: `crude_price_usd_per_bbl` drops to $15 (default $40), so production wells run at a loss until the window closes.
- **Regulatory tightening.** Day 30: a permanent `carbon_price × 2.0` step; an `active_events` marker tagged with `REGULATORY_DURATION_DAYS=200` exists for run-log visibility and auto-expires through `expire_finite_events`. The carbon-price step is NOT restored — the regulatory bump is permanent, matching the stochastic sampler's semantics.

Source: [`economy_stress.py`](economy_stress.py).

## Authoring a new scenario

1. **One file per scenario.** Drop `<your_slug>.py` next to `baseline.py` in this package. The module docstring should name the stress profile in one paragraph and list the levers it pulls — the UI prints it as the scenario's plan, so write it for a reader.
2. **Subclass `Scenario`.** Override `apply(world, day)` and pin `seed`. Use class attributes for tuning values so a maintainer can retune in review without touching `apply` (see the `LOW_WIND_WINDOWS` / `HEATWAVE_DAYS` pattern in [`grid_stress.py`](grid_stress.py)).

   ```python
   # scenarios/cold_snap.py
   """Cold-snap scenario — sustained low temperature window stresses
   residential demand without touching the dispatch surface."""

   from __future__ import annotations
   from typing import TYPE_CHECKING
   from world.scenario import Scenario

   if TYPE_CHECKING:
       from world.sim import World


   class ColdSnap(Scenario):
       """Two-week residential-demand surge from a sustained cold snap."""

       seed: int = 42
       WINDOW = (60, 74)        # [start_day, end_day_exclusive)
       DEMAND_MULTIPLIER = 1.35

       def apply(self, world: "World", day: int) -> None:
           start, end = self.WINDOW
           if start <= day < end:
               world.state.active_events.append({
                   "type": "heatwave",   # reuse the residential-demand lever
                   "started_day": day,
                   "ends_day": day + 1,  # daily re-injection for a sustained window
                   "severity": self.DEMAND_MULTIPLIER,
               })
   ```

3. **Stay on the override taxonomy.** Weather overrides, mutable pricing fields, named-event injections, scenario trace. Anything else breaks the determinism contract (see [Determinism](#determinism)).
4. **Restore on the closing day.** Mutable pricing fields must be restored on the window's closing day; otherwise a mid-window `/reset` leaves the world in a wrong state.
5. **Write a regression test.** Add `tests/test_<your_slug>.py` that drives the world a few days and asserts overrides fire on the documented days. [`tests/test_grid_stress.py`](tests/test_grid_stress.py) is a worked example.
6. **Smoke-attach via the UI.** `make serve`, open the Events tab, **Choose scenario** → your slug. The class docstring is the plan; the module source is the box below it. If either looks wrong to a reader, fix it now.
7. **Verify locally.** `make check` must pass. `python evaluate.py --agent agents.scripted --scenario scenarios.<your_slug> --seed 42` runs end-to-end and prints a score.

## Submitting a scenario

Open a PR with `scenarios/<your_slug>.py` and its test. CI runs `make check`. A maintainer reviews the override taxonomy and merges.

See [`../README.md`](../README.md) for the broader contributor flow.

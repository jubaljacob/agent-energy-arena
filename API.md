# API

The FastAPI server in `world/api.py` is the single source of truth. Every endpoint shape below is exactly what an agent author talks to, regardless of transport (live HTTP via `httpx`, or in-process `TestClient`). The Python wrapper `agents.api_client.ApiClient` is a one-to-one mirror — if you prefer not to roll your own HTTP, use it.

The default base URL is `http://localhost:8000`. Run `make serve` (or `docker compose up`) to bring the server up.

## Conventions

All mutating endpoints return a common envelope:

```json
{
  "ok": true | false,
  "error": "string?",
  "treasury_after": 432100.5,
  "result": { /* endpoint-specific payload, only on ok=true */ }
}
```

- `ok: false` means the call was rejected; **no state change** occurred and `error` is a short machine-readable token (`insufficient_funds`, `tile_occupied`, …).
- `ok: true` means the action applied and `result` carries the endpoint-specific payload.

Read endpoints return the payload directly (no `ok` envelope). HTTP 4xx is reserved for **input validation** (bad path, missing required field, out-of-range parameter) and surfaces through the FastAPI/Pydantic layer; gameplay-level failures (`insufficient_funds`, `no_road_adjacency`) come back as `ok: false` with HTTP 200.

Every mutating call is appended to `runs/{run_id}/actions.jsonl`, even when `ok: false`. The action log is the substrate for `python evaluate.py --replay runs/{run_id}`.

## Endpoint index

State & metadata: [`/state`](#get-state) · [`/scenario`](#get-scenario) · [`/run`](#get-run) · [`/seed`](#get-seed) · [`/catalog`](#get-catalog) · [`/events`](#get-events) · [`/score`](#get-score) · [`/forecast`](#get-forecast) · [`/reservoirs`](#get-reservoirs)

Mutations: [`/reset`](#post-reset) · [`/scenario`](#post-scenario) · [`/step`](#post-step) · [`/build`](#post-build) · [`/demolish`](#post-demolish) · [`/survey`](#post-survey) · [`/drill`](#post-drill) · [`/control/well`](#post-controlwell) · [`/control/battery`](#post-controlbattery) · [`/control/refinery`](#post-controlrefinery)

---

## State and metadata

### `GET /state`

Returns the full world snapshot.

```json
{
  "seed": 42,
  "day": 145,
  "hour": 0,
  "treasury": 432100.50,
  "population": 1230,
  "employed": 980,
  "unemployed": 250,
  "housing_capacity": 1480,
  "jobs_total": 1100,
  "jobs_vacant": 120,
  "happiness": 0.85,
  "config": {
    "world_w": 32, "world_h": 32, "world_d": 16,
    "game_days": 3650, "manual_game_days": 365, "ticks_per_day": 24,
    "carbon_price": 25.0, "starting_cash": 500000, "starting_pop": 100,
    "session": "agent",
    "active_game_days": 3650
  },
  "tiles": [
    {
      "id": "tile_42", "type": "solar_farm",
      "x": 12, "y": 8, "built_day": 23,
      "operational": true, "current_output_kw": 95.2
    }
  ],
  "wells": [
    {
      "id": "well_3", "type": "production",
      "x": 5, "y": 14, "target_z": 8, "drilled_day": 67,
      "setpoint_rate_bbl_day": 150, "current_rate_bbl_day": 122.4,
      "cumulative_produced_bbl": 8542.1
    }
  ],
  "reservoirs_revealed": [...],
  "reservoirs_summary": {...},
  "active_events": [
    {"type": "heatwave", "started_day": 142, "ends_day": 147, "severity": 1.4}
  ],
  "historical_events": [...],
  "regulatory_tightenings_applied": 0,
  "weather_now": {
    "solar_irradiance": 0.78, "wind_speed_mps": 8.2,
    "wind_direction_deg": 145, "cloud_factor": 0.91
  },
  "power_now": {
    "demand_kw": 1840, "supply_kw": 1880, "balance_state": "balanced",
    "by_source_kw": {"solar": 320, "wind": 410, "gas": 800, "coal": 350}
  },
  "last_day_supply_kw_by_hour": [...],
  "last_day_demand_kw_by_hour": [...],
  "last_day_balance_state_by_hour": [...],
  "next_24h_preview": {...},
  "today_summary_so_far": {...},
  "cumulative_renewable_served_kwh": 1234567.0,
  "cumulative_total_served_kwh": 2345678.0,
  "pipeline_networks": [...],
  "orphan_well_ids": [],
  "orphan_refinery_ids": []
}
```

`config.active_game_days` is the cap your day loop should stop at — `game_days` in the agent (`session=agent`) session, `manual_game_days` in the UI (`session=manual`) session.

Errors: none.

### `GET /scenario`

```json
{ "dotted_path": "scenarios.grid_stress" }
```

`dotted_path` is `null` when no scenario is attached (the default `NullScenario`).

### `GET /run`

```json
{ "run_id": "20260513-153219-1A2B", "dir": "runs/20260513-153219-1A2B" }
```

Returns `{"run_id": null, "dir": null}` when the world has no recorder attached (test/in-process callers may opt out).

### `GET /seed`

```json
{ "seed": 42 }
```

### `GET /catalog`

Returns the machine-readable build catalog. Shape:

```json
{
  "tiles": [{"tile_type": "house", "capex": 3000, ...}, ...],
  "wells": [{"tile_type": "oil_well", "capex": 50000, ...}, ...],
  "subsurface": {
    "survey": {"base_cost": 15000, "base_size": 8, "min_size": 1, "max_size": 32},
    "drilling": {"cost": 50000},
    "wells": {...}
  },
  "constants": {
    "carbon_price": 25.0,
    "refined_price_per_bbl": 90.0,
    "crude_price_per_bbl": 40.0,
    ...
  }
}
```

Use this rather than hardcoding numbers — re-tunes land here automatically.

### `GET /events`

```json
{
  "active": [{"type": "heatwave", "started_day": 142, "ends_day": 147, "severity": 1.4}],
  "historical": [...],
  "regulatory_tightenings_applied": 0
}
```

### `GET /score`

Returns the score breakdown for the current world against the committed dev-seed baseline:

```json
{
  "P": 5230, "P_ref": 1850, "p_term": 1.4135,
  "T": 1850000, "T_ref": 320000, "t_term": 0.498,
  "R": 0.55, "r_term": 0.055,
  "score": 1.967
}
```

Errors:

- `404 baseline_missing` — no `baselines/seed_{N}.json` file for the active seed. Run `make baselines` or commit the file.

### `GET /forecast`

Query: `hours` (1–168, default 24).

```json
[
  {"hour_offset": 0, "solar_irradiance": 0.78, "wind_speed_mps": 8.2, "demand_factor": 1.02},
  ...
]
```

Forecasts are noisy and re-sampled per call — re-querying reduces variance via averaging.

Errors: `400` if `hours` is out of `[1, 168]`.

### `GET /reservoirs`

Query: `min_oil` (float ≥ 0, default 0), `top_k` (1–4096, default 100). Returns the voxels ever revealed by surveys, filtered and sorted by current oil estimate.

```json
{
  "voxels": [
    {
      "x": 5, "y": 14, "z": 8,
      "oil_estimate_bbl": 18250, "perm_estimate_md": 412,
      "last_survey_day": 60
    }
  ],
  "total_revealed": 384
}
```

Errors: `400` if `top_k` is out of `[1, 4096]`.

---

## Mutations

### `POST /reset`

Body: `{ "seed": int?, "scenario": "dotted.path"? }`.

```json
// → 200
{
  "ok": true,
  "treasury_after": 500000.0,
  "result": { "seed": 42, "day": 0 }
}
```

- `seed` omitted: reuse the configured `WORLD_SEED` (env or 42).
- `scenario` omitted: keep whatever scenario is currently attached (typically `NullScenario`).
- `scenario` set: must be a dotted module path importable from `PYTHONPATH` and exposing a `Scenario` subclass.

A reset finalizes the in-progress recorder run (writing `final_state.json`) and allocates a fresh `run_id`. The action log rebinds to the new run folder.

Errors:

- `400 could not import scenario module ...` — bad dotted path or import error.
- `400 module ... does not define a Scenario subclass` — module imported but has no `Scenario` subclass.

### `POST /scenario`

Body: `{ "dotted_path": "scenarios.grid_stress" }`.

```json
// → 200
{ "ok": true, "dotted_path": "scenarios.grid_stress" }
```

Attaches the scenario mid-game without resetting. Subsequent `POST /step` calls invoke the new scenario's `apply(world, day)` hook. The call is captured in the action log so a replay reproduces the attach.

Errors: same as `POST /reset` for scenario-resolution issues.

### `POST /step`

Body: `{ "days": int }` (1–7, default 7).

```json
{
  "ok": true,
  "day_completed": 145,
  "summary": {
    "treasury_start": 430850.00,
    "treasury_end":   432100.50,
    "delta":            1250.50,
    "tax_revenue":      4920,
    "power_revenue":    1450,
    "oil_revenue":      8100,
    "industrial_revenue": 1500,
    "commercial_revenue": 1320,
    "opex":             2100,
    "fuel_cost":         800,
    "carbon_cost":       320,
    "blackout_penalty":    0,
    "blackout_hours":      0,
    "brownout_hours":      1,
    "renewable_share":   0.42,
    "co2_emitted_t":    12.8,
    "population_start": 1228,
    "population_end":   1230,
    "happiness":        0.85,
    "events_active":    ["heatwave"]
  },
  "treasury_after": 432100.50
}
```

`day_completed` is the last simulated day; on `days > 1` only the final day's summary is returned. End-of-day per-day states are written to `runs/{run_id}/states.jsonl` for every stepped day.

Errors: `400` if `days` is out of `[1, 7]` or if the world is already at `active_game_days`.

### `POST /build`

Body: `{ "tile_type": "solar_farm", "x": 4, "y": 4 }`.

```json
{
  "ok": true, "treasury_after": 475000.0,
  "result": { "tile_id": "tile_17", "x": 4, "y": 4, "type": "solar_farm" }
}
```

Errors (returned as `ok: false`):

- `unknown_tile_type` · `not_buildable` · `out_of_bounds` · `tile_occupied`
- `no_road_adjacency` · `insufficient_funds`

### `POST /demolish`

Body: `{ "x": 4, "y": 4 }`.

```json
{
  "ok": true, "treasury_after": 481250.0,
  "result": { "tile_id": "tile_17", "refund": 6250.0 }
}
```

Refunds 25% of the original CAPEX paid for that tile. The town hall is immutable.

Errors: `no_tile_at_xy` · `immutable_tile` (town hall).

### `POST /survey`

Body: `{ "x": 10, "y": 10, "size": 8 }` (size 1–32, quadratic cost).

```json
{
  "ok": true, "treasury_after": 485000.0,
  "result": {
    "x": 10, "y": 10, "size": 8, "cost": 15000.0, "n_voxels": 128,
    "voxels": [
      {"x": 10, "y": 10, "z": 0,
       "oil_estimate_bbl": 0, "perm_estimate_md": 0, "survey_day": 23},
      ...
    ]
  }
}
```

The `voxels` array is the full surveyed column; the action log strips it and keeps only `n_voxels` to avoid bloat.

Errors: `out_of_bounds` · `invalid_size` · `insufficient_funds`.

### `POST /drill`

Body: `{ "x": 10, "y": 10, "target_z": 8, "well_type": "production" }`. `well_type` is `"production"` or `"injection"`.

```json
{
  "ok": true, "treasury_after": 435000.0,
  "result": { "well_id": "well_3", "x": 10, "y": 10, "target_z": 8, "type": "production" }
}
```

Errors: `out_of_bounds` · `voxel_out_of_bounds` · `tile_occupied` · `unknown_well_type` · `insufficient_funds`.

### `POST /control/well`

Body: `{ "well_id": "well_3", "rate_bbl_day": 180 }`. Clamped to `[0, 200]`.

```json
{ "ok": true, "treasury_after": 432100.5,
  "result": { "well_id": "well_3", "setpoint_rate_bbl_day": 180.0 } }
```

Errors: `unknown_well` · `invalid_rate`.

### `POST /control/battery`

Body: `{ "tile_id": "tile_42", "charge_kw": 100 }`. Positive charges, negative discharges, 0 returns to auto policy. Clamped to `[-200, +200]` for the standard 200 kW battery.

```json
{ "ok": true, "treasury_after": 432100.5,
  "result": { "tile_id": "tile_42", "charge_setpoint_kw": 100.0 } }
```

Errors: `unknown_tile` · `not_a_battery` · `invalid_rate`.

### `POST /control/refinery`

Body: `{ "refinery_id": "tile_55", "rate_bbl_day": 400 }`. Clamped to `[0, REFINERY_MAX_BBL_DAY]` (500 by default).

```json
{ "ok": true, "treasury_after": 432100.5,
  "result": { "refinery_id": "tile_55", "setpoint_rate_bbl_day": 400.0 } }
```

Errors: `unknown_refinery` · `invalid_rate`.

---

## Worked example

A one-day flow as an agent author might write it (using `agents.api_client.ApiClient`):

```python
from agents.api_client import ApiClient

api = ApiClient("http://localhost:8000")
api.reset(seed=42, scenario="scenarios.grid_stress")

state = api.state()
forecast = api.forecast(hours=24)

api.survey(x=10, y=10, size=8)
api.build("solar_farm", x=4, y=4)
api.control_well("well_3", rate_bbl_day=180)

summary = api.step(days=1)
print(summary["summary"]["renewable_share"])
```

For the raw HTTP transport, every method on `ApiClient` corresponds 1:1 to an endpoint above; `_get`/`_post` use `httpx` under the hood.

## Static UI

The world also serves the manual-play UI:

- `GET /` — `world/ui/index.html`
- `GET /ui/*` — static assets, served with `Cache-Control: no-store` so dev-server edits take effect on reload.

The UI is a thin client over this same API; nothing it does is unavailable to agents.

# Energy–AI Nexus Hackathon — World & Agent Brief

**Event:** EAGE Annual 2026 — Energy–AI Nexus Hackathon
**Duration:** 24 hours, in-person
**Language:** Python 3.11+
**Deployment:** Docker Compose, runs locally on each team's laptop
**Status of this document:** Authoritative design brief. A downstream agent will use this as input to produce a Product Requirements Document (PRD). Implementer agents will build from the PRD. Where this brief specifies a number, equation, or contract, treat it as binding. Where it is silent, the PRD should resolve in favor of *readability* and *simplicity*.

---

## 1. Purpose & vision

### 1.1 What we are building

A small, readable Python simulation of a city's energy economy, played on a tile-based map with a 3D voxel subsurface. Players site renewable and non-renewable power generation, civilian buildings, and oil/gas infrastructure. They explore the subsurface for hydrocarbon reservoirs via seismic surveys, drill production and injection wells, manage reservoir depletion, refine and sell products, and grow population — all while keeping the grid balanced hour-by-hour without battery storage.

The simulation is wrapped by a FastAPI server. The server is the single source of truth. Two clients consume it: a browser-based UI for **manual human play**, and **AI agents** that consume the same API to play autonomously. A canonical scripted agent and a canonical LLM-based ReAct agent are shipped as starting points.

The package deploys as a single `docker compose up`. The hosted LLM is an external dependency configured via environment variables.

### 1.2 Why this design

The combination of (a) intermittent renewables, (b) no batteries, (c) ramp-limited fossil plants, (d) partially observable subsurface, and (e) population dynamics tied to grid reliability creates a multi-timescale planning problem with portfolio, dispatch, and exploration components. It is rich enough to challenge AI agents for 24 hours but reducible to ~2000 readable lines of Python.

### 1.3 Audience for this document

1. The PRD-writing agent (immediate consumer)
2. Implementer agents (builders of world + reference agents + UI)
3. Hackathon participants (consumers of the resulting code)
4. Judges (consumers of scoring infrastructure and write-ups)

### 1.4 Success criteria for the artifact

- `docker compose up` brings up the world API and UI in under 60 seconds on a developer laptop
- Manual play through `localhost:8000` is genuinely engaging and teaches the mechanics
- Reference scripted agent completes a full game on the dev seed without errors and posts a stable baseline score
- Reference LLM agent beats the scripted agent on the dev seed by a clear margin (>15%)
- Whole world codebase is approximately 2000 lines of Python, every file readable in 10 minutes
- Every formula in §4 of this brief appears as a named function in code with the same variable names
- Reference agents are each 200–400 lines, fully readable in one sitting

### 1.5 Success criteria for participants

- Submit a single `submit/agent.py` file implementing the `Agent` interface (§7.1)
- Agent runs to completion in ≤ 5 minutes wall-clock per seed
- Agent does not exceed 500,000 LLM tokens per game evaluation
- Agent does not crash or hang
- Final score on the held-out evaluation seed exceeds the scripted baseline (this is the floor for being ranked at all)
- Top 3 ranked submissions present their approach at the close-out

---

## 2. Glossary

| Term | Meaning |
|---|---|
| Tick | One simulated hour, the world's smallest time unit |
| Day | 24 ticks; the cadence at which agents make decisions |
| Game | 365 days; one full agent run |
| Tile | A 1×1 cell on the surface grid |
| Voxel | A 1×1×1 cell in the subsurface grid |
| Reservoir | A connected blob of hydrocarbon-bearing voxels |
| OOIP | Original Oil In Place — initial recoverable oil in a region |
| Pool | The 3×3×3 voxel neighborhood drained by a single well |
| Well | A surface tile that drills to a target voxel z-coordinate; type ∈ {production, injection} |
| Dispatch | The hourly assignment of output to power plants to meet demand |
| Reserve margin | (supply − demand) / demand at a given hour |
| Brownout / Blackout | States triggered when supply falls below demand thresholds |
| Frozen world | The canonical, unmodifiable world all participants share. The only track this event runs. |
| Dev seed | Public seed all participants use during the 24h. Set to `42`. |
| Eval seed | Held-out seed used by organizers for final scoring. Not disclosed to participants. |

---

## 3. Game design

### 3.1 Core loop

Each day the agent:

1. Calls `GET /state` to observe the world
2. Optionally calls `GET /forecast` for next-24h weather and demand
3. Submits actions (build, demolish, survey, drill, set well/refinery/plant rates) via `POST` endpoints
4. Calls `POST /step` to advance the simulation 24 hours

The world processes hourly dynamics internally — sun arc, wind drift, demand cycle, plant ramping, reservoir depletion, finance accrual, event sampling — and returns a daily summary. Repeat for 365 days.

Manual play is structurally identical: the UI is a thin client over the same API, with a "Next Day" button that calls `POST /step`.

### 3.2 Time

| Parameter | Default | Env var |
|---|---|---|
| Ticks per day | 24 (one per hour) | `TICKS_PER_DAY` |
| Days per agent decision | 1 | `DAYS_PER_DECISION` |
| Game length in days | 365 | `GAME_DAYS` |

The agent decides at hour 0 of each day. All build/demolish/survey/drill actions submitted before `POST /step` apply at hour 0. Well/refinery/plant control rates apply for the duration of the day until changed.

### 3.3 Map dimensions

| Parameter | Default | Env var |
|---|---|---|
| Surface width | 32 | `WORLD_W` |
| Surface height | 32 | `WORLD_H` |
| Subsurface depth | 16 | `WORLD_D` |

All three are configurable. Voxel coordinates are `(x, y, z)` with `z=0` at the top (just below surface) and `z=WORLD_D-1` at the bottom.

### 3.4 Starting conditions

| Parameter | Default | Env var |
|---|---|---|
| Treasury | $500,000 | `STARTING_CASH` |
| Population | 100 | `STARTING_POP` |
| Town hall | 1 placed at center | hardcoded |

The town hall provides 100 housing capacity and 30 jobs at no cost or upkeep; it cannot be demolished. All other tiles start empty. The subsurface is hidden until surveyed.

### 3.5 Reservoir generation per seed

At reset, given the seed, the world generates 3–7 reservoir blobs:

1. For each blob: pick a random center voxel `(x, y, z)` with `z ∈ [4, WORLD_D-2]` (no surface seepage)
2. Pick a random "size" radius `r ∈ [3, 6]`
3. For each voxel within Manhattan distance `r` of center, with probability `0.6 · (1 - dist/r)`, mark it as hydrocarbon-bearing
4. Assign each hydrocarbon voxel:
   - Porosity `φ ~ Uniform(0.10, 0.30)`
   - Permeability `k ~ LogUniform(10, 1000)` mD
   - Oil saturation `S_o ~ Uniform(0.55, 0.80)`
   - `oil_in_place_bbl = φ · S_o · VOXEL_VOLUME_BBL` where `VOXEL_VOLUME_BBL = 100,000` (calibration constant)
5. Total OOIP across all reservoirs varies per seed; expected ~5–15 million bbl on default size

The dev seed (`42`) and eval seed produce *different* reservoir layouts, weather profiles, and event rolls.

### 3.6 What the agent controls

- What to build, where, when (subject to cost, adjacency, and validity rules)
- Demolition (returns 25% of CAPEX)
- Seismic surveys (location, region size)
- Drilling (location, target depth, well type)
- Production rate per production well, [0, q_max_well]
- Injection rate per injection well, [0, q_max_well]
- Refinery throughput rate
- *Optional:* per-plant dispatch setpoint override (otherwise auto-dispatched)

### 3.7 What the agent does not control

- Hourly dispatch (auto-resolved by world unless overridden)
- Weather (deterministic per seed)
- Population growth dynamics (deterministic given inputs)
- Stochastic events (sampled per seed from published distributions)

---

## 4. World mechanics — equations

All formulas in this section must appear in code as named functions in the file specified, using the same variable names. The PRD should preserve this 1:1 mapping.

### 4.1 Solar (`world/weather.py`)

Day-of-year `D ∈ [0, 364]`, hour-of-day `h ∈ [0, 23]`.

```
sunrise(D)    = 6  - 2 · sin(2π · D / 365)             # range 4..8
sunset(D)     = 18 + 2 · sin(2π · D / 365)             # range 16..20
day_length(D) = sunset(D) - sunrise(D)

if h < sunrise(D) or h > sunset(D):
    irradiance(D, h) = 0
else:
    angle = π · (h - sunrise(D)) / day_length(D)
    irradiance(D, h) = sin(angle) · cloud_factor(t)
```

`cloud_factor(t)` is an AR(1) process:

```
cloud_factor(t+1) = clip(0.7 · cloud_factor(t) + 0.3 · 0.85 + N(0, 0.10), 0.1, 1.0)
```

Per-panel solar output:

```
P_solar_kw(t) = SOLAR_PEAK_KW · irradiance(D, h)        # SOLAR_PEAK_KW = 150
```

### 4.2 Wind (`world/weather.py`)

```
v_mean(D)  = 7 + 2 · sin(2π · D / 365 + φ_seed)         # m/s, seasonal
v(t+1)     = clip(0.85 · v(t) + 0.15 · v_mean(D) + N(0, 1.5), 0, 30)
θ(t+1)     = (θ(t) + N(0, 5°)) mod 360°
```

Turbine power curve:

```
def turbine_kw(v):
    if v < 3.0 or v > 25.0:
        return 0
    if v >= 12.0:
        return WIND_RATED_KW                            # 200
    return WIND_RATED_KW · ((v - 3.0) / 9.0) ** 3
```

Direction `θ` is reported in state but does not affect output in v1 (turbines auto-yaw). Kept in the model for extensibility and for spectator visualization.

### 4.3 Demand (`world/power.py`)

Per-capita residential demand:

```
PER_CAPITA_KW = 0.333                                    # 8 kWh/day = 0.333 kW continuous

residential_kw(h, pop) = pop · PER_CAPITA_KW · hourly_factor(h)

def hourly_factor(h):
    if h < 5:    return 0.6     # night
    if h < 9:    return 1.0     # morning
    if h < 17:   return 0.8     # midday
    if h < 22:   return 1.5     # evening peak
    return 0.7                  # late night
```

Industrial tiles draw their full power continuously. Commercial tiles draw full power 8:00–20:00, 20% otherwise.

```
total_demand_kw(h) = residential_kw(h, pop)
                   + Σ industrial_tile.demand_kw
                   + Σ commercial_tile.demand_kw · (1.0 if 8 ≤ h < 20 else 0.2)
                   + Σ injection_well.power_kw          # see §4.5
                   + Σ refinery.power_kw                # see §4.6
                   · heatwave_multiplier(t)             # 1.4 if active, else 1.0
                   · demand_surprise_multiplier(t)      # 1.3 if active, else 1.0
```

### 4.4 Dispatch and grid balance (`world/power.py`)

Auto-dispatch each hour, given previous-hour outputs `prev`:

```
def dispatch(plants, demand_kw, prev):
    outputs = {}
    supply = 0

    # Step 1: must-take renewables
    for p in solar_plants + wind_plants:
        outputs[p.id] = p.available_kw(weather)
        supply += outputs[p.id]

    # Step 2: coal must-run minimum (25% of capacity, slow ramp)
    for p in coal_plants:
        outputs[p.id] = p.capacity_kw · 0.25
        supply += outputs[p.id]

    # Step 3: ramp coal upward by merit (low fuel cost first)
    remaining = demand_kw - supply
    for p in sorted(coal_plants, key=cost):
        ramp_room = p.capacity_kw · COAL_RAMP_PER_HOUR     # 0.10
        max_inc = min(p.capacity_kw - outputs[p.id], ramp_room, remaining)
        if max_inc <= 0: continue
        outputs[p.id] += max_inc
        supply += max_inc
        remaining -= max_inc

    # Step 4: ramp gas peakers
    for p in sorted(gas_plants, key=cost):
        prev_out = prev.get(p.id, 0)
        ramp_room = p.capacity_kw · GAS_RAMP_PER_HOUR      # 0.50
        max_out = min(p.capacity_kw, prev_out + ramp_room)
        delivered = min(max_out, remaining)
        outputs[p.id] = delivered
        supply += delivered
        remaining -= delivered
        if remaining <= 0: break

    # Apply per-plant overrides if set by agent
    apply_overrides(outputs, agent_overrides)

    return outputs, supply
```

Constants:

```
COAL_RAMP_PER_HOUR = 0.10
GAS_RAMP_PER_HOUR  = 0.50
COAL_MIN_RUN       = 0.25
```

Grid-balance state, with `R = supply / max(demand, 1)`:

```
if R >= 1.15:        state = "curtailment"; served = demand;  excess sold @ 0.5 · GRID_PRICE
elif R >= 0.95:      state = "balanced";    served = demand
elif R >= 0.70:      state = "brownout";    served = supply;  happiness -= 0.05 · (1 - R)
else:                state = "blackout";    served = supply;  happiness -= 0.20
                                                              treasury -= BLACKOUT_PENALTY_HOUR  # $5,000
```

### 4.5 Reservoir physics (`world/subsurface.py`)

For each production well `w` at `(x, y, target_z)`:

```
def well_production_bbl_day(w, world):
    pool = voxels_in_3x3x3(w.x, w.y, w.target_z, clipped_to_grid)

    V_remain = sum(v.oil_remaining for v in pool)
    V_init   = sum(v.oil_in_place  for v in pool)
    if V_init == 0:
        return 0

    fraction = V_remain / V_init
    k_eff    = mean(v.permeability for v in pool) / 500.0   # normalized

    # Injection support: cumulative injected by injection wells in same pool
    inj_total = sum(iw.cumulative_injected_bbl
                    for iw in injection_wells_intersecting(pool))
    pressure_boost = min(0.5, inj_total / V_init)
    effective_fraction = min(1.0, fraction + pressure_boost)

    # Capacity at this well
    q_potential = Q_MAX_WELL_BBL_DAY · k_eff · effective_fraction   # Q_MAX = 200

    # Agent setpoint clamps from above
    q_actual = min(w.setpoint_rate_bbl_day, q_potential)

    # Drain pool weighted by perm × remaining
    weights = [v.permeability · v.oil_remaining for v in pool]
    W = sum(weights)
    if W > 0:
        for v, ω in zip(pool, weights):
            v.oil_remaining -= q_actual · ω / W

    return q_actual
```

For each injection well `iw`:

```
def well_injection(iw):
    q = min(iw.setpoint_rate_bbl_day, Q_MAX_WELL_BBL_DAY)
    iw.cumulative_injected_bbl += q
    iw.power_kw = q · INJECTION_KWH_PER_BBL / 24            # 50 kWh/bbl
    return q
```

Notes:

- The 3×3×3 pool is clipped at grid boundaries (no padding)
- Multiple wells targeting overlapping pools share the resource (each runs the equation against the current `oil_remaining`; order of execution is deterministic by `well.id`)
- A well that targets a voxel with no hydrocarbon (`V_init = 0`) produces nothing and is wasted CAPEX

### 4.6 Refinery (`world/economy.py`)

```
def refine(refinery, available_crude_bbl_day):
    actual = min(refinery.setpoint_rate, available_crude_bbl_day, REFINERY_MAX_BBL_DAY)
    refined_bbl = actual · REFINERY_YIELD                   # 0.85
    refinery.power_kw = actual · REFINERY_KWH_PER_BBL / 24  # 200 kWh/bbl
    refinery.co2_t_day = actual · REFINERY_CO2_PER_BBL      # 0.30 t/bbl
    return refined_bbl, actual
```

Daily oil revenue:

```
total_crude   = Σ well_production_bbl_day
crude_to_ref  = min(total_crude, Σ refinery throughput available)
refined       = refine returns refined_bbl total
crude_direct  = total_crude - crude_to_ref

revenue_oil = crude_direct · CRUDE_PRICE        # $40/bbl
            + refined · REFINED_PRICE           # $90/bbl
```

Crude is auto-routed to refineries (preferring higher-throughput refineries first) and surplus sold raw. No pipeline transport cost in v1; pipelines are aesthetic/connectivity-only. (The PRD may upgrade this if scope allows.)

### 4.7 Carbon and emissions (`world/economy.py`)

```
CARBON_PRICE_USD_PER_TON = 25
COAL_CO2_T_PER_MWH       = 0.90
GAS_CO2_T_PER_MWH        = 0.40
INDUSTRIAL_CO2_T_PER_MWH = 0.30          # for industrial tiles' own consumption
REFINERY_CO2_PER_BBL     = 0.30

daily_emissions_t  = Σ coal_mwh_today · COAL_CO2_T_PER_MWH
                   + Σ gas_mwh_today  · GAS_CO2_T_PER_MWH
                   + Σ industrial_mwh_consumed · INDUSTRIAL_CO2_T_PER_MWH
                   + Σ refined_bbl · REFINERY_CO2_PER_BBL

daily_carbon_cost = daily_emissions_t · CARBON_PRICE_USD_PER_TON
```

### 4.8 Population (`world/population.py`)

```
def update_population(world):
    capacity = sum(t.housing_capacity for t in housing_and_townhall_tiles)
    jobs     = sum(t.jobs              for t in job_providing_tiles)

    happiness = 1.0
    happiness += 0.05 · max(0, park_count - 1)
    happiness -= 0.10 · (yesterday_blackout_hours / 24)
    happiness -= 0.05 · houses_within_3_of_coal_plant_count / max(1, house_count)
    happiness  = clip(happiness, 0.0, 1.5)

    pop = world.population

    if jobs >= pop and capacity > pop and happiness >= 0.5:
        growth = BASE_GROWTH_RATE · pop · happiness          # BASE_GROWTH_RATE = 0.012
        growth = min(growth, capacity - pop, jobs - pop)
        pop = pop + growth

    elif capacity < pop:
        pop = max(capacity, pop - 5)                          # housing exodus

    elif jobs < 0.7 · pop:
        pop = max(jobs / 0.7, pop · 0.99)                     # job-driven decline

    elif happiness < 0.5:
        pop = pop · 0.99

    world.population = max(0, int(pop))
    world.happiness  = happiness
```

Tax revenue:

```
DAILY_TAX_PER_CAPITA = 4.0
daily_tax_revenue    = world.population · DAILY_TAX_PER_CAPITA
```

### 4.9 Forecasts (`world/weather.py`)

```
def forecast(world, hours=24):
    out = []
    for i in range(hours):
        future_t = world.tick + i
        true_solar  = compute_irradiance(future_t)
        true_wind   = compute_wind(future_t)
        true_demand = compute_demand_factor(future_t)
        sigma       = 0.05 + 0.25 · (i / hours)             # 0.05 → 0.30

        out.append({
            'hour_offset':       i,
            'solar_irradiance':  clip(true_solar  · (1 + N(0, sigma)),       0, 1),
            'wind_speed_mps':    max(0, true_wind + N(0, sigma · 5)),
            'demand_factor':     true_demand · (1 + N(0, sigma · 0.3)),
        })
    return out
```

Forecasts are independently sampled per call (resampling is allowed and reduces variance via averaging).

### 4.10 Seismic surveys (`world/subsurface.py`)

```
SEISMIC_COST       = 15_000
SEISMIC_DEFAULT_N  = 8
SEISMIC_OIL_SIGMA  = 0.25
SEISMIC_PERM_SIGMA = 0.30

def survey(x, y, n=SEISMIC_DEFAULT_N):
    # Returns the n×n column of voxels at all depths around (x, y)
    region = voxels_in_column(x, y, n)
    out = []
    for v in region:
        out.append({
            'x': v.x, 'y': v.y, 'z': v.z,
            'oil_estimate_bbl':  max(0, v.oil_in_place · (1 + N(0, SEISMIC_OIL_SIGMA))),
            'perm_estimate_md':  max(0, v.permeability · (1 + N(0, SEISMIC_PERM_SIGMA))),
        })
    treasury -= SEISMIC_COST
    record_revealed(region)
    return out
```

Resurveying the same column is allowed and produces independent noise samples.

### 4.11 Events (`world/events.py`)

Event probabilities are checked once per day. At most one of each type can be active at a time except plant failures.

| Event | Daily probability | Duration | Effect |
|---|---|---|---|
| Heatwave | 0.003 | 5 days | demand × 1.40 |
| Plant failure | 0.001 per fossil plant | 3–7 days uniform | affected plant outputs 0 |
| Fuel price shock | 0.002 | 30 days | gas & coal fuel cost × 2 |
| Demand surprise | 0.003 | 10 days | industrial+commercial demand × 1.30 |
| Regulatory tightening | 0.001 | permanent | carbon price × 1.5 (cumulative) |

Active events are reported in `GET /state` under `active_events` and as new entries in the `GET /events` history. Probabilities are sampled at the *start of each day* before that day simulates.

### 4.12 Build catalog

All costs in USD. CAPEX is paid up-front at build time. OPEX is deducted daily as long as the tile exists. Demolition refunds 25% of CAPEX, takes effect immediately.

| Tile type | CAPEX | OPEX/day | Effect / specs |
|---|---:|---:|---|
| `road` | 500 | 0 | enables connectivity for civilian tiles |
| `house` | 3,000 | 20 | +8 housing capacity. Requires road adjacency. |
| `commercial` | 8,000 | 50 | +12 jobs. 50 kW demand (8–20h), 10 kW otherwise. Road adj. |
| `industrial` | 20,000 | 200 | +30 jobs. 300 kW demand continuous. Emits CO₂ on consumed power. Road adj. |
| `park` | 5,000 | 30 | +happiness multiplier (per §4.8) |
| `solar_farm` | 25,000 | 50 | up to 150 kW (sun-dependent, §4.1) |
| `wind_turbine` | 40,000 | 80 | up to 200 kW (wind-dependent, §4.2) |
| `gas_peaker` | 80,000 | 150 + fuel | 0–500 kW. Ramp 50%/h. Fuel: $30/MWh. CO₂: 0.4 t/MWh. |
| `coal_plant` | 200,000 | 400 + fuel | 200–800 kW. Ramp 10%/h. Min run 25%. Fuel: $20/MWh. CO₂: 0.9 t/MWh. |
| `oil_well` | 50,000 | 100 | production well. Setpoint 0–200 bbl/day. |
| `injection_well` | 30,000 | 50 | injection well. Setpoint 0–200 bbl/day. Power: 50 kWh/bbl. |
| `refinery` | 150,000 | 300 | +25 jobs. Up to 500 bbl/day. 200 kWh/bbl. 0.3 t CO₂/bbl. Road adj. |
| `pipeline` | 2,000 | 5 | aesthetic / connectivity tile (v1: no transport cost) |
| `town_hall` | n/a | 0 | placed at start, +100 capacity, +30 jobs, immutable |

Adjacency rules (v1):

- `house`, `commercial`, `industrial`, `refinery` must be orthogonally adjacent to a `road` tile or to another tile that is itself road-connected (4-connected flood-fill from any road)
- Power plants and oil wells do *not* require road or pipeline adjacency in v1 (grid is implicit; pipelines are visual)
- The town hall counts as a road for adjacency purposes

Validity:

- `oil_well` and `injection_well` placement requires specifying `target_z`; the tile becomes a "drilled well" and cannot be re-targeted (demolish and re-drill if needed)
- Two wells cannot share the exact same `(x, y)` tile but may target different `z`
- Tiles cannot be placed on top of existing tiles (demolish first)

---

## 5. State and API

### 5.1 Architectural principles

- The FastAPI server is the single source of truth.
- The simulation is fully deterministic given `(seed, action_log)`. This is required for reproducibility, scoring, and debugging.
- Random number generation must use a single seeded `numpy.random.Generator` threaded through the simulation, never global `random` or `np.random`.
- Actions accumulate during a day. `POST /step` is the only call that advances time.
- All POST actions are idempotent within a day in the sense that they can be undone via `/demolish` before the day commits. After `/step`, side effects are permanent.

### 5.2 Endpoints

Sixteen total. Group them in `world/api.py` by section.

**State & metadata**

| Method | Path | Description |
|---|---|---|
| `GET` | `/state` | Full world state (see §5.3) |
| `GET` | `/state/summary` | Compact summary (cash, pop, day, balance state, today's P&L) |
| `GET` | `/forecast?hours=24` | 24-hour forecast (§4.9) |
| `GET` | `/score` | Current score breakdown (see §8) |
| `GET` | `/seed` | `{ "seed": 42 }` |
| `GET` | `/catalog` | Build catalog (§4.12) as machine-readable JSON |
| `GET` | `/history?days=N` | Last N days of daily summaries |
| `GET` | `/events` | Active and historical events |

**Inspection**

| Method | Path | Description |
|---|---|---|
| `GET` | `/tiles` | List of all placed tiles |
| `GET` | `/wells` | List of all wells with status |
| `GET` | `/reservoirs` | All voxels ever revealed by surveys, with the noisy estimates from each survey |

**Mutations**

| Method | Path | Body |
|---|---|---|
| `POST` | `/reset` | `{ "seed": int? }` — resets world; if seed omitted, uses configured default |
| `POST` | `/step` | `{}` — advances 1 day |
| `POST` | `/build` | `{ "tile_type": str, "x": int, "y": int }` |
| `POST` | `/demolish` | `{ "x": int, "y": int }` |
| `POST` | `/survey` | `{ "x": int, "y": int, "size": int? }` (default size = 8) |
| `POST` | `/drill` | `{ "x": int, "y": int, "target_z": int, "well_type": "production" \| "injection" }` |
| `POST` | `/control/well` | `{ "well_id": str, "rate_bbl_day": float }` |
| `POST` | `/control/refinery` | `{ "refinery_id": str, "rate_bbl_day": float }` |
| `POST` | `/control/plant` | `{ "plant_id": str, "setpoint_kw": float \| null }` (null clears override) |

All mutating endpoints return:

```json
{
  "ok": true | false,
  "error": "string?",
  "treasury_after": 432100.5,
  "result": { ... endpoint-specific ... }
}
```

If `ok: false`, no state change occurs and `error` is human-readable (e.g. `"insufficient_funds"`, `"no_road_adjacency"`, `"tile_occupied"`, `"voxel_out_of_bounds"`).

### 5.3 `GET /state` schema

```json
{
  "seed": 42,
  "day": 145,
  "hour": 0,
  "treasury": 432100.50,
  "population": 1230,
  "happiness": 0.85,
  "config": {
    "world_w": 32, "world_h": 32, "world_d": 16,
    "game_days": 365,
    "ticks_per_day": 24,
    "carbon_price": 25.0,
    "starting_cash": 500000,
    "starting_pop": 100
  },
  "tiles": [
    {
      "id": "tile_42",
      "type": "solar_farm",
      "x": 12, "y": 8,
      "built_day": 23,
      "operational": true,
      "current_output_kw": 95.2
    }
  ],
  "wells": [
    {
      "id": "well_3",
      "type": "production",
      "x": 5, "y": 14, "target_z": 8,
      "drilled_day": 67,
      "setpoint_rate_bbl_day": 150,
      "current_rate_bbl_day": 122.4,
      "cumulative_produced_bbl": 8542.1
    }
  ],
  "reservoirs_revealed": [
    {
      "x": 5, "y": 14, "z": 8,
      "estimates": [
        { "survey_day": 15, "oil_estimate_bbl": 18250, "perm_estimate_md": 412 },
        { "survey_day": 60, "oil_estimate_bbl": 14100, "perm_estimate_md": 388 }
      ]
    }
  ],
  "active_events": [
    { "type": "heatwave", "started_day": 142, "ends_day": 147, "severity": 1.4 }
  ],
  "weather_now": {
    "solar_irradiance": 0.78,
    "wind_speed_mps": 8.2,
    "wind_direction_deg": 145,
    "cloud_factor": 0.91
  },
  "power_now": {
    "demand_kw": 1840,
    "supply_kw": 1880,
    "balance_state": "balanced",
    "by_source_kw": {
      "solar": 320, "wind": 410, "gas": 800, "coal": 350
    }
  },
  "today_summary_so_far": {
    "tax_revenue": 4920,
    "power_revenue": 1450,
    "oil_revenue": 8100,
    "opex": 2100,
    "fuel_cost": 800,
    "carbon_cost": 320,
    "blackout_hours": 0,
    "renewable_share": 0.42
  }
}
```

### 5.4 Daily summary (returned by `POST /step`)

```json
{
  "ok": true,
  "day_completed": 145,
  "summary": {
    "treasury_start": 430850.00,
    "treasury_end": 432100.50,
    "delta": 1250.50,
    "tax_revenue": 4920,
    "power_revenue": 1450,
    "oil_revenue": 8100,
    "opex": 2100,
    "fuel_cost": 800,
    "carbon_cost": 320,
    "blackout_hours": 0,
    "brownout_hours": 1,
    "renewable_share": 0.42,
    "co2_emitted_t": 12.8,
    "population_start": 1228,
    "population_end": 1230,
    "happiness": 0.85,
    "events_active": ["heatwave"]
  },
  "treasury_after": 432100.50
}
```

### 5.5 Example flow (one day)

```
GET  /state              → see treasury, pop, weather
GET  /forecast           → see next 24h
POST /survey             { "x": 10, "y": 10 }            → spend $15k, get noisy estimates
POST /build              { "tile_type": "solar_farm", "x": 4, "y": 4 }
POST /control/well       { "well_id": "well_3", "rate_bbl_day": 180 }
POST /step               {}                              → returns daily summary
```

---

## 6. Manual play UI

Served by FastAPI as static files at `/` (i.e. `localhost:8000`). Plain HTML + Canvas + vanilla JS or Preact + htm — no build step. Polls `GET /state` every 500ms while the day is paused and during day execution.

### 6.1 Layout

- **Top bar**: day counter, treasury, population, happiness, balance state badge, big "Next Day" button
- **Center**: surface tile grid, 32×32 by default. Each tile rendered with a color and icon. Click to select; if a build type is active, click places it.
- **Left rail**: build menu — list of buildable tile types with cost and brief description. Selecting one enters "build mode."
- **Right rail (tabs)**:
  - **Subsurface**: cross-section view. Pick an axis (X or Y) and an index; show the perpendicular slice with voxels colored by oil-estimate (revealed only) and outlines for unrevealed voxels.
  - **Power**: line chart of last 24h supply vs demand, plus list of plants with current output bars.
  - **Finance**: line chart of treasury, daily P&L breakdown.
  - **Wells**: table of wells with rate sliders, cumulative production, estimated remaining.
  - **Events**: active and recent events.
  - **History**: scrollable log of past daily summaries.
- **Bottom bar**: action ticker showing pending actions to be applied at next `/step`.

### 6.2 UX requirements

- Manual play must be possible without reading code or API docs
- Tile placement should give immediate visual feedback (valid/invalid based on adjacency, cash)
- The "Next Day" button must show what will happen (events about to trigger, etc.) but the agent API does not need this hint
- Keyboard shortcut: `Space` advances one day
- Speed mode: hold `Shift`+`Space` to auto-step at 1 day/sec for fast-forwarding

---

## 7. Reference agents

Two reference agents ship in `agents/`. Both implement the `Agent` interface in `agents/base.py`. Participants copy `agents/llm_react.py` to `submit/agent.py` and modify.

### 7.1 Base interface (`agents/base.py`)

```python
from typing import Protocol

class Agent(Protocol):
    def __init__(self, api_url: str, **kwargs): ...
    def play_game(self) -> dict:
        """Plays a complete game; returns final score breakdown."""
        ...
```

A standard implementation pattern (provided in `base.py` as a non-abstract helper):

```python
class BaseAgent:
    def __init__(self, api_url, **kwargs):
        self.api = ApiClient(api_url)

    def play_game(self):
        self.api.reset()
        while self.api.day() < self.api.config()['game_days']:
            obs = self.api.state()
            self.act(obs)
            self.api.step()
        return self.api.score()

    def act(self, obs):
        raise NotImplementedError
```

### 7.2 Scripted baseline (`agents/scripted.py`, ~200 lines)

Pure rule-based, deterministic, no LLM calls. Plays a complete competent game and serves as the floor baseline `P_ref`/`T_ref` for scoring (§8). High-level logic:

```
Phase A (days 0–30): bootstrap residential and basic power
  - Build roads in a 4×4 cross around town hall
  - Build 4 solar farms
  - Build 2 gas peakers (for evening peaks)
  - Build 6 houses
  - Build 2 commercial tiles
  - Survey center column

Phase B (days 30–120): scale and explore
  - When treasury > $200k: build commercial/industrial pairs
  - When reserve margin < 20% at peak: build a power plant (prefer wind if windy seed, else gas)
  - Every 30 days: survey a new column
  - When a survey reveals an estimated voxel with oil > 5000 bbl AND perm > 200 mD: drill production well there

Phase C (days 120+): operate
  - Run wells at 80% of estimated max
  - When a reservoir's local fraction (from re-surveys) drops below 0.5: place injection well in same pool
  - Build refinery once 2+ wells are producing
  - Maintain reserve margin 15–25% by adding peakers

Crisis response:
  - On heatwave: temporarily build extra gas peaker, demolish after
  - On blackout: emergency build gas peaker
```

The scripted agent is fully readable, comments cite mechanic sections of this brief, and serves as a how-to for participants who have never seen the API.

### 7.3 LLM ReAct agent (`agents/llm_react.py`, ~400 lines)

An OpenAI-compatible chat-completions agent. Configured via env:

| Env var | Example |
|---|---|
| `LLM_BASE_URL` | `https://api.openai.com/v1` |
| `LLM_API_KEY` | (secret) |
| `LLM_MODEL` | `gpt-4o-mini` |

Loop:

```
each day:
  obs = api.state()
  forecast = api.forecast()
  history = api.history(7)

  prompt = system_prompt + summarize(obs, forecast, history)
  response = llm.chat(prompt, tools=ACTION_TOOLS, max_tokens=2000)

  for tool_call in response.tool_calls:
    api.<dispatch_tool>(tool_call.arguments)

  api.step()
```

Where `ACTION_TOOLS` is a fixed JSON schema for the action vocabulary:

```python
ACTION_TOOLS = [
  {"name": "build",        "parameters": {...}},
  {"name": "demolish",     "parameters": {...}},
  {"name": "survey",       "parameters": {...}},
  {"name": "drill",        "parameters": {...}},
  {"name": "set_well_rate","parameters": {...}},
  {"name": "set_refinery_rate","parameters": {...}},
  {"name": "skip",         "parameters": {}},
]
```

`summarize(obs, forecast, history)` must compress state to ≤ 1500 tokens, dropping low-information detail. Implementation in `agents/state_summary.py` (~80 lines), exposed for participants to override.

System prompt structure (fully visible to participants in `agents/prompts.py`):

```
You are an AI managing a small city's energy and economy. ...
[brief mechanic primer, 200-300 tokens]
[scoring objective summary]
[output format: tool calls only]
```

Default token budget per game: 500,000. Agent should warn (not crash) if approaching.

### 7.4 Extension points (documented for participants)

- `summarize_state(obs) -> str` — replace state compression
- `system_prompt: str` — replace the policy prompt
- `decide(obs) -> List[Action]` — replace the entire decision logic (e.g. with a planner-executor split, classical optimizer for dispatch, learned policy)
- `ACTION_TOOLS` — extend or restrict the action vocabulary
- The `Agent` class as a whole — can be replaced with anything, as long as it exposes `play_game()`

Participants are not allowed to modify `world/`, `agents/base.py`, or `scoring.py`. They can add code anywhere under `submit/`.

---

## 8. Scoring

### 8.1 Formula

For a single seed:

```
P    = final population
T    = treasury_end - treasury_start                 # can be negative
R    = year-averaged renewable share of energy delivered

P_ref = scripted agent's final population on the same seed
T_ref = scripted agent's treasury delta on the same seed (positive baseline)

S = 0.5 · (P / P_ref)
  + 0.4 · 0.5 · (1 + tanh(T / max(T_ref, 1)))         # maps to [0, 1], with neutral at T = T_ref
  + 0.1 · R
```

Final agent score: mean of `S` across `N_eval` seeds (default 1: the held-out eval seed).

### 8.2 Properties

- Bankruptcy is heavily punished: very negative `T` makes the second term approach 0
- Hoarding cash without growth is also punished: very high `T` saturates the tanh, capped contribution 0.4 max
- Renewable share is a tie-breaker (0–10% of total)
- An agent matching the scripted baseline exactly scores ~0.5 + 0.2 + R
- A clearly winning agent scores > 1.0

### 8.3 Implementation (`scoring.py`, ~30 lines)

Pure function `score(world_final_state, baseline_p_ref, baseline_t_ref) -> dict` returning:

```json
{
  "P": 5230, "P_ref": 1850, "p_term": 1.4135,
  "T": 1850000, "T_ref": 320000, "t_term": 0.498,
  "R": 0.55, "r_term": 0.055,
  "score": 1.967
}
```

### 8.4 Baselines

Baselines (`P_ref`, `T_ref`) are computed once per seed by running the scripted agent. Cached in `baselines/{seed}.json`. The dev seed baseline is committed to the repo. The eval seed baseline is computed by organizers at scoring time.

---

## 9. Configuration

All tunables are environment variables, read at server startup. `world/config.py` is the single point of definition; every other module imports from it. The PRD must keep this centralization.

| Var | Default | Notes |
|---|---|---|
| `WORLD_SEED` | 42 | dev seed |
| `WORLD_W` | 32 | |
| `WORLD_H` | 32 | |
| `WORLD_D` | 16 | |
| `GAME_DAYS` | 365 | |
| `TICKS_PER_DAY` | 24 | |
| `STARTING_CASH` | 500000 | |
| `STARTING_POP` | 100 | |
| `CARBON_PRICE_USD_PER_TON` | 25 | |
| `GRID_PRICE_USD_PER_KWH` | 0.08 | sale price to grid |
| `BASE_GROWTH_RATE` | 0.012 | population |
| `BLACKOUT_PENALTY_HOUR` | 5000 | |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | for reference LLM agent only |
| `LLM_API_KEY` | (none) | |
| `LLM_MODEL` | `gpt-4o-mini` | |
| `API_PORT` | 8000 | |

---

## 10. Repository layout

```
hackathon-brief.md            # this file
README.md                      # quickstart for participants
docker-compose.yml
Makefile                       # `make play`, `make eval`, `make score`

world/
  __init__.py
  config.py                    # all env-var tunables (§9)
  state.py                     # core dataclasses
  grid.py                      # surface tile placement, adjacency, demolition
  subsurface.py                # voxel grid, reservoir generation, surveys, well physics (§4.5, §4.10)
  weather.py                   # sun, wind, forecasts (§4.1, §4.2, §4.9)
  power.py                     # demand, dispatch, balance (§4.3, §4.4)
  economy.py                   # carbon, refinery, daily P&L (§4.6, §4.7)
  population.py                # population dynamics (§4.8)
  events.py                    # event sampling and effects (§4.11)
  sim.py                       # tick loop, orchestrator
  api.py                       # FastAPI endpoints (§5.2)
  ui/                          # static HTML/JS/CSS for manual play
    index.html
    app.js
    style.css
  tests/
    test_grid.py
    test_subsurface.py
    test_dispatch.py
    test_population.py
    test_economy.py
    test_api_smoke.py
    test_determinism.py        # same seed + actions → same outcome

agents/
  __init__.py
  base.py                      # Agent interface + BaseAgent helper (§7.1)
  api_client.py                # thin wrapper over requests
  scripted.py                  # rule-based baseline (§7.2)
  llm_react.py                 # LLM ReAct agent (§7.3)
  state_summary.py             # state compression for LLM context
  prompts.py                   # system prompts (extension point)

submit/
  agent.py                     # starts as copy of llm_react.py; participants edit

scoring.py                     # scoring function (§8)
baselines/
  seed_42.json                 # baselines for dev seed, committed

evaluate.py                    # CLI: `python evaluate.py --agent submit.agent --seed 42`
```

Approximate line counts (target):

| Path | Lines |
|---|---|
| `world/` | ~1800 |
| `world/ui/` | ~600 |
| `agents/` | ~800 |
| `tests/` | ~600 |
| Total Python | ~3200 |

---

## 11. Deployment

### 11.1 `docker-compose.yml`

Two services:

```yaml
services:
  world:
    build: .
    ports: ["8000:8000"]
    environment:
      - WORLD_SEED=42
      - WORLD_W=32
      - WORLD_H=32
      - WORLD_D=16
      - GAME_DAYS=365
    command: uvicorn world.api:app --host 0.0.0.0 --port 8000

  agent:
    build: .
    depends_on: [world]
    environment:
      - WORLD_API_URL=http://world:8000
      - LLM_BASE_URL=${LLM_BASE_URL}
      - LLM_API_KEY=${LLM_API_KEY}
      - LLM_MODEL=${LLM_MODEL}
    volumes:
      - ./submit:/app/submit
    command: python evaluate.py --agent submit.agent --seed 42
    profiles: ["eval"]
```

The `agent` service is opt-in via `--profile eval` so manual play doesn't trigger it.

### 11.2 Three commands participants must remember

```
docker compose up                                    # play manually at localhost:8000
docker compose --profile eval run agent              # evaluate submit/agent.py on seed 42
make score                                           # evaluate on all dev seeds, print line
```

### 11.3 LLM access

Hosted by organizers, OpenAI-compatible. Each team gets an API key with the per-game token budget enforced server-side. Participants never see organizer infrastructure details — they get a `.env` file at registration with `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` set.

---

## 12. Submission and judging

### 12.1 What participants submit

A pull request (or a zipped repo) containing:

1. `submit/agent.py` — the agent
2. Any additional files under `submit/` they want (utilities, prompts, cached priors)
3. `submit/WRITEUP.md` — 1 page describing approach, design choices, what they tried that didn't work

### 12.2 Constraints

- May only modify files under `submit/`
- Final agent must run via `docker compose --profile eval run agent`
- ≤ 5 minutes wall-clock per evaluation seed
- ≤ 500,000 LLM tokens per game (enforced by token-counting middleware)
- No outbound network access from `submit/` other than the configured LLM endpoint
- No file I/O outside `submit/` (read-only); model checkpoints, caches, etc., must live in `submit/`

### 12.3 Evaluation

Organizers run the held-out eval seed against each submission. Each agent runs once on the eval seed (no resampling; weather and events are already stochastic within the seed via the deterministic seeded RNG). Top 3 by score present approach.

---

## 13. 24-hour participant schedule (suggested)

| Hour | Activity |
|---|---|
| 0–1 | Setup, `docker compose up`, play 1 manual game to feel mechanics |
| 1–3 | Read `world/` end-to-end (it's ~2000 lines, this is achievable) |
| 3–4 | Read `agents/scripted.py` and `agents/llm_react.py` |
| 4–6 | Run `agents/llm_react.py` baseline on dev seed; observe its decisions |
| 6–10 | First iteration — improve state summarization, prompt, action selection |
| 10–14 | Second iteration — add memory/reflection, planner-executor split |
| 14–18 | Third iteration — domain-specific subroutines (reservoir Bayesian update, classical-LP dispatcher, etc.) |
| 18–22 | Tune, debug, beat the scripted baseline reliably |
| 22–24 | Write writeup, package, submit |

---

## 14. Code reading tour (for the "30-minute onboarding" claim)

Order to read for understanding:

1. `world/config.py` — every magic number
2. `world/state.py` — core dataclasses
3. `world/sim.py` — the tick loop (the spine)
4. `world/weather.py` — sun, wind, forecasts
5. `world/power.py` — demand, dispatch, balance
6. `world/subsurface.py` — voxels, surveys, well physics
7. `world/economy.py` — finances and carbon
8. `world/population.py` — pop dynamics
9. `world/events.py` — shocks
10. `world/api.py` — the surface
11. `agents/base.py` and `agents/scripted.py` — minimal complete agent
12. `agents/llm_react.py` and `agents/state_summary.py` — the LLM agent

Any one file is at most ~250 lines. Reading all of `world/` should take an experienced Python developer 30–45 minutes.

---

## 15. Determinism, testing, and replay

- Single seeded `numpy.random.Generator` threaded through `world/sim.py`. No global RNG access anywhere.
- Every action is logged to `runs/{run_id}/actions.jsonl`.
- `python evaluate.py --replay runs/{id}` re-runs identically.
- `tests/test_determinism.py` verifies that running the scripted agent twice on the same seed produces byte-identical state.

---

## 16. Open questions / explicit non-goals for v1

These are intentionally not in scope; the PRD should not introduce them unless explicitly extending scope:

- No transmission losses, no spatial power grid topology
- No pipeline transport cost or capacity
- No water resource constraint on injection
- No drill-through-rock cost variation by depth (drilling cost is flat $50k regardless of `target_z`)
- No multi-agent / multi-player interaction
- No real-time websocket streaming (poll is fine)
- No persistence across container restarts (game is in-memory; reset on restart)
- No authentication on the API (single-tenant local deployment)
- No rate-limiting (trust local agent)
- No save/load mid-game (a game is a single uninterrupted run)

---

## 17. Notes for the PRD-writing agent

- Treat all numbers in §3.4, §4.x, §4.12, §8, and §9 as defaults that may be tuned during playtesting. Centralize them in `world/config.py` so tuning is a one-line change per parameter.
- Treat all equations in §4 as binding for the v1 implementation. They have been chosen specifically for hackathon-appropriate complexity (closed-form, fast, readable). Do not silently substitute "more realistic" alternatives.
- The 1:1 mapping between brief sections and code files (§10) is a design goal, not coincidence. Preserve it.
- Express user stories from the perspective of two personas: the *human player* (manual play) and the *AI agent author* (writes `submit/agent.py`). The judges and organizers are secondary personas.
- Acceptance criteria for the world should test mechanics in isolation (each equation as a unit test) plus an end-to-end smoke test (scripted agent completes 365 days on seed 42 with score within 5% of recorded baseline).
- Acceptance criteria for the UI should focus on the manual-play scenario: a person who has never seen the game can place tiles, run surveys, drill a well, and complete a year, with the mechanics legible.
- Acceptance criteria for the reference agents should cover (a) the scripted agent runs deterministically and produces the committed baseline, (b) the LLM ReAct agent runs end-to-end with a realistic small model (e.g. gpt-4o-mini) within token budget.
- The brief deliberately leaves UX details (colors, fonts, exact panel layouts) to the implementer. Do not over-specify them in the PRD.
- The brief deliberately fixes API contracts (endpoints, schemas, error semantics) tightly. Specify these as hard requirements in the PRD with example request/response pairs derived from §5.

---

*End of brief.*

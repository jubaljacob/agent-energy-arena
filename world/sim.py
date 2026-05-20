"""Simulation orchestrator.

Owns the deterministic tick loop and the two RNG streams that the rest of the
world will draw from. The skeleton slice has no dynamics yet — the daily loop
exists only to lock in the determinism contract:

  * `sim_rng` advances per **simulated day**, not per `/step` call, so
    `step(days=7)` is byte-identical to `step(days=1)` × 7.
  * `forecast_rng` is an independent child of the master seed, so
    `/forecast` calls never perturb simulation state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from world import placement
from world.catalog import TILE_CATALOG, is_buildable
from world.config import Config, load_config
from world.economy import (
    CARBON_PRICE_USD_PER_TON,
    COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY,
    INDUSTRIAL_REVENUE_PER_DAY,
    REFINED_PRICE_USD_PER_BBL,
    REFINERY_SETPOINT_MAX,
    REFINERY_SETPOINT_MIN,
    pin_yesterday,
    settle_carbon,
    settle_eod_treasury,
    settle_fuel,
    settle_opex,
    update_civic_revenue,
)
from world.events import (
    expire_finite_events,
    sample_and_apply_events,
)
from world.grid import has_road_adjacency, in_bounds, road_connected_set
from world.hourly_tick import commit_tick, hourly_tick
from world.pipelines import build_pipeline_graph, peaker_supplied_ids, route_oil, routing_units
from world.population import DAILY_TAX_PER_CAPITA, update_population
from world.power import PLANT_TYPES
from world.recorder import Recorder
from world.scenario import NullScenario, Scenario
from world.snapshots import WeatherNow
from world.state import Tile, Well, WorldState
from world.state_view import tile_view, well_view
from world.subsurface import (
    CRUDE_PRICE_USD_PER_BBL,
    WELL_SETPOINT_MAX,
    WELL_SETPOINT_MIN,
    SubsurfaceGrid,
    drill_capex,
    drill_collision,
    generate_subsurface,
    is_size_valid,
    reservoirs_summary,
    reservoirs_voxel_summary,
    revealed_voxels,
    survey_cost,
    well_reservoir_id,
)
from world.subsurface import survey as run_survey
from world.weather import (
    INITIAL_CLOUD_FACTOR,
    INITIAL_WIND_DIRECTION_DEG,
    derive_phi_seed,
    step_weather_one_hour,
    v_mean,
)
from world.wells import commit_well_injections, run_production_loop
from world.workforce import employed as workforce_employed
from world.workforce import hire_to_fill
from world.workforce import total_jobs as workforce_total_jobs
from world.workforce import unemployed as workforce_unemployed


def _scenario_dotted_path(scenario: Scenario, override: str | None = None) -> str | None:
    """Best-effort dotted-path for a Scenario instance for metadata.json.

    `override` wins when the caller (e.g. the API loader, evaluate.py
    `--scenario` flag) knows the exact dotted path supplied by the user
    — that path is authoritative because the loader walks the module
    for any matching subclass, so the class-derived fallback may be
    `module.ClassName` while the user wrote `module`. Returns `None`
    for the default `NullScenario`; otherwise returns either the
    override or `"module.ClassName"`.
    """
    if override is not None:
        return override
    if isinstance(scenario, NullScenario):
        return None
    cls = type(scenario)
    return f"{cls.__module__}.{cls.__name__}"


@dataclass
class StepSummary:
    ok: bool
    day_completed: int
    summary: dict[str, Any]
    treasury_after: float


class World:
    def __init__(
        self,
        config: Config | None = None,
        *,
        session: str = "agent",
        scenario: Scenario | None = None,
        runs_root: str | None = None,
        seed_starter_grid: bool = False,
    ) -> None:
        self.config: Config = config or load_config()
        self.session: str = session
        self.scenario: Scenario = scenario if scenario is not None else NullScenario()
        # open-source-arena slice 04: user-supplied dotted path captured
        # by `POST /scenario` / `POST /reset {"scenario": ...}` /
        # `evaluate.py --scenario`. When non-None it is preferred over
        # the class-derived fallback in `_scenario_dotted_path`, so
        # `metadata.json` records the path the caller actually wrote
        # rather than the loader's resolved class location.
        self.scenario_dotted_path: str | None = None
        # open-source-arena slice 03: optional per-game state log. When
        # `runs_root` is set, every reset allocates a fresh `Recorder`
        # and the in-progress one (if any) is finalized first — no run
        # is destroyed by a reset. Tests pass `runs_root=None` to skip
        # filesystem side effects; api.py / evaluate.py pass "runs".
        self.runs_root: str | None = runs_root
        # Production callers (`create_app`, `evaluate.py`) opt in to
        # a starter coal plant + road bridge at reset so the agent
        # doesn't have to bootstrap power from a blank field. Unit
        # tests of individual mechanics leave it False so they keep
        # a controlled "town hall only" baseline.
        self._seed_starter_grid: bool = seed_starter_grid
        self.recorder: Recorder | None = None
        self.state: WorldState = WorldState(seed=self.config.world_seed)
        self.sim_rng: np.random.Generator
        self.forecast_rng: np.random.Generator
        self.event_rng: np.random.Generator
        self.wind_phi_seed: float = 0.0
        self._tile_seq: int = 0
        self._well_seq: int = 0
        self.subsurface: SubsurfaceGrid = SubsurfaceGrid(
            width=self.config.world_w,
            height=self.config.world_h,
            depth=self.config.world_d,
        )
        self.reset(seed=self.config.world_seed)

    # -- Convenience accessors --------------------------------------------

    @property
    def day(self) -> int:
        return self.state.day

    @property
    def hour(self) -> int:
        return self.state.hour

    # -- Lifecycle ---------------------------------------------------------

    def reset(
        self,
        seed: int | None = None,
        *,
        scenario: Scenario | None = None,
        scenario_dotted_path: str | None = None,
    ) -> None:
        seed_used = self.config.world_seed if seed is None else int(seed)
        if scenario is not None:
            self.scenario = scenario
            # When the caller passes a scenario instance, the dotted path
            # is reset alongside it — None means "use class-derived
            # fallback", a non-None override pins the user-supplied path.
            self.scenario_dotted_path = scenario_dotted_path
        # open-source-arena slice 03: close out any in-progress recorder
        # before allocating a fresh one. Calling finalize on a recorder
        # that has been finalized already is a no-op (idempotent).
        if self.recorder is not None:
            self.recorder.finalize(self)
        if self.runs_root is not None:
            self.recorder = Recorder(
                root=self.runs_root,
                seed=seed_used,
                scenario_name=_scenario_dotted_path(self.scenario, self.scenario_dotted_path),
                session=self.session,
            )
        master = np.random.SeedSequence(seed_used)
        # Three independent streams: sim drives world dynamics (weather, etc.),
        # forecast drives /forecast noise, event drives the slice-11 daily
        # event rolls. SeedSequence.spawn(n) is incremental, so adding a third
        # child preserves the exact bytes of children 0/1 — sim_rng and
        # forecast_rng remain identical to the slice-01 baseline.
        sim_seed, forecast_seed, event_seed = master.spawn(3)
        self.sim_rng = np.random.default_rng(sim_seed)
        self.forecast_rng = np.random.default_rng(forecast_seed)
        self.event_rng = np.random.default_rng(event_seed)

        self.wind_phi_seed = derive_phi_seed(seed_used)
        self.state = WorldState(
            seed=seed_used,
            day=0,
            hour=0,
            treasury=float(self.config.starting_cash),
            population=float(self.config.starting_pop),
            happiness=1.0,
            carbon_price=CARBON_PRICE_USD_PER_TON,
            # open-source-arena slice 01: pricing/rate fields flow from the
            # module-level constants and Config defaults at reset time.
            # Scenarios mutate them mid-game via `apply(world, day)`.
            crude_price_usd_per_bbl=CRUDE_PRICE_USD_PER_BBL,
            refined_price_usd_per_bbl=REFINED_PRICE_USD_PER_BBL,
            grid_price_retail=self.config.grid_price_retail,
            grid_price_export=self.config.grid_price_export,
            industrial_revenue_per_day=INDUSTRIAL_REVENUE_PER_DAY,
            commercial_revenue_per_resident_per_day=COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY,
            daily_tax_per_capita=DAILY_TAX_PER_CAPITA,
            outage_penalty_hour=self.config.outage_penalty_hour,
            brownout_flat_penalty_hour=self.config.brownout_flat_penalty_hour,
            plant_fuel_cost_per_mwh={
                "coal_plant": TILE_CATALOG["coal_plant"].fuel_cost_per_mwh,
                "gas_peaker": TILE_CATALOG["gas_peaker"].fuel_cost_per_mwh,
            },
        )
        # Seed the AR(1) carry-overs at their long-run means so the first
        # hour's update is well-conditioned (no transient from a 0 init).
        self.state.weather_now = WeatherNow(
            cloud_factor=INITIAL_CLOUD_FACTOR,
            wind_speed_mps=v_mean(0, self.wind_phi_seed),
            wind_direction_deg=INITIAL_WIND_DIRECTION_DEG,
            solar_irradiance=0.0,
        )
        self._tile_seq = 0
        self._well_seq = 0
        # Subsurface generation consumes sim_rng draws BEFORE any /step is
        # called. Same-seed reset is therefore byte-reproducible (§3.5
        # "two /reset calls with the same seed produce byte-identical
        # voxel grids").
        self.subsurface = generate_subsurface(
            self.sim_rng,
            self.config.world_w,
            self.config.world_h,
            self.config.world_d,
        )
        self._place_town_hall()
        if self._seed_starter_grid:
            self._place_starter_grid()
        # Workforce slice 01: auto-staff the town hall (and the
        # starter coal plant when seeded) from the starting
        # unemployed pool. With 100 starting pop, 30 town-hall + 30
        # coal-plant jobs leave 40 idle in the production starter.
        hire_to_fill(self.state)

    def _place_town_hall(self) -> None:
        spec = TILE_CATALOG["town_hall"]
        self.state.tiles.append(
            Tile(
                id=self._next_tile_id("town_hall"),
                type="town_hall",
                x=self.config.world_w // 2,
                y=self.config.world_h // 2,
                built_day=0,
                operational=True,
                capex_paid=0.0,
                opex_per_day=spec.opex_per_day,
                housing_capacity=spec.housing_capacity,
                jobs=spec.jobs,
                demand_kw=spec.demand_kw,
            )
        )

    def _place_starter_grid(self) -> None:
        """Drop a coal plant + a road bridge to the town hall at reset
        so the agent doesn't have to bootstrap power from scratch. The
        capex is *not* charged against starting_cash — the starter
        layout is a free gift, equivalent to the town hall itself.
        Normal per-day opex still applies.

        Layout (default 32x32 world): town hall at (16, 16); coal at
        (8, 16); roads at (9, 16) .. (15, 16). The road chain
        satisfies the coal plant's road-adjacency requirement and
        plugs into the town hall (which counts as road via
        `grid.ROAD_TYPES`)."""
        tx = self.config.world_w // 2
        ty = self.config.world_h // 2
        coal_xy = (tx - 8, ty)
        road_xs = range(tx - 7, tx)  # (tx-7) inclusive through (tx-1)

        for x in road_xs:
            spec = TILE_CATALOG["road"]
            self.state.tiles.append(
                Tile(
                    id=self._next_tile_id("road"),
                    type="road",
                    x=x,
                    y=ty,
                    built_day=0,
                    operational=True,
                    capex_paid=0.0,
                    opex_per_day=spec.opex_per_day,
                    housing_capacity=spec.housing_capacity,
                    jobs=spec.jobs,
                    demand_kw=spec.demand_kw,
                )
            )

        coal_spec = TILE_CATALOG["coal_plant"]
        self.state.tiles.append(
            Tile(
                id=self._next_tile_id("coal_plant"),
                type="coal_plant",
                x=coal_xy[0],
                y=coal_xy[1],
                built_day=0,
                operational=True,
                capex_paid=0.0,
                opex_per_day=coal_spec.opex_per_day,
                housing_capacity=coal_spec.housing_capacity,
                jobs=coal_spec.jobs,
                demand_kw=coal_spec.demand_kw,
            )
        )

    def _next_tile_id(self, tile_type: str) -> str:
        self._tile_seq += 1
        return f"{tile_type}-{self._tile_seq}"

    # -- Build / demolish --------------------------------------------------

    def build(self, tile_type: str, x: int, y: int) -> dict[str, Any]:
        if not is_buildable(tile_type):
            return self._build_error("unknown_tile_type")
        if not in_bounds(x, y, self.config.world_w, self.config.world_h):
            return self._build_error("out_of_bounds")
        if self._tile_at(x, y) is not None:
            return self._build_error("tile_occupied")

        spec = TILE_CATALOG[tile_type]
        if spec.requires_road and not has_road_adjacency(
            x, y, self.state.tiles, self.config.world_w, self.config.world_h
        ):
            return self._build_error("no_road_adjacency")
        spacing_offender = placement.validate(tile_type, (x, y), self.state.tiles)
        if spacing_offender is not None:
            return self._build_error(
                "spacing_violation",
                result={"x": spacing_offender.x, "y": spacing_offender.y},
            )
        if self.state.treasury < spec.capex:
            return self._build_error("insufficient_funds")

        self.state.treasury -= spec.capex
        tile = Tile(
            id=self._next_tile_id(tile_type),
            type=tile_type,
            x=x,
            y=y,
            built_day=self.state.day,
            operational=True,
            capex_paid=spec.capex,
            opex_per_day=spec.opex_per_day,
            housing_capacity=spec.housing_capacity,
            jobs=spec.jobs,
            demand_kw=spec.demand_kw,
        )
        self.state.tiles.append(tile)
        hire_to_fill(self.state)
        return {
            "ok": True,
            "treasury_after": self.state.treasury,
            "result": tile_view(tile, self),
        }

    def demolish(self, x: int, y: int) -> dict[str, Any]:
        if not in_bounds(x, y, self.config.world_w, self.config.world_h):
            return self._build_error("out_of_bounds")
        tile = self._tile_at(x, y)
        if tile is None:
            return self._build_error("no_tile")
        if tile.type == "town_hall":
            return self._build_error("cannot_demolish_townhall")

        if tile.type == "road":
            stranded = self._roads_stranded_if_removed(tile)
            if stranded:
                return {
                    "ok": False,
                    "error": "would_disconnect",
                    "treasury_after": self.state.treasury,
                    "result": {"stranded": stranded},
                }

        refund = 0.25 * tile.capex_paid
        self.state.treasury += refund
        self.state.tiles.remove(tile)
        # Workforce slice 01: the demolished tile's staffed_jobs are gone with
        # the tile, returning those workers to the unemployed pool. Backfill
        # any older under-staffed facility before the response goes out.
        hire_to_fill(self.state)
        return {
            "ok": True,
            "treasury_after": self.state.treasury,
            "result": {
                "demolished_id": tile.id,
                "type": tile.type,
                "x": tile.x,
                "y": tile.y,
                "refund": refund,
            },
        }

    def _roads_stranded_if_removed(self, target: Tile) -> list[dict[str, Any]]:
        remaining = [t for t in self.state.tiles if t is not target]
        new_network = road_connected_set(remaining, self.config.world_w, self.config.world_h)
        stranded: list[dict[str, Any]] = []
        for t in remaining:
            spec = TILE_CATALOG.get(t.type)
            if spec is None or not spec.requires_road:
                continue
            has_neighbor = any(
                (t.x + dx, t.y + dy) in new_network for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
            )
            if not has_neighbor:
                stranded.append({"x": t.x, "y": t.y, "type": t.type})
        return stranded

    def _tile_at(self, x: int, y: int) -> Tile | None:
        for t in self.state.tiles:
            if t.x == x and t.y == y:
                return t
        return None

    def _build_error(self, code: str, result: Any = None) -> dict[str, Any]:
        return {
            "ok": False,
            "error": code,
            "treasury_after": self.state.treasury,
            "result": result,
        }

    # -- Surveys -----------------------------------------------------------

    def survey(self, x: int, y: int, size: int) -> dict[str, Any]:
        if not is_size_valid(size):
            return self._build_error("invalid_size")
        if not in_bounds(x, y, self.config.world_w, self.config.world_h):
            return self._build_error("out_of_bounds")
        cost = survey_cost(size)
        if self.state.treasury < cost:
            return self._build_error("insufficient_funds")

        self.state.treasury -= cost
        records = run_survey(
            self.subsurface,
            self.sim_rng,
            x,
            y,
            size,
            self.state.day,
        )
        return {
            "ok": True,
            "treasury_after": self.state.treasury,
            "result": {
                "x": x,
                "y": y,
                "size": size,
                "cost": cost,
                "voxels": records,
            },
        }

    # -- Wells (drill + control) ------------------------------------------

    def drill(self, x: int, y: int, target_z: int, well_type: str) -> dict[str, Any]:
        if well_type not in ("production", "injection"):
            return self._build_error("invalid_well_type")
        if not in_bounds(x, y, self.config.world_w, self.config.world_h):
            return self._build_error("out_of_bounds")
        if not (0 <= target_z < self.config.world_d):
            return self._build_error("voxel_out_of_bounds")
        # Stacked completions are allowed at the same (x, y) iff the two
        # 3×3×3 drainage cubes do not overlap on the z-axis (|Δtarget_z|
        # ≥ 3). A non-well build (road / refinery / pipeline) on the
        # surface tile still blocks with `tile_occupied`. Both checks
        # live in the pure helper so the UI hover-affordance can share
        # the predicate.
        collision = drill_collision(self.state.wells, self.state.tiles, x, y, target_z)
        if collision is not None:
            return self._build_error(collision)

        spec_type = "oil_well" if well_type == "production" else "injection_well"
        spec = TILE_CATALOG[spec_type]
        capex = drill_capex(float(spec.capex), target_z, self.config.world_d)
        if self.state.treasury < capex:
            return self._build_error("insufficient_funds")

        self.state.treasury -= capex
        well = Well(
            id=self._next_well_id(well_type),
            type=well_type,
            x=x,
            y=y,
            target_z=target_z,
            drilled_day=self.state.day,
            capex_paid=capex,
            opex_per_day=spec.opex_per_day,
            reservoir_id=well_reservoir_id(self.subsurface, x, y, target_z),
        )
        self.state.wells.append(well)
        hire_to_fill(self.state)
        return {
            "ok": True,
            "treasury_after": self.state.treasury,
            "result": well_view(well, self),
        }

    def control_well(self, well_id: str, rate_bbl_day: float) -> dict[str, Any]:
        well = next((w for w in self.state.wells if w.id == well_id), None)
        if well is None:
            return self._build_error("unknown_well")
        # Setpoint is clamped to the hardware bounds [0, 200] bbl/day. Out
        # of band requests succeed with the clamped value rather than fail,
        # so an over-eager agent can't brick a control loop on a typo.
        clamped = max(WELL_SETPOINT_MIN, min(WELL_SETPOINT_MAX, float(rate_bbl_day)))
        well.setpoint_rate_bbl_day = clamped
        return {
            "ok": True,
            "treasury_after": self.state.treasury,
            "result": {
                "well_id": well.id,
                "setpoint_rate_bbl_day": clamped,
            },
        }

    def control_battery(self, tile_id: str, charge_kw: float) -> dict[str, Any]:
        # Battery setpoint sign convention: >0 = charge, <0 = discharge,
        # 0 = auto. Slice 01 only stores the value; slice 02 lights it up in
        # dispatch (manual positive is clamped to renewable surplus at step
        # 1.5; manual negative is honored up to SoC at step 5).
        tile = next(
            (t for t in self.state.tiles if t.id == tile_id and t.type == "battery"),
            None,
        )
        if tile is None:
            return self._build_error("unknown_battery")
        tile.charge_setpoint_kw = float(charge_kw)
        return {
            "ok": True,
            "treasury_after": self.state.treasury,
            "result": {
                "tile_id": tile.id,
                "charge_setpoint_kw": tile.charge_setpoint_kw,
                "soc_kwh": tile.soc_kwh,
            },
        }

    def control_refinery(self, refinery_id: str, rate_bbl_day: float) -> dict[str, Any]:
        tile = next(
            (t for t in self.state.tiles if t.id == refinery_id and t.type == "refinery"),
            None,
        )
        if tile is None:
            return self._build_error("unknown_refinery")
        clamped = max(REFINERY_SETPOINT_MIN, min(REFINERY_SETPOINT_MAX, float(rate_bbl_day)))
        tile.setpoint_rate_bbl_day = clamped
        return {
            "ok": True,
            "treasury_after": self.state.treasury,
            "result": {
                "refinery_id": tile.id,
                "setpoint_rate_bbl_day": clamped,
            },
        }

    def _next_well_id(self, well_type: str) -> str:
        self._well_seq += 1
        return f"{well_type}-{self._well_seq}"

    def reservoirs(self, *, min_oil: float = 0.0, top_k: int = 100) -> dict[str, Any]:
        rows = revealed_voxels(self.subsurface, min_oil=min_oil, top_k=top_k)
        return {
            "voxels": rows,
            "n_returned": len(rows),
            "filter": {"min_oil": min_oil, "top_k": top_k},
        }

    # -- Time advance ------------------------------------------------------

    def step(self, days: int = 7) -> StepSummary:
        if not isinstance(days, int) or days < 1 or days > 7:
            raise ValueError(f"days must be an int in [1, 7]; got {days!r}")

        treasury_start = self.state.treasury
        pop_start = int(self.state.population)

        for _ in range(days):
            self._advance_one_day()

        return StepSummary(
            ok=True,
            day_completed=self.state.day,
            summary={
                "treasury_start": treasury_start,
                "treasury_end": self.state.treasury,
                "delta": self.state.treasury - treasury_start,
                "population_start": pop_start,
                "population_end": int(self.state.population),
                "happiness": self.state.happiness,
                "events_active": list(self.state.active_events),
            },
            treasury_after=self.state.treasury,
        )

    def _advance_one_day(self) -> None:
        # Reset today's running ledger at the start of each simulated day so
        # callers can read per-day P&L from `state.today`.
        self.state.today.reset()

        # oilfield-v2 slice 03: snapshot every well's current_rate_bbl_day
        # into yesterday_rate_bbl_day BEFORE any production/injection
        # computation. Producers' rate-based pressure_boost is computed
        # against this single-day snapshot, so an injector that idled
        # yesterday contributes 0 today even if its setpoint is non-zero.
        for w in self.state.wells:
            w.yesterday_rate_bbl_day = w.current_rate_bbl_day

        # facility-economics-popup slice 03: reset per-plant daily kWh-served
        # accumulators. Hourly dispatch outputs are summed in below; the
        # end-of-day copy to `kwh_served_yesterday` feeds the next day's
        # per-plant revenue estimate.
        for t in self.state.tiles:
            if t.type in PLANT_TYPES:
                t.kwh_served_today = 0.0

        # Pipeline graph (one BFS over pipeline tiles): the tile grid is
        # immutable for the duration of `_advance_one_day`, so the graph
        # the hourly peaker-supply check needs is the same graph
        # `route_oil` consumes at end-of-day. Build once here, reuse
        # below. Refinery operational flags are also day-stable, so the
        # peaker-supplied set computed from `graph` is constant across
        # the 24-hour loop.
        graph = build_pipeline_graph(self.state.tiles)
        supplied_peaker_ids = peaker_supplied_ids(graph, self.state.tiles)

        # Events (slice 11): expire yesterday's finished events first (so a
        # fresh roll today doesn't get stomped by an immediate expiry), then
        # roll new events from event_rng. Effects apply via:
        #  - heatwave / demand_surprise: read by world.power.total_demand_kw
        #  - fuel_price_shock: read at the end-of-day fuel-cost step below
        #  - plant_failure: sets tile.operational=False; dispatch already
        #    filters non-operational plants
        #  - regulatory_tightening: bumps state.carbon_price immediately.
        expire_finite_events(self)
        # Scenario hook (open-source-arena slice 02). Called once per day
        # AFTER expiry and BEFORE the stochastic sampler, so a scenario-
        # injected event (heatwave, plant_failure, ...) on the same day
        # suppresses a stochastic roll of the same type via the existing
        # "already active" guard in `sample_and_apply_events`. The default
        # NullScenario is a no-op, so determinism is preserved.
        self.scenario.apply(self, self.state.day)
        sample_and_apply_events(self)

        for hour in range(self.config.ticks_per_day):
            self.state.hour = hour
            # Each hour: 3 sim_rng draws (cloud, wind speed, wind dir) inside
            # step_weather_one_hour, then the deterministic demand + dispatch
            # calculation inside hourly_tick. RNG draws are confined to
            # step_weather_one_hour to anchor the slice-01 step-size
            # determinism contract; hourly_tick itself consumes no RNG.
            self.state.weather_now = step_weather_one_hour(self)

            # Previous hour's per-plant outputs feed the next hour's ramp
            # limits. ``tile.current_output_kw`` is the canonical store,
            # updated by ``commit_tick`` at the bottom of each hour. Day-0
            # hour-0 reads 0.0 across the board, which is exactly how
            # ``dispatch`` treats unknown plants (warm-starts coal, cold-
            # starts gas).
            prev_outputs = {
                p.id: p.current_output_kw for p in self.state.tiles if p.type in PLANT_TYPES
            }

            # Fresh-world hour 0 reads state.power_now.balance_state, which
            # defaults to BalanceState.BALANCED. DR-on-injection and producer
            # power shedding both run against the PREVIOUS hour's balance to
            # break the otherwise-circular dependency between load and
            # dispatch.
            prev_balance = self.state.power_now.balance_state
            result = hourly_tick(
                self.state,
                hour,
                prev_outputs,
                prev_balance,
                self.state.weather_now,
                supplied_peaker_ids,
            )
            # commit_tick applies every per-hour mutation (battery SoC,
            # outage bookkeeping, power revenue, renewable share, well
            # commits, by-source accumulators, per-plant outputs,
            # PowerNow snapshot, hourly traces) into state. Preview never
            # calls commit_tick — that's the seam that keeps preview/sim
            # drift-impossible. See world/hourly_tick.py.
            commit_tick(self.state, result)

        # End-of-day phase sequence. Each phase is a self-contained module
        # documented at its declaration site; the order here is the only
        # place the sequence is visible. The constraints are:
        #
        #   * settle_fuel reads state.today.coal_kwh / gas_kwh (commit_tick).
        #   * commit_well_injections must precede route_oil (route_oil
        #     reads well.current_rate_bbl_day, set by both well loops).
        #   * run_production_loop must precede route_oil (same reason).
        #   * route_oil must precede settle_carbon (pins refined_bbl).
        #   * pin_yesterday must precede update_civic_revenue (which reads
        #     last_day_trace.supply_kw_by_hour for the industrial gate).
        #   * update_civic_revenue must precede update_population
        #     (commercial revenue uses today's lived population).
        settle_opex(self.state)
        settle_fuel(self.state)
        settle_eod_treasury(self.state)
        commit_well_injections(self.state)
        run_production_loop(self)
        route_oil(self.state, graph)
        settle_carbon(self)
        pin_yesterday(self.state)
        update_civic_revenue(self)
        update_population(self)

        # Recorder hook (open-source-arena slice 03). Pin one
        # `states.jsonl` entry per simulated day BEFORE the day counter
        # increments, so the embedded `state_dict()` reflects the end
        # of day N. `today` (DayLedger) is the just-completed day's
        # P&L. No-op when running without a runs_root (tests).
        if self.recorder is not None:
            self.recorder.record_step(self, self.state.day)

        self.state.day += 1
        self.state.hour = 0

    # -- Forecast (uses forecast_rng; never perturbs sim state) ------------

    def forecast(self, hours: int = 24) -> list[dict[str, Any]]:
        from world.forecast import forecast_records

        return forecast_records(self, int(hours))

    # -- Read-models -------------------------------------------------------

    def state_dict(self) -> dict[str, Any]:
        from world.preview import preview_next_day

        s = self.state
        c = self.config
        # oilfield-v2 slice 08: per-day pipeline graph summary. Pure derivation
        # over the current tiles + wells, so it stays in lockstep with the
        # routing decision taken in `_advance_one_day`.
        graph = build_pipeline_graph(s.tiles)
        networks, orphan_wells, orphan_refineries = routing_units(graph, s.tiles, s.wells)
        pipeline_networks = [
            {
                "component_id": i,
                "well_ids": [w.id for w in net_wells],
                "refinery_ids": [r.id for r in net_refs],
            }
            for i, (net_wells, net_refs) in enumerate(networks)
        ]
        orphan_well_ids = [w.id for w in orphan_wells]
        orphan_refinery_ids = [r.id for r in orphan_refineries]
        return {
            "seed": s.seed,
            "day": s.day,
            "hour": s.hour,
            "treasury": s.treasury,
            "population": int(s.population),
            "employed": workforce_employed(s),
            "unemployed": workforce_unemployed(s),
            "housing_capacity": sum(t.housing_capacity for t in s.tiles),
            "jobs_total": workforce_total_jobs(s),
            "jobs_vacant": max(0, workforce_total_jobs(s) - workforce_employed(s)),
            "happiness": s.happiness,
            "config": {
                "world_w": c.world_w,
                "world_h": c.world_h,
                "world_d": c.world_d,
                "game_days": c.game_days,
                "manual_game_days": c.manual_game_days,
                "ticks_per_day": c.ticks_per_day,
                "carbon_price": c.carbon_price,
                "starting_cash": c.starting_cash,
                "starting_pop": c.starting_pop,
                "session": self.session,
                "active_game_days": (
                    c.manual_game_days if self.session == "manual" else c.game_days
                ),
                "ui_play_ms": c.ui_play_ms,
                "ui_fast_play_ms": c.ui_fast_play_ms,
            },
            "tiles": [tile_view(t, self) for t in s.tiles],
            "wells": [well_view(w, self) for w in s.wells],
            "reservoirs_revealed": reservoirs_voxel_summary(self.subsurface, top_k=10),
            "reservoirs_summary": reservoirs_summary(self.subsurface, s.wells),
            "active_events": list(s.active_events),
            "historical_events": list(s.historical_events),
            "regulatory_tightenings_applied": s.regulatory_tightenings_applied,
            "weather_now": s.weather_now.model_dump(),
            "power_now": s.power_now.model_dump(),
            "last_day_supply_kw_by_hour": list(s.last_day_trace.supply_kw_by_hour),
            "last_day_demand_kw_by_hour": list(s.last_day_trace.demand_kw_by_hour),
            "last_day_balance_state_by_hour": [
                str(b) for b in s.last_day_trace.balance_state_by_hour
            ],
            "next_24h_preview": preview_next_day(self),
            "today": s.today.model_dump(),
            "cumulative_renewable_served_kwh": s.cumulative_renewable_served_kwh,
            "cumulative_total_served_kwh": s.cumulative_total_served_kwh,
            "pipeline_networks": pipeline_networks,
            "orphan_well_ids": orphan_well_ids,
            "orphan_refinery_ids": orphan_refinery_ids,
        }

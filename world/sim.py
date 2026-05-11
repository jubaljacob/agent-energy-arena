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

from world.catalog import TILE_CATALOG, is_buildable
from world.config import Config, load_config
from world.economy import (
    CARBON_PRICE_USD_PER_TON,
    REFINED_PRICE_USD_PER_BBL,
    REFINERY_SETPOINT_MAX,
    REFINERY_SETPOINT_MIN,
    REFINERY_YIELD,
    daily_emissions_t,
    refinery_process_kw,
    route_crude,
)
from world.events import (
    expire_finite_events,
    fuel_price_shock_multiplier,
    sample_and_apply_events,
)
from world.grid import has_road_adjacency, in_bounds, road_connected_set
from world.population import update_population
from world.power import (
    PLANT_TYPES,
    compute_balance_state,
    dispatch,
    total_demand_kw,
)
from world.state import Tile, Well, WorldState
from world.subsurface import (
    CRUDE_PRICE_USD_PER_BBL,
    INJECTION_KWH_PER_BBL,
    Q_MAX_WELL_BBL_DAY,
    WELL_SETPOINT_MAX,
    WELL_SETPOINT_MIN,
    SubsurfaceGrid,
    generate_subsurface,
    is_size_valid,
    pools_intersect,
    reservoirs_summary,
    revealed_voxels,
    survey_cost,
    well_production_bbl_day,
)
from world.subsurface import survey as run_survey
from world.weather import (
    INITIAL_CLOUD_FACTOR,
    INITIAL_WIND_DIRECTION_DEG,
    derive_phi_seed,
    step_weather_one_hour,
    v_mean,
)


def _tile_to_dict(t: Tile) -> dict[str, Any]:
    return {
        "id": t.id,
        "type": t.type,
        "x": t.x,
        "y": t.y,
        "built_day": t.built_day,
        "operational": t.operational,
        "capex_paid": t.capex_paid,
        "opex_per_day": t.opex_per_day,
        "housing_capacity": t.housing_capacity,
        "jobs": t.jobs,
        "current_output_kw": t.current_output_kw,
        "setpoint_rate_bbl_day": t.setpoint_rate_bbl_day,
        "current_throughput_bbl_day": t.current_throughput_bbl_day,
    }


def _well_to_dict(w: Well) -> dict[str, Any]:
    return {
        "id": w.id,
        "type": w.type,
        "x": w.x,
        "y": w.y,
        "target_z": w.target_z,
        "drilled_day": w.drilled_day,
        "setpoint_rate_bbl_day": w.setpoint_rate_bbl_day,
        "current_rate_bbl_day": w.current_rate_bbl_day,
        "cumulative_produced_bbl": w.cumulative_produced_bbl,
        "cumulative_injected_bbl": w.cumulative_injected_bbl,
        "capex_paid": w.capex_paid,
        "opex_per_day": w.opex_per_day,
    }


@dataclass
class StepSummary:
    ok: bool
    day_completed: int
    summary: dict[str, Any]
    treasury_after: float


class World:
    def __init__(self, config: Config | None = None, *, session: str = "agent") -> None:
        self.config: Config = config or load_config()
        self.session: str = session
        self.state: WorldState = WorldState(seed=self.config.world_seed)
        self.sim_rng: np.random.Generator
        self.forecast_rng: np.random.Generator
        self.event_rng: np.random.Generator
        self.wind_phi_seed: float = 0.0
        self._tile_seq: int = 0
        self._well_seq: int = 0
        # Previous hour's per-plant outputs, persisted across hours and days.
        # Keyed by plant id. Used by `dispatch` to enforce ramp limits.
        self._prev_plant_outputs: dict[str, float] = {}
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

    def reset(self, seed: int | None = None) -> None:
        seed_used = self.config.world_seed if seed is None else int(seed)
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
            population=int(self.config.starting_pop),
            happiness=1.0,
            carbon_price=CARBON_PRICE_USD_PER_TON,
        )
        # Seed the AR(1) carry-overs at their long-run means so the first
        # hour's update is well-conditioned (no transient from a 0 init).
        self.state.weather_now["cloud_factor"] = INITIAL_CLOUD_FACTOR
        self.state.weather_now["wind_speed_mps"] = v_mean(0, self.wind_phi_seed)
        self.state.weather_now["wind_direction_deg"] = INITIAL_WIND_DIRECTION_DEG
        self.state.weather_now["solar_irradiance"] = 0.0
        self._tile_seq = 0
        self._well_seq = 0
        self._prev_plant_outputs = {}
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
        )
        self.state.tiles.append(tile)
        return {
            "ok": True,
            "treasury_after": self.state.treasury,
            "result": _tile_to_dict(tile),
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

    def _build_error(self, code: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": code,
            "treasury_after": self.state.treasury,
            "result": None,
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
        # Brief §4.12: two wells cannot share the same (x, y) — even if
        # they target different z. The `tile_occupied` error name matches
        # the build-side rejection for consistency with the issue AC.
        if self._well_at(x, y) is not None:
            return self._build_error("tile_occupied")

        spec_type = "oil_well" if well_type == "production" else "injection_well"
        spec = TILE_CATALOG[spec_type]
        if self.state.treasury < spec.capex:
            return self._build_error("insufficient_funds")

        self.state.treasury -= spec.capex
        well = Well(
            id=self._next_well_id(well_type),
            type=well_type,
            x=x,
            y=y,
            target_z=target_z,
            drilled_day=self.state.day,
            capex_paid=spec.capex,
            opex_per_day=spec.opex_per_day,
        )
        self.state.wells.append(well)
        return {
            "ok": True,
            "treasury_after": self.state.treasury,
            "result": _well_to_dict(well),
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

    def _well_at(self, x: int, y: int) -> Well | None:
        for w in self.state.wells:
            if w.x == x and w.y == y:
                return w
        return None

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
        pop_start = self.state.population

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
                "population_end": self.state.population,
                "happiness": self.state.happiness,
                "events_active": list(self.state.active_events),
            },
            treasury_after=self.state.treasury,
        )

    def _advance_one_day(self) -> None:
        # Reset today's running summary at the start of each simulated day so
        # callers can read per-day P&L from `state.today_summary_so_far`.
        for k in self.state.today_summary_so_far:
            self.state.today_summary_so_far[k] = 0.0

        # Events (slice 11): expire yesterday's finished events first (so a
        # fresh roll today doesn't get stomped by an immediate expiry), then
        # roll new events from event_rng. Effects apply via:
        #  - heatwave / demand_surprise: read by world.power.total_demand_kw
        #  - fuel_price_shock: read at the end-of-day fuel-cost step below
        #  - plant_failure: sets tile.operational=False; dispatch already
        #    filters non-operational plants
        #  - regulatory_tightening: bumps state.carbon_price immediately.
        expire_finite_events(self)
        sample_and_apply_events(self)

        # Per-hour traces for the most-recently-completed day. Reset here and
        # pinned to last_day_* fields once the day finishes.
        supply_trace: list[float] = []
        demand_trace: list[float] = []
        balance_trace: list[str] = []

        # Running served-kWh by source for the day (renewable share + per-source
        # totals available downstream).
        coal_kwh = 0.0
        gas_kwh = 0.0

        # Per-injection-well daily accumulators. The DR mechanic splits each
        # day into 24 hours of variable bbl/hr; we sum them to the well's
        # current_rate_bbl_day and bump cumulative_injected_bbl below.
        inj_bbl_today: dict[str, float] = {
            w.id: 0.0 for w in self.state.wells if w.type == "injection"
        }
        inj_kwh_today = 0.0  # sum of hourly kW values; kWh delivered to injection.

        for hour in range(self.config.ticks_per_day):
            self.state.hour = hour
            # Each hour: 3 sim_rng draws (cloud, wind speed, wind dir) then
            # the deterministic demand + dispatch calculation. RNG draws are
            # confined to step_weather_one_hour to anchor the slice-01
            # step-size determinism contract.
            step_weather_one_hour(self)
            civilian_demand_kw = total_demand_kw(self.state, hour)

            # DR-on-injection (PRD §"Demand-response on injection wells").
            # Each injection well's power for THIS hour is set by the PREVIOUS
            # hour's balance state, breaking the otherwise-circular dependency
            # between injection load and dispatch. Fresh-world hour 0 reads
            # state.power_now["balance_state"], which defaults to "balanced".
            prev_balance = self.state.power_now.get("balance_state", "balanced")
            inj_total_kw = 0.0
            inj_hour_assignments: dict[str, tuple[float, float]] = {}
            for iw in self.state.wells:
                if iw.type != "injection":
                    continue
                baseline_kw = iw.setpoint_rate_bbl_day * INJECTION_KWH_PER_BBL / 24.0
                cap_kw = Q_MAX_WELL_BBL_DAY * INJECTION_KWH_PER_BBL / 24.0
                if prev_balance in ("brownout", "blackout"):
                    power_kw = 0.0
                elif prev_balance == "curtailment":
                    power_kw = min(2.0 * baseline_kw, cap_kw)
                else:  # balanced (or any unexpected value falls through to baseline)
                    power_kw = baseline_kw
                bbl_this_hour = power_kw / INJECTION_KWH_PER_BBL
                inj_hour_assignments[iw.id] = (power_kw, bbl_this_hour)
                inj_total_kw += power_kw
            inj_kwh_today += inj_total_kw

            # Refinery process load (slice 09): hourly kW = yesterday's actual
            # throughput × KWH_PER_BBL / 24. The 1-day lag mirrors DR injection
            # — actual throughput for day D is only known AFTER the production
            # loop runs at end of day, so the hourly demand must read the
            # previous day's pinned value. Day 0 sees 0 since no crude has
            # been produced yet.
            refinery_process_load_kw = sum(
                refinery_process_kw(t.current_throughput_bbl_day)
                for t in self.state.tiles
                if t.type == "refinery" and t.operational
            )

            demand_kw = civilian_demand_kw + inj_total_kw + refinery_process_load_kw

            plants = [t for t in self.state.tiles if t.type in PLANT_TYPES]
            outputs, supply_kw, by_source = dispatch(
                plants,
                demand_kw,
                self._prev_plant_outputs,
                self.state.weather_now,
                self.state.day,
                hour,
            )

            balance, served_kw, excess_kw, _R = compute_balance_state(supply_kw, demand_kw)

            # Outage bookkeeping. Happiness damage is applied in one shot
            # at end of day by `update_population` reading
            # yesterday_blackout_hours / yesterday_brownout_hours; per-hour
            # decrements here would be clobbered by that reassignment
            # anyway (issue 22).
            if balance == "blackout":
                self.state.treasury -= self.config.blackout_penalty_hour
                self.state.today_summary_so_far["blackout_hours"] += 1.0
                self.state.today_summary_so_far["blackout_penalty"] += (
                    self.config.blackout_penalty_hour
                )
            elif balance == "brownout":
                self.state.today_summary_so_far["brownout_hours"] += 1.0

            # Power revenue: civilian served kWh × retail. Process loads
            # (injection wells, refinery) are unbilled per PRD §"Power
            # economics". Curtailment exports the post-injection surplus only.
            billable_served_kw = min(supply_kw, civilian_demand_kw)
            self.state.today_summary_so_far["power_revenue"] += (
                billable_served_kw * self.config.grid_price_retail
            )
            if balance == "curtailment" and excess_kw > 0:
                self.state.today_summary_so_far["power_revenue"] += (
                    excess_kw * self.config.grid_price_export
                )

            # Renewable-share accumulator (PRD §"Scoring"). served_kw is the
            # kWh actually delivered to loads this hour (curtailed export
            # excluded by construction — compute_balance_state caps served at
            # demand). renewable_served caps the renewable supply at served
            # so any renewable kWh that fell into curtailment is dropped from
            # both numerator and denominator.
            renewable_supply_kw = by_source.get("solar", 0.0) + by_source.get("wind", 0.0)
            renewable_served_kw = min(renewable_supply_kw, served_kw)
            self.state.cumulative_total_served_kwh += served_kw
            self.state.cumulative_renewable_served_kwh += renewable_served_kw

            # DR injection commits: only count bbl actually delivered. If
            # supply collapsed and the grid went brownout/blackout this hour,
            # injection wells STILL contributed their pre-set baseline to
            # demand (they shed *next* hour, when prev_balance reflects the
            # bad state). The bbl delivered this hour reflects the power
            # they *attempted* to draw — DR is a 1-hour-lagged mechanism.
            for iw_id, (_power_kw, bbl_this_hour) in inj_hour_assignments.items():
                inj_bbl_today[iw_id] += bbl_this_hour

            coal_kwh += by_source["coal"]
            gas_kwh += by_source["gas"]

            # Persist per-plant outputs for ramp-limit accounting next hour.
            for p in plants:
                p.current_output_kw = outputs.get(p.id, 0.0)
            self._prev_plant_outputs = dict(outputs)

            # Snapshot power_now for /state consumers + traces for the UI chart.
            self.state.power_now["demand_kw"] = demand_kw
            self.state.power_now["supply_kw"] = supply_kw
            self.state.power_now["balance_state"] = balance
            self.state.power_now["by_source_kw"] = dict(by_source)
            supply_trace.append(supply_kw)
            demand_trace.append(demand_kw)
            balance_trace.append(balance)

        # End-of-day OPEX accrual: every standing tile and drilled well
        # pays its daily OPEX.
        opex_total = sum(t.opex_per_day for t in self.state.tiles) + sum(
            w.opex_per_day for w in self.state.wells
        )
        if opex_total:
            self.state.treasury -= opex_total
            self.state.today_summary_so_far["opex"] = opex_total

        # Fuel cost (kWh / 1000 = MWh) × $/MWh. Coal+gas only. A
        # fuel_price_shock event (slice 11) doubles both costs while active.
        if coal_kwh or gas_kwh:
            coal_cost_per_mwh = TILE_CATALOG["coal_plant"].fuel_cost_per_mwh
            gas_cost_per_mwh = TILE_CATALOG["gas_peaker"].fuel_cost_per_mwh
            shock = fuel_price_shock_multiplier(self.state)
            fuel_total = shock * (
                (coal_kwh / 1000.0) * coal_cost_per_mwh + (gas_kwh / 1000.0) * gas_cost_per_mwh
            )
            self.state.treasury -= fuel_total
            self.state.today_summary_so_far["fuel_cost"] = fuel_total

        # Apply power revenue to treasury (the running tally was just adding
        # to today_summary_so_far; treasury credit happens once at day end).
        self.state.treasury += self.state.today_summary_so_far["power_revenue"]

        # Pin per-well injection totals from the hourly DR pass before the
        # production loop runs — production reads `cumulative_injected_bbl`
        # for its pressure_boost term, so injection bookkeeping has to land
        # first.
        for w in self.state.wells:
            if w.type != "injection":
                continue
            bbl = inj_bbl_today.get(w.id, 0.0)
            w.current_rate_bbl_day = bbl
            w.cumulative_injected_bbl += bbl
        if inj_kwh_today:
            self.state.today_summary_so_far["injection_kw"] = inj_kwh_today

        # Production-well daily output (brief §4.5). Iterates wells in
        # creation order — `state.wells` is appended-to on /drill — which
        # is the deterministic ordering required for shared-pool resolution.
        # `pressure_boost = min(0.5, inj_total / V_init)` from injection
        # wells whose 3×3×3 pools intersect this production well's pool.
        total_crude_bbl = 0.0
        for well in self.state.wells:
            if well.type != "production":
                continue
            inj_total = sum(
                iw.cumulative_injected_bbl
                for iw in self.state.wells
                if iw.type == "injection"
                and pools_intersect(well.x, well.y, well.target_z, iw.x, iw.y, iw.target_z)
            )
            q = well_production_bbl_day(
                self.subsurface,
                well.x,
                well.y,
                well.target_z,
                well.setpoint_rate_bbl_day,
                inj_total_bbl=inj_total,
            )
            well.current_rate_bbl_day = q
            well.cumulative_produced_bbl += q
            total_crude_bbl += q

        # Refinery routing (brief §4.6). Crude flows to operational refineries
        # by descending setpoint, id-ascending tiebreak; surplus sells raw.
        # Process loads are unbilled (no retail revenue) — they only show up
        # in dispatch demand and on plant fuel-burn / carbon ledgers.
        refineries = [t for t in self.state.tiles if t.type == "refinery" and t.operational]
        per_refinery_actual = route_crude(refineries, total_crude_bbl)
        total_refined_input = sum(per_refinery_actual.values())
        for r in refineries:
            r.current_throughput_bbl_day = per_refinery_actual.get(r.id, 0.0)
        # Inoperable refineries reset to 0 so their next-day power load is 0.
        for t in self.state.tiles:
            if t.type == "refinery" and not t.operational:
                t.current_throughput_bbl_day = 0.0

        crude_direct_bbl = max(0.0, total_crude_bbl - total_refined_input)
        crude_revenue = crude_direct_bbl * CRUDE_PRICE_USD_PER_BBL
        # Yield is applied here (not in route_crude) so the routing remains
        # purely about input allocation; one place owns the 0.85 constant.
        refined_revenue = total_refined_input * REFINERY_YIELD * REFINED_PRICE_USD_PER_BBL
        oil_revenue = crude_revenue + refined_revenue
        if oil_revenue:
            self.state.today_summary_so_far["oil_revenue"] = oil_revenue
            self.state.today_summary_so_far["crude_revenue"] = crude_revenue
            self.state.today_summary_so_far["refined_revenue"] = refined_revenue
            self.state.treasury += oil_revenue

        # Carbon emissions + cost (PRD §4.7 / slice 10). Pin the day's running
        # totals (consumed by daily_emissions_t) onto today_summary_so_far so
        # the read-models surface them and the helper has a single source of
        # truth. The order matters: refining is done above, so refined_bbl is
        # final; the hourly loop accumulated coal_kwh and gas_kwh.
        self.state.today_summary_so_far["coal_kwh"] = coal_kwh
        self.state.today_summary_so_far["gas_kwh"] = gas_kwh
        self.state.today_summary_so_far["refined_bbl"] = total_refined_input
        co2_t = daily_emissions_t(self)
        carbon_cost = co2_t * self.state.carbon_price
        self.state.today_summary_so_far["co2_emitted_t"] = co2_t
        self.state.today_summary_so_far["carbon_cost"] = carbon_cost
        if carbon_cost:
            self.state.treasury -= carbon_cost

        # Carry today's outage-hour counts into tomorrow's population update.
        self.state.yesterday_blackout_hours = self.state.today_summary_so_far["blackout_hours"]
        self.state.yesterday_brownout_hours = self.state.today_summary_so_far["brownout_hours"]

        # Pin per-hour traces for the UI's "yesterday" chart.
        self.state.last_day_supply_kw_by_hour = supply_trace
        self.state.last_day_demand_kw_by_hour = demand_trace
        self.state.last_day_balance_state_by_hour = balance_trace

        # Population dynamics + tax revenue (brief §4.8). Deterministic; no
        # RNG draws, so the sim_rng contract is unaffected.
        update_population(self)

        self.state.day += 1
        self.state.hour = 0

    # -- Forecast (uses forecast_rng; never perturbs sim state) ------------

    def forecast(self, hours: int = 24) -> list[dict[str, Any]]:
        from world.forecast import forecast_records

        return forecast_records(self, int(hours))

    # -- Read-models -------------------------------------------------------

    def state_dict(self) -> dict[str, Any]:
        s = self.state
        c = self.config
        return {
            "seed": s.seed,
            "day": s.day,
            "hour": s.hour,
            "treasury": s.treasury,
            "population": s.population,
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
            },
            "tiles": [_tile_to_dict(t) for t in s.tiles],
            "wells": [_well_to_dict(w) for w in s.wells],
            "reservoirs_revealed": reservoirs_summary(self.subsurface, top_k=10),
            "active_events": list(s.active_events),
            "historical_events": list(s.historical_events),
            "regulatory_tightenings_applied": s.regulatory_tightenings_applied,
            "weather_now": s.weather_now,
            "power_now": s.power_now,
            "last_day_supply_kw_by_hour": list(s.last_day_supply_kw_by_hour),
            "last_day_demand_kw_by_hour": list(s.last_day_demand_kw_by_hour),
            "last_day_balance_state_by_hour": list(s.last_day_balance_state_by_hour),
            "today_summary_so_far": s.today_summary_so_far,
            "cumulative_renewable_served_kwh": s.cumulative_renewable_served_kwh,
            "cumulative_total_served_kwh": s.cumulative_total_served_kwh,
        }

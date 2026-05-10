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
from world.grid import has_road_adjacency, in_bounds
from world.population import update_population
from world.power import (
    BLACKOUT_HAPPINESS_PENALTY,
    BROWNOUT_HAPPINESS_COEF,
    PLANT_TYPES,
    compute_balance_state,
    dispatch,
    total_demand_kw,
)
from world.state import Tile, WorldState
from world.subsurface import (
    SubsurfaceGrid,
    generate_subsurface,
    is_size_valid,
    reservoirs_summary,
    revealed_voxels,
    survey_cost,
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
        self.wind_phi_seed: float = 0.0
        self._tile_seq: int = 0
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
        sim_seed, forecast_seed = master.spawn(2)
        self.sim_rng = np.random.default_rng(sim_seed)
        self.forecast_rng = np.random.default_rng(forecast_seed)

        self.wind_phi_seed = derive_phi_seed(seed_used)
        self.state = WorldState(
            seed=seed_used,
            day=0,
            hour=0,
            treasury=float(self.config.starting_cash),
            population=int(self.config.starting_pop),
            happiness=1.0,
        )
        # Seed the AR(1) carry-overs at their long-run means so the first
        # hour's update is well-conditioned (no transient from a 0 init).
        self.state.weather_now["cloud_factor"] = INITIAL_CLOUD_FACTOR
        self.state.weather_now["wind_speed_mps"] = v_mean(0, self.wind_phi_seed)
        self.state.weather_now["wind_direction_deg"] = INITIAL_WIND_DIRECTION_DEG
        self.state.weather_now["solar_irradiance"] = 0.0
        self._tile_seq = 0
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
                "events_active": [],
            },
            treasury_after=self.state.treasury,
        )

    def _advance_one_day(self) -> None:
        # Reset today's running summary at the start of each simulated day so
        # callers can read per-day P&L from `state.today_summary_so_far`.
        for k in self.state.today_summary_so_far:
            self.state.today_summary_so_far[k] = 0.0

        # Per-hour traces for the most-recently-completed day. Reset here and
        # pinned to last_day_* fields once the day finishes.
        supply_trace: list[float] = []
        demand_trace: list[float] = []
        balance_trace: list[str] = []

        # Running served-kWh by source for the day (renewable share + per-source
        # totals available downstream).
        coal_kwh = 0.0
        gas_kwh = 0.0

        for hour in range(self.config.ticks_per_day):
            self.state.hour = hour
            # Each hour: 3 sim_rng draws (cloud, wind speed, wind dir) then
            # the deterministic demand + dispatch calculation. RNG draws are
            # confined to step_weather_one_hour to anchor the slice-01
            # step-size determinism contract.
            step_weather_one_hour(self)
            demand_kw = total_demand_kw(self.state, hour)

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

            # Apply hourly happiness penalties (brief §4.4).
            if balance == "blackout":
                self.state.happiness = max(
                    0.0, min(1.5, self.state.happiness - BLACKOUT_HAPPINESS_PENALTY)
                )
                self.state.treasury -= self.config.blackout_penalty_hour
                self.state.today_summary_so_far["blackout_hours"] += 1.0
                self.state.today_summary_so_far["blackout_penalty"] += (
                    self.config.blackout_penalty_hour
                )
            elif balance == "brownout":
                penalty = BROWNOUT_HAPPINESS_COEF * (1.0 - _R)
                self.state.happiness = max(0.0, min(1.5, self.state.happiness - penalty))

            # Power revenue: served kWh × retail + excess kWh × export.
            self.state.today_summary_so_far["power_revenue"] += (
                served_kw * self.config.grid_price_retail
            )
            if balance == "curtailment" and excess_kw > 0:
                self.state.today_summary_so_far["power_revenue"] += (
                    excess_kw * self.config.grid_price_export
                )

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

        # End-of-day OPEX accrual: every standing tile pays its daily OPEX.
        opex_total = sum(t.opex_per_day for t in self.state.tiles)
        if opex_total:
            self.state.treasury -= opex_total
            self.state.today_summary_so_far["opex"] = opex_total

        # Fuel cost (kWh / 1000 = MWh) × $/MWh. Coal+gas only.
        if coal_kwh or gas_kwh:
            coal_cost_per_mwh = TILE_CATALOG["coal_plant"].fuel_cost_per_mwh
            gas_cost_per_mwh = TILE_CATALOG["gas_peaker"].fuel_cost_per_mwh
            fuel_total = (coal_kwh / 1000.0) * coal_cost_per_mwh + (
                gas_kwh / 1000.0
            ) * gas_cost_per_mwh
            self.state.treasury -= fuel_total
            self.state.today_summary_so_far["fuel_cost"] = fuel_total

        # Apply power revenue to treasury (the running tally was just adding
        # to today_summary_so_far; treasury credit happens once at day end).
        self.state.treasury += self.state.today_summary_so_far["power_revenue"]

        # Carry today's blackout-hour count into tomorrow's population update.
        self.state.yesterday_blackout_hours = self.state.today_summary_so_far["blackout_hours"]

        # Pin per-hour traces for the UI's "yesterday" chart.
        self.state.last_day_supply_kw_by_hour = supply_trace
        self.state.last_day_demand_kw_by_hour = demand_trace
        self.state.last_day_balance_state_by_hour = balance_trace

        # Population dynamics + tax revenue (brief §4.8). Deterministic; no
        # RNG draws, so the sim_rng contract is unaffected.
        update_population(self)

        self.state.day += 1
        self.state.hour = 0

    # -- Forecast (placeholder; uses forecast_rng) -------------------------

    def forecast(self, hours: int = 24) -> dict[str, Any]:
        # Skeleton: emit zero-mean noise from forecast_rng so we can prove
        # this stream is independent from sim_rng.
        noise = self.forecast_rng.standard_normal(int(hours)).tolist()
        return {
            "hours": int(hours),
            "solar_irradiance": [0.0] * int(hours),
            "wind_speed_mps": [0.0] * int(hours),
            "demand_kw": [0.0] * int(hours),
            "noise": noise,
        }

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
            "wells": [],
            "reservoirs_revealed": reservoirs_summary(self.subsurface, top_k=10),
            "active_events": [],
            "weather_now": s.weather_now,
            "power_now": s.power_now,
            "last_day_supply_kw_by_hour": list(s.last_day_supply_kw_by_hour),
            "last_day_demand_kw_by_hour": list(s.last_day_demand_kw_by_hour),
            "last_day_balance_state_by_hour": list(s.last_day_balance_state_by_hour),
            "today_summary_so_far": s.today_summary_so_far,
        }

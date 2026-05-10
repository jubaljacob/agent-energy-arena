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
from world.power import total_demand_kw
from world.state import Tile, WorldState
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

        for hour in range(self.config.ticks_per_day):
            self.state.hour = hour
            # Each hour: 3 sim_rng draws (cloud, wind speed, wind dir) then
            # the deterministic demand calculation. These per-hour draws are
            # what now anchors the slice-01 step-size determinism contract.
            step_weather_one_hour(self)
            self.state.power_now["demand_kw"] = total_demand_kw(self.state, hour)

        # End-of-day OPEX accrual: every standing tile pays its daily OPEX.
        opex_total = sum(t.opex_per_day for t in self.state.tiles)
        if opex_total:
            self.state.treasury -= opex_total
            self.state.today_summary_so_far["opex"] = opex_total

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
            "reservoirs_revealed": [],
            "active_events": [],
            "weather_now": s.weather_now,
            "power_now": s.power_now,
            "today_summary_so_far": s.today_summary_so_far,
        }

"""Deterministic 24-hour projection of the next day's supply vs demand.

`preview_next_day(world)` is a pure read-model over the current world
state: it never mutates state and never consumes any RNG, so the UI
can poll it on every `/state` tick. The projection runs the same
`world.hourly_tick.hourly_tick` that `World.step` runs each hour, except
weather is held at the natural deterministic baseline (current
`cloud_factor`, seasonal wind `v_mean`) — the same truth-without-noise
that `world.forecast._project_truth` uses. The balance-state chain feeds
back into DR-on-injection (`prev_balance`) and into ramp limits
(`prev_plant_outputs`) so the 24-element trace matches what a quiet
`/step` would produce given the same tile/well/refinery setpoints.

Sharing `hourly_tick` with `World._advance_one_day` is load-bearing:
any change to the hour's dispatch math (well power coupling, peaker
filtering, battery netting, solar derate, ...) lands in both sim and
preview at once. Before the seam existed, this module duplicated the
hourly loop and drifted whenever a new dispatch input was added.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from world.hourly_tick import hourly_tick
from world.weather import v_mean

if TYPE_CHECKING:
    from world.sim import World


def preview_next_day(world: World) -> dict[str, Any]:
    """Project the next 24h of supply/demand/by-source/balance state.

    Returns arrays of length `ticks_per_day` plus a small summary
    (`peak_demand_kw`, `peak_supply_kw`, `min_reserve_margin`). The
    projection is deterministic given the current state and config.
    """
    state = world.state
    cfg = world.config

    cloud = float(state.weather_now.get("cloud_factor", 0.85))
    weather_proj: dict[str, float] = {
        "cloud_factor": cloud,
        "wind_speed_mps": float(v_mean(state.day, world.wind_phi_seed)),
    }

    # Carry the last completed hour's plant outputs and balance into the
    # projection so DR-on-injection, ramp limits, and producer shedding
    # read the same way the next `/step` would. Plants built since the
    # last step are absent from `prev_outputs`, which is exactly how
    # `dispatch` treats them (warm-starts coal at must-run, cold-starts
    # gas at 0).
    prev_outputs: dict[str, float] = dict(world._prev_plant_outputs)
    prev_balance: str = state.power_now.get("balance_state", "balanced")

    demand_by_hour: list[float] = []
    supply_by_hour: list[float] = []
    balance_by_hour: list[str] = []
    source_by_hour: dict[str, list[float]] = {
        "solar": [],
        "wind": [],
        "coal": [],
        "gas": [],
    }

    for h in range(cfg.ticks_per_day):
        result = hourly_tick(state, h, prev_outputs, prev_balance, weather_proj)
        demand_by_hour.append(float(result.demand_kw))
        supply_by_hour.append(float(result.supply_kw))
        balance_by_hour.append(result.balance)
        for key in source_by_hour:
            source_by_hour[key].append(float(result.by_source[key]))
        prev_outputs = result.outputs
        prev_balance = result.balance

    peak_demand_kw = max(demand_by_hour) if demand_by_hour else 0.0
    peak_supply_kw = max(supply_by_hour) if supply_by_hour else 0.0
    if demand_by_hour:
        min_reserve_margin = min(
            (s - d) / max(d, 1.0) for s, d in zip(supply_by_hour, demand_by_hour, strict=True)
        )
    else:
        min_reserve_margin = 0.0

    return {
        "supply_kw_by_hour": supply_by_hour,
        "demand_kw_by_hour": demand_by_hour,
        "balance_state_by_hour": balance_by_hour,
        "by_source_kw_by_hour": source_by_hour,
        "peak_demand_kw": float(peak_demand_kw),
        "peak_supply_kw": float(peak_supply_kw),
        "min_reserve_margin": float(min_reserve_margin),
    }

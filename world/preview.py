"""Deterministic 24-hour projection of the next day's supply vs demand.

`preview_next_day(world)` is a pure read-model over the current world
state: it never mutates state and never consumes any RNG, so the UI
can poll it on every `/state` tick. The projection mirrors the hourly
loop in `world.sim.World.step` step-for-step, except weather is held at
the natural deterministic baseline (current `cloud_factor`, seasonal
wind `v_mean`) — the same truth-without-noise that
`world.forecast._project_truth` uses. The balance-state chain feeds
back into DR-on-injection (`prev_balance`) and into ramp limits
(`prev_plant_outputs`) so the 24-element trace matches what a quiet
`/step` would produce given the same tile/well/refinery setpoints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from world.economy import refinery_process_kw
from world.power import (
    PLANT_TYPES,
    compute_balance_state,
    dispatch,
    total_demand_kw,
)
from world.subsurface import INJECTION_KWH_PER_BBL, Q_MAX_WELL_BBL_DAY
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
    weather_proj = {
        "cloud_factor": cloud,
        "wind_speed_mps": float(v_mean(state.day, world.wind_phi_seed)),
    }

    plants = [t for t in state.tiles if t.type in PLANT_TYPES]
    # Carry the last completed hour's plant outputs into the projection so
    # ramp limits read the same way the next `/step` would. Plants built
    # since the last step are absent from this dict, which is exactly how
    # `dispatch` would treat them (its `prev_outputs.get(..., default)`
    # warm-starts coal at must-run and cold-starts gas at 0).
    prev_outputs: dict[str, float] = dict(world._prev_plant_outputs)
    prev_balance = state.power_now.get("balance_state", "balanced")

    inj_wells = [w for w in state.wells if w.type == "injection"]
    refineries = [t for t in state.tiles if t.type == "refinery" and t.operational]
    inj_cap_kw = Q_MAX_WELL_BBL_DAY * INJECTION_KWH_PER_BBL / 24.0

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
        civilian_demand = total_demand_kw(state, h)

        inj_kw = 0.0
        for iw in inj_wells:
            baseline_kw = iw.setpoint_rate_bbl_day * INJECTION_KWH_PER_BBL / 24.0
            if prev_balance in ("brownout", "blackout"):
                power_kw = 0.0
            elif prev_balance == "curtailment":
                power_kw = min(2.0 * baseline_kw, inj_cap_kw)
            else:
                power_kw = baseline_kw
            inj_kw += power_kw

        refinery_kw = sum(refinery_process_kw(t.current_throughput_bbl_day) for t in refineries)

        demand_kw = civilian_demand + inj_kw + refinery_kw

        outputs, supply_kw, by_source = dispatch(
            plants,
            demand_kw,
            prev_outputs,
            weather_proj,
            state.day,
            h,
        )
        balance, _served, _excess, _R = compute_balance_state(supply_kw, demand_kw)

        demand_by_hour.append(float(demand_kw))
        supply_by_hour.append(float(supply_kw))
        balance_by_hour.append(balance)
        for key in source_by_hour:
            source_by_hour[key].append(float(by_source[key]))

        prev_outputs = outputs
        prev_balance = balance

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

"""Hourly demand + dispatch + balance-state model.

`total_demand_kw(state, h)` is the per-hour total electric load (brief §4.3
with the PRD's split-scope event multipliers).

`dispatch(plants, demand_kw, prev_outputs, weather, D, h)` runs the merit
order from brief §4.4: must-take renewables → coal must-run → coal ramp by
fuel cost → gas peakers ramp by fuel cost. Returns per-plant outputs,
total supply, and an aggregate by source.

`compute_balance_state(supply, demand)` returns one of "curtailment",
"balanced", "brownout", "blackout" along with served/excess kWh — the
thresholds match brief §4.4 with `R = supply / max(demand, 1)`.

Event multipliers (PRD's correction to the brief's bottom-line multipliers):

  * Heatwave (1.40) multiplies *residential demand only* — A/C drives it.
  * Demand surprise (1.30) multiplies *commercial + industrial only*.
  * Process loads are unaffected by either multiplier.

In slice 04 events are stubbed; both `heatwave_active` and
`demand_surprise_active` return False unless a test injects a matching
entry into `state.active_events`. The flags go live in slice 11.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from world.catalog import TILE_CATALOG
from world.weather import P_solar_kw, turbine_kw

if TYPE_CHECKING:
    from world.state import Tile, WorldState

PER_CAPITA_KW: float = 0.333  # 8 kWh/day continuous; brief §4.3
HEATWAVE_RESIDENTIAL_MULT: float = 1.40
DEMAND_SURPRISE_IC_MULT: float = 1.30

# Dispatch ramp/min-run (brief §4.4).
COAL_RAMP_PER_HOUR: float = 0.10
GAS_RAMP_PER_HOUR: float = 0.50
COAL_MIN_RUN: float = 0.25

# Balance-state thresholds (brief §4.4).
R_CURTAILMENT: float = 1.15
R_BALANCED: float = 0.95
R_BROWNOUT: float = 0.70

# Per-hour happiness penalties from outages live in
# `world.population` as BLACKOUT_HAPPINESS_PER_HOUR /
# BROWNOUT_HAPPINESS_PER_HOUR. The hourly-decrement-on-state.happiness
# pattern was removed in issue 22 — `update_population` reassigns
# happiness end-of-day, so per-hour writes were silently clobbered.

# Plant types that participate in dispatch.
RENEWABLE_TYPES: frozenset[str] = frozenset({"solar_farm", "wind_turbine"})
FOSSIL_TYPES: frozenset[str] = frozenset({"coal_plant", "gas_peaker"})
PLANT_TYPES: frozenset[str] = RENEWABLE_TYPES | FOSSIL_TYPES


def hourly_factor(h: int) -> float:
    if h < 5:
        return 0.6  # night
    if h < 9:
        return 1.0  # morning
    if h < 17:
        return 0.8  # midday
    if h < 22:
        return 1.5  # evening peak
    return 0.7  # late night


def residential_kw(h: int, pop: int) -> float:
    return pop * PER_CAPITA_KW * hourly_factor(h)


def commercial_factor(h: int) -> float:
    return 1.0 if 8 <= h < 20 else 0.2


def heatwave_active(state: WorldState) -> bool:
    return any(e.get("type") == "heatwave" for e in state.active_events)


def demand_surprise_active(state: WorldState) -> bool:
    return any(e.get("type") == "demand_surprise" for e in state.active_events)


def heatwave_multiplier(state: WorldState) -> float:
    return HEATWAVE_RESIDENTIAL_MULT if heatwave_active(state) else 1.0


def demand_surprise_multiplier(state: WorldState) -> float:
    return DEMAND_SURPRISE_IC_MULT if demand_surprise_active(state) else 1.0


def _industrial_kw(state: WorldState) -> float:
    return sum(TILE_CATALOG[t.type].demand_kw for t in state.tiles if t.type == "industrial")


def _commercial_peak_kw(state: WorldState) -> float:
    return sum(TILE_CATALOG[t.type].demand_kw for t in state.tiles if t.type == "commercial")


def _process_loads_kw(state: WorldState) -> float:
    # Process loads (injection wells, refineries) are added directly by the
    # sim loop alongside civilian demand — they need to be split out so power
    # revenue bills only the civilian portion. This stub stays at 0.0 so
    # `total_demand_kw` returns the civilian-only figure.
    return 0.0


def total_demand_kw(state: WorldState, h: int) -> float:
    res = residential_kw(h, state.population) * heatwave_multiplier(state)
    ic = (_industrial_kw(state) + _commercial_peak_kw(state) * commercial_factor(h)) * (
        demand_surprise_multiplier(state)
    )
    process = _process_loads_kw(state)
    return float(res + ic + process)


# -- Dispatch ----------------------------------------------------------------


def dispatch(
    plants: list[Tile],
    demand_kw: float,
    prev_outputs: dict[str, float],
    weather: dict[str, float],
    D: int,
    h: int,
) -> tuple[dict[str, float], float, dict[str, float]]:
    """Run the merit-order dispatch for one hour.

    Returns (outputs_by_plant_id, supply_kw, by_source_kw). by_source_kw
    aggregates outputs into the four canonical keys: "solar", "wind",
    "coal", "gas". Non-operational plants are zeroed; they neither
    consume ramp room nor count toward must-run.
    """
    outputs: dict[str, float] = {p.id: 0.0 for p in plants}

    cloud = float(weather.get("cloud_factor", 0.85))
    wind_v = float(weather.get("wind_speed_mps", 0.0))

    operational = [p for p in plants if p.operational]
    solar = [p for p in operational if p.type == "solar_farm"]
    wind = [p for p in operational if p.type == "wind_turbine"]
    coal = sorted(
        (p for p in operational if p.type == "coal_plant"),
        key=lambda x: (TILE_CATALOG[x.type].fuel_cost_per_mwh, x.id),
    )
    gas = sorted(
        (p for p in operational if p.type == "gas_peaker"),
        key=lambda x: (TILE_CATALOG[x.type].fuel_cost_per_mwh, x.id),
    )

    # Step 1: must-take renewables
    for p in solar:
        outputs[p.id] = P_solar_kw(D, h, cloud)
    for p in wind:
        outputs[p.id] = turbine_kw(wind_v)

    supply = sum(outputs.values())

    # Step 2: coal must-run minimum (25% of capacity).
    for p in coal:
        cap = TILE_CATALOG[p.type].capacity_kw
        outputs[p.id] = cap * COAL_MIN_RUN
        supply += outputs[p.id]

    remaining = max(0.0, demand_kw - supply)

    # Step 3: ramp coal upward by cost (already sorted). Bound by ramp_room
    # measured from the previous hour's output, capped at capacity.
    for p in coal:
        if remaining <= 0:
            break
        cap = TILE_CATALOG[p.type].capacity_kw
        ramp_room = cap * COAL_RAMP_PER_HOUR
        # Newly-built coal: assume it warm-starts at must-run, no prior hour.
        prev_out = prev_outputs.get(p.id, cap * COAL_MIN_RUN)
        upper = min(cap, prev_out + ramp_room)
        headroom = upper - outputs[p.id]
        if headroom <= 0:
            continue
        inc = min(headroom, remaining)
        outputs[p.id] += inc
        supply += inc
        remaining -= inc

    # Step 4: gas peakers ramp by cost.
    for p in gas:
        if remaining <= 0:
            outputs[p.id] = 0.0
            continue
        cap = TILE_CATALOG[p.type].capacity_kw
        ramp_room = cap * GAS_RAMP_PER_HOUR
        prev_out = prev_outputs.get(p.id, 0.0)
        max_out = min(cap, prev_out + ramp_room)
        delivered = min(max_out, remaining)
        outputs[p.id] = delivered
        supply += delivered
        remaining -= delivered

    by_source = {
        "solar": sum(outputs[p.id] for p in solar),
        "wind": sum(outputs[p.id] for p in wind),
        "coal": sum(outputs[p.id] for p in coal),
        "gas": sum(outputs[p.id] for p in gas),
    }
    return outputs, supply, by_source


# -- Balance state -----------------------------------------------------------


def compute_balance_state(supply_kw: float, demand_kw: float) -> tuple[str, float, float, float]:
    """Map (supply, demand) onto the four balance states.

    Returns (state, served_kw, excess_kw, R). When demand is zero the grid
    is treated as balanced with served=excess=0 (no loads to serve, no
    export market either).
    """
    if demand_kw <= 0:
        return "balanced", 0.0, 0.0, 0.0
    R = supply_kw / max(demand_kw, 1.0)
    if R >= R_CURTAILMENT:
        return "curtailment", demand_kw, max(0.0, supply_kw - demand_kw), R
    if R >= R_BALANCED:
        return "balanced", demand_kw, 0.0, R
    if R >= R_BROWNOUT:
        return "brownout", supply_kw, 0.0, R
    return "blackout", supply_kw, 0.0, R

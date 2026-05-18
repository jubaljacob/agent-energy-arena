"""External-facing dict shapes for ``World`` mutators and ``/state``.

`World.build()` / `World.drill()` / `World.state_dict()` (and indirectly the
UI, agents, tests) consume these projectors to turn a single ``Tile`` or
``Well`` into the dict the API surfaces. Pure functions, no mutation, no
I/O — given the same ``(tile|well, world)`` they always return the same
dict. Co-located here so the wire format is one grep target, not 120 lines
buried at the top of the simulation loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from world.catalog import TILE_CATALOG
from world.power import PLANT_TYPES
from world.pricing import (
    COMMERCIAL_RADIUS,
    commercial_revenue_for_tile,
    industrial_co2_for_tile,
    industrial_revenue_for_tile,
    occupancy_ratio,
    plant_carbon_cost_for_tile,
    plant_co2_for_tile,
    plant_fuel_cost_for_tile,
    plant_revenue_for_tile,
    refinery_carbon_cost_for_tile,
    refinery_co2_for_tile,
    refinery_revenue_for_tile,
    well_gross_crude_value_for_tile,
    well_injection_kwh_per_day,
)
from world.subsurface import injector_supports

if TYPE_CHECKING:
    from world.sim import World
    from world.state import Tile, Well


def tile_view(t: Tile, world: World) -> dict[str, Any]:
    """Wire-format dict for one ``Tile`` at its current operating state.

    Includes the per-tile economics rows (revenue, CO2, fuel/carbon cost,
    net) the hover popup surfaces, computed via the public ``pricing``
    helpers so the popup row and the city-wide aggregator share a source
    of truth. ``residents_in_radius`` on commercial tiles is the
    capacity-in-radius × city occupancy figure the popup needs and is
    computed locally — it exists only to feed this dict.
    """
    extra: dict[str, Any] = {}
    fuel_cost = 0.0
    if t.type == "industrial":
        revenue = industrial_revenue_for_tile(world.state, t)
        co2_t = industrial_co2_for_tile(t)
        carbon_cost = co2_t * world.state.carbon_price
        net = revenue - t.opex_per_day - carbon_cost
    elif t.type == "commercial":
        revenue = commercial_revenue_for_tile(world.state, t)
        co2_t = 0.0
        carbon_cost = 0.0
        net = revenue - t.opex_per_day
        extra["residents_in_radius"] = _residents_in_radius(world.state, t)
    elif t.type in PLANT_TYPES:
        spec = TILE_CATALOG[t.type]
        revenue = plant_revenue_for_tile(world.state, t)
        co2_t = plant_co2_for_tile(t, spec)
        fuel_cost = plant_fuel_cost_for_tile(world.state, t, spec)
        carbon_cost = plant_carbon_cost_for_tile(world.state, t, spec)
        net = revenue - t.opex_per_day - fuel_cost - carbon_cost
    elif t.type == "refinery":
        revenue = refinery_revenue_for_tile(world.state, t)
        co2_t = refinery_co2_for_tile(t)
        carbon_cost = refinery_carbon_cost_for_tile(world.state, t)
        net = revenue - t.opex_per_day - carbon_cost
    else:
        revenue = 0.0
        co2_t = 0.0
        carbon_cost = 0.0
        net = 0.0
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
        "demand_kw": t.demand_kw,
        "staffed_jobs": t.staffed_jobs,
        "current_output_kw": t.current_output_kw,
        "kwh_served_today": t.kwh_served_today,
        "kwh_served_yesterday": t.kwh_served_yesterday,
        "setpoint_rate_bbl_day": t.setpoint_rate_bbl_day,
        "current_throughput_bbl_day": t.current_throughput_bbl_day,
        "estimated_revenue_per_day": revenue,
        "estimated_co2_per_day": co2_t,
        "estimated_fuel_cost_per_day": fuel_cost,
        "estimated_carbon_cost_per_day": carbon_cost,
        "estimated_net_per_day": net,
        **extra,
        **(
            {"soc_kwh": t.soc_kwh, "charge_setpoint_kw": t.charge_setpoint_kw}
            if t.type == "battery"
            else {}
        ),
    }


def well_view(w: Well, world: World) -> dict[str, Any]:
    """Wire-format dict for one ``Well`` at its current operating state.

    ``supports_producer_ids`` mirrors the same-reservoir + Chebyshev > 1
    gate that the day loop's ``pressure_boost`` resolution uses, so the
    popup row and the simulator share one source of truth. Producer wells
    carry an empty list for type symmetry; the UI ignores the field on
    producer rows.
    """
    revenue = well_gross_crude_value_for_tile(world.state, w)
    injection_kwh = well_injection_kwh_per_day(w)
    # Injection wells: power cost is internalized through plants, so Net is
    # -opex with no $-cost from kWh consumption.
    net = revenue - w.opex_per_day if w.type == "production" else -w.opex_per_day
    supports: list[str] = injector_supports(w, world.state.wells) if w.type == "injection" else []
    return {
        "id": w.id,
        "type": w.type,
        "x": w.x,
        "y": w.y,
        "target_z": w.target_z,
        "reservoir_id": w.reservoir_id,
        "drilled_day": w.drilled_day,
        "setpoint_rate_bbl_day": w.setpoint_rate_bbl_day,
        "current_rate_bbl_day": w.current_rate_bbl_day,
        "yesterday_rate_bbl_day": w.yesterday_rate_bbl_day,
        "yesterday_inj_rate_bbl_day": w.yesterday_inj_rate_bbl_day,
        "pressure_boost": w.pressure_boost,
        "cumulative_produced_bbl": w.cumulative_produced_bbl,
        "cumulative_injected_bbl": w.cumulative_injected_bbl,
        "capex_paid": w.capex_paid,
        "opex_per_day": w.opex_per_day,
        "staffed_jobs": w.staffed_jobs,
        "supports_producer_ids": supports,
        "estimated_revenue_per_day": revenue,
        "injection_power_kwh_per_day": injection_kwh,
        "estimated_net_per_day": net,
    }


def _residents_in_radius(state: Any, tile: Tile) -> float:
    """Capacity-in-radius × city occupancy for the commercial popup row.

    Lives here (not in ``pricing``) because it serves only this dict —
    it's a UI-facing convenience derived from the same data
    ``commercial_revenue_for_tile`` uses, but without the rate and
    workforce-efficiency multipliers. Inlining keeps ``pricing``'s public
    surface focused on per-tile economics rather than popup helpers.
    """
    capacity_in_radius = 0
    for other in state.tiles:
        if other.housing_capacity <= 0:
            continue
        if max(abs(other.x - tile.x), abs(other.y - tile.y)) <= COMMERCIAL_RADIUS:
            capacity_in_radius += other.housing_capacity
    return capacity_in_radius * occupancy_ratio(state)

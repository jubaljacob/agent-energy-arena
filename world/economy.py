"""Refinery economics: refining yield + crude routing + daily carbon emissions.

Implements §4.6 of the brief: each refinery refines up to its setpoint
(capped at REFINERY_MAX_BBL_DAY and at the available crude). Refined
output = actual × REFINERY_YIELD. Surplus crude that no refinery
consumes sells raw at CRUDE_PRICE.

Crude is routed across refineries by descending setpoint (with id
ascending as the deterministic tiebreak), so the agent can prioritise a
high-throughput refinery over a low one without surprises. Process load
(actual × REFINERY_KWH_PER_BBL / 24) is unbilled to the agent — it
counts toward dispatch demand and toward fuel-burn / carbon emissions
on whichever plants serve it, but no retail revenue is paid.

Carbon (slice 10, PRD §4.7): `daily_emissions_t(world)` reads the day's
running coal/gas/refined totals from `today_summary_so_far` plus the
operational-industrial tile count. The PRD revises the brief by removing
the per-MWh-consumed industrial term — industrial tiles emit a flat
INDUSTRIAL_PROCESS_CO2_T_PER_DAY regardless of grid load, and the kWh
they consume is already counted via the coal/gas plants serving them.
Carbon cost = emissions × `state.carbon_price` (mutable, initialised to
CARBON_PRICE_USD_PER_TON on /reset; slice 11 events tighten it).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from world import workforce

if TYPE_CHECKING:
    from world.sim import World
    from world.state import Tile

REFINERY_MAX_BBL_DAY: float = 500.0
REFINERY_YIELD: float = 0.85
REFINERY_KWH_PER_BBL: float = 200.0
REFINERY_CO2_PER_BBL: float = 0.30
REFINERY_SETPOINT_MIN: float = 0.0
REFINERY_SETPOINT_MAX: float = REFINERY_MAX_BBL_DAY
REFINED_PRICE_USD_PER_BBL: float = 90.0

# Carbon (PRD §4.7).
COAL_CO2_T_PER_MWH: float = 0.90
GAS_CO2_T_PER_MWH: float = 0.40
INDUSTRIAL_PROCESS_CO2_T_PER_DAY: float = 2.0
CARBON_PRICE_USD_PER_TON: float = 25.0


def refine_one(
    setpoint_rate_bbl_day: float,
    available_crude_bbl: float,
    max_bbl_day: float = REFINERY_MAX_BBL_DAY,
) -> tuple[float, float]:
    """Run one refinery's daily refining step.

    Returns (actual_input_bbl, refined_bbl). actual is bounded by setpoint,
    available crude, and ``max_bbl_day`` (the per-refinery effective cap,
    which `route_crude` scales by workforce efficiency). Floored at 0.
    """
    actual = min(
        float(setpoint_rate_bbl_day),
        float(available_crude_bbl),
        max(0.0, float(max_bbl_day)),
    )
    actual = max(0.0, actual)
    return actual, actual * REFINERY_YIELD


def route_crude(refineries: list[Tile], total_crude_bbl: float) -> dict[str, float]:
    """Allocate the day's crude across refineries.

    Sort key: (-setpoint_rate_bbl_day, id). The negative-setpoint primary
    key sends crude to the highest-throughput refinery first; id ascending
    is the deterministic tiebreak when two refineries share a setpoint.

    Per-refinery cap = ``REFINERY_MAX_BBL_DAY × workforce.efficiency(r)``,
    so a half-staffed refinery routes at most 250 bbl/day and an idle one
    routes 0. The player-facing ``setpoint_rate_bbl_day`` itself is not
    re-clamped here — only the actual throughput respects the cap.

    Returns {refinery_id: actual_input_bbl}. Refineries that get no crude
    (either because the queue ran dry or their setpoint was 0) appear with
    actual=0.0 so the caller can pin current_throughput uniformly.
    """
    sorted_refs = sorted(refineries, key=lambda r: (-r.setpoint_rate_bbl_day, r.id))
    available = max(0.0, float(total_crude_bbl))
    per_refinery: dict[str, float] = {}
    for r in sorted_refs:
        effective_max = REFINERY_MAX_BBL_DAY * workforce.efficiency(r)
        actual, _ = refine_one(r.setpoint_rate_bbl_day, available, effective_max)
        per_refinery[r.id] = actual
        available -= actual
    return per_refinery


def refinery_process_kw(throughput_bbl_day: float) -> float:
    """Refinery hourly process power load: actual × KWH_PER_BBL / 24."""
    return float(throughput_bbl_day) * REFINERY_KWH_PER_BBL / 24.0


def daily_emissions_t(world: World) -> float:
    """Total CO2 emitted today, summed across the four PRD-revised sources.

    Reads coal_kwh / gas_kwh / refined_bbl from `state.today_summary_so_far`
    (populated in the daily loop before this is called), and delegates the
    per-industrial-tile flat term to `world.pricing.industrial_co2_for_tile`
    so the aggregate and the per-tile popup row stay in lockstep. The
    brief's per-MWh-consumed industrial term is intentionally absent —
    industrial kWh already shows up in the coal/gas plant emissions serving
    those tiles.
    """
    from world.pricing import industrial_co2_for_tile

    s = world.state
    coal_mwh = s.today_summary_so_far.get("coal_kwh", 0.0) / 1000.0
    gas_mwh = s.today_summary_so_far.get("gas_kwh", 0.0) / 1000.0
    refined_bbl = s.today_summary_so_far.get("refined_bbl", 0.0)
    industrial_flat_co2 = sum(industrial_co2_for_tile(t) for t in s.tiles)
    return (
        coal_mwh * COAL_CO2_T_PER_MWH
        + gas_mwh * GAS_CO2_T_PER_MWH
        + industrial_flat_co2
        + refined_bbl * REFINERY_CO2_PER_BBL
    )

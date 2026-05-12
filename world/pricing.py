"""Per-facility daily economics helpers.

Pure functions that price the revenue, fuel cost, carbon cost, and net-per-day
contribution of a single tile or well at the current operating state. No I/O,
no RNG, no internal mutation — callers pass a tile (or well) plus the
ambient ``WorldState`` and read back floats.

This module is the single source of truth for the rate constants the hover
popup and the ``/catalog`` endpoint surface to API consumers. Slice 01 wires
up industrial revenue and the industrial CO2 helper; subsequent slices add
commercial, plant, refinery, and well pricing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from world import workforce
from world.economy import INDUSTRIAL_PROCESS_CO2_T_PER_DAY

if TYPE_CHECKING:
    from world.sim import World
    from world.state import Tile

# Industrial: flat daily revenue at full staffing, scaled linearly by workforce
# efficiency. Calibrated at ~$500/day so a fully-staffed industrial tile
# offsets its $200 OPEX and the ~$50/day carbon cost at the default carbon
# price ($25/t × 2 t = $50), leaving net ~$250/day.
INDUSTRIAL_REVENUE_PER_DAY: float = 500.0


def industrial_revenue_for_tile(tile: Tile) -> float:
    """Daily revenue for one industrial tile at its current staffing.

    Zero when the tile is non-operational or not an industrial. Otherwise
    ``INDUSTRIAL_REVENUE_PER_DAY × workforce.efficiency(tile)``.
    """
    if tile.type != "industrial" or not tile.operational:
        return 0.0
    return INDUSTRIAL_REVENUE_PER_DAY * workforce.efficiency(tile)


def industrial_co2_for_tile(tile: Tile) -> float:
    """Daily CO2 in tonnes for one industrial tile at its current staffing.

    Mirrors the existing aggregator term (``daily_emissions_t``) so the
    aggregate sum and the per-tile popup row are computed by the same helper.
    """
    if tile.type != "industrial" or not tile.operational:
        return 0.0
    return INDUSTRIAL_PROCESS_CO2_T_PER_DAY * workforce.efficiency(tile)


def update_civic_revenue(world: World) -> None:
    """Accrue civic revenue (commercial + industrial) for the current day.

    Adds to ``today_summary_so_far["industrial_revenue"]`` and credits
    ``state.treasury`` by the same amount. Idempotent within a day only in the
    sense that ``today_summary_so_far`` is reset at the start of each day by
    ``_advance_one_day``; calling this function twice in one day would
    double-credit.

    Must be called after the daily power/oil/refining loops have settled
    today's operational state and **before** ``update_population`` so the
    commercial revenue (slice 02) uses today's lived population, not
    tomorrow's survivors.
    """
    state = world.state
    industrial = 0.0
    for tile in state.tiles:
        industrial += industrial_revenue_for_tile(tile)
    if industrial:
        state.today_summary_so_far["industrial_revenue"] = (
            state.today_summary_so_far.get("industrial_revenue", 0.0) + industrial
        )
        state.treasury += industrial

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
    from world.state import Tile, WorldState

# Industrial: flat daily revenue at full staffing, scaled linearly by workforce
# efficiency. Calibrated at ~$500/day so a fully-staffed industrial tile
# offsets its $200 OPEX and the ~$50/day carbon cost at the default carbon
# price ($25/t × 2 t = $50), leaving net ~$250/day.
INDUSTRIAL_REVENUE_PER_DAY: float = 500.0

# Commercial: $/resident-served/day at 100% occupancy and full staffing. A
# commercial tile sums housing_capacity in a 5×5 chebyshev box around itself
# (radius 2) and earns capacity × occupancy × rate × efficiency. Calibrated
# against the $50/day OPEX so a well-placed commercial near full-occupancy
# housing nets a modest positive at full staffing.
COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY: float = 1.0
COMMERCIAL_RADIUS: int = 2


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


def _occupancy_ratio(state: WorldState) -> float:
    """City-wide occupancy = ``min(1.0, population / total_housing_capacity)``.

    Returns 0.0 when there is no housing (zero population in a city with no
    homes still ends up at 0/1 = 0). The cap at 1.0 means a temporary
    overshoot (e.g. drained housing without a population resolution yet)
    cannot inflate commercial revenue.
    """
    capacity = sum(t.housing_capacity for t in state.tiles)
    return min(1.0, state.population / max(1, capacity))


def commercial_revenue_for_tile(state: WorldState, tile: Tile) -> float:
    """Daily revenue for one commercial tile at the current city occupancy.

    Sums ``housing_capacity`` over every tile within chebyshev distance
    ``COMMERCIAL_RADIUS`` (==2) of ``tile`` (a 5×5 box clipped at grid edges),
    multiplies by city-wide occupancy ratio, the per-resident rate, and the
    tile's workforce efficiency. The "every tile with
    ``housing_capacity > 0``" rule means the town hall (100 housing) counts
    just like a house. Returns 0 for non-commercial or non-operational tiles.

    Overlapping commercials independently full-count residents — there is no
    cross-commercial deduplication in v1. Two adjacent commercials over the
    same housing both earn the full amount.
    """
    if tile.type != "commercial" or not tile.operational:
        return 0.0
    capacity_in_radius = 0
    for other in state.tiles:
        if other.housing_capacity <= 0:
            continue
        if max(abs(other.x - tile.x), abs(other.y - tile.y)) <= COMMERCIAL_RADIUS:
            capacity_in_radius += other.housing_capacity
    if capacity_in_radius == 0:
        return 0.0
    occupancy = _occupancy_ratio(state)
    return (
        capacity_in_radius
        * occupancy
        * COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY
        * workforce.efficiency(tile)
    )


def _commercial_residents_in_radius(state: WorldState, tile: Tile) -> float:
    """Raw capacity × occupancy for the popup ``residents_in_radius`` row.

    Does not multiply by efficiency or the per-resident rate — this is the
    "how many residents are within reach" number a player wants to read.
    Returns 0.0 for non-commercial tiles.
    """
    if tile.type != "commercial":
        return 0.0
    capacity_in_radius = 0
    for other in state.tiles:
        if other.housing_capacity <= 0:
            continue
        if max(abs(other.x - tile.x), abs(other.y - tile.y)) <= COMMERCIAL_RADIUS:
            capacity_in_radius += other.housing_capacity
    return capacity_in_radius * _occupancy_ratio(state)


def update_civic_revenue(world: World) -> None:
    """Accrue civic revenue (commercial + industrial) for the current day.

    Adds to ``today_summary_so_far["industrial_revenue"]`` and
    ``today_summary_so_far["commercial_revenue"]`` and credits
    ``state.treasury`` by the sum. Idempotent within a day only in the sense
    that ``today_summary_so_far`` is reset at the start of each day by
    ``_advance_one_day``; calling this function twice in one day would
    double-credit.

    Must be called after the daily power/oil/refining loops have settled
    today's operational state and **before** ``update_population`` so the
    commercial revenue uses today's lived population, not tomorrow's
    survivors.
    """
    state = world.state
    industrial = 0.0
    commercial = 0.0
    for tile in state.tiles:
        industrial += industrial_revenue_for_tile(tile)
        commercial += commercial_revenue_for_tile(state, tile)
    if industrial:
        state.today_summary_so_far["industrial_revenue"] = (
            state.today_summary_so_far.get("industrial_revenue", 0.0) + industrial
        )
        state.treasury += industrial
    if commercial:
        state.today_summary_so_far["commercial_revenue"] = (
            state.today_summary_so_far.get("commercial_revenue", 0.0) + commercial
        )
        state.treasury += commercial

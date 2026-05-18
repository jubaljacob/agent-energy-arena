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
from world.economy import (
    INDUSTRIAL_PROCESS_CO2_T_PER_DAY,
    REFINERY_CO2_PER_BBL,
    REFINERY_YIELD,
)
from world.power import PLANT_TYPES
from world.subsurface import INJECTION_KWH_PER_BBL, PRODUCTION_KWH_PER_BBL

if TYPE_CHECKING:
    from world.catalog import TileSpec
    from world.sim import World
    from world.state import Tile, Well, WorldState

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
COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY: float = 2.0
COMMERCIAL_RADIUS: int = 2


def industrial_revenue_for_tile(state: WorldState, tile: Tile) -> float:
    """Daily revenue for one industrial tile at its current staffing.

    Zero when the tile is non-operational or not an industrial. Otherwise
    ``state.industrial_revenue_per_day × workforce.efficiency(tile)``.
    """
    if tile.type != "industrial" or not tile.operational:
        return 0.0
    return state.industrial_revenue_per_day * workforce.efficiency(tile)


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
        * state.commercial_revenue_per_resident_per_day
        * workforce.efficiency(tile)
    )


def plant_revenue_for_tile(state: WorldState, tile: Tile) -> float:
    """Daily revenue estimate for one plant tile at its current operating state.

    Returns ``kwh_served_yesterday × state.grid_price_retail`` for plant
    tiles (solar / wind / coal / gas). Day 0 has no completed dispatch yet
    so ``kwh_served_yesterday == 0`` and revenue is 0; from day 1 onwards
    the value reflects the just-completed day's gross dispatch output.

    Non-plant tiles and non-operational plants return 0.0. This is an
    estimate — total billable revenue at the city level still goes through
    the dispatch/curtailment split in ``_advance_one_day``; this helper
    surfaces a per-plant attribution for the hover popup.
    """
    if tile.type not in PLANT_TYPES or not tile.operational:
        return 0.0
    return tile.kwh_served_yesterday * state.grid_price_retail


def plant_fuel_cost_for_tile(state: WorldState, tile: Tile, spec: TileSpec) -> float:
    """Daily fuel cost for one plant tile, based on yesterday's served kWh.

    Returns ``kwh_served_yesterday / 1000 × cost_per_mwh`` where
    ``cost_per_mwh`` is sourced from ``state.plant_fuel_cost_per_mwh`` for
    coal/gas (the mutable per-type dict scenarios can override) and falls
    back to ``spec.fuel_cost_per_mwh`` for tiles outside the dict
    (renewables, which have 0 there anyway). Non-plant tiles and
    non-operational plants return 0.
    """
    if tile.type not in PLANT_TYPES or not tile.operational:
        return 0.0
    cost_per_mwh = state.plant_fuel_cost_per_mwh.get(tile.type, spec.fuel_cost_per_mwh)
    return (tile.kwh_served_yesterday / 1000.0) * cost_per_mwh


def plant_co2_for_tile(tile: Tile, spec: TileSpec) -> float:
    """Daily CO2 in tonnes for one plant tile, based on yesterday's served kWh.

    Returns ``kwh_served_yesterday / 1000 × spec.co2_t_per_mwh``. Renewables
    have ``co2_t_per_mwh == 0`` so the result is 0. This is the popup-row
    figure; the city-wide aggregator still drives carbon accounting via the
    hourly dispatch path.
    """
    if tile.type not in PLANT_TYPES or not tile.operational:
        return 0.0
    return (tile.kwh_served_yesterday / 1000.0) * spec.co2_t_per_mwh


def plant_carbon_cost_for_tile(state: WorldState, tile: Tile, spec: TileSpec) -> float:
    """Daily carbon cost in $ for one plant tile.

    Reads ``state.carbon_price`` at compute time so regulatory-tightening
    events flow through into the Net row the same day they fire.
    """
    return plant_co2_for_tile(tile, spec) * state.carbon_price


def refinery_revenue_for_tile(state: WorldState, tile: Tile) -> float:
    """Daily revenue estimate for one refinery tile at its current throughput.

    Returns ``current_throughput_bbl_day × REFINERY_YIELD ×
    state.refined_price_usd_per_bbl``. Non-refinery and non-operational
    tiles return 0. The throughput pinned on the tile is the previous day's
    actual refining input (set by ``_advance_one_day`` via ``route_crude``),
    so the popup row reflects yesterday's accounting window.
    """
    if tile.type != "refinery" or not tile.operational:
        return 0.0
    return tile.current_throughput_bbl_day * REFINERY_YIELD * state.refined_price_usd_per_bbl


def refinery_co2_for_tile(tile: Tile) -> float:
    """Daily CO2 in tonnes for one refinery tile.

    Returns ``current_throughput_bbl_day × REFINERY_CO2_PER_BBL`` (0.30
    t/bbl). Non-refinery / non-operational tiles return 0.
    """
    if tile.type != "refinery" or not tile.operational:
        return 0.0
    return tile.current_throughput_bbl_day * REFINERY_CO2_PER_BBL


def refinery_carbon_cost_for_tile(state: WorldState, tile: Tile) -> float:
    """Daily carbon cost in $ for one refinery tile.

    Reads ``state.carbon_price`` at compute time so regulatory-tightening
    events flow through into the Net row the same day they fire.
    """
    return refinery_co2_for_tile(tile) * state.carbon_price


def well_gross_crude_value_for_tile(state: WorldState, well: Well) -> float:
    """Daily gross crude value estimate for one production well.

    Returns ``current_rate_bbl_day × state.crude_price_usd_per_bbl`` for
    production wells. Injection wells return 0 (they consume bbl/day, not
    produce it).
    """
    if well.type != "production":
        return 0.0
    return well.current_rate_bbl_day * state.crude_price_usd_per_bbl


def well_injection_kwh_per_day(well: Well) -> float:
    """Daily kWh consumed by one injection well at its current pump rate.

    Returns ``current_rate_bbl_day × INJECTION_KWH_PER_BBL``. Production wells
    return 0. This is informational — the player isn't billed in $ for it
    (the cost is internalized through whichever plants serve the load), but
    it's useful in the popup to gauge the operational power draw.
    """
    if well.type != "injection":
        return 0.0
    return well.current_rate_bbl_day * INJECTION_KWH_PER_BBL


def well_production_kwh_per_day(well: Well) -> float:
    """Daily kWh consumed by one production well at its current lift rate.

    Returns ``current_rate_bbl_day × PRODUCTION_KWH_PER_BBL``. Injection wells
    return 0. Symmetric to :func:`well_injection_kwh_per_day`; the cost is
    internalized through dispatch (the player isn't billed $ for it), but the
    figure is useful in the popup to gauge the operational power draw.
    """
    if well.type != "production":
        return 0.0
    return well.current_rate_bbl_day * PRODUCTION_KWH_PER_BBL


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
        industrial += industrial_revenue_for_tile(state, tile)
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

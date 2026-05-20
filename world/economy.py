"""Civic economics, refinery operations, and end-of-day settle.

Owns the dual-consumer per-tile helpers that both the day-loop
aggregator and ``world.state_view`` read (industrial / commercial
revenue, industrial CO2, ``occupancy_ratio``); the end-of-day
settle/update functions that fold per-facility figures into
``state.today`` and the treasury; and the refinery operational
helpers (``refine_one``, ``route_crude``, ``refinery_process_kw``)
consumed by ``world.pipelines`` and ``world.hourly_tick``.

Per-type popup-only helpers (plant, refinery, well) live in
``world.state_view`` next to their sole caller; the constants
(``REFINERY_YIELD``, ``REFINERY_CO2_PER_BBL``, …) stay here as the
shared source of truth.

The per-facility helpers are pure: pass a tile (or well) plus the
ambient ``WorldState`` and read back floats. The settle functions
write to ``state.today`` and ``state.treasury`` and are called from
``World._advance_one_day``.

Carbon (PRD §4.7): ``daily_emissions_t`` reads the day's running
coal/gas/refined totals from ``state.today`` (DayLedger) plus the
operational-industrial tile count. The PRD revises the brief by
removing the per-MWh-consumed industrial term — industrial tiles emit
a flat ``INDUSTRIAL_PROCESS_CO2_T_PER_DAY`` regardless of grid load,
and the kWh they consume is already counted via the coal/gas plants
serving them. Carbon cost = emissions × ``state.carbon_price``
(mutable, initialised to ``CARBON_PRICE_USD_PER_TON`` on /reset;
events tighten it).

Refining (PRD §4.6): each refinery refines up to its setpoint (capped
at ``REFINERY_MAX_BBL_DAY`` and at the available crude). Refined
output = actual × ``REFINERY_YIELD``. Crude is routed across
refineries by descending setpoint (with id ascending as the
deterministic tiebreak). Process load
(actual × ``REFINERY_KWH_PER_BBL`` / 24) is unbilled to the agent — it
counts toward dispatch demand and toward fuel-burn / carbon emissions
on whichever plants serve it, but no retail revenue is paid.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from world import workforce
from world.event_effects import fuel_price_shock_bill_mult
from world.power import PLANT_TYPES, daily_met_demand_fraction

if TYPE_CHECKING:
    from world.sim import World
    from world.state import Tile, WorldState

# Refinery (PRD §4.6).
REFINERY_MAX_BBL_DAY: float = 250.0
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


# ---------------------------------------------------------------------------
# Dual-consumer per-tile helpers (pure: tile + state -> float).
# Read by both ``world.state_view`` (popup row) and the settle/update
# functions below (city-wide aggregates), so the two figures cannot drift.
# ---------------------------------------------------------------------------


def industrial_revenue_for_tile(
    state: WorldState, tile: Tile, power_supply_ratio: float = 1.0
) -> float:
    """Daily revenue for one industrial tile at its current staffing.

    Zero when the tile is non-operational or not an industrial. Otherwise
    ``state.industrial_revenue_per_day × workforce.efficiency(tile) ×
    power_supply_ratio``.

    ``power_supply_ratio`` is the grid's daily met-demand fraction in
    ``[0, 1]`` (see :func:`world.power.daily_met_demand_fraction`). A ratio
    of 1.0 returns the workforce-only baseline; 0.5 halves revenue; 0.0
    zeroes it. Composes multiplicatively with workforce efficiency — a
    half-staffed tile under 50% supply earns 25% of baseline. Default 1.0
    so direct unit-test callers that don't model the grid keep the
    workforce-only contract.
    """
    if tile.type != "industrial" or not tile.operational:
        return 0.0
    return state.industrial_revenue_per_day * workforce.efficiency(tile) * power_supply_ratio


def industrial_co2_for_tile(tile: Tile) -> float:
    """Daily CO2 in tonnes for one industrial tile at its current staffing.

    Mirrors the existing aggregator term (``daily_emissions_t``) so the
    aggregate sum and the per-tile popup row are computed by the same helper.
    """
    if tile.type != "industrial" or not tile.operational:
        return 0.0
    return INDUSTRIAL_PROCESS_CO2_T_PER_DAY * workforce.efficiency(tile)


def occupancy_ratio(state: WorldState) -> float:
    """City-wide occupancy = ``min(1.0, population / total_housing_capacity)``.

    Returns 0.0 when there is no housing (zero population in a city with no
    homes still ends up at 0/1 = 0). The cap at 1.0 means a temporary
    overshoot (e.g. drained housing without a population resolution yet)
    cannot inflate commercial revenue.

    Public because ``state_view`` consumes it to compute the commercial
    popup's ``residents_in_radius`` field; commercial revenue here uses
    the same call so the two figures stay reconciled.
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
    occupancy = occupancy_ratio(state)
    return (
        capacity_in_radius
        * occupancy
        * state.commercial_revenue_per_resident_per_day
        * workforce.efficiency(tile)
    )


# ---------------------------------------------------------------------------
# Refinery operational helpers (consumed by ``world.pipelines`` and
# ``world.hourly_tick`` to drive the sim loop).
# ---------------------------------------------------------------------------


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
    so a half-staffed refinery routes at most 125 bbl/day and an idle one
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


# ---------------------------------------------------------------------------
# End-of-day settle / update functions. Mutate ``state.today`` and
# ``state.treasury``; called from ``World._advance_one_day``.
# ---------------------------------------------------------------------------


def settle_opex(state: WorldState) -> None:
    """End-of-day OPEX accrual: every standing tile and drilled well pays
    its daily OPEX.

    Writes ``state.today.opex`` and debits ``state.treasury``. Zero-cost
    days leave both untouched. Idempotent within a day only in the sense
    that ``state.today`` is reset at the start of each day; calling this
    twice in one day would double-debit.
    """
    opex_total = sum(t.opex_per_day for t in state.tiles) + sum(w.opex_per_day for w in state.wells)
    if opex_total:
        state.treasury -= opex_total
        state.today.opex = opex_total


def settle_fuel(state: WorldState) -> None:
    """End-of-day fuel cost: ``coal_kwh / 1000 × $/MWh + gas_kwh / 1000 × $/MWh``.

    A ``fuel_price_shock`` event (slice 11) doubles both costs while
    active. Reads the day's coal/gas kWh that ``commit_tick`` accumulated
    on ``state.today``; writes ``state.today.fuel_cost`` and debits
    ``state.treasury``.
    """
    if not (state.today.coal_kwh or state.today.gas_kwh):
        return
    coal_cost_per_mwh = state.plant_fuel_cost_per_mwh["coal_plant"]
    gas_cost_per_mwh = state.plant_fuel_cost_per_mwh["gas_peaker"]
    coal_shock = fuel_price_shock_bill_mult(state, "coal_plant")
    gas_shock = fuel_price_shock_bill_mult(state, "gas_peaker")
    fuel_total = (state.today.coal_kwh / 1000.0) * coal_cost_per_mwh * coal_shock + (
        state.today.gas_kwh / 1000.0
    ) * gas_cost_per_mwh * gas_shock
    state.treasury -= fuel_total
    state.today.fuel_cost = fuel_total


def settle_carbon(world: World) -> None:
    """End-of-day carbon emissions + cost (PRD §4.7).

    Pins ``state.today.refined_bbl`` from the day's routed refining input
    (already on ``state.today`` after ``route_oil``), then sums emissions
    via ``daily_emissions_t``. Writes ``co2_emitted_t`` and
    ``carbon_cost`` on ``state.today``; debits ``state.treasury``.

    Must run **after** ``route_oil`` (which pins ``refined_bbl``) — the
    emissions formula reads coal_kwh + gas_kwh + refined_bbl.
    """
    state = world.state
    co2_t = daily_emissions_t(world)
    carbon_cost = co2_t * state.carbon_price
    state.today.co2_emitted_t = co2_t
    state.today.carbon_cost = carbon_cost
    if carbon_cost:
        state.treasury -= carbon_cost


def daily_emissions_t(world: World) -> float:
    """Total CO2 emitted today, summed across the four PRD-revised sources.

    Reads coal_kwh / gas_kwh / refined_bbl from `state.today`
    (populated in the daily loop before this is called), and delegates the
    per-industrial-tile flat term to :func:`industrial_co2_for_tile` so the
    aggregate and the per-tile popup row stay in lockstep. The brief's
    per-MWh-consumed industrial term is intentionally absent — industrial
    kWh already shows up in the coal/gas plant emissions serving those
    tiles.
    """
    s = world.state
    coal_mwh = s.today.coal_kwh / 1000.0
    gas_mwh = s.today.gas_kwh / 1000.0
    refined_bbl = s.today.refined_bbl
    industrial_flat_co2 = sum(industrial_co2_for_tile(t) for t in s.tiles)
    return (
        coal_mwh * COAL_CO2_T_PER_MWH
        + gas_mwh * GAS_CO2_T_PER_MWH
        + industrial_flat_co2
        + refined_bbl * REFINERY_CO2_PER_BBL
    )


def settle_eod_treasury(state: WorldState) -> None:
    """End-of-day treasury settle for power-side accumulators.

    ``commit_tick`` accumulated ``today.power_revenue`` (civilian retail +
    curtailment export) and ``today.outage_penalty`` (per-hour outage
    cost scaled by the civilian-unserved fraction) across the day's 24
    ticks. This applies both to treasury in one shot: credit revenue,
    debit penalty.

    The pattern is deliberately symmetric with ``settle_opex`` /
    ``settle_fuel`` / ``settle_carbon``: per-hour accumulators move
    treasury once at end of day, never mid-tick.
    """
    state.treasury += state.today.power_revenue
    state.treasury -= state.today.outage_penalty


def pin_yesterday(state: WorldState) -> None:
    """End-of-day rollup: pin the just-completed day's state for tomorrow's
    consumers.

    Three concerns, all about "yesterday" as seen from tomorrow:

      * Outage carry — ``yesterday_blackout_hours`` /
        ``yesterday_brownout_hours`` feed the next day's happiness
        velocity in ``update_population``.
      * UI trace — ``last_day_trace`` is copied (not aliased) from
        ``state.today.{supply,demand,balance_state}_by_hour`` so the next
        day's ``DayLedger.reset()`` (which replaces the lists with fresh
        ones) doesn't also clear the UI trace.
      * Plant kWh — ``kwh_served_yesterday`` snapshots ``kwh_served_today``
        on every plant tile, so tomorrow's hover popup and
        ``estimated_revenue_per_day`` are priced on the just-completed
        day's actual served energy.

    Must run before ``update_civic_revenue`` (which reads
    ``last_day_trace.supply_kw_by_hour`` to compute the day's met-demand
    fraction for industrial revenue gating).
    """
    state.yesterday_blackout_hours = state.today.blackout_hours
    state.yesterday_brownout_hours = state.today.brownout_hours

    state.last_day_trace.supply_kw_by_hour = list(state.today.supply_kw_by_hour)
    state.last_day_trace.demand_kw_by_hour = list(state.today.demand_kw_by_hour)
    state.last_day_trace.balance_state_by_hour = list(state.today.balance_state_by_hour)

    for t in state.tiles:
        if t.type in PLANT_TYPES:
            t.kwh_served_yesterday = t.kwh_served_today


def update_civic_revenue(world: World) -> None:
    """Accrue civic revenue (commercial + industrial) for the current day.

    Adds to ``state.today.industrial_revenue`` and
    ``state.today.commercial_revenue`` and credits ``state.treasury`` by the
    sum. Idempotent within a day only in the sense that ``state.today`` is
    reset at the start of each day by ``_advance_one_day``; calling this
    function twice in one day would double-credit.

    Must be called after the daily power/oil/refining loops have settled
    today's operational state and **before** ``update_population`` so the
    commercial revenue uses today's lived population, not tomorrow's
    survivors.
    """
    state = world.state
    # Gate industrial revenue by the just-completed day's grid met-demand
    # fraction (issue 08). Brownouts and blackouts now cost the industrial
    # tile money — not just the city's happiness — so keeping the grid
    # healthy is a producer-side concern, not only a citizen-side one.
    # `state.last_day_trace` was pinned at the end of the hourly loop earlier
    # in `_advance_one_day`, so traces are present on day 0+.
    power_supply_ratio = daily_met_demand_fraction(
        state.last_day_trace.supply_kw_by_hour,
        state.last_day_trace.demand_kw_by_hour,
    )
    industrial = 0.0
    commercial = 0.0
    for tile in state.tiles:
        industrial += industrial_revenue_for_tile(
            state, tile, power_supply_ratio=power_supply_ratio
        )
        commercial += commercial_revenue_for_tile(state, tile)
    if industrial:
        state.today.industrial_revenue += industrial
        state.treasury += industrial
    if commercial:
        state.today.commercial_revenue += commercial
        state.treasury += commercial

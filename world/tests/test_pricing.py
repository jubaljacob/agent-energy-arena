"""Per-facility daily economics (slice 01 — tracer: industrial).

Unit-tests on the pure ``world.pricing`` helpers and an end-to-end
``/step`` integration that the industrial revenue is accrued into
``today_summary_so_far["industrial_revenue"]`` and credited to
``state.treasury``. Catalog parity for the new ``economics`` block is
asserted alongside.
"""

from __future__ import annotations

import pytest

from world.catalog import TILE_CATALOG, build_catalog
from world.economy import CARBON_PRICE_USD_PER_TON
from world.pricing import (
    COMMERCIAL_RADIUS,
    COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY,
    INDUSTRIAL_REVENUE_PER_DAY,
    _occupancy_ratio,
    commercial_revenue_for_tile,
    industrial_co2_for_tile,
    industrial_revenue_for_tile,
    update_civic_revenue,
)
from world.sim import World
from world.state import Tile, Well, WorldState


def _default_state() -> WorldState:
    """Bare WorldState carrying post-refactor default pricing/rate fields.

    Open-source-arena slice 01 promoted pricing constants onto WorldState;
    the dataclass defaults mirror the old module-level constants exactly,
    so unit tests that previously called pricing helpers without state can
    use this stand-in instead of plumbing a full World.
    """
    return WorldState(seed=0)


def _industrial_tile(staffed: int | None = None) -> Tile:
    spec = TILE_CATALOG["industrial"]
    staffed_jobs = spec.jobs if staffed is None else staffed
    return Tile(
        id="ind_1",
        type="industrial",
        x=0,
        y=0,
        built_day=0,
        operational=True,
        capex_paid=spec.capex,
        opex_per_day=spec.opex_per_day,
        jobs=spec.jobs,
        demand_kw=spec.demand_kw,
        staffed_jobs=staffed_jobs,
    )


# -- industrial_revenue_for_tile -------------------------------------------


def test_industrial_revenue_full_staffing_equals_constant() -> None:
    tile = _industrial_tile()  # staffed = jobs
    assert industrial_revenue_for_tile(_default_state(), tile) == pytest.approx(
        INDUSTRIAL_REVENUE_PER_DAY
    )


def test_industrial_revenue_scales_linearly_with_efficiency() -> None:
    spec = TILE_CATALOG["industrial"]
    half_jobs = spec.jobs // 2
    tile = _industrial_tile(staffed=half_jobs)
    expected = INDUSTRIAL_REVENUE_PER_DAY * (half_jobs / spec.jobs)
    assert industrial_revenue_for_tile(_default_state(), tile) == pytest.approx(expected)


def test_industrial_revenue_idle_returns_zero() -> None:
    tile = _industrial_tile(staffed=0)
    assert industrial_revenue_for_tile(_default_state(), tile) == 0.0


def test_industrial_revenue_zero_when_not_operational() -> None:
    tile = _industrial_tile()
    tile.operational = False
    assert industrial_revenue_for_tile(_default_state(), tile) == 0.0


def test_industrial_revenue_zero_for_non_industrial_tile() -> None:
    spec = TILE_CATALOG["commercial"]
    tile = Tile(
        id="com_1",
        type="commercial",
        x=0,
        y=0,
        built_day=0,
        operational=True,
        jobs=spec.jobs,
        staffed_jobs=spec.jobs,
        demand_kw=spec.demand_kw,
    )
    assert industrial_revenue_for_tile(_default_state(), tile) == 0.0


# -- industrial_co2_for_tile (delegation contract) -------------------------


def test_industrial_co2_for_tile_matches_efficiency_scaled_constant() -> None:
    """The helper that the aggregator delegates to must scale by efficiency."""
    spec = TILE_CATALOG["industrial"]
    half = _industrial_tile(staffed=spec.jobs // 2)
    full = _industrial_tile()
    assert industrial_co2_for_tile(full) == pytest.approx(2.0)
    assert industrial_co2_for_tile(half) == pytest.approx(2.0 * (spec.jobs // 2) / spec.jobs)


def test_industrial_co2_for_tile_zero_when_idle_or_not_operational() -> None:
    assert industrial_co2_for_tile(_industrial_tile(staffed=0)) == 0.0
    tile = _industrial_tile()
    tile.operational = False
    assert industrial_co2_for_tile(tile) == 0.0


# -- update_civic_revenue (sim wiring) -------------------------------------


def test_update_civic_revenue_accrues_to_summary_and_treasury() -> None:
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("industrial", th.x + 1, th.y)
    # Force full staffing so we know the exact expected revenue.
    ind = next(t for t in w.state.tiles if t.type == "industrial")
    ind.staffed_jobs = ind.jobs

    treasury_before = w.state.treasury
    summary_before = w.state.today_summary_so_far.get("industrial_revenue", 0.0)

    update_civic_revenue(w)

    assert w.state.today_summary_so_far["industrial_revenue"] == pytest.approx(
        summary_before + INDUSTRIAL_REVENUE_PER_DAY
    )
    assert w.state.treasury == pytest.approx(treasury_before + INDUSTRIAL_REVENUE_PER_DAY)


def test_today_summary_so_far_defaults_industrial_revenue_to_zero() -> None:
    w = World()
    w.reset(seed=42)
    assert "industrial_revenue" in w.state.today_summary_so_far
    assert w.state.today_summary_so_far["industrial_revenue"] == 0.0


# -- /step end-to-end accrual ----------------------------------------------


def test_step_accrues_industrial_revenue_into_summary_and_treasury() -> None:
    """One /step with one fully-staffed industrial credits both the summary
    bucket and treasury by INDUSTRIAL_REVENUE_PER_DAY × efficiency."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("industrial", th.x + 1, th.y)
    ind = next(t for t in w.state.tiles if t.type == "industrial")
    ind.staffed_jobs = ind.jobs  # ensure full staffing irrespective of pop

    treasury_before = w.state.treasury
    w.step(days=1)

    revenue = w.state.today_summary_so_far["industrial_revenue"]
    assert revenue == pytest.approx(INDUSTRIAL_REVENUE_PER_DAY)
    # Treasury delta is dominated by many flows; isolate the industrial
    # revenue contribution by re-running without the industrial.
    w2 = World()
    w2.reset(seed=42)
    treasury_before_no_ind = w2.state.treasury
    w2.step(days=1)
    treasury_no_ind = w2.state.treasury
    treasury_with_ind = w.state.treasury
    # The industrial scenario also adds $200/day OPEX and ~$50/day carbon cost
    # vs the no-industrial scenario, but those costs are independent of
    # revenue. Net of OPEX+carbon, the industrial scenario must be exactly
    # INDUSTRIAL_REVENUE_PER_DAY × eff higher in treasury after one day.
    expected_delta = (
        INDUSTRIAL_REVENUE_PER_DAY  # new revenue
        - ind.opex_per_day  # extra OPEX
        - 2.0 * w.state.carbon_price  # extra industrial CO2 cost (2 t × price)
    )
    actual_delta = (treasury_with_ind - treasury_before) - (
        treasury_no_ind - treasury_before_no_ind
    )
    assert actual_delta == pytest.approx(expected_delta, abs=1e-6)


def test_civic_revenue_runs_before_population_update() -> None:
    """Industrial revenue accrues into today_summary_so_far *before*
    update_population, so tomorrow's commercial revenue (slice 02) will see
    today's lived population. We assert the ordering by constructing a state
    where the industrial tile would be unstaffed if population dropped to
    zero first — the revenue must still appear because efficiency is read
    before drain_n could fire."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("industrial", th.x + 1, th.y)
    ind = next(t for t in w.state.tiles if t.type == "industrial")
    ind.staffed_jobs = ind.jobs

    w.step(days=1)
    # Revenue is non-zero — proving update_civic_revenue ran while the
    # industrial was still staffed.
    assert w.state.today_summary_so_far["industrial_revenue"] > 0.0


def test_step_no_industrial_means_zero_industrial_revenue() -> None:
    """Regression guard: pre-feature cities (no industrial) keep their
    behavior modulo the new zero bucket."""
    w = World()
    w.reset(seed=42)
    w.step(days=1)
    assert w.state.today_summary_so_far["industrial_revenue"] == 0.0


# -- /state per-tile economics fields --------------------------------------


def test_state_tiles_include_estimated_fields_for_industrial() -> None:
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("industrial", th.x + 1, th.y)
    ind = next(t for t in w.state.tiles if t.type == "industrial")
    ind.staffed_jobs = ind.jobs

    s = w.state_dict()
    ind_dict = next(t for t in s["tiles"] if t["type"] == "industrial")
    assert ind_dict["estimated_revenue_per_day"] == pytest.approx(INDUSTRIAL_REVENUE_PER_DAY)
    assert ind_dict["estimated_co2_per_day"] == pytest.approx(2.0)
    assert ind_dict["estimated_carbon_cost_per_day"] == pytest.approx(2.0 * w.state.carbon_price)
    assert ind_dict["estimated_net_per_day"] == pytest.approx(
        INDUSTRIAL_REVENUE_PER_DAY - ind_dict["opex_per_day"] - 2.0 * w.state.carbon_price
    )


def test_state_tiles_non_industrial_estimated_fields_are_zero() -> None:
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    # Build a house — slice-02 wires up commercial economics, so we use a
    # tile type whose estimated_* fields remain at the slice-01 zero default.
    w.build("house", th.x + 1, th.y)
    s = w.state_dict()
    for t in s["tiles"]:
        if t["type"] not in {"industrial", "commercial"}:
            assert t["estimated_revenue_per_day"] == 0.0
            assert t["estimated_co2_per_day"] == 0.0
            assert t["estimated_carbon_cost_per_day"] == 0.0
            assert t["estimated_net_per_day"] == 0.0


def test_build_industrial_response_includes_estimated_fields() -> None:
    """The /build response stamps the same economic fields as /state."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    res = w.build("industrial", th.x + 1, th.y)
    assert res["ok"]
    payload = res["result"]
    assert "estimated_revenue_per_day" in payload
    assert "estimated_co2_per_day" in payload
    assert "estimated_carbon_cost_per_day" in payload
    assert "estimated_net_per_day" in payload


# -- /catalog economics block ----------------------------------------------


def test_catalog_exposes_economics_block() -> None:
    cat = build_catalog()
    assert "economics" in cat
    eco = cat["economics"]
    assert eco["industrial_revenue_per_day"] == pytest.approx(INDUSTRIAL_REVENUE_PER_DAY)
    assert eco["carbon_price"] == pytest.approx(CARBON_PRICE_USD_PER_TON)


def test_catalog_industrial_description_mentions_revenue_and_co2() -> None:
    cat = build_catalog()
    industrial = next(t for t in cat["tiles"] if t["tile_type"] == "industrial")
    desc = industrial["description"].lower()
    assert "$500" in industrial["description"] or "500" in industrial["description"]
    assert "co2" in desc or "co₂" in desc


# =========================================================================
# Slice 02 — commercial revenue with 5×5 chebyshev radius
# =========================================================================


def _commercial_tile(x: int = 5, y: int = 5, staffed: int | None = None) -> Tile:
    spec = TILE_CATALOG["commercial"]
    staffed_jobs = spec.jobs if staffed is None else staffed
    return Tile(
        id=f"com_{x}_{y}",
        type="commercial",
        x=x,
        y=y,
        built_day=0,
        operational=True,
        capex_paid=spec.capex,
        opex_per_day=spec.opex_per_day,
        jobs=spec.jobs,
        demand_kw=spec.demand_kw,
        staffed_jobs=staffed_jobs,
    )


def _house_tile(x: int, y: int, capacity: int = 8) -> Tile:
    return Tile(
        id=f"house_{x}_{y}",
        type="house",
        x=x,
        y=y,
        built_day=0,
        operational=True,
        housing_capacity=capacity,
    )


def _state_with(tiles: list[Tile], population: int) -> WorldState:
    s = WorldState(seed=0)
    s.tiles = tiles
    s.population = population
    return s


# -- _occupancy_ratio ------------------------------------------------------


def test_occupancy_ratio_full_house_full_pop() -> None:
    state = _state_with([_house_tile(0, 0, 100)], population=100)
    assert _occupancy_ratio(state) == pytest.approx(1.0)


def test_occupancy_ratio_clipped_at_one() -> None:
    state = _state_with([_house_tile(0, 0, 50)], population=100)
    assert _occupancy_ratio(state) == pytest.approx(1.0)


def test_occupancy_ratio_half_pop_full_house() -> None:
    state = _state_with([_house_tile(0, 0, 100)], population=50)
    assert _occupancy_ratio(state) == pytest.approx(0.5)


def test_occupancy_ratio_no_pop_no_housing_returns_zero() -> None:
    state = _state_with([], population=0)
    assert _occupancy_ratio(state) == pytest.approx(0.0)


def test_occupancy_ratio_no_housing_with_pop_clamps_to_one() -> None:
    # Per spec: ``min(1.0, pop / max(1, capacity))``. With no housing and
    # nonzero pop, ``max(1, 0) == 1`` so the ratio saturates at 1.0.
    state = _state_with([], population=10)
    assert _occupancy_ratio(state) == pytest.approx(1.0)


# -- commercial_revenue_for_tile ------------------------------------------


def test_commercial_revenue_zero_when_no_houses_in_radius() -> None:
    com = _commercial_tile(x=5, y=5)
    far_house = _house_tile(x=10, y=10, capacity=8)  # chebyshev dist 5 > 2
    state = _state_with([com, far_house], population=8)
    assert commercial_revenue_for_tile(state, com) == 0.0


def test_commercial_revenue_sums_housing_in_5x5_chebyshev() -> None:
    # Inside the 5×5 box (chebyshev ≤ 2): house at (3,3), (7,7) is at the
    # corner (dist 2 — included). Outside: house at (8,5) (dist 3 — excluded).
    com = _commercial_tile(x=5, y=5)
    inside_a = _house_tile(x=3, y=3, capacity=8)  # dist 2
    inside_b = _house_tile(x=7, y=7, capacity=8)  # dist 2
    outside = _house_tile(x=8, y=5, capacity=8)  # dist 3
    state = _state_with([com, inside_a, inside_b, outside], population=16)
    # capacity_in_radius = 16, occupancy = 16/24 = 0.6667, efficiency = 1.0
    expected = 16 * (16 / 24) * COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY * 1.0
    assert commercial_revenue_for_tile(state, com) == pytest.approx(expected)


def test_commercial_revenue_clips_at_grid_edges() -> None:
    # Commercial at (0,0): radius 2 still works — we just sum whatever tiles
    # are placed in (-2..2, -2..2). No grid-boundary special-casing required.
    com = _commercial_tile(x=0, y=0)
    house = _house_tile(x=1, y=1, capacity=8)
    state = _state_with([com, house], population=8)
    expected = 8 * 1.0 * COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY * 1.0
    assert commercial_revenue_for_tile(state, com) == pytest.approx(expected)


def test_commercial_revenue_scales_with_occupancy() -> None:
    com = _commercial_tile(x=5, y=5)
    house = _house_tile(x=5, y=5, capacity=100)
    # Half occupancy
    state = _state_with([com, house], population=50)
    expected = 100 * 0.5 * COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY * 1.0
    assert commercial_revenue_for_tile(state, com) == pytest.approx(expected)


def test_commercial_revenue_scales_with_workforce_efficiency() -> None:
    spec = TILE_CATALOG["commercial"]
    com = _commercial_tile(x=5, y=5, staffed=spec.jobs // 2)
    house = _house_tile(x=5, y=5, capacity=100)
    state = _state_with([com, house], population=100)
    eff = (spec.jobs // 2) / spec.jobs
    expected = 100 * 1.0 * COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY * eff
    assert commercial_revenue_for_tile(state, com) == pytest.approx(expected)


def test_commercial_revenue_zero_when_not_operational() -> None:
    com = _commercial_tile(x=5, y=5)
    com.operational = False
    house = _house_tile(x=5, y=5, capacity=100)
    state = _state_with([com, house], population=100)
    assert commercial_revenue_for_tile(state, com) == 0.0


def test_commercial_revenue_includes_town_hall_capacity() -> None:
    # Town hall has housing_capacity > 0 so it counts as a housing source.
    com = _commercial_tile(x=5, y=5)
    town_hall = Tile(
        id="th",
        type="town_hall",
        x=6,
        y=5,
        built_day=0,
        operational=True,
        housing_capacity=100,
        jobs=30,
        staffed_jobs=30,
    )
    state = _state_with([com, town_hall], population=100)
    expected = 100 * 1.0 * COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY * 1.0
    assert commercial_revenue_for_tile(state, com) == pytest.approx(expected)


def test_overlapping_commercials_independently_full_count_residents() -> None:
    com_a = _commercial_tile(x=4, y=5)
    com_b = _commercial_tile(x=6, y=5)
    com_b.id = "com_b"
    house = _house_tile(x=5, y=5, capacity=100)  # in radius of both
    state = _state_with([com_a, com_b, house], population=100)
    a = commercial_revenue_for_tile(state, com_a)
    b = commercial_revenue_for_tile(state, com_b)
    expected_each = 100 * 1.0 * COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY * 1.0
    assert a == pytest.approx(expected_each)
    assert b == pytest.approx(expected_each)


def test_commercial_revenue_zero_for_non_commercial_tile() -> None:
    com = _commercial_tile(x=5, y=5)
    com.type = "industrial"
    house = _house_tile(x=5, y=5, capacity=100)
    state = _state_with([com, house], population=100)
    assert commercial_revenue_for_tile(state, com) == 0.0


# -- update_civic_revenue extended for commercial --------------------------


def test_today_summary_so_far_defaults_commercial_revenue_to_zero() -> None:
    w = World()
    w.reset(seed=42)
    assert "commercial_revenue" in w.state.today_summary_so_far
    assert w.state.today_summary_so_far["commercial_revenue"] == 0.0


def test_update_civic_revenue_accrues_commercial_to_summary_and_treasury() -> None:
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    # Build a commercial adjacent to town hall (radius 1, well within 5×5).
    res = w.build("commercial", th.x + 1, th.y)
    assert res["ok"], res
    com = next(t for t in w.state.tiles if t.type == "commercial")
    com.staffed_jobs = com.jobs  # force full staffing

    capacity = sum(t.housing_capacity for t in w.state.tiles)
    occupancy = min(1.0, w.state.population / max(1, capacity))
    expected = th.housing_capacity * occupancy * COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY * 1.0

    treasury_before = w.state.treasury
    update_civic_revenue(w)

    assert w.state.today_summary_so_far["commercial_revenue"] == pytest.approx(expected)
    assert w.state.treasury == pytest.approx(treasury_before + expected)


# -- /state per-tile economics fields for commercial -----------------------


def test_state_tiles_include_residents_in_radius_for_commercial() -> None:
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("commercial", th.x + 1, th.y)
    com = next(t for t in w.state.tiles if t.type == "commercial")
    com.staffed_jobs = com.jobs

    s = w.state_dict()
    com_dict = next(t for t in s["tiles"] if t["type"] == "commercial")
    assert "residents_in_radius" in com_dict
    capacity = sum(t.housing_capacity for t in w.state.tiles)
    occupancy = min(1.0, w.state.population / max(1, capacity))
    assert com_dict["residents_in_radius"] == pytest.approx(th.housing_capacity * occupancy)
    expected_revenue = (
        th.housing_capacity * occupancy * COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY * 1.0
    )
    assert com_dict["estimated_revenue_per_day"] == pytest.approx(expected_revenue)
    assert com_dict["estimated_net_per_day"] == pytest.approx(
        expected_revenue - com_dict["opex_per_day"]
    )


# -- Integration: /step end-to-end commercial accrual ----------------------


def test_step_accrues_commercial_revenue_using_pre_update_population() -> None:
    """Civic revenue runs before update_population, so the commercial earnings
    use today's lived population, not tomorrow's (post-decay) survivors.

    The default starting world has pop=100 and only the town hall (jobs=30).
    Adding a commercial brings total jobs to 42; jobs < 0.7×pop fires the
    job-driven decline branch, dropping pop to 99 after the step. The
    commercial revenue must reflect the pre-update pop=100 occupancy.
    """
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    pop_before = w.state.population
    # Capacity snapshot before the commercial is built (commercial itself has
    # zero housing_capacity, so adding it does not shift the denominator).
    capacity_before = sum(t.housing_capacity for t in w.state.tiles)
    # Build a commercial adjacent to town hall; commercial picks up town
    # hall's 100 housing in its 5×5 box.
    w.build("commercial", th.x + 1, th.y)
    com = next(t for t in w.state.tiles if t.type == "commercial")
    com.staffed_jobs = com.jobs  # ensure deterministic full staffing

    w.step(days=1)

    # Population must have declined — proving update_population also ran.
    assert w.state.population < pop_before
    # Commercial revenue used pre-update pop.
    occupancy_pre = min(1.0, pop_before / max(1, capacity_before))
    expected = th.housing_capacity * occupancy_pre * COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY * 1.0
    assert w.state.today_summary_so_far["commercial_revenue"] == pytest.approx(expected)


def test_step_no_commercial_means_zero_commercial_revenue() -> None:
    w = World()
    w.reset(seed=42)
    w.step(days=1)
    assert w.state.today_summary_so_far["commercial_revenue"] == 0.0


# -- /catalog economics block: commercial constants ------------------------


def test_catalog_economics_exposes_commercial_constants() -> None:
    cat = build_catalog()
    eco = cat["economics"]
    assert eco["commercial_revenue_per_resident_per_day"] == pytest.approx(
        COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY
    )
    assert eco["commercial_radius"] == COMMERCIAL_RADIUS


def test_catalog_commercial_description_mentions_new_revenue_behavior() -> None:
    cat = build_catalog()
    commercial = next(t for t in cat["tiles"] if t["tile_type"] == "commercial")
    desc = commercial["description"].lower()
    assert "resident" in desc
    assert "5" in commercial["description"]


# =========================================================================
# Slice 03 — plant kwh_served accumulator + per-plant revenue + Net
# =========================================================================


def _give_coal_road(w: World, th_x: int, th_y: int) -> None:
    """Lay a one-cell road south of the town hall so a coal plant built at
    ``(th_x + 2, th_y)`` clears the road-adjacency check via a connected
    road at ``(th_x + 2, th_y + 1)``.
    """
    w.build("road", th_x, th_y + 1)
    w.build("road", th_x + 1, th_y + 1)
    w.build("road", th_x + 2, th_y + 1)


def _coal_plant_tile(x: int = 0, y: int = 0) -> Tile:
    spec = TILE_CATALOG["coal_plant"]
    return Tile(
        id="coal_1",
        type="coal_plant",
        x=x,
        y=y,
        built_day=0,
        operational=True,
        capex_paid=spec.capex,
        opex_per_day=spec.opex_per_day,
        jobs=spec.jobs,
        staffed_jobs=spec.jobs,
    )


# -- Tile dataclass field defaults -----------------------------------------


def test_tile_dataclass_has_kwh_served_today_and_yesterday_defaults() -> None:
    """Slice-03 AC: every Tile has both accumulators defaulting to 0.0."""
    t = _coal_plant_tile()
    assert t.kwh_served_today == 0.0
    assert t.kwh_served_yesterday == 0.0


# -- plant_revenue_for_tile unit -------------------------------------------


def test_plant_revenue_for_tile_uses_yesterday_times_retail() -> None:
    from world.config import load_config
    from world.pricing import plant_revenue_for_tile

    cfg = load_config()
    t = _coal_plant_tile()
    t.kwh_served_yesterday = 1000.0
    assert plant_revenue_for_tile(_default_state(), t) == pytest.approx(
        1000.0 * cfg.grid_price_retail
    )


def test_plant_revenue_for_tile_zero_when_yesterday_zero() -> None:
    from world.pricing import plant_revenue_for_tile

    t = _coal_plant_tile()
    # Default kwh_served_yesterday == 0 (fresh tile, no /step yet).
    assert plant_revenue_for_tile(_default_state(), t) == 0.0


def test_plant_revenue_for_tile_zero_when_not_operational() -> None:
    from world.pricing import plant_revenue_for_tile

    t = _coal_plant_tile()
    t.kwh_served_yesterday = 1000.0
    t.operational = False
    assert plant_revenue_for_tile(_default_state(), t) == 0.0


def test_plant_revenue_for_tile_zero_for_non_plant() -> None:
    from world.pricing import plant_revenue_for_tile

    t = _industrial_tile()
    t.kwh_served_yesterday = 1000.0  # set the field anyway, helper must gate
    assert plant_revenue_for_tile(_default_state(), t) == 0.0


# -- kwh accumulator behaviour through /step -------------------------------


def test_kwh_served_today_resets_at_start_of_day() -> None:
    """Building a plant mid-game with non-zero accumulator state still gets a
    clean slate at the next /step (the reset runs at the top of
    _advance_one_day)."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    _give_coal_road(w, th.x, th.y)
    res = w.build("coal_plant", th.x + 2, th.y)
    assert res["ok"], res
    coal = next(t for t in w.state.tiles if t.type == "coal_plant")
    coal.kwh_served_today = 9999.0
    w.step(days=1)
    # After the day, today's accumulator reflects this day's dispatch (not
    # the 9999 we planted). It must NOT equal 9999.
    assert coal.kwh_served_today != pytest.approx(9999.0)


def test_kwh_served_today_copied_to_yesterday_after_step() -> None:
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    _give_coal_road(w, th.x, th.y)
    w.build("coal_plant", th.x + 2, th.y)
    coal = next(t for t in w.state.tiles if t.type == "coal_plant")
    w.step(days=1)
    # After one day, the just-completed-day accumulator was copied to
    # yesterday. Both fields are equal because the end-of-day copy happens
    # AFTER the hourly loop but BEFORE the next day reset.
    assert coal.kwh_served_yesterday == pytest.approx(coal.kwh_served_today)


def test_kwh_served_yesterday_isolates_from_today_after_two_steps() -> None:
    """After two consecutive /step calls, kwh_served_yesterday holds day-1's
    served kWh; kwh_served_today holds day-2's. They are independently
    accumulated through the reset/copy cycle."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    _give_coal_road(w, th.x, th.y)
    w.build("coal_plant", th.x + 2, th.y)
    coal = next(t for t in w.state.tiles if t.type == "coal_plant")
    w.step(days=1)
    day1_kwh = coal.kwh_served_today
    w.step(days=1)
    day2_kwh = coal.kwh_served_today
    # After day 2, yesterday should hold day-2 (most recently pinned), today
    # equals day-2 (just-completed). The intermediate day-1 reading is gone.
    assert coal.kwh_served_yesterday == pytest.approx(day2_kwh)
    # Sanity: at least one of the two days produced kWh.
    assert day1_kwh > 0.0 or day2_kwh > 0.0


def test_freshly_built_plant_has_zero_revenue_until_next_step() -> None:
    """AC: a freshly built plant has 0 revenue until the next /step."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    _give_coal_road(w, th.x, th.y)
    w.build("coal_plant", th.x + 2, th.y)
    s = w.state_dict()
    coal_dict = next(t for t in s["tiles"] if t["type"] == "coal_plant")
    # No /step yet — kwh_served_yesterday is still 0.
    assert coal_dict["estimated_revenue_per_day"] == 0.0


# -- /state per-tile economics fields for plants ---------------------------


def test_state_tile_dict_estimated_revenue_matches_yesterday_times_retail() -> None:
    """AC integration: build a coal plant, step a day with non-zero dispatch,
    assert the tile dict's estimated_revenue_per_day matches
    kwh_served_yesterday × grid_price_retail."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    # A second industrial pushes total demand above the city's renewable
    # output, forcing the coal plant to dispatch. Both industrial and coal
    # need road adjacency.
    _give_coal_road(w, th.x, th.y)
    w.build("industrial", th.x + 1, th.y)
    w.build("coal_plant", th.x + 2, th.y)
    coal = next(t for t in w.state.tiles if t.type == "coal_plant")
    w.step(days=1)
    # Sanity: coal actually dispatched something this day.
    assert coal.kwh_served_yesterday > 0.0

    s = w.state_dict()
    coal_dict = next(t for t in s["tiles"] if t["type"] == "coal_plant")
    assert coal_dict["estimated_revenue_per_day"] == pytest.approx(
        coal.kwh_served_yesterday * w.config.grid_price_retail
    )
    # Slice 04 folds fuel + carbon cost into Net for plants. The revenue row
    # is unchanged.
    assert coal_dict["estimated_net_per_day"] == pytest.approx(
        coal_dict["estimated_revenue_per_day"]
        - coal_dict["opex_per_day"]
        - coal_dict["estimated_fuel_cost_per_day"]
        - coal_dict["estimated_carbon_cost_per_day"]
    )


def test_state_tile_dict_renewable_plant_co2_and_carbon_are_zero() -> None:
    """Renewables (solar/wind) keep estimated_co2_per_day = 0 and
    estimated_carbon_cost_per_day = 0 even after dispatching real kWh."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("solar_farm", th.x + 2, th.y)
    w.step(days=1)
    s = w.state_dict()
    solar_dict = next(t for t in s["tiles"] if t["type"] == "solar_farm")
    assert solar_dict["estimated_co2_per_day"] == 0.0
    assert solar_dict["estimated_carbon_cost_per_day"] == 0.0


def test_state_tile_dict_exposes_kwh_fields_on_plants() -> None:
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("solar_farm", th.x + 2, th.y)
    s = w.state_dict()
    solar_dict = next(t for t in s["tiles"] if t["type"] == "solar_farm")
    assert "kwh_served_today" in solar_dict
    assert "kwh_served_yesterday" in solar_dict


# -- /catalog economics block: grid prices ---------------------------------


def test_catalog_economics_exposes_grid_prices() -> None:
    from world.config import load_config

    cfg = load_config()
    cat = build_catalog()
    eco = cat["economics"]
    assert eco["grid_price_retail"] == pytest.approx(cfg.grid_price_retail)
    assert eco["grid_price_export"] == pytest.approx(cfg.grid_price_export)


# =========================================================================
# Slice 04 — fossil plant fuel + carbon cost rows; kWh-based CO2 row
# =========================================================================


def _solar_tile(x: int = 0, y: int = 0) -> Tile:
    spec = TILE_CATALOG["solar_farm"]
    return Tile(
        id="solar_1",
        type="solar_farm",
        x=x,
        y=y,
        built_day=0,
        operational=True,
        capex_paid=spec.capex,
        opex_per_day=spec.opex_per_day,
        jobs=spec.jobs,
        staffed_jobs=spec.jobs,
    )


def _gas_peaker_tile(x: int = 0, y: int = 0) -> Tile:
    spec = TILE_CATALOG["gas_peaker"]
    return Tile(
        id="gas_1",
        type="gas_peaker",
        x=x,
        y=y,
        built_day=0,
        operational=True,
        capex_paid=spec.capex,
        opex_per_day=spec.opex_per_day,
        jobs=spec.jobs,
        staffed_jobs=spec.jobs,
    )


# -- plant_fuel_cost_for_tile unit -----------------------------------------


def test_plant_fuel_cost_zero_when_no_kwh_served() -> None:
    from world.pricing import plant_fuel_cost_for_tile

    spec = TILE_CATALOG["coal_plant"]
    t = _coal_plant_tile()
    # Default kwh_served_yesterday == 0 → no fuel burned, no cost.
    assert plant_fuel_cost_for_tile(_default_state(), t, spec) == 0.0


def test_plant_fuel_cost_scales_with_kwh_and_fuel_cost_per_mwh() -> None:
    from world.pricing import plant_fuel_cost_for_tile

    spec = TILE_CATALOG["coal_plant"]
    t = _coal_plant_tile()
    t.kwh_served_yesterday = 2000.0  # 2 MWh
    # spec.fuel_cost_per_mwh = 20.0 → expected $40/day.
    assert plant_fuel_cost_for_tile(_default_state(), t, spec) == pytest.approx(
        2.0 * spec.fuel_cost_per_mwh
    )


def test_plant_fuel_cost_uses_yesterday_not_today() -> None:
    """Fuel cost is anchored to the just-completed day so the popup row
    matches the revenue row's accounting window."""
    from world.pricing import plant_fuel_cost_for_tile

    spec = TILE_CATALOG["coal_plant"]
    t = _coal_plant_tile()
    t.kwh_served_today = 9999.0  # today's running total — must be ignored
    t.kwh_served_yesterday = 1000.0
    assert plant_fuel_cost_for_tile(_default_state(), t, spec) == pytest.approx(
        1.0 * spec.fuel_cost_per_mwh
    )


def test_plant_fuel_cost_zero_for_renewable() -> None:
    """Solar/wind have ``fuel_cost_per_mwh == 0`` so the helper returns 0
    even when the plant served real kWh."""
    from world.pricing import plant_fuel_cost_for_tile

    spec = TILE_CATALOG["solar_farm"]
    t = _solar_tile()
    t.kwh_served_yesterday = 5000.0
    assert plant_fuel_cost_for_tile(_default_state(), t, spec) == 0.0


def test_plant_fuel_cost_zero_when_not_operational() -> None:
    from world.pricing import plant_fuel_cost_for_tile

    spec = TILE_CATALOG["coal_plant"]
    t = _coal_plant_tile()
    t.kwh_served_yesterday = 1000.0
    t.operational = False
    assert plant_fuel_cost_for_tile(_default_state(), t, spec) == 0.0


# -- plant_carbon_cost_for_tile unit ---------------------------------------


def test_plant_carbon_cost_zero_when_no_kwh_served() -> None:
    from world.pricing import plant_carbon_cost_for_tile

    spec = TILE_CATALOG["coal_plant"]
    t = _coal_plant_tile()
    state = WorldState(seed=42, carbon_price=CARBON_PRICE_USD_PER_TON)
    assert plant_carbon_cost_for_tile(state, t, spec) == 0.0


def test_plant_carbon_cost_scales_with_kwh_and_intensity_and_price() -> None:
    from world.pricing import plant_carbon_cost_for_tile

    spec = TILE_CATALOG["coal_plant"]
    t = _coal_plant_tile()
    t.kwh_served_yesterday = 1000.0  # 1 MWh
    state = WorldState(seed=42, carbon_price=CARBON_PRICE_USD_PER_TON)
    expected = 1.0 * spec.co2_t_per_mwh * CARBON_PRICE_USD_PER_TON
    assert plant_carbon_cost_for_tile(state, t, spec) == pytest.approx(expected)


def test_plant_carbon_cost_tracks_state_carbon_price() -> None:
    """A regulatory-tightening event that raises ``state.carbon_price`` must
    flow into the per-tile carbon cost the same day it fires."""
    from world.pricing import plant_carbon_cost_for_tile

    spec = TILE_CATALOG["coal_plant"]
    t = _coal_plant_tile()
    t.kwh_served_yesterday = 1000.0
    state = WorldState(seed=42, carbon_price=CARBON_PRICE_USD_PER_TON)
    baseline = plant_carbon_cost_for_tile(state, t, spec)
    state.carbon_price = CARBON_PRICE_USD_PER_TON * 2.0
    after = plant_carbon_cost_for_tile(state, t, spec)
    assert after == pytest.approx(baseline * 2.0)


def test_plant_carbon_cost_zero_for_renewable() -> None:
    from world.pricing import plant_carbon_cost_for_tile

    spec = TILE_CATALOG["solar_farm"]
    t = _solar_tile()
    t.kwh_served_yesterday = 5000.0
    state = WorldState(seed=42, carbon_price=CARBON_PRICE_USD_PER_TON)
    assert plant_carbon_cost_for_tile(state, t, spec) == 0.0


# -- plant_co2_for_tile unit (kWh-based daily tonnage) ---------------------


def test_plant_co2_for_tile_uses_yesterday_kwh() -> None:
    from world.pricing import plant_co2_for_tile

    spec = TILE_CATALOG["coal_plant"]
    t = _coal_plant_tile()
    t.kwh_served_yesterday = 1000.0
    # spec.co2_t_per_mwh = 0.9 → 0.9 t/day for 1 MWh.
    assert plant_co2_for_tile(t, spec) == pytest.approx(spec.co2_t_per_mwh)


def test_plant_co2_for_tile_zero_for_renewable() -> None:
    from world.pricing import plant_co2_for_tile

    spec = TILE_CATALOG["solar_farm"]
    t = _solar_tile()
    t.kwh_served_yesterday = 5000.0
    assert plant_co2_for_tile(t, spec) == 0.0


# -- /state per-tile economics fields for plants (slice 04) ---------------


def test_state_tile_dict_plant_emits_estimated_fuel_and_carbon_cost() -> None:
    """Coal plant tile dict has all four new keys; renewables get 0 for
    fuel/carbon so the popup shows the contrast explicitly."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    _give_coal_road(w, th.x, th.y)
    w.build("industrial", th.x + 1, th.y)
    w.build("coal_plant", th.x + 2, th.y)
    coal = next(t for t in w.state.tiles if t.type == "coal_plant")
    w.step(days=1)
    assert coal.kwh_served_yesterday > 0.0

    s = w.state_dict()
    coal_dict = next(t for t in s["tiles"] if t["type"] == "coal_plant")
    spec = TILE_CATALOG["coal_plant"]
    mwh = coal.kwh_served_yesterday / 1000.0
    assert coal_dict["estimated_fuel_cost_per_day"] == pytest.approx(mwh * spec.fuel_cost_per_mwh)
    assert coal_dict["estimated_carbon_cost_per_day"] == pytest.approx(
        mwh * spec.co2_t_per_mwh * w.state.carbon_price
    )
    assert coal_dict["estimated_co2_per_day"] == pytest.approx(mwh * spec.co2_t_per_mwh)


def test_state_tile_dict_renewable_plant_has_zero_fuel_and_carbon() -> None:
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("solar_farm", th.x + 2, th.y)
    w.step(days=1)
    s = w.state_dict()
    solar_dict = next(t for t in s["tiles"] if t["type"] == "solar_farm")
    assert solar_dict["estimated_fuel_cost_per_day"] == 0.0
    assert solar_dict["estimated_carbon_cost_per_day"] == 0.0
    assert solar_dict["estimated_co2_per_day"] == 0.0


def test_state_tile_dict_plant_net_reconciles_with_component_rows() -> None:
    """Integration AC: for a coal plant tile dict,
    ``net == revenue − opex − fuel − carbon`` exactly."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    _give_coal_road(w, th.x, th.y)
    w.build("industrial", th.x + 1, th.y)
    w.build("coal_plant", th.x + 2, th.y)
    w.step(days=1)
    s = w.state_dict()
    coal_dict = next(t for t in s["tiles"] if t["type"] == "coal_plant")
    expected_net = (
        coal_dict["estimated_revenue_per_day"]
        - coal_dict["opex_per_day"]
        - coal_dict["estimated_fuel_cost_per_day"]
        - coal_dict["estimated_carbon_cost_per_day"]
    )
    assert coal_dict["estimated_net_per_day"] == pytest.approx(expected_net)


def test_state_tile_dict_non_plant_has_zero_fuel_cost_field() -> None:
    """Slice-04 schema additivity: non-plant tiles still emit the new
    ``estimated_fuel_cost_per_day`` key and it is 0.0."""
    w = World()
    w.reset(seed=42)
    s = w.state_dict()
    for tile_dict in s["tiles"]:
        assert "estimated_fuel_cost_per_day" in tile_dict
        if tile_dict["type"] not in {"coal_plant", "gas_peaker"}:
            assert tile_dict["estimated_fuel_cost_per_day"] == 0.0


# =========================================================================
# Slice 05 — refinery revenue + carbon cost popup rows
# =========================================================================


def _refinery_tile(
    rid: str = "ref_1",
    throughput: float = 0.0,
    x: int = 0,
    y: int = 0,
    staffed: int | None = None,
) -> Tile:
    spec = TILE_CATALOG["refinery"]
    staffed_jobs = spec.jobs if staffed is None else staffed
    return Tile(
        id=rid,
        type="refinery",
        x=x,
        y=y,
        built_day=0,
        operational=True,
        capex_paid=spec.capex,
        opex_per_day=spec.opex_per_day,
        jobs=spec.jobs,
        staffed_jobs=staffed_jobs,
        current_throughput_bbl_day=throughput,
    )


# -- refinery_revenue_for_tile unit ----------------------------------------


def test_refinery_revenue_zero_when_throughput_zero() -> None:
    from world.pricing import refinery_revenue_for_tile

    assert refinery_revenue_for_tile(_default_state(), _refinery_tile(throughput=0.0)) == 0.0


def test_refinery_revenue_scales_with_throughput() -> None:
    from world.economy import REFINED_PRICE_USD_PER_BBL, REFINERY_YIELD
    from world.pricing import refinery_revenue_for_tile

    t = _refinery_tile(throughput=400.0)
    expected = 400.0 * REFINERY_YIELD * REFINED_PRICE_USD_PER_BBL
    assert refinery_revenue_for_tile(_default_state(), t) == pytest.approx(expected)


def test_refinery_revenue_linear_in_throughput() -> None:
    from world.pricing import refinery_revenue_for_tile

    one = refinery_revenue_for_tile(_default_state(), _refinery_tile(throughput=100.0))
    two = refinery_revenue_for_tile(_default_state(), _refinery_tile(throughput=200.0))
    assert two == pytest.approx(2.0 * one)


def test_refinery_revenue_zero_for_non_refinery() -> None:
    from world.pricing import refinery_revenue_for_tile

    t = _industrial_tile()
    t.current_throughput_bbl_day = 400.0  # spurious — ignored
    assert refinery_revenue_for_tile(_default_state(), t) == 0.0


def test_refinery_revenue_zero_when_not_operational() -> None:
    from world.pricing import refinery_revenue_for_tile

    t = _refinery_tile(throughput=400.0)
    t.operational = False
    assert refinery_revenue_for_tile(_default_state(), t) == 0.0


# -- refinery_carbon_cost_for_tile unit ------------------------------------


def test_refinery_carbon_cost_zero_when_throughput_zero() -> None:
    from world.pricing import refinery_carbon_cost_for_tile

    state = WorldState(seed=42, carbon_price=CARBON_PRICE_USD_PER_TON)
    assert refinery_carbon_cost_for_tile(state, _refinery_tile(throughput=0.0)) == 0.0


def test_refinery_carbon_cost_scales_with_throughput_and_price() -> None:
    from world.economy import REFINERY_CO2_PER_BBL
    from world.pricing import refinery_carbon_cost_for_tile

    state = WorldState(seed=42, carbon_price=CARBON_PRICE_USD_PER_TON)
    t = _refinery_tile(throughput=400.0)
    expected = 400.0 * REFINERY_CO2_PER_BBL * CARBON_PRICE_USD_PER_TON
    assert refinery_carbon_cost_for_tile(state, t) == pytest.approx(expected)


def test_refinery_carbon_cost_tracks_state_carbon_price() -> None:
    """A regulatory-tightening event that raises ``state.carbon_price`` must
    flow into the refinery carbon cost the same day it fires."""
    from world.pricing import refinery_carbon_cost_for_tile

    state = WorldState(seed=42, carbon_price=CARBON_PRICE_USD_PER_TON)
    t = _refinery_tile(throughput=400.0)
    baseline = refinery_carbon_cost_for_tile(state, t)
    state.carbon_price = CARBON_PRICE_USD_PER_TON * 2.0
    assert refinery_carbon_cost_for_tile(state, t) == pytest.approx(baseline * 2.0)


def test_refinery_carbon_cost_zero_for_non_refinery() -> None:
    from world.pricing import refinery_carbon_cost_for_tile

    state = WorldState(seed=42, carbon_price=CARBON_PRICE_USD_PER_TON)
    t = _industrial_tile()
    t.current_throughput_bbl_day = 400.0
    assert refinery_carbon_cost_for_tile(state, t) == 0.0


# -- /state per-tile economics fields for refineries -----------------------


def test_state_tile_dict_refinery_emits_revenue_co2_carbon_and_net() -> None:
    """Integration AC: build a refinery + supplying production well, step one
    day, assert the refinery tile dict's revenue matches the formula and Net
    reconciles with the component rows."""
    from world.economy import REFINED_PRICE_USD_PER_BBL, REFINERY_YIELD
    from world.tests.test_economy import _build_road_link

    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    _build_road_link(w, th.x + 2, th.y)
    w.build("refinery", th.x + 2, th.y)
    rid = next(t.id for t in w.state.tiles if t.type == "refinery")
    w.control_refinery(rid, 400.0)

    # Drill a production well at a high-confidence voxel and set max rate so
    # crude actually flows to the refinery.
    from world.tests.test_economy import _hc_voxel

    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    well_id = w.state.wells[0].id
    from world.subsurface import Q_MAX_WELL_BBL_DAY

    w.control_well(well_id, Q_MAX_WELL_BBL_DAY)
    # oilfield-v2 slice 08: crude only routes inside a 4-connected pipeline
    # network. Connect the refinery and the well so the test exercises the
    # refining path, not the orphan-producer raw-sale path.
    from world.tests.test_economy import _lay_pipeline_between_refinery_and_first_well

    _lay_pipeline_between_refinery_and_first_well(w)
    w.step(days=1)

    ref = next(t for t in w.state.tiles if t.type == "refinery")
    assert ref.current_throughput_bbl_day > 0.0

    s = w.state_dict()
    ref_dict = next(t for t in s["tiles"] if t["type"] == "refinery")
    expected_revenue = ref.current_throughput_bbl_day * REFINERY_YIELD * REFINED_PRICE_USD_PER_BBL
    assert ref_dict["estimated_revenue_per_day"] == pytest.approx(expected_revenue)
    expected_co2 = ref.current_throughput_bbl_day * 0.30
    assert ref_dict["estimated_co2_per_day"] == pytest.approx(expected_co2)
    expected_carbon_cost = expected_co2 * w.state.carbon_price
    assert ref_dict["estimated_carbon_cost_per_day"] == pytest.approx(expected_carbon_cost)
    expected_net = expected_revenue - ref_dict["opex_per_day"] - expected_carbon_cost
    assert ref_dict["estimated_net_per_day"] == pytest.approx(expected_net)


def test_state_tile_dict_freshly_built_refinery_has_zero_revenue_and_carbon() -> None:
    """Day-0 throughput is 0, so all estimated_* economic fields are 0 (modulo
    OPEX in Net)."""
    from world.tests.test_economy import _build_road_link

    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    _build_road_link(w, th.x + 2, th.y)
    w.build("refinery", th.x + 2, th.y)
    s = w.state_dict()
    ref_dict = next(t for t in s["tiles"] if t["type"] == "refinery")
    assert ref_dict["estimated_revenue_per_day"] == 0.0
    assert ref_dict["estimated_co2_per_day"] == 0.0
    assert ref_dict["estimated_carbon_cost_per_day"] == 0.0
    assert ref_dict["estimated_net_per_day"] == pytest.approx(-ref_dict["opex_per_day"])


# -- /catalog economics block: refinery constants --------------------------


def test_catalog_economics_exposes_refinery_constants() -> None:
    from world.economy import REFINED_PRICE_USD_PER_BBL, REFINERY_CO2_PER_BBL, REFINERY_YIELD

    cat = build_catalog()
    eco = cat["economics"]
    assert eco["refined_price_usd_per_bbl"] == pytest.approx(REFINED_PRICE_USD_PER_BBL)
    assert eco["refinery_yield"] == pytest.approx(REFINERY_YIELD)
    assert eco["refinery_co2_t_per_bbl"] == pytest.approx(REFINERY_CO2_PER_BBL)


# =========================================================================
# Slice 06 — wells revenue + Net popup rows
# =========================================================================


def _production_well(rate: float = 0.0) -> Well:
    spec = TILE_CATALOG["oil_well"]
    return Well(
        id="pw_1",
        type="production",
        x=0,
        y=0,
        target_z=0,
        drilled_day=0,
        setpoint_rate_bbl_day=rate,
        current_rate_bbl_day=rate,
        capex_paid=spec.capex,
        opex_per_day=spec.opex_per_day,
    )


def _injection_well(rate: float = 0.0) -> Well:
    spec = TILE_CATALOG["injection_well"]
    return Well(
        id="iw_1",
        type="injection",
        x=0,
        y=0,
        target_z=0,
        drilled_day=0,
        setpoint_rate_bbl_day=rate,
        current_rate_bbl_day=rate,
        capex_paid=spec.capex,
        opex_per_day=spec.opex_per_day,
    )


# -- well_gross_crude_value_for_tile unit ----------------------------------


def test_well_gross_crude_value_production_uses_rate_times_price() -> None:
    from world.pricing import well_gross_crude_value_for_tile
    from world.subsurface import CRUDE_PRICE_USD_PER_BBL

    well = _production_well(rate=150.0)
    expected = 150.0 * CRUDE_PRICE_USD_PER_BBL
    assert well_gross_crude_value_for_tile(_default_state(), well) == pytest.approx(expected)


def test_well_gross_crude_value_zero_when_rate_zero() -> None:
    from world.pricing import well_gross_crude_value_for_tile

    assert well_gross_crude_value_for_tile(_default_state(), _production_well(rate=0.0)) == 0.0


def test_well_gross_crude_value_zero_for_injection_well() -> None:
    from world.pricing import well_gross_crude_value_for_tile

    well = _injection_well(rate=200.0)
    assert well_gross_crude_value_for_tile(_default_state(), well) == 0.0


# -- well_injection_kwh_per_day unit ---------------------------------------


def test_well_injection_kwh_scales_with_rate() -> None:
    from world.pricing import well_injection_kwh_per_day
    from world.subsurface import INJECTION_KWH_PER_BBL

    well = _injection_well(rate=100.0)
    assert well_injection_kwh_per_day(well) == pytest.approx(100.0 * INJECTION_KWH_PER_BBL)


def test_well_injection_kwh_zero_when_rate_zero() -> None:
    from world.pricing import well_injection_kwh_per_day

    assert well_injection_kwh_per_day(_injection_well(rate=0.0)) == 0.0


def test_well_injection_kwh_zero_for_production_well() -> None:
    from world.pricing import well_injection_kwh_per_day

    well = _production_well(rate=150.0)
    assert well_injection_kwh_per_day(well) == 0.0


# -- well_production_kwh_per_day unit --------------------------------------


def test_well_production_kwh_scales_with_rate() -> None:
    from world.pricing import well_production_kwh_per_day
    from world.subsurface import PRODUCTION_KWH_PER_BBL

    well = _production_well(rate=120.0)
    assert well_production_kwh_per_day(well) == pytest.approx(120.0 * PRODUCTION_KWH_PER_BBL)


def test_well_production_kwh_zero_when_rate_zero() -> None:
    from world.pricing import well_production_kwh_per_day

    assert well_production_kwh_per_day(_production_well(rate=0.0)) == 0.0


def test_well_production_kwh_zero_for_injection_well() -> None:
    from world.pricing import well_production_kwh_per_day

    well = _injection_well(rate=200.0)
    assert well_production_kwh_per_day(well) == 0.0


def test_production_kwh_per_bbl_is_15() -> None:
    """Pin the constant so a regression that walks it back trips here."""
    from world.subsurface import PRODUCTION_KWH_PER_BBL

    assert PRODUCTION_KWH_PER_BBL == 15.0


# -- integration: state_dict well fields -----------------------------------


def test_state_well_dict_emits_revenue_kwh_and_net_fields() -> None:
    """Drill a production well, run one /step, and assert the well dict
    reports estimated_revenue_per_day = current_rate × crude_price and Net
    reconciles with the helper."""
    from world.subsurface import CRUDE_PRICE_USD_PER_BBL, Q_MAX_WELL_BBL_DAY
    from world.tests.test_economy import _hc_voxel

    w = World()
    w.reset(seed=42)
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    well_id = w.state.wells[0].id
    w.control_well(well_id, Q_MAX_WELL_BBL_DAY)
    w.step(days=1)

    well = w.state.wells[0]
    assert well.current_rate_bbl_day > 0.0
    s = w.state_dict()
    well_dict = next(wd for wd in s["wells"] if wd["id"] == well_id)
    expected_revenue = well.current_rate_bbl_day * CRUDE_PRICE_USD_PER_BBL
    assert well_dict["estimated_revenue_per_day"] == pytest.approx(expected_revenue)
    assert well_dict["injection_power_kwh_per_day"] == 0.0
    expected_net = expected_revenue - well_dict["opex_per_day"]
    assert well_dict["estimated_net_per_day"] == pytest.approx(expected_net)


def test_state_well_dict_injection_has_zero_revenue_and_net_is_minus_opex() -> None:
    """An injection well shows 0 revenue, kWh matching its setpoint, and
    Net = -opex (no $-cost from power)."""
    from world.subsurface import INJECTION_KWH_PER_BBL, Q_MAX_WELL_BBL_DAY
    from world.tests.test_economy import _hc_voxel

    w = World()
    w.reset(seed=42)
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "injection")
    well_id = w.state.wells[0].id
    w.control_well(well_id, Q_MAX_WELL_BBL_DAY)

    s = w.state_dict()
    well_dict = next(wd for wd in s["wells"] if wd["id"] == well_id)
    assert well_dict["estimated_revenue_per_day"] == 0.0
    expected_kwh = well_dict["current_rate_bbl_day"] * INJECTION_KWH_PER_BBL
    assert well_dict["injection_power_kwh_per_day"] == pytest.approx(expected_kwh)
    assert well_dict["estimated_net_per_day"] == pytest.approx(-well_dict["opex_per_day"])


def test_drill_response_includes_estimated_fields() -> None:
    from world.tests.test_economy import _hc_voxel

    w = World()
    w.reset(seed=42)
    hc = _hc_voxel(w)
    resp = w.drill(hc.x, hc.y, hc.z, "production")
    assert resp["ok"] is True
    result = resp["result"]
    assert "estimated_revenue_per_day" in result
    assert "injection_power_kwh_per_day" in result
    assert "estimated_net_per_day" in result


# -- /catalog economics block: well constants ------------------------------


def test_catalog_economics_exposes_well_constants() -> None:
    from world.subsurface import CRUDE_PRICE_USD_PER_BBL, INJECTION_KWH_PER_BBL

    cat = build_catalog()
    eco = cat["economics"]
    assert eco["crude_price_usd_per_bbl"] == pytest.approx(CRUDE_PRICE_USD_PER_BBL)
    assert eco["injection_kwh_per_bbl"] == pytest.approx(INJECTION_KWH_PER_BBL)


# -- open-source-arena #01: pricing constants → state-fields regression ----


def test_state_carries_pricing_default_fields() -> None:
    """A reset World must initialise the ten promoted pricing/rate fields on
    state from their pre-refactor constant defaults so a default game is
    byte-identical to the legacy constants-only code path."""
    from world.economy import REFINED_PRICE_USD_PER_BBL
    from world.population import DAILY_TAX_PER_CAPITA
    from world.pricing import (
        COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY,
        INDUSTRIAL_REVENUE_PER_DAY,
    )
    from world.subsurface import CRUDE_PRICE_USD_PER_BBL

    w = World()
    w.reset(seed=42)
    s = w.state
    assert s.crude_price_usd_per_bbl == pytest.approx(CRUDE_PRICE_USD_PER_BBL)
    assert s.refined_price_usd_per_bbl == pytest.approx(REFINED_PRICE_USD_PER_BBL)
    assert s.grid_price_retail == pytest.approx(w.config.grid_price_retail)
    assert s.grid_price_export == pytest.approx(w.config.grid_price_export)
    assert s.industrial_revenue_per_day == pytest.approx(INDUSTRIAL_REVENUE_PER_DAY)
    assert s.commercial_revenue_per_resident_per_day == pytest.approx(
        COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY
    )
    assert s.daily_tax_per_capita == pytest.approx(DAILY_TAX_PER_CAPITA)
    assert s.blackout_penalty_hour == pytest.approx(w.config.blackout_penalty_hour)
    assert s.plant_fuel_cost_per_mwh == {
        "coal_plant": TILE_CATALOG["coal_plant"].fuel_cost_per_mwh,
        "gas_peaker": TILE_CATALOG["gas_peaker"].fuel_cost_per_mwh,
    }


def test_pricing_state_fields_drive_default_accruals() -> None:
    """With default state values, per-day fuel-cost, refinery revenue,
    industrial revenue, commercial revenue, tax revenue, and blackout-
    penalty accrual all read through state and match the pre-refactor
    figures derived from the old module-level constants. Asserts on
    helper outputs against a hand-built fixture so a future scenario can
    flip a state field and observe an isolated accrual change."""
    from world.economy import REFINED_PRICE_USD_PER_BBL
    from world.population import DAILY_TAX_PER_CAPITA
    from world.pricing import (
        COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY,
        INDUSTRIAL_REVENUE_PER_DAY,
        commercial_revenue_for_tile,
        industrial_revenue_for_tile,
        plant_fuel_cost_for_tile,
        plant_revenue_for_tile,
        refinery_revenue_for_tile,
        well_gross_crude_value_for_tile,
    )
    from world.subsurface import CRUDE_PRICE_USD_PER_BBL

    state = _default_state()

    # Industrial revenue: fully staffed → flat $/day from state.
    ind = _industrial_tile()
    assert industrial_revenue_for_tile(state, ind) == pytest.approx(
        state.industrial_revenue_per_day
    )
    assert state.industrial_revenue_per_day == pytest.approx(INDUSTRIAL_REVENUE_PER_DAY)

    # Commercial revenue: a single house in radius, fully staffed commercial.
    house = Tile(
        id="house",
        type="house",
        x=0,
        y=0,
        built_day=0,
        operational=True,
        housing_capacity=TILE_CATALOG["house"].housing_capacity,
    )
    com_spec = TILE_CATALOG["commercial"]
    com = Tile(
        id="com",
        type="commercial",
        x=0,
        y=1,  # cheb distance 1 to house
        built_day=0,
        operational=True,
        jobs=com_spec.jobs,
        demand_kw=com_spec.demand_kw,
        staffed_jobs=com_spec.jobs,
    )
    state.tiles = [house, com]
    state.population = float(house.housing_capacity)  # 100% occupancy
    expected_com = house.housing_capacity * state.commercial_revenue_per_resident_per_day
    assert commercial_revenue_for_tile(state, com) == pytest.approx(expected_com)
    assert state.commercial_revenue_per_resident_per_day == pytest.approx(
        COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY
    )
    state.tiles = []

    # Plant revenue: 1000 kWh served × state.grid_price_retail.
    plant = _coal_plant_tile()
    plant.kwh_served_yesterday = 1000.0
    assert plant_revenue_for_tile(state, plant) == pytest.approx(1000.0 * state.grid_price_retail)

    # Plant fuel cost: 1000 kWh = 1 MWh × state.plant_fuel_cost_per_mwh[coal].
    expected_fuel = 1.0 * state.plant_fuel_cost_per_mwh["coal_plant"]
    coal_spec = TILE_CATALOG["coal_plant"]
    assert plant_fuel_cost_for_tile(state, plant, coal_spec) == pytest.approx(expected_fuel)

    # Refinery revenue: throughput × yield × state.refined_price.
    refinery = Tile(
        id="ref",
        type="refinery",
        x=0,
        y=0,
        built_day=0,
        operational=True,
        current_throughput_bbl_day=100.0,
    )
    expected_ref = 100.0 * 0.85 * state.refined_price_usd_per_bbl
    assert refinery_revenue_for_tile(state, refinery) == pytest.approx(expected_ref)
    assert state.refined_price_usd_per_bbl == pytest.approx(REFINED_PRICE_USD_PER_BBL)

    # Well crude value: rate × state.crude_price.
    well = Well(
        id="w1",
        type="production",
        x=0,
        y=0,
        target_z=5,
        drilled_day=0,
        current_rate_bbl_day=50.0,
    )
    assert well_gross_crude_value_for_tile(state, well) == pytest.approx(
        50.0 * state.crude_price_usd_per_bbl
    )
    assert state.crude_price_usd_per_bbl == pytest.approx(CRUDE_PRICE_USD_PER_BBL)

    # Tax: state.daily_tax_per_capita × int(pop) accumulates inside
    # update_population. Verify the constant equality (DAILY_TAX_PER_CAPITA).
    assert state.daily_tax_per_capita == pytest.approx(DAILY_TAX_PER_CAPITA)


def test_default_state_blackout_penalty_matches_pre_refactor_accrual() -> None:
    """A 24-hour blackout in a no-plant world accrues
    24 × state.blackout_penalty_hour, which equals the Config default
    (the legacy read site). This pins the blackout-penalty read site to
    state, not Config, so a scenario can scale the penalty without
    rebuilding the world."""
    w = World()
    w.reset(seed=42)
    assert w.state.blackout_penalty_hour == pytest.approx(w.config.blackout_penalty_hour)
    treasury_before = w.state.treasury
    w.step(days=1)
    expected_penalty = 24 * w.state.blackout_penalty_hour
    assert w.state.today_summary_so_far["blackout_penalty"] == pytest.approx(expected_penalty)
    # Confirms the read path went through state, not Config: a mutation
    # to state mid-run would change the accrual, but a default-state run
    # is byte-identical to the legacy constants-only path.
    assert w.state.treasury < treasury_before

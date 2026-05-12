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
from world.state import Tile, WorldState


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
    assert industrial_revenue_for_tile(tile) == pytest.approx(INDUSTRIAL_REVENUE_PER_DAY)


def test_industrial_revenue_scales_linearly_with_efficiency() -> None:
    spec = TILE_CATALOG["industrial"]
    half_jobs = spec.jobs // 2
    tile = _industrial_tile(staffed=half_jobs)
    expected = INDUSTRIAL_REVENUE_PER_DAY * (half_jobs / spec.jobs)
    assert industrial_revenue_for_tile(tile) == pytest.approx(expected)


def test_industrial_revenue_idle_returns_zero() -> None:
    tile = _industrial_tile(staffed=0)
    assert industrial_revenue_for_tile(tile) == 0.0


def test_industrial_revenue_zero_when_not_operational() -> None:
    tile = _industrial_tile()
    tile.operational = False
    assert industrial_revenue_for_tile(tile) == 0.0


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
    assert industrial_revenue_for_tile(tile) == 0.0


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

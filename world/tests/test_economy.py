"""Refinery + crude routing (slice 09, brief §4.6).

Covers the refining yield (0.85), crude routing priority (descending
setpoint, id-ascending tiebreak), single-refinery throughput limit,
surplus-crude direct sale, and the no-double-billing contract on
refinery process load (it counts toward dispatch demand but earns no
retail revenue).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from world.api import create_app
from world.catalog import build_catalog
from world.economy import (
    CARBON_PRICE_USD_PER_TON,
    COAL_CO2_T_PER_MWH,
    GAS_CO2_T_PER_MWH,
    INDUSTRIAL_PROCESS_CO2_T_PER_DAY,
    REFINED_PRICE_USD_PER_BBL,
    REFINERY_CO2_PER_BBL,
    REFINERY_KWH_PER_BBL,
    REFINERY_MAX_BBL_DAY,
    REFINERY_YIELD,
    daily_emissions_t,
    refine_one,
    refinery_process_kw,
    route_crude,
)
from world.sim import World
from world.state import Tile, Well
from world.subsurface import CRUDE_PRICE_USD_PER_BBL, Q_MAX_WELL_BBL_DAY, Voxel


def _hc_voxel(world: World) -> Voxel:
    return next(iter(world.subsurface.voxels.values()))


def _refinery_tile(rid: str, setpoint: float) -> Tile:
    from world.catalog import TILE_CATALOG

    spec = TILE_CATALOG["refinery"]
    return Tile(
        id=rid,
        type="refinery",
        x=0,
        y=0,
        built_day=0,
        operational=True,
        jobs=spec.jobs,
        staffed_jobs=spec.jobs,
        setpoint_rate_bbl_day=setpoint,
    )


def _lay_pipeline_path(world: World, sx: int, sy: int, ex: int, ey: int) -> None:
    """oilfield-v2 slice 08: lay an L-shaped pipeline path from (sx, sy) to
    (ex, ey), inclusive on both ends.

    Skips any cell already occupied so callers can paint over road tiles or
    existing pipeline tiles without raising `tile_occupied`.
    """
    cx, cy = sx, sy
    while True:
        if world._tile_at(cx, cy) is None:
            world.build("pipeline", cx, cy)
        if (cx, cy) == (ex, ey):
            return
        if cx != ex:
            cx += 1 if cx < ex else -1
        elif cy != ey:
            cy += 1 if cy < ey else -1


def _lay_pipeline_between_refinery_and_first_well(world: World) -> None:
    """Convenience wrapper: connect the only refinery to the only well via an
    L-shaped pipeline run."""
    ref = next(t for t in world.state.tiles if t.type == "refinery")
    well = world.state.wells[0]
    _lay_pipeline_path(world, ref.x + 1, ref.y, well.x - 1, well.y)


def _build_road_link(world: World, x: int, y: int) -> None:
    """Drop a road tile bridging town hall to (x, y) so a refinery built
    nearby has road adjacency."""
    th = next(t for t in world.state.tiles if t.type == "town_hall")
    # Walk from town_hall to (x, y) along x then along y, dropping roads
    # everywhere except the destination cell.
    cx, cy = th.x, th.y
    while cx != x:
        cx += 1 if cx < x else -1
        if (cx, cy) == (x, y):
            return
        world.build("road", cx, cy)
    while cy != y:
        cy += 1 if cy < y else -1
        if (cx, cy) == (x, y):
            return
        world.build("road", cx, cy)


# -- refine_one (yield + caps) ---------------------------------------------


def test_refine_one_yield_is_85_percent():
    actual, refined = refine_one(setpoint_rate_bbl_day=200, available_crude_bbl=200)
    assert actual == 200
    assert refined == pytest.approx(200 * REFINERY_YIELD)


def test_refine_one_capped_at_max_throughput():
    """Setpoint above the cap is silently bounded by REFINERY_MAX_BBL_DAY."""
    actual, refined = refine_one(setpoint_rate_bbl_day=999, available_crude_bbl=1_000)
    assert actual == REFINERY_MAX_BBL_DAY
    assert refined == pytest.approx(REFINERY_MAX_BBL_DAY * REFINERY_YIELD)


def test_refine_one_capped_at_available_crude():
    """If crude runs short, actual = available_crude (refined yield applies)."""
    actual, refined = refine_one(setpoint_rate_bbl_day=400, available_crude_bbl=120)
    assert actual == 120
    assert refined == pytest.approx(120 * REFINERY_YIELD)


def test_refine_one_zero_setpoint_zero_actual():
    actual, refined = refine_one(setpoint_rate_bbl_day=0, available_crude_bbl=500)
    assert actual == 0.0
    assert refined == 0.0


# -- route_crude (priority + tiebreak) -------------------------------------


def test_route_crude_higher_setpoint_first():
    big = _refinery_tile("ref-1", setpoint=200)
    small = _refinery_tile("ref-2", setpoint=50)
    # Total crude = 225 → big takes 200, small takes 25.
    actual = route_crude([small, big], total_crude_bbl=225)
    assert actual["ref-1"] == 200
    assert actual["ref-2"] == 25


def test_route_crude_id_ascending_tiebreak():
    """Two refineries with the same setpoint: lower-id wins crude first."""
    a = _refinery_tile("refinery-1", setpoint=200)
    b = _refinery_tile("refinery-2", setpoint=200)
    # Total crude = 250 → -1 takes 200, -2 takes 50.
    actual = route_crude([b, a], total_crude_bbl=250)
    assert actual["refinery-1"] == 200
    assert actual["refinery-2"] == 50


def test_route_crude_surplus_unallocated_when_setpoints_satisfied():
    """When total_crude exceeds Σ effective setpoints, the leftover stays
    unallocated — the caller treats that as crude_direct."""
    a = _refinery_tile("ref-1", setpoint=100)
    b = _refinery_tile("ref-2", setpoint=100)
    actual = route_crude([a, b], total_crude_bbl=500)
    assert sum(actual.values()) == 200
    # Surplus = 300 → caller will sell as crude_direct at $40/bbl.


def test_route_crude_no_refineries_returns_empty():
    actual = route_crude([], total_crude_bbl=500)
    assert actual == {}


def test_route_crude_caps_per_refinery_at_max():
    """Even with infinite crude, a single refinery never refines more than
    REFINERY_MAX_BBL_DAY."""
    big = _refinery_tile("ref-1", setpoint=999)
    actual = route_crude([big], total_crude_bbl=10_000)
    assert actual["ref-1"] == REFINERY_MAX_BBL_DAY


# -- route_crude × workforce efficiency (slice 06) ------------------------


def _half_staffed_refinery(rid: str, setpoint: float) -> Tile:
    """Refinery at staffed_jobs=12 (jobs=25 → efficiency=0.48)."""
    r = _refinery_tile(rid, setpoint=setpoint)
    r.staffed_jobs = 12
    return r


def _idle_refinery(rid: str, setpoint: float) -> Tile:
    r = _refinery_tile(rid, setpoint=setpoint)
    r.staffed_jobs = 0
    return r


def test_route_crude_half_staffed_caps_at_efficiency_scaled_max():
    """staffed_jobs=12 / jobs=25 → eff=0.48, cap = 250 × 0.48 = 120."""
    r = _half_staffed_refinery("ref-1", setpoint=999)
    actual = route_crude([r], total_crude_bbl=1000)
    assert actual["ref-1"] == pytest.approx(REFINERY_MAX_BBL_DAY * (12 / 25))


def test_route_crude_idle_refinery_takes_zero():
    """staffed_jobs=0 → cap=0 → 0 bbl routed. Crude stays unallocated."""
    r = _idle_refinery("ref-1", setpoint=500)
    actual = route_crude([r], total_crude_bbl=1000)
    assert actual["ref-1"] == 0.0


def test_route_crude_full_then_half_staffed_with_constrained_crude():
    """Two refineries with the same setpoint, ordered by id ascending.
    A (full, cap=250) gets first crack; B (half, cap=120) absorbs the rest."""
    a = _refinery_tile("ref-1", setpoint=250)  # staffed full by helper
    b = _half_staffed_refinery("ref-2", setpoint=250)
    actual = route_crude([b, a], total_crude_bbl=300)
    assert actual["ref-1"] == 250
    assert actual["ref-2"] == 50  # 300 - 250 = 50, well under B's 120 cap


def test_route_crude_half_staffed_cap_overrides_setpoint():
    """Setpoint=500 but staffed_jobs=12 (cap=120): actual is the cap, not
    the setpoint. The player-set setpoint stays at 500 (not auto-clamped)."""
    r = _half_staffed_refinery("ref-1", setpoint=500)
    actual = route_crude([r], total_crude_bbl=1000)
    assert actual["ref-1"] == pytest.approx(120.0)
    assert r.setpoint_rate_bbl_day == 500.0  # unchanged by routing


# -- refinery_process_kw (hourly load) -------------------------------------


def test_refinery_process_kw_hourly_load():
    """Hourly load = throughput × 200 / 24."""
    assert refinery_process_kw(120) == pytest.approx(120 * REFINERY_KWH_PER_BBL / 24.0)


def test_refinery_process_kw_zero_throughput_zero_load():
    assert refinery_process_kw(0.0) == 0.0


# -- catalog ---------------------------------------------------------------


def test_catalog_exposes_refinery_spec():
    cat = build_catalog()
    refinery = next(t for t in cat["tiles"] if t["tile_type"] == "refinery")
    assert refinery["capex"] == 150_000
    assert refinery["opex_per_day"] == 300
    assert refinery["requires_road"] is True
    assert refinery["jobs"] == 25
    assert refinery["buildable"] is True


# -- /build refinery -------------------------------------------------------


def test_build_refinery_deducts_capex_with_road_adjacency():
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    treasury_before = w.state.treasury
    res = w.build("refinery", th.x + 1, th.y)  # adjacent to town_hall (counts as road)
    assert res["ok"] is True
    assert w.state.treasury == treasury_before - 150_000


def test_build_refinery_rejects_no_road_adjacency():
    w = World()
    w.reset(seed=42)
    res = w.build("refinery", 0, 0)
    assert res["ok"] is False
    assert res["error"] == "no_road_adjacency"


def test_build_refinery_rejects_insufficient_funds():
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.state.treasury = 100.0
    res = w.build("refinery", th.x + 1, th.y)
    assert res["ok"] is False
    assert res["error"] == "insufficient_funds"


# -- /control/refinery -----------------------------------------------------


def test_control_refinery_clamps_setpoint_above_max():
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("refinery", th.x + 1, th.y)
    rid = next(t.id for t in w.state.tiles if t.type == "refinery")
    res = w.control_refinery(rid, 999.0)
    assert res["ok"] is True
    assert res["result"]["setpoint_rate_bbl_day"] == REFINERY_MAX_BBL_DAY


def test_control_refinery_clamps_setpoint_below_zero():
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("refinery", th.x + 1, th.y)
    rid = next(t.id for t in w.state.tiles if t.type == "refinery")
    res = w.control_refinery(rid, -50.0)
    assert res["ok"] is True
    assert res["result"]["setpoint_rate_bbl_day"] == 0.0


def test_control_refinery_unknown_id():
    w = World()
    w.reset(seed=42)
    res = w.control_refinery("refinery-99", 200.0)
    assert res["ok"] is False
    assert res["error"] == "unknown_refinery"


def test_control_refinery_rejects_well_id():
    """Wells aren't refineries — control/refinery must not accept a well id."""
    w = World()
    w.reset(seed=42)
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    well_id = w.state.wells[0].id
    res = w.control_refinery(well_id, 200.0)
    assert res["ok"] is False
    assert res["error"] == "unknown_refinery"


# -- End-to-end: routing + revenue split + process load --------------------


def test_refined_revenue_at_full_throughput():
    """Daily routing: all crude refined; refined revenue = actual × 0.85 × $90."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("refinery", th.x + 1, th.y)
    rid = next(t.id for t in w.state.tiles if t.type == "refinery")
    w.control_refinery(rid, REFINERY_MAX_BBL_DAY)

    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    well_id = w.state.wells[0].id
    w.control_well(well_id, Q_MAX_WELL_BBL_DAY)
    _lay_pipeline_between_refinery_and_first_well(w)

    w.step(days=1)
    rate = w.state.wells[0].current_rate_bbl_day
    refinery = next(t for t in w.state.tiles if t.type == "refinery")
    # First well's daily output is ≤ 200 bbl/day; well below the 250-bbl
    # refinery cap, so all crude is refined.
    assert refinery.current_throughput_bbl_day == pytest.approx(rate)
    # No surplus → crude_revenue = 0.
    assert w.state.today_summary_so_far["crude_revenue"] == 0.0
    expected_refined_revenue = rate * REFINERY_YIELD * REFINED_PRICE_USD_PER_BBL
    assert w.state.today_summary_so_far["refined_revenue"] == pytest.approx(
        expected_refined_revenue
    )
    assert w.state.today_summary_so_far["oil_revenue"] == pytest.approx(expected_refined_revenue)


def test_surplus_crude_sells_at_crude_price_when_no_refinery():
    """Without a refinery, today_summary_so_far.oil_revenue = total_crude × $40 —
    the existing slice-07 contract is preserved when no refinery exists."""
    w = World()
    w.reset(seed=42)
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    well_id = w.state.wells[0].id
    w.control_well(well_id, Q_MAX_WELL_BBL_DAY)
    w.step(days=1)
    rate = w.state.wells[0].current_rate_bbl_day
    assert rate > 0
    assert w.state.today_summary_so_far["oil_revenue"] == pytest.approx(
        rate * CRUDE_PRICE_USD_PER_BBL
    )
    # Pure-crude path: refined_revenue stays at 0, crude_revenue = oil_revenue.
    assert w.state.today_summary_so_far["refined_revenue"] == 0.0
    assert w.state.today_summary_so_far["crude_revenue"] == pytest.approx(
        w.state.today_summary_so_far["oil_revenue"]
    )


def test_surplus_crude_after_refinery_setpoint_satisfied():
    """If wells produce more crude than refineries can absorb, surplus
    sells raw at $40/bbl. Construct a setup with refinery setpoint=10 so
    surplus exists."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("refinery", th.x + 1, th.y)
    rid = next(t.id for t in w.state.tiles if t.type == "refinery")
    w.control_refinery(rid, 10.0)

    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    w.control_well(w.state.wells[0].id, Q_MAX_WELL_BBL_DAY)
    _lay_pipeline_between_refinery_and_first_well(w)

    w.step(days=1)
    rate = w.state.wells[0].current_rate_bbl_day
    assert rate > 10  # well produces more than refinery setpoint

    refinery = next(t for t in w.state.tiles if t.type == "refinery")
    assert refinery.current_throughput_bbl_day == pytest.approx(10.0)
    expected_refined_revenue = 10.0 * REFINERY_YIELD * REFINED_PRICE_USD_PER_BBL
    expected_crude_revenue = (rate - 10.0) * CRUDE_PRICE_USD_PER_BBL
    assert w.state.today_summary_so_far["refined_revenue"] == pytest.approx(
        expected_refined_revenue
    )
    assert w.state.today_summary_so_far["crude_revenue"] == pytest.approx(expected_crude_revenue)
    assert w.state.today_summary_so_far["oil_revenue"] == pytest.approx(
        expected_refined_revenue + expected_crude_revenue
    )


def test_process_load_unbilled_no_retail_revenue_from_refinery():
    """The refinery's hourly process load contributes to demand but is
    unbilled. With population=0 and no civilian tiles, civilian_demand_kw
    is 0; a refinery drawing 2000 kW of process load must produce zero
    retail revenue (and zero export revenue, since demand exceeds supply
    so there is no curtailment surplus)."""
    w = World()
    w.reset(seed=42)
    w.state.population = 0  # zero out civilian load
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("refinery", th.x + 1, th.y)
    refinery = next(t for t in w.state.tiles if t.type == "refinery")
    # Pin throughput so it draws process load immediately on day 1's hourly
    # loop (skipping the production-loop lag).
    refinery.current_throughput_bbl_day = 240.0  # 240 × 200 / 24 = 2000 kW/h
    w.build("coal_plant", th.x + 2, th.y)

    w.step(days=1)

    # Demand each hour ≥ 2000 kW (refinery load). Civilian = 0.
    for d in w.state.last_day_demand_kw_by_hour:
        assert d >= 2000.0 - 0.01
    # Refinery process load earns no retail revenue and no export revenue —
    # demand strictly exceeds supply, so there's never a surplus to export.
    assert w.state.today_summary_so_far["power_revenue"] == 0.0


def test_process_load_zero_on_day_one_then_lags_actual_throughput():
    """Day 1 has no prior actual_throughput, so refinery process load is 0.
    After day 1's production loop pins throughput, day 2's hourly loop draws
    process power."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("refinery", th.x + 1, th.y)
    rid = next(t.id for t in w.state.tiles if t.type == "refinery")
    w.control_refinery(rid, REFINERY_MAX_BBL_DAY)

    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    w.control_well(w.state.wells[0].id, Q_MAX_WELL_BBL_DAY)
    _lay_pipeline_between_refinery_and_first_well(w)

    w.step(days=1)  # day 1: no prior throughput → no refinery load
    refinery = next(t for t in w.state.tiles if t.type == "refinery")
    day1_throughput = refinery.current_throughput_bbl_day
    assert day1_throughput > 0  # day 1's production refined at end of day

    # Day 1 hourly demand SHOULD NOT include refinery process load (lag).
    # We can't isolate that from civilian demand here, but day 2's demand
    # WILL include it. Use last_day_demand_kw_by_hour after day 2.
    w.step(days=1)  # day 2: refinery now drawing process load all day
    expected_process_kw = day1_throughput * REFINERY_KWH_PER_BBL / 24.0
    # Every hour of day 2 includes at least the refinery process load.
    for d in w.state.last_day_demand_kw_by_hour:
        assert d >= expected_process_kw - 0.01


# -- Routing priority integration -----------------------------------------


def test_two_refineries_higher_throughput_takes_more_crude():
    """Build two refineries, set one to 400 setpoint and one to 100. With
    well producing ~150 bbl, the high-setpoint refinery takes all of it;
    the low-setpoint refinery refines 0."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("refinery", th.x + 1, th.y)
    w.build("refinery", th.x - 1, th.y)
    refs = [t for t in w.state.tiles if t.type == "refinery"]
    high = next(r for r in refs if r.x == th.x + 1)
    low = next(r for r in refs if r.x == th.x - 1)
    w.control_refinery(high.id, 400.0)
    w.control_refinery(low.id, 100.0)

    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    w.control_well(w.state.wells[0].id, Q_MAX_WELL_BBL_DAY)
    # Join both refineries + the well onto one pipeline network: row of
    # pipeline tiles south of both refineries, then a south jog to the well.
    well = w.state.wells[0]
    _lay_pipeline_path(w, low.x, low.y + 1, well.x - 1, low.y + 1)
    _lay_pipeline_path(w, well.x - 1, low.y + 1, well.x - 1, well.y)

    w.step(days=1)
    rate = w.state.wells[0].current_rate_bbl_day
    # High-throughput refinery gets all crude (rate ≤ 400 ≤ its setpoint).
    high_after = next(r for r in w.state.tiles if r.id == high.id)
    low_after = next(r for r in w.state.tiles if r.id == low.id)
    if rate <= 400:
        assert high_after.current_throughput_bbl_day == pytest.approx(rate)
        assert low_after.current_throughput_bbl_day == 0.0
    else:
        assert high_after.current_throughput_bbl_day == pytest.approx(400)
        assert low_after.current_throughput_bbl_day == pytest.approx(min(100.0, rate - 400))


# -- Workforce efficiency end-to-end (slice 06) ---------------------------


def test_idle_refinery_refines_zero_with_available_crude():
    """End-to-end: idle refinery (staffed=0) routes 0 even when crude flows."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("refinery", th.x + 1, th.y)
    rid = next(t.id for t in w.state.tiles if t.type == "refinery")
    w.control_refinery(rid, REFINERY_MAX_BBL_DAY)

    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    w.control_well(w.state.wells[0].id, Q_MAX_WELL_BBL_DAY)

    # Drop staffing AFTER all build/drill hooks have run — those hooks call
    # hire_to_fill, which would otherwise immediately re-staff the refinery
    # from the unemployed pool.
    ref = next(t for t in w.state.tiles if t.type == "refinery")
    ref.staffed_jobs = 0

    w.step(days=1)
    ref_after = next(t for t in w.state.tiles if t.type == "refinery")
    assert ref_after.current_throughput_bbl_day == 0.0
    # Refined revenue is 0; all crude sells direct.
    assert w.state.today_summary_so_far["refined_revenue"] == 0.0
    rate = w.state.wells[0].current_rate_bbl_day
    assert w.state.today_summary_so_far["crude_revenue"] == pytest.approx(
        rate * CRUDE_PRICE_USD_PER_BBL
    )


def test_idle_refinery_draws_zero_process_load():
    """Pin a refinery's prior-day throughput to 0 via idle staffing; day 2's
    hourly demand never picks up a refinery process-load contribution."""
    w = World()
    w.reset(seed=42)
    w.state.population = 0  # zero out civilian load entirely
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("refinery", th.x + 1, th.y)
    ref = next(t for t in w.state.tiles if t.type == "refinery")
    ref.staffed_jobs = 0
    rid = ref.id
    w.control_refinery(rid, REFINERY_MAX_BBL_DAY)

    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    w.control_well(w.state.wells[0].id, Q_MAX_WELL_BBL_DAY)

    w.step(days=2)
    # Idle refinery routed 0 crude → 0 throughput → 0 process load.
    ref_after = next(t for t in w.state.tiles if t.type == "refinery")
    assert ref_after.current_throughput_bbl_day == 0.0
    for d in w.state.last_day_demand_kw_by_hour:
        assert d == pytest.approx(0.0)


def test_half_staffed_refinery_process_load_tracks_throughput():
    """A half-staffed refinery refining at its 120-bbl cap draws exactly
    half the process kW a fully-staffed refinery at 250 bbl/day would."""
    half_kw = refinery_process_kw(120.0)
    full_kw = refinery_process_kw(250.0)
    assert half_kw == pytest.approx(120.0 * REFINERY_KWH_PER_BBL / 24.0)
    assert half_kw == pytest.approx(full_kw * 0.48)


def test_idle_refinery_emits_zero_refinery_co2():
    """End-to-end: idle refinery contributes 0 t/day refinery CO2."""
    w = World()
    w.reset(seed=42)
    w.state.population = 0  # silence industrial+civilian contributions
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("refinery", th.x + 1, th.y)
    ref = next(t for t in w.state.tiles if t.type == "refinery")
    ref.staffed_jobs = 0
    w.control_refinery(ref.id, REFINERY_MAX_BBL_DAY)

    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    w.control_well(w.state.wells[0].id, Q_MAX_WELL_BBL_DAY)
    w.step(days=1)
    assert w.state.today_summary_so_far["co2_emitted_t"] == pytest.approx(0.0)


def test_setpoint_not_auto_clamped_when_staffing_drops():
    """The player-facing setpoint stays at the value the player set even when
    staffing drops below the level that could deliver it. Only the actual
    throughput respects the efficiency-scaled cap."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("refinery", th.x + 1, th.y)
    ref = next(t for t in w.state.tiles if t.type == "refinery")
    w.control_refinery(ref.id, REFINERY_MAX_BBL_DAY)

    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    w.control_well(w.state.wells[0].id, Q_MAX_WELL_BBL_DAY)
    _lay_pipeline_between_refinery_and_first_well(w)

    # Drop staffing AFTER all hire_to_fill hooks have run — they would
    # otherwise re-staff the refinery from the unemployed pool.
    ref.staffed_jobs = 12  # eff = 0.48 → cap = 120

    w.step(days=1)
    s = w.state_dict()
    ref_state = next(t for t in s["tiles"] if t["type"] == "refinery")
    assert ref_state["setpoint_rate_bbl_day"] == REFINERY_MAX_BBL_DAY  # unchanged
    # Throughput is capped at min(setpoint=250, eff_cap=120, available_crude).
    rate = w.state.wells[0].current_rate_bbl_day
    expected = min(REFINERY_MAX_BBL_DAY * (12 / 25), rate)
    assert ref_state["current_throughput_bbl_day"] == pytest.approx(expected)


# -- API smoke ------------------------------------------------------------


def test_api_build_refinery():
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    client = TestClient(create_app(world=w))
    res = client.post("/build", json={"tile_type": "refinery", "x": th.x + 1, "y": th.y}).json()
    assert res["ok"] is True


def test_api_control_refinery_endpoint():
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    client = TestClient(create_app(world=w))
    client.post("/build", json={"tile_type": "refinery", "x": th.x + 1, "y": th.y})
    refinery = next(t for t in w.state.tiles if t.type == "refinery")
    res = client.post(
        "/control/refinery", json={"refinery_id": refinery.id, "rate_bbl_day": 250.0}
    ).json()
    assert res["ok"] is True
    assert res["result"]["setpoint_rate_bbl_day"] == 250.0


def test_api_control_refinery_unknown_id():
    w = World()
    w.reset(seed=42)
    client = TestClient(create_app(world=w))
    res = client.post(
        "/control/refinery", json={"refinery_id": "refinery-99", "rate_bbl_day": 200.0}
    ).json()
    assert res["ok"] is False
    assert res["error"] == "unknown_refinery"


# -- /state.tiles schema ---------------------------------------------------


def test_state_tiles_refinery_exposes_setpoint_and_throughput():
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("refinery", th.x + 1, th.y)
    rid = next(t.id for t in w.state.tiles if t.type == "refinery")
    w.control_refinery(rid, 180.0)
    s = w.state_dict()
    refinery = next(t for t in s["tiles"] if t["type"] == "refinery")
    assert refinery["setpoint_rate_bbl_day"] == 180.0
    assert refinery["current_throughput_bbl_day"] == 0.0  # not yet stepped


# -- Determinism -----------------------------------------------------------


# -- Carbon emissions (slice 10, PRD §4.7) --------------------------------


def test_carbon_constants_have_prd_values():
    assert COAL_CO2_T_PER_MWH == 0.90
    assert GAS_CO2_T_PER_MWH == 0.40
    assert INDUSTRIAL_PROCESS_CO2_T_PER_DAY == 2.0
    assert REFINERY_CO2_PER_BBL == 0.30
    assert CARBON_PRICE_USD_PER_TON == 25.0


def test_carbon_price_initialized_on_reset_at_25():
    w = World()
    w.reset(seed=42)
    assert w.state.carbon_price == 25.0


def test_carbon_price_resets_back_to_25_after_mutation():
    """Slice 11's regulatory tightening will mutate state.carbon_price; /reset
    must restore it to the constant."""
    w = World()
    w.reset(seed=42)
    w.state.carbon_price = 75.0
    w.reset(seed=42)
    assert w.state.carbon_price == 25.0


def test_daily_emissions_t_zero_with_no_sources():
    w = World()
    w.reset(seed=42)
    # Fresh world: no plants ran, no industrial, no refinery.
    assert daily_emissions_t(w) == 0.0


def test_daily_emissions_t_industrial_flat_per_tile():
    """Per-tile-day flat 2 t/day regardless of electricity input — no per-MWh
    industrial term."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("industrial", th.x + 1, th.y)
    w.build("industrial", th.x - 1, th.y)
    # Bypass the daily loop: industrial counter alone should yield 4 t/day.
    assert daily_emissions_t(w) == pytest.approx(2 * INDUSTRIAL_PROCESS_CO2_T_PER_DAY)


def test_daily_emissions_t_no_double_count_industrial_consumed_kwh():
    """Industrial must NOT pay for kWh consumed by industrial (which already
    bills via the coal/gas plant emitting on its behalf). Adding more
    industrial-served kWh — without changing the industrial tile count or the
    coal/gas dispatch — must not increase emissions."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("industrial", th.x + 1, th.y)
    base = daily_emissions_t(w)
    # Stuff a fake "industrial kWh consumed" key on today_summary_so_far.
    # If the formula were per-MWh-on-industrial, this would change emissions.
    w.state.today_summary_so_far["industrial_kwh_consumed"] = 999_000.0
    assert daily_emissions_t(w) == pytest.approx(base)


def test_daily_emissions_t_coal_emissions():
    """1 MWh coal → 0.90 t CO2."""
    w = World()
    w.reset(seed=42)
    w.state.today_summary_so_far["coal_kwh"] = 1000.0
    assert daily_emissions_t(w) == pytest.approx(COAL_CO2_T_PER_MWH)


def test_daily_emissions_t_gas_emissions():
    """1 MWh gas → 0.40 t CO2."""
    w = World()
    w.reset(seed=42)
    w.state.today_summary_so_far["gas_kwh"] = 1000.0
    assert daily_emissions_t(w) == pytest.approx(GAS_CO2_T_PER_MWH)


def test_daily_emissions_t_refinery_scales_with_refined_bbl():
    """Refinery CO2 scales linearly with input bbl — 100 bbl → 30 t CO2."""
    w = World()
    w.reset(seed=42)
    w.state.today_summary_so_far["refined_bbl"] = 100.0
    assert daily_emissions_t(w) == pytest.approx(100.0 * REFINERY_CO2_PER_BBL)


def test_daily_emissions_t_sums_all_sources():
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("industrial", th.x + 1, th.y)
    w.state.today_summary_so_far["coal_kwh"] = 2000.0  # 2 MWh × 0.90 = 1.80 t
    w.state.today_summary_so_far["gas_kwh"] = 500.0  # 0.5 MWh × 0.40 = 0.20 t
    w.state.today_summary_so_far["refined_bbl"] = 50.0  # 50 × 0.30 = 15.0 t
    expected = 1.80 + 0.20 + 2.0 + 15.0  # +2.0 t industrial
    assert daily_emissions_t(w) == pytest.approx(expected)


def test_carbon_cost_deducted_from_treasury_during_step():
    """End-to-end: a coal plant + civilian load runs for one day, emissions
    accrue, and treasury is decremented by daily_emissions_t × carbon_price."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("coal_plant", th.x + 1, th.y)
    w.step(days=1)
    co2 = w.state.today_summary_so_far["co2_emitted_t"]
    carbon_cost = w.state.today_summary_so_far["carbon_cost"]
    assert co2 > 0
    assert carbon_cost == pytest.approx(co2 * w.state.carbon_price)
    # Isolate the carbon delta: same setup with carbon_price=0 should leave
    # treasury exactly carbon_cost higher.
    w2 = World()
    w2.reset(seed=42)
    w2.state.carbon_price = 0.0
    w2.build("coal_plant", th.x + 1, th.y)
    w2.step(days=1)
    delta = w2.state.treasury - w.state.treasury
    assert delta == pytest.approx(carbon_cost)


def test_carbon_cost_uses_current_carbon_price():
    """Mutating state.carbon_price (slice 11 regulatory tightening) is
    reflected immediately in the next day's carbon-cost accrual."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("coal_plant", th.x + 1, th.y)
    w.state.carbon_price = 100.0  # 4× the default
    w.step(days=1)
    co2 = w.state.today_summary_so_far["co2_emitted_t"]
    assert w.state.today_summary_so_far["carbon_cost"] == pytest.approx(co2 * 100.0)


def test_co2_emitted_t_in_today_summary_so_far():
    w = World()
    w.reset(seed=42)
    assert "co2_emitted_t" in w.state.today_summary_so_far
    assert w.state.today_summary_so_far["co2_emitted_t"] == 0.0


def test_industrial_pays_flat_co2_even_when_no_grid():
    """A fully-staffed industrial tile (no plants serving it) still emits 2 t/day
    flat — the term is independent of grid dispatch. Slice 05 ties the term to
    workforce efficiency, so the original ``pop=0`` setup would zero-staff and
    drop the contribution. Manually staff to full and assert the flat term
    arrives regardless of dispatch state."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("industrial", th.x + 1, th.y)
    w.state.population = 0
    # The /build hook auto-hires; pop=0 leaves the industrial at staffed=0.
    # Force-staff to full so the flat CO2 term fires.
    ind = next(t for t in w.state.tiles if t.type == "industrial")
    ind.staffed_jobs = ind.jobs
    w.step(days=1)
    # No plants → no coal/gas kWh. Refinery=0. Only industrial × 2 t/day.
    assert w.state.today_summary_so_far["co2_emitted_t"] == pytest.approx(
        INDUSTRIAL_PROCESS_CO2_T_PER_DAY
    )
    assert w.state.today_summary_so_far["carbon_cost"] == pytest.approx(
        INDUSTRIAL_PROCESS_CO2_T_PER_DAY * w.state.carbon_price
    )


def test_idle_industrial_emits_zero_flat_co2():
    """An industrial with staffed_jobs=0 contributes 0 t/day flat CO2."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("industrial", th.x + 1, th.y)
    w.state.population = 0
    ind = next(t for t in w.state.tiles if t.type == "industrial")
    ind.staffed_jobs = 0
    w.step(days=1)
    # No plants, no refinery, idle industrial → no CO2 anywhere.
    assert w.state.today_summary_so_far["co2_emitted_t"] == pytest.approx(0.0)


def test_half_staffed_industrial_emits_half_flat_co2():
    """staffed_jobs=15 (jobs=30) → 1.0 t/day flat CO2 contribution."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("industrial", th.x + 1, th.y)
    w.state.population = 0
    ind = next(t for t in w.state.tiles if t.type == "industrial")
    ind.staffed_jobs = ind.jobs // 2  # 15 of 30
    w.step(days=1)
    assert w.state.today_summary_so_far["co2_emitted_t"] == pytest.approx(
        INDUSTRIAL_PROCESS_CO2_T_PER_DAY * 0.5
    )


def test_refinery_emissions_scale_with_refined_throughput():
    """End-to-end: a refinery refining N bbl emits N × 0.30 t/day."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("refinery", th.x + 1, th.y)
    rid = next(t.id for t in w.state.tiles if t.type == "refinery")
    w.control_refinery(rid, REFINERY_MAX_BBL_DAY)
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    w.control_well(w.state.wells[0].id, Q_MAX_WELL_BBL_DAY)
    _lay_pipeline_between_refinery_and_first_well(w)
    w.step(days=1)
    refinery = next(t for t in w.state.tiles if t.type == "refinery")
    refined_bbl = refinery.current_throughput_bbl_day
    co2 = w.state.today_summary_so_far["co2_emitted_t"]
    # Baseline: no plants here either, no industrial, so co2 == refinery term.
    assert co2 == pytest.approx(refined_bbl * REFINERY_CO2_PER_BBL)


def test_step_size_invariance_with_carbon():
    """Carbon accrual is RNG-free; step(7) ≡ step(1)×7."""
    a = World()
    b = World()
    a.reset(seed=42)
    b.reset(seed=42)
    for w in (a, b):
        th = next(t for t in w.state.tiles if t.type == "town_hall")
        w.build("coal_plant", th.x + 1, th.y)
        w.build("industrial", th.x, th.y + 1)
    a.step(days=7)
    for _ in range(7):
        b.step(days=1)
    assert a.state.treasury == pytest.approx(b.state.treasury)
    assert a.state.today_summary_so_far["co2_emitted_t"] == pytest.approx(
        b.state.today_summary_so_far["co2_emitted_t"]
    )


def test_step_size_invariance_with_refinery():
    """Refinery routing is RNG-free, so step(7) ≡ step(1)×7."""
    a = World()
    b = World()
    a.reset(seed=42)
    b.reset(seed=42)
    for w in (a, b):
        th = next(t for t in w.state.tiles if t.type == "town_hall")
        w.build("refinery", th.x + 1, th.y)
        rid = next(t.id for t in w.state.tiles if t.type == "refinery")
        w.control_refinery(rid, REFINERY_MAX_BBL_DAY)
        hc = _hc_voxel(w)
        w.drill(hc.x, hc.y, hc.z, "production")
        w.control_well(w.state.wells[0].id, Q_MAX_WELL_BBL_DAY)
        _lay_pipeline_between_refinery_and_first_well(w)
    a.step(days=7)
    for _ in range(7):
        b.step(days=1)
    assert a.state.treasury == pytest.approx(b.state.treasury)
    a_ref = next(t for t in a.state.tiles if t.type == "refinery")
    b_ref = next(t for t in b.state.tiles if t.type == "refinery")
    assert a_ref.current_throughput_bbl_day == pytest.approx(b_ref.current_throughput_bbl_day)


# -- Pipeline routing (oilfield-v2 slice 08) ------------------------------


def _setup_well_and_refinery(w: World, *, with_pipeline: bool) -> tuple[Tile, Well]:
    """Build a refinery adjacent to the town_hall and drill the first HC well;
    optionally lay an L-pipeline so they share a network."""
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("refinery", th.x + 1, th.y)
    refinery = next(t for t in w.state.tiles if t.type == "refinery")
    w.control_refinery(refinery.id, REFINERY_MAX_BBL_DAY)
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    well = w.state.wells[0]
    w.control_well(well.id, Q_MAX_WELL_BBL_DAY)
    if with_pipeline:
        _lay_pipeline_between_refinery_and_first_well(w)
    return refinery, well


def test_pipeline_connected_producer_refinery_routes_crude():
    """Producer + refinery on the same 4-connected pipeline network → the
    refinery receives the producer's crude (subject to setpoint/cap)."""
    w = World()
    w.reset(seed=42)
    refinery, well = _setup_well_and_refinery(w, with_pipeline=True)
    w.step(days=1)
    rate = w.state.wells[0].current_rate_bbl_day
    assert rate > 0
    refinery_after = next(t for t in w.state.tiles if t.id == refinery.id)
    assert refinery_after.current_throughput_bbl_day == pytest.approx(rate)
    # State exposes the network and zero orphans.
    s = w.state_dict()
    assert s["orphan_well_ids"] == []
    assert s["orphan_refinery_ids"] == []
    assert len(s["pipeline_networks"]) == 1
    net = s["pipeline_networks"][0]
    assert well.id in net["well_ids"]
    assert refinery.id in net["refinery_ids"]


def test_orphan_producer_sells_raw_refinery_starves():
    """Producer with no pipeline neighbor → its crude shows in crude_revenue
    at $40/bbl; the lone refinery's throughput stays at 0."""
    w = World()
    w.reset(seed=42)
    refinery, well = _setup_well_and_refinery(w, with_pipeline=False)
    w.step(days=1)
    rate = w.state.wells[0].current_rate_bbl_day
    assert rate > 0
    refinery_after = next(t for t in w.state.tiles if t.id == refinery.id)
    assert refinery_after.current_throughput_bbl_day == 0.0
    assert w.state.today_summary_so_far["refined_revenue"] == 0.0
    assert w.state.today_summary_so_far["crude_revenue"] == pytest.approx(
        rate * CRUDE_PRICE_USD_PER_BBL
    )
    s = w.state_dict()
    assert well.id in s["orphan_well_ids"]
    assert refinery.id in s["orphan_refinery_ids"]
    assert s["pipeline_networks"] == []


def test_orphan_refinery_with_producer_on_another_network_starves():
    """Producer + pipeline isolated from a separate, pipeline-less refinery →
    refinery throughput is 0; the producer's crude routes only within its
    own network (no refinery there → sells raw)."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    # Lone refinery, no pipeline adjacent → orphan.
    w.build("refinery", th.x + 1, th.y)
    refinery = next(t for t in w.state.tiles if t.type == "refinery")
    w.control_refinery(refinery.id, REFINERY_MAX_BBL_DAY)
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    well = w.state.wells[0]
    w.control_well(well.id, Q_MAX_WELL_BBL_DAY)
    # Build a *separate* pipeline network adjacent to the well only.
    _lay_pipeline_path(w, well.x - 1, well.y, well.x - 1, well.y + 2)
    w.step(days=1)
    rate = w.state.wells[0].current_rate_bbl_day
    assert rate > 0
    refinery_after = next(t for t in w.state.tiles if t.id == refinery.id)
    assert refinery_after.current_throughput_bbl_day == 0.0
    # The producer's network contains no refinery → all crude sells raw.
    assert w.state.today_summary_so_far["crude_revenue"] == pytest.approx(
        rate * CRUDE_PRICE_USD_PER_BBL
    )
    s = w.state_dict()
    assert refinery.id in s["orphan_refinery_ids"]
    assert well.id not in s["orphan_well_ids"]


def test_two_disjoint_networks_do_not_share_crude():
    """Network A: refinery + well via pipeline. Network B: refinery + well via
    pipeline. A's crude must not reach B's refinery and vice versa."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    # Pick two HC voxels at distinct, non-adjacent (x, y) so each network
    # can have its own well without sharing tiles.
    unique_xy: dict[tuple[int, int], object] = {}
    for v in w.subsurface.voxels.values():
        unique_xy.setdefault((v.x, v.y), v)
    sorted_xy = sorted(unique_xy.keys())
    hc_west = unique_xy[sorted_xy[0]]
    hc_east = unique_xy[sorted_xy[-1]]
    # Refinery A on the east side of town_hall, paired with the east well; B
    # on the west side, paired with the west well. Pipelines for A run east /
    # north and never touch B's tiles, and vice versa.
    w.build("refinery", th.x + 1, th.y)
    ref_a = next(t for t in w.state.tiles if t.type == "refinery")
    w.control_refinery(ref_a.id, REFINERY_MAX_BBL_DAY)
    w.build("refinery", th.x - 1, th.y)
    ref_b = next(t for t in w.state.tiles if t.id != ref_a.id and t.type == "refinery")
    w.control_refinery(ref_b.id, REFINERY_MAX_BBL_DAY)
    w.drill(hc_east.x, hc_east.y, hc_east.z, "production")  # type: ignore[attr-defined]
    well_a = w.state.wells[0]  # paired with ref_a (east)
    w.control_well(well_a.id, Q_MAX_WELL_BBL_DAY)
    w.drill(hc_west.x, hc_west.y, hc_west.z, "production")  # type: ignore[attr-defined]
    well_b = w.state.wells[1]  # paired with ref_b (west)
    w.control_well(well_b.id, Q_MAX_WELL_BBL_DAY)
    # Network A: ref_a (17, 16) → well_a (east). Run pipeline NORTH then EAST
    # so it never touches ref_b or its row.
    _lay_pipeline_path(w, ref_a.x, ref_a.y - 1, well_a.x, ref_a.y - 1)
    _lay_pipeline_path(w, well_a.x, ref_a.y - 1, well_a.x, well_a.y - 1)
    # Network B: ref_b (15, 16) → well_b (west). Run pipeline SOUTH then WEST
    # so it never touches ref_a or its row.
    _lay_pipeline_path(w, ref_b.x, ref_b.y + 1, well_b.x, ref_b.y + 1)
    _lay_pipeline_path(w, well_b.x, ref_b.y + 1, well_b.x, well_b.y - 1)
    s_pre = w.state_dict()
    # Sanity: two networks, each pairing exactly one refinery with one well.
    assert len(s_pre["pipeline_networks"]) == 2
    assert s_pre["orphan_well_ids"] == []
    assert s_pre["orphan_refinery_ids"] == []
    for net in s_pre["pipeline_networks"]:
        assert len(net["well_ids"]) == 1
        assert len(net["refinery_ids"]) == 1
    w.step(days=1)
    rate_a = next(x for x in w.state.wells if x.id == well_a.id).current_rate_bbl_day
    rate_b = next(x for x in w.state.wells if x.id == well_b.id).current_rate_bbl_day
    assert rate_a > 0 and rate_b > 0
    ref_a_after = next(t for t in w.state.tiles if t.id == ref_a.id)
    ref_b_after = next(t for t in w.state.tiles if t.id == ref_b.id)
    # Each refinery sees only its network's well — strictly bounded by that
    # well's rate (capped at REFINERY_MAX_BBL_DAY).
    assert ref_a_after.current_throughput_bbl_day == pytest.approx(
        min(rate_a, REFINERY_MAX_BBL_DAY)
    )
    assert ref_b_after.current_throughput_bbl_day == pytest.approx(
        min(rate_b, REFINERY_MAX_BBL_DAY)
    )


def test_demolishing_bridging_pipeline_orphans_well_next_day():
    """Network: well — pipeline — pipeline (bridge) — pipeline — refinery.
    Day 1 routes crude; demolishing the bridge produces TWO components on day
    2 (well-side has no refinery, refinery-side has no well)."""
    w = World()
    w.reset(seed=42)
    refinery, well = _setup_well_and_refinery(w, with_pipeline=True)
    # Step once with the pipeline intact → routing happens.
    w.step(days=1)
    refinery_after_day1 = next(t for t in w.state.tiles if t.id == refinery.id)
    assert refinery_after_day1.current_throughput_bbl_day > 0
    # Identify a midpoint pipeline tile and demolish it.
    pipelines = [t for t in w.state.tiles if t.type == "pipeline"]
    mid = pipelines[len(pipelines) // 2]
    res = w.demolish(mid.x, mid.y)
    assert res["ok"] is True
    # /state immediately reflects the new graph (no step needed).
    s_after = w.state_dict()
    # Either the well is now orphan or the refinery is — whichever side lost
    # the bridge. The split must produce ≥2 components (well-side OR
    # refinery-side might consist of just the endpoint adjacency, but the AC
    # only cares that the formerly-joined pair is no longer in the same net).
    well_net = next(
        (
            n
            for n in s_after["pipeline_networks"]
            if well.id in n["well_ids"] and refinery.id in n["refinery_ids"]
        ),
        None,
    )
    assert well_net is None  # well and refinery no longer share a network
    # Step a day with the bridge demolished → refinery routes 0.
    w.step(days=1)
    refinery_after = next(t for t in w.state.tiles if t.id == refinery.id)
    assert refinery_after.current_throughput_bbl_day == 0.0

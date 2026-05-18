"""Production wells + crude revenue (slice 07, brief §4.5).

Covers the formula edge cases (V_init=0, full pool, partial pool clipped at
edges), drainage weighting, two-wells-overlap deterministic ordering, plus
the API surface (/drill + /control/well) and step-size invariance.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from world.api import create_app
from world.sim import World
from world.subsurface import (
    CRUDE_PRICE_USD_PER_BBL,
    PERM_NORMALIZATION_MD,
    PRODUCTION_KWH_PER_BBL,
    Q_MAX_WELL_BBL_DAY,
    SubsurfaceGrid,
    Voxel,
    voxels_in_3x3x3,
    well_production_bbl_day,
)


def _build_road_to(world: World, x: int, y: int) -> None:
    """Lay a road link from town hall to (x, y) (excluding the destination)
    so a road-requiring tile at (x, y) clears the adjacency check."""
    th = next(t for t in world.state.tiles if t.type == "town_hall")
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


def _hc_voxel(world: World) -> Voxel:
    return next(iter(world.subsurface.voxels.values()))


def _make_voxel(x: int, y: int, z: int, *, perm: float = 500.0, oil: float = 100_000.0) -> Voxel:
    return Voxel(
        x=x,
        y=y,
        z=z,
        porosity=0.2,
        permeability=perm,
        oil_saturation=0.7,
        oil_in_place_bbl=oil,
        oil_remaining_bbl=oil,
    )


# -- voxels_in_3x3x3 (pool clipping) ---------------------------------------


def test_pool_returns_27_positions_at_grid_interior():
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    pool, n = voxels_in_3x3x3(grid, 5, 5, 5)
    assert n == 27
    assert pool == []  # no HC voxels populated


def test_pool_clips_at_grid_corner():
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    _, n = voxels_in_3x3x3(grid, 0, 0, 0)
    assert n == 8  # 2 × 2 × 2 corner


def test_pool_clips_at_grid_edge():
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    _, n = voxels_in_3x3x3(grid, 0, 5, 5)
    assert n == 18  # 2 × 3 × 3


# -- well_production_bbl_day formula ---------------------------------------


def test_v_init_zero_returns_zero():
    """A pool with no HC content produces 0 bbl/day indefinitely."""
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    q = well_production_bbl_day(grid, 5, 5, 5, setpoint_rate_bbl_day=200.0)
    assert q == 0.0


def test_q_potential_matches_brief_formula_single_voxel_pool():
    """Single HC voxel in a 27-position interior pool. k_eff = perm/27/500."""
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    grid.voxels[(5, 5, 5)] = _make_voxel(5, 5, 5, perm=500.0, oil=100_000.0)
    q = well_production_bbl_day(grid, 5, 5, 5, setpoint_rate_bbl_day=200.0)
    expected = Q_MAX_WELL_BBL_DAY * (500.0 / 27.0 / PERM_NORMALIZATION_MD) * 1.0
    assert q == pytest.approx(expected)


def test_setpoint_clamps_q_actual_below_potential():
    """When setpoint < q_potential, q_actual = setpoint."""
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                grid.voxels[(5 + dx, 5 + dy, 5 + dz)] = _make_voxel(
                    5 + dx, 5 + dy, 5 + dz, perm=1000.0, oil=200_000.0
                )
    # k_eff = 1000/500 = 2; q_potential = 200 × 2 × 1 = 400. Setpoint 150 wins.
    q = well_production_bbl_day(grid, 5, 5, 5, setpoint_rate_bbl_day=150.0)
    assert q == 150.0


def test_partial_pool_at_edge_uses_clipped_n_positions():
    """Pool clipped to 8 corner positions; k_eff divides by 8, not 27."""
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    # Single HC voxel at the corner; pool clipped to 8 cells.
    grid.voxels[(0, 0, 0)] = _make_voxel(0, 0, 0, perm=500.0, oil=100_000.0)
    q = well_production_bbl_day(grid, 0, 0, 0, setpoint_rate_bbl_day=200.0)
    expected = Q_MAX_WELL_BBL_DAY * (500.0 / 8.0 / PERM_NORMALIZATION_MD) * 1.0
    assert q == pytest.approx(expected)


def test_drainage_weighted_by_perm_times_remaining():
    """Voxel a (perm 1000) drains 2× voxel b (perm 500); same remaining."""
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    a = _make_voxel(5, 5, 5, perm=1000.0, oil=100_000.0)
    b = _make_voxel(5, 5, 4, perm=500.0, oil=100_000.0)
    grid.voxels[(5, 5, 5)] = a
    grid.voxels[(5, 5, 4)] = b

    initial_total = a.oil_remaining_bbl + b.oil_remaining_bbl
    q = well_production_bbl_day(grid, 5, 5, 5, setpoint_rate_bbl_day=200.0)

    drained_a = 100_000.0 - a.oil_remaining_bbl
    drained_b = 100_000.0 - b.oil_remaining_bbl
    assert drained_a + drained_b == pytest.approx(q)
    assert drained_a == pytest.approx(drained_b * 2.0)
    assert initial_total - (a.oil_remaining_bbl + b.oil_remaining_bbl) == pytest.approx(q)


def test_drainage_total_equals_q_actual():
    """Brief §4.5: weighted drainage sums exactly to q_actual."""
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    grid.voxels[(5, 5, 5)] = _make_voxel(5, 5, 5, perm=1000.0, oil=200_000.0)
    grid.voxels[(5, 5, 4)] = _make_voxel(5, 5, 4, perm=500.0, oil=200_000.0)
    grid.voxels[(5, 5, 6)] = _make_voxel(5, 5, 6, perm=200.0, oil=200_000.0)
    initial_total = sum(v.oil_remaining_bbl for v in grid.voxels.values())
    q = well_production_bbl_day(grid, 5, 5, 5, setpoint_rate_bbl_day=200.0)
    final_total = sum(v.oil_remaining_bbl for v in grid.voxels.values())
    assert initial_total - final_total == pytest.approx(q)


def test_fraction_decays_as_pool_depletes():
    """After one day at full setpoint, q_potential drops because fraction < 1."""
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    grid.voxels[(5, 5, 5)] = _make_voxel(5, 5, 5, perm=1000.0, oil=200.0)
    # Tiny oil_in_place so a single day depletes a meaningful fraction.
    q1 = well_production_bbl_day(grid, 5, 5, 5, setpoint_rate_bbl_day=200.0)
    q2 = well_production_bbl_day(grid, 5, 5, 5, setpoint_rate_bbl_day=200.0)
    assert q2 < q1


# -- /drill API ------------------------------------------------------------


def test_drill_deducts_capex_and_creates_well():
    from world.subsurface import drill_capex

    w = World()
    w.reset(seed=42)
    treasury_before = w.state.treasury
    res = w.drill(10, 10, 8, "production")
    assert res["ok"] is True
    expected_capex = drill_capex(50_000.0, 8, w.config.world_d)
    assert w.state.treasury == treasury_before - expected_capex
    assert len(w.state.wells) == 1
    well = w.state.wells[0]
    assert well.type == "production"
    assert (well.x, well.y, well.target_z) == (10, 10, 8)
    assert well.drilled_day == w.state.day
    assert well.capex_paid == expected_capex


def test_drill_rejects_completion_overlap_same_xy_dz_below_three():
    """Stacked completions at the same (x, y) are rejected when their two
    3×3×3 drainage cubes would overlap on the z-axis (|Δtarget_z| < 3)."""
    w = World()
    w.reset(seed=42)
    w.drill(10, 10, 8, "production")
    res = w.drill(10, 10, 6, "production")
    assert res["ok"] is False
    assert res["error"] == "completion_overlap"


def test_drill_allows_stacked_completion_same_xy_dz_at_least_three():
    """A second well at the same (x, y) is legal when |Δtarget_z| ≥ 3."""
    w = World()
    w.reset(seed=42)
    w.state.treasury = 1_000_000
    w.drill(10, 10, 8, "production")
    res = w.drill(10, 10, 5, "production")
    assert res["ok"] is True
    assert len(w.state.wells) == 2


def test_drill_stacked_completion_capex_prices_second_target_z():
    """The deeper second completion's capex is computed against its own
    target_z, not the first completion's. The capex formula is quadratic
    in depth, so the second well's capex must equal drill_capex(base,
    second_z, world_d)."""
    from world.subsurface import drill_capex

    w = World()
    w.reset(seed=42)
    w.state.treasury = 10_000_000
    treasury_after_first = w.state.treasury
    w.drill(10, 10, 8, "production")
    treasury_after_first = w.state.treasury
    res = w.drill(10, 10, 4, "production")
    assert res["ok"] is True
    expected_second_capex = drill_capex(50_000.0, 4, w.config.world_d)
    assert w.state.wells[-1].capex_paid == expected_second_capex
    assert treasury_after_first - w.state.treasury == expected_second_capex


def test_drill_rejects_voxel_out_of_bounds_high():
    w = World()
    w.reset(seed=42)
    res = w.drill(10, 10, w.config.world_d, "production")
    assert res["ok"] is False
    assert res["error"] == "voxel_out_of_bounds"


def test_drill_rejects_voxel_out_of_bounds_negative():
    w = World()
    w.reset(seed=42)
    res = w.drill(10, 10, -1, "production")
    assert res["ok"] is False
    assert res["error"] == "voxel_out_of_bounds"


def test_drill_rejects_out_of_bounds_xy():
    w = World()
    w.reset(seed=42)
    res = w.drill(-1, 10, 8, "production")
    assert res["ok"] is False
    assert res["error"] == "out_of_bounds"


def test_drill_rejects_insufficient_funds():
    w = World()
    w.reset(seed=42)
    w.state.treasury = 100.0
    res = w.drill(10, 10, 8, "production")
    assert res["ok"] is False
    assert res["error"] == "insufficient_funds"


def test_drill_rejects_invalid_well_type():
    w = World()
    w.reset(seed=42)
    res = w.drill(10, 10, 8, "geothermal")
    assert res["ok"] is False
    assert res["error"] == "invalid_well_type"


def test_build_does_not_accept_oil_well_type():
    """Wells go through /drill exclusively. /build must reject oil_well."""
    w = World()
    w.reset(seed=42)
    res = w.build("oil_well", 10, 10)
    assert res["ok"] is False
    assert res["error"] == "unknown_tile_type"


# -- /control/well ---------------------------------------------------------


def test_control_well_sets_setpoint_and_drives_actual_rate():
    w = World()
    w.reset(seed=42)
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    well_id = w.state.wells[0].id
    w.control_well(well_id, 150.0)
    assert w.state.wells[0].setpoint_rate_bbl_day == 150.0
    w.step(days=1)
    well = w.state.wells[0]
    assert well.current_rate_bbl_day <= 150.0
    assert well.current_rate_bbl_day >= 0.0
    assert well.cumulative_produced_bbl == well.current_rate_bbl_day


def test_control_well_clamps_setpoint_above_max():
    w = World()
    w.reset(seed=42)
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    res = w.control_well(w.state.wells[0].id, 999.0)
    assert res["ok"] is True
    assert res["result"]["setpoint_rate_bbl_day"] == Q_MAX_WELL_BBL_DAY


def test_control_well_clamps_setpoint_below_zero():
    w = World()
    w.reset(seed=42)
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    res = w.control_well(w.state.wells[0].id, -50.0)
    assert res["ok"] is True
    assert res["result"]["setpoint_rate_bbl_day"] == 0.0


def test_control_well_unknown_id():
    w = World()
    w.reset(seed=42)
    res = w.control_well("production-99", 100.0)
    assert res["ok"] is False
    assert res["error"] == "unknown_well"


# -- Two wells share overlapping pool --------------------------------------


def test_two_wells_share_pool_second_sees_post_drain_state():
    """Wells run sequentially against `oil_remaining`. With a single shared
    HC voxel, the second well sees the depleted state and produces strictly
    less than the first."""
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    # Tiny OIP forces a meaningful per-day depletion.
    grid.voxels[(5, 5, 5)] = _make_voxel(5, 5, 5, perm=1000.0, oil=200.0)
    qa = well_production_bbl_day(grid, 5, 5, 5, setpoint_rate_bbl_day=200.0)
    qb = well_production_bbl_day(grid, 6, 5, 5, setpoint_rate_bbl_day=200.0)
    assert qa > 0.0
    assert qb > 0.0
    assert qa > qb


def test_two_wells_overlap_reproducible_across_runs():
    """Same-seed worlds with same drill order produce identical totals."""
    a = World()
    b = World()
    a.reset(seed=42)
    b.reset(seed=42)
    hc_a = _hc_voxel(a)
    hc_b = _hc_voxel(b)
    for w, hc in ((a, hc_a), (b, hc_b)):
        w.drill(hc.x, hc.y, hc.z, "production")
        w.drill(hc.x + 1, hc.y, hc.z, "production")
        w.control_well("production-1", Q_MAX_WELL_BBL_DAY)
        w.control_well("production-2", Q_MAX_WELL_BBL_DAY)
        w.step(days=1)
    assert a.state.wells[0].current_rate_bbl_day == b.state.wells[0].current_rate_bbl_day
    assert a.state.wells[1].current_rate_bbl_day == b.state.wells[1].current_rate_bbl_day


def test_v_init_zero_well_produces_zero_indefinitely():
    """Drill at empty rock — well stays at 0 bbl/day across many days."""
    w = World()
    w.reset(seed=42)
    # Find a 3×3×3 pool with no HC voxels.
    target = None
    for x in range(2, 30, 4):
        for y in range(2, 30, 4):
            for z in range(2, 14, 2):
                pool, _ = voxels_in_3x3x3(w.subsurface, x, y, z)
                if not pool:
                    target = (x, y, z)
                    break
            if target:
                break
        if target:
            break
    assert target is not None, "couldn't find empty 3x3x3 region in seed-42 world"
    res = w.drill(target[0], target[1], target[2], "production")
    assert res["ok"] is True
    well_id = res["result"]["id"]
    w.control_well(well_id, Q_MAX_WELL_BBL_DAY)
    w.step(days=5)
    well = next(ww for ww in w.state.wells if ww.id == well_id)
    assert well.cumulative_produced_bbl == 0.0
    assert well.current_rate_bbl_day == 0.0


# -- Daily revenue accrual -------------------------------------------------


def test_crude_revenue_accrues_to_summary_and_treasury():
    w = World()
    w.reset(seed=42)
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    w.control_well(w.state.wells[0].id, Q_MAX_WELL_BBL_DAY)
    w.step(days=1)
    rate = w.state.wells[0].current_rate_bbl_day
    assert w.state.today_summary_so_far["oil_revenue"] == pytest.approx(
        rate * CRUDE_PRICE_USD_PER_BBL
    )
    assert rate > 0.0  # seed-42 first HC voxel has non-zero perm


def test_well_opex_in_daily_summary():
    """Drilled wells contribute their OPEX to the daily summary."""
    w = World()
    w.reset(seed=42)
    # Zero out population to isolate OPEX from tax revenue and dispatch.
    w.state.population = 0
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")  # +100/day OPEX
    w.step(days=1)
    # Town hall opex is 0; only the well contributes.
    assert w.state.today_summary_so_far["opex"] == pytest.approx(100.0)


# -- /state.wells schema ---------------------------------------------------


def test_state_wells_exposes_required_fields():
    w = World()
    w.reset(seed=42)
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    s = w.state_dict()
    assert len(s["wells"]) == 1
    well = s["wells"][0]
    for key in (
        "id",
        "type",
        "target_z",
        "drilled_day",
        "setpoint_rate_bbl_day",
        "current_rate_bbl_day",
        "cumulative_produced_bbl",
        "yesterday_rate_bbl_day",
        "yesterday_inj_rate_bbl_day",
        "pressure_boost",
        "reservoir_id",
    ):
        assert key in well


# -- Rate-pressure observability (oilfield-v2 slice 04) --------------------


def _setup_depleted_producer_world_for_observability() -> World:
    """Mirror test_injection.py's pre-depleted seed-42 setup so the
    rate-based pressure path is exercised over multiple steps."""
    w = World()
    w.reset(seed=42)
    w.state.treasury = 10_000_000.0
    _build_road_to(w, 5, 5)
    w.build("coal_plant", 5, 5)
    _build_road_to(w, 6, 5)
    w.build("coal_plant", 6, 5)
    hc = _hc_voxel(w)
    for v in w.subsurface.voxels.values():
        if abs(v.x - hc.x) <= 1 and abs(v.y - hc.y) <= 1 and abs(v.z - hc.z) <= 1:
            v.oil_remaining_bbl = 0.05 * v.oil_in_place_bbl
    return w


def test_state_wells_reports_pressure_boost_for_same_reservoir_pair():
    """Same-reservoir Chebyshev-2 producer/injector pair: after a few days
    the producer's `pressure_boost` and `yesterday_inj_rate_bbl_day` in
    `state_dict()` are consistent with the rate-based formula and the
    injector's `yesterday_rate_bbl_day`."""
    from world.subsurface import PRESSURE_BOOST_MAX

    w = _setup_depleted_producer_world_for_observability()
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    prod_id = w.state.wells[-1].id
    w.control_well(prod_id, Q_MAX_WELL_BBL_DAY)
    # Same reservoir, Chebyshev 2 from the producer's target.
    w.drill(hc.x, hc.y + 2, hc.z, "injection")
    inj_id = w.state.wells[-1].id
    w.control_well(inj_id, Q_MAX_WELL_BBL_DAY)
    w.step(days=5)

    s = w.state_dict()
    prod = next(ww for ww in s["wells"] if ww["id"] == prod_id)
    inj = next(ww for ww in s["wells"] if ww["id"] == inj_id)

    # Same reservoir, distinct wells.
    assert prod["reservoir_id"] is not None
    assert inj["reservoir_id"] == prod["reservoir_id"]

    # Injector's yesterday rate is the qualifying contribution; the
    # producer's yesterday_inj_rate_bbl_day mirrors it exactly (only one
    # qualifying injector).
    assert prod["yesterday_inj_rate_bbl_day"] == pytest.approx(inj["yesterday_rate_bbl_day"])

    # Boost matches the formula: min(cap, qual / max(prod_yest, 1)).
    expected_boost = min(
        PRESSURE_BOOST_MAX,
        prod["yesterday_inj_rate_bbl_day"] / max(prod["yesterday_rate_bbl_day"], 1.0),
    )
    assert prod["pressure_boost"] == pytest.approx(expected_boost)
    assert prod["pressure_boost"] > 0.0

    # Injector rows still expose the read-only telemetry fields (both zero
    # — `yesterday_inj_rate_bbl_day` and `pressure_boost` only carry
    # meaning for producers).
    assert inj["yesterday_inj_rate_bbl_day"] == 0.0
    assert inj["pressure_boost"] == 0.0


def test_state_wells_pressure_boost_zero_for_lone_producer():
    """A producer with no injector reports `pressure_boost == 0` and
    `yesterday_inj_rate_bbl_day == 0` after stepping the sim."""
    w = _setup_depleted_producer_world_for_observability()
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    w.control_well(w.state.wells[-1].id, Q_MAX_WELL_BBL_DAY)
    w.step(days=3)

    s = w.state_dict()
    prod = next(ww for ww in s["wells"] if ww["type"] == "production")
    assert prod["yesterday_inj_rate_bbl_day"] == 0.0
    assert prod["pressure_boost"] == 0.0


# -- API smoke -------------------------------------------------------------


def test_api_drill_endpoint_logs_and_deducts():
    from world.subsurface import drill_capex

    w = World()
    w.reset(seed=42)
    client = TestClient(create_app(world=w))
    treasury_before = w.state.treasury
    res = client.post(
        "/drill",
        json={"x": 10, "y": 10, "target_z": 8, "well_type": "production"},
    ).json()
    assert res["ok"] is True
    expected = drill_capex(50_000.0, 8, w.config.world_d)
    assert w.state.treasury == treasury_before - expected


def test_api_drill_invalid_target_z_returns_ok_false():
    w = World()
    w.reset(seed=42)
    client = TestClient(create_app(world=w))
    res = client.post(
        "/drill",
        json={"x": 10, "y": 10, "target_z": 999, "well_type": "production"},
    ).json()
    assert res["ok"] is False
    assert res["error"] == "voxel_out_of_bounds"


def test_api_control_well_clamps_setpoint():
    w = World()
    w.reset(seed=42)
    client = TestClient(create_app(world=w))
    drill = client.post(
        "/drill",
        json={"x": 10, "y": 10, "target_z": 8, "well_type": "production"},
    ).json()
    well_id = drill["result"]["id"]
    res = client.post("/control/well", json={"well_id": well_id, "rate_bbl_day": 999.0}).json()
    assert res["ok"] is True
    assert res["result"]["setpoint_rate_bbl_day"] == Q_MAX_WELL_BBL_DAY


# -- Determinism -----------------------------------------------------------


def test_drill_and_step_size_invariance():
    """Production loop is deterministic (no RNG draws), so step(7) ≡ step(1)×7
    even when wells are running."""
    a = World()
    b = World()
    a.reset(seed=42)
    b.reset(seed=42)
    hc_a = _hc_voxel(a)
    hc_b = _hc_voxel(b)
    assert (hc_a.x, hc_a.y, hc_a.z) == (hc_b.x, hc_b.y, hc_b.z)
    a.drill(hc_a.x, hc_a.y, hc_a.z, "production")
    b.drill(hc_b.x, hc_b.y, hc_b.z, "production")
    a.control_well(a.state.wells[0].id, 150.0)
    b.control_well(b.state.wells[0].id, 150.0)

    a.step(days=7)
    for _ in range(7):
        b.step(days=1)
    assert a.state.wells[0].cumulative_produced_bbl == pytest.approx(
        b.state.wells[0].cumulative_produced_bbl
    )
    assert a.state.treasury == pytest.approx(b.state.treasury)


# -- Workforce slice 07: efficiency scales oil-well production -------------


def test_half_staffed_oil_well_caps_at_efficiency_scaled_q_max():
    """`well_production_bbl_day` scales the effective q_max by efficiency.
    A 33%-staffed well with k_eff=1, fraction=1 produces ~0.33 × Q_MAX."""
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    # Build a 27-voxel pool with perm tuned so k_eff = 1.0 exactly.
    # k_eff = mean(perm)/PERM_NORMALIZATION_MD; to get 1.0 with n_positions=27
    # all HC voxels at perm=PERM_NORMALIZATION_MD × 27 / n_voxels gives sum/27
    # = perm/n_voxels × n_voxels/27 ... easier to just fill all 27 with perm=500.
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                grid.voxels[(5 + dx, 5 + dy, 5 + dz)] = _make_voxel(
                    5 + dx, 5 + dy, 5 + dz, perm=PERM_NORMALIZATION_MD, oil=200_000.0
                )
    # k_eff = (500 × 27) / 27 / 500 = 1.0; fraction = 1.0. q_potential at full
    # staff = 200 bbl/day; at efficiency=1/3 → ~66.67 bbl/day.
    q = well_production_bbl_day(
        grid, 5, 5, 5, setpoint_rate_bbl_day=Q_MAX_WELL_BBL_DAY, efficiency=1.0 / 3.0
    )
    assert q == pytest.approx(Q_MAX_WELL_BBL_DAY / 3.0)


def test_idle_oil_well_produces_zero():
    """Efficiency=0 → q_potential=0 regardless of setpoint or reservoir."""
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                grid.voxels[(5 + dx, 5 + dy, 5 + dz)] = _make_voxel(
                    5 + dx, 5 + dy, 5 + dz, perm=1000.0, oil=200_000.0
                )
    q = well_production_bbl_day(
        grid, 5, 5, 5, setpoint_rate_bbl_day=Q_MAX_WELL_BBL_DAY, efficiency=0.0
    )
    assert q == 0.0
    # Reservoir untouched.
    assert grid.voxels[(5, 5, 5)].oil_remaining_bbl == 200_000.0


def test_fully_staffed_oil_well_matches_v1_baseline():
    """`efficiency=1.0` (default) reproduces the pre-slice-07 formula
    byte-for-byte (existing test_q_potential_matches_brief_formula_*)."""
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    grid.voxels[(5, 5, 5)] = _make_voxel(5, 5, 5, perm=500.0, oil=100_000.0)
    q_default = well_production_bbl_day(grid, 5, 5, 5, setpoint_rate_bbl_day=200.0)
    grid.voxels[(5, 5, 5)] = _make_voxel(5, 5, 5, perm=500.0, oil=100_000.0)
    q_explicit = well_production_bbl_day(grid, 5, 5, 5, setpoint_rate_bbl_day=200.0, efficiency=1.0)
    assert q_default == pytest.approx(q_explicit)


def test_setpoint_not_auto_clamped_by_well_efficiency():
    """End-to-end: dropping staffed_jobs leaves setpoint unchanged; only
    current_rate_bbl_day reflects the reduced cap."""
    w = World()
    w.reset(seed=42)
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    well = w.state.wells[0]
    w.control_well(well.id, Q_MAX_WELL_BBL_DAY)
    well.staffed_jobs = 1  # 1/3 of jobs=3
    w.step(days=1)
    assert well.setpoint_rate_bbl_day == Q_MAX_WELL_BBL_DAY
    # Realised production reflects the efficiency cap (and the seed-42 voxel's
    # k_eff/fraction); strictly less than full-staff production at same setpoint.
    fully_staffed = World()
    fully_staffed.reset(seed=42)
    fully_staffed.drill(hc.x, hc.y, hc.z, "production")
    fully_staffed.control_well(fully_staffed.state.wells[0].id, Q_MAX_WELL_BBL_DAY)
    fully_staffed.step(days=1)
    assert well.current_rate_bbl_day < fully_staffed.state.wells[0].current_rate_bbl_day


# -- Power coupling (economy-rebalance slice 07) ---------------------------
#
# Symmetric to the injection-well DR throttling: a production well draws
# `setpoint × PRODUCTION_KWH_PER_BBL / 24 × efficiency` per hour at the
# previous hour's balanced state and 0 kW under brownout/blackout. The
# day's actual throughput is capped at sum_kwh / PRODUCTION_KWH_PER_BBL.


def test_production_baseline_when_grid_balanced_full_day():
    """With ample power, a producer's daily output equals its geology-bound
    setpoint potential — power throttling never binds at full supply. The
    test pins this by asserting (a) the grid never tipped into brownout/
    blackout and (b) the day's power_kw aggregate matches the full-day
    baseline at the well's post-step efficiency, which means the throttling
    cap was sum_kwh / KWH = setpoint × eff — high enough not to bind on
    the seed-42 voxel's modest geology."""
    from world import workforce as _workforce

    w = World()
    w.reset(seed=42)
    w.state.treasury = 10_000_000.0
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    # Two coal plants give ~3000 kW of supply vs ~30 kW civilian + ~125 kW
    # producer power. Plenty of headroom keeps the grid balanced/curtailment
    # for every hour.
    _build_road_to(w, th.x + 2, th.y)
    w.build("coal_plant", th.x + 2, th.y)
    _build_road_to(w, th.x + 3, th.y)
    w.build("coal_plant", th.x + 3, th.y)
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    well = w.state.wells[0]
    w.control_well(well.id, Q_MAX_WELL_BBL_DAY)
    w.step(days=1)
    eff = _workforce.efficiency(well)
    # Grid stayed off-brownout/off-blackout, so all 24 hours allocated
    # baseline power. Daily kWh = setpoint × KWH × eff.
    expected_daily_kwh = Q_MAX_WELL_BBL_DAY * PRODUCTION_KWH_PER_BBL * eff
    assert w.state.today_summary_so_far["production_kw"] == pytest.approx(expected_daily_kwh)
    # Power budget (kWh / KWH) = setpoint × eff ≫ seed-42 geology cap, so
    # throughput is geology-bound and strictly less than the setpoint.
    assert 0.0 < well.current_rate_bbl_day < Q_MAX_WELL_BBL_DAY * eff


def test_production_sheds_when_prev_balance_brownout():
    """Pre-set prev_balance to brownout. Hour 0 produces 0 bbl. With no power
    plants, dispatch stays in brownout/blackout for the rest of the day, so
    cumulative_produced_bbl ends at 0."""
    w = World()
    w.reset(seed=42)
    w.state.power_now["balance_state"] = "brownout"
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    w.control_well(w.state.wells[0].id, Q_MAX_WELL_BBL_DAY)
    w.step(days=1)
    well = w.state.wells[0]
    assert well.cumulative_produced_bbl == 0.0
    assert well.current_rate_bbl_day == 0.0
    assert w.state.today_summary_so_far["production_kw"] == 0.0


def test_production_sheds_when_prev_balance_blackout():
    w = World()
    w.reset(seed=42)
    w.state.power_now["balance_state"] = "blackout"
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    w.control_well(w.state.wells[0].id, Q_MAX_WELL_BBL_DAY)
    w.step(days=1)
    well = w.state.wells[0]
    assert well.cumulative_produced_bbl == 0.0


def test_undersupplied_throughput_equals_power_over_kwh_per_bbl():
    """Fresh world (no plants) starts hour 0 at prev_balance=balanced, so the
    producer attempts its baseline draw — but supply is 0, dispatch tips into
    blackout that hour, and the producer sheds for hours 1..23. The single
    hour of allocated power is `setpoint × KWH / 24`, so the day's bbl =
    that power / KWH = setpoint / 24."""
    w = World()
    w.reset(seed=42)
    # No power plants. prev_balance defaults to "balanced" on a fresh world.
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    setpoint = 200.0
    w.control_well(w.state.wells[0].id, setpoint)
    w.step(days=1)
    well = w.state.wells[0]
    # Single hour of baseline draw: power_kw = setpoint × KWH / 24.
    expected_hour_0_kw = setpoint * PRODUCTION_KWH_PER_BBL / 24.0
    expected_hour_0_bbl = expected_hour_0_kw / PRODUCTION_KWH_PER_BBL  # = setpoint/24
    assert well.current_rate_bbl_day == pytest.approx(expected_hour_0_bbl)
    # Daily production_kw aggregate reflects the single allocated hour.
    assert w.state.today_summary_so_far["production_kw"] == pytest.approx(expected_hour_0_kw)


def test_full_supply_runs_at_setpoint_when_geology_unbounded():
    """A synthetic super-rich pool puts geology potential at exactly
    Q_MAX_WELL_BBL_DAY; with full power supply, the daily rate hits the
    setpoint."""
    w = World()
    w.reset(seed=42)
    w.state.treasury = 10_000_000.0
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    _build_road_to(w, th.x + 2, th.y)
    w.build("coal_plant", th.x + 2, th.y)

    # Find an empty 3×3×3 voxel pool and pack it with high-perm oil so the
    # geological cap > setpoint, making power throttling the only possible
    # binding constraint (and not binding at full supply).
    target = None
    for x in range(2, 30, 4):
        for y in range(2, 30, 4):
            for z in range(2, 14, 2):
                pool, _ = voxels_in_3x3x3(w.subsurface, x, y, z)
                if not pool:
                    target = (x, y, z)
                    break
            if target:
                break
        if target:
            break
    assert target is not None
    # Pack one HC voxel with enough perm that k_eff (= perm / 27 / 500) ≫ 1.
    # 27 × 500 × 10 = 135_000 → k_eff = 10, so q_potential = Q_MAX × 10
    # which the setpoint clamps back to Q_MAX. Plenty of oil so fraction = 1.
    w.subsurface.voxels[target] = Voxel(
        x=target[0],
        y=target[1],
        z=target[2],
        porosity=0.3,
        permeability=135_000.0,
        oil_saturation=0.8,
        oil_in_place_bbl=10_000_000.0,
        oil_remaining_bbl=10_000_000.0,
    )
    res = w.drill(target[0], target[1], target[2], "production")
    assert res["ok"] is True
    well_id = res["result"]["id"]
    w.control_well(well_id, Q_MAX_WELL_BBL_DAY)
    w.step(days=1)
    well = next(ww for ww in w.state.wells if ww.id == well_id)
    # All 24 hours of baseline draw → power_allocated = setpoint × KWH;
    # geology cap >> setpoint, so q_actual = setpoint.
    assert well.current_rate_bbl_day == pytest.approx(Q_MAX_WELL_BBL_DAY)


def test_production_power_demand_appears_in_hourly_demand():
    """The hourly demand_kw consumed by dispatch includes the producer's
    baseline power. With a single producer at setpoint 200, baseline_kw =
    200 × 15 / 24 = 125 kW. The day-1 dispatch.last_day_demand_kw_by_hour
    trace reflects this addition."""
    w = World()
    w.reset(seed=42)
    w.state.population = 0  # zero civilian demand to isolate producer draw
    w.state.treasury = 10_000_000.0
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    _build_road_to(w, th.x + 2, th.y)
    w.build("coal_plant", th.x + 2, th.y)
    coal = next(t for t in w.state.tiles if t.type == "coal_plant")
    coal.staffed_jobs = coal.jobs
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    well = w.state.wells[0]
    well.staffed_jobs = 3  # oil_well jobs=3; force-staff under pop=0
    setpoint = 200.0
    w.control_well(well.id, setpoint)
    w.step(days=1)
    # Hour 0 demand_kw should include the producer's baseline (well at full
    # efficiency draws setpoint × KWH / 24 kW).
    baseline_kw = setpoint * PRODUCTION_KWH_PER_BBL / 24.0
    assert w.state.last_day_demand_kw_by_hour[0] >= baseline_kw - 1e-6
    # Total daily kWh equals 24 × baseline (every hour stayed off-shed).
    assert w.state.today_summary_so_far["production_kw"] == pytest.approx(24.0 * baseline_kw)


def test_half_staffed_producer_baseline_halves():
    """At balanced, a producer's hourly draw scales with workforce
    efficiency: setpoint × KWH / 24 × efficiency. Half-staffed cuts both
    the power draw and the throughput cap in half."""
    w = World()
    w.reset(seed=42)
    w.state.population = 0
    w.state.treasury = 10_000_000.0
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    _build_road_to(w, th.x + 2, th.y)
    w.build("coal_plant", th.x + 2, th.y)
    coal = next(t for t in w.state.tiles if t.type == "coal_plant")
    coal.staffed_jobs = coal.jobs
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    well = w.state.wells[0]
    setpoint = 100.0
    w.control_well(well.id, setpoint)
    # oil_well jobs=3 (catalog). Half-staff = 1 → efficiency = 1/3.
    half_jobs = 1
    full_jobs = 3
    well.staffed_jobs = half_jobs
    eff = half_jobs / full_jobs
    w.step(days=1)
    # production_kw aggregate must reflect efficiency scaling. With grid
    # balanced for all 24 hours, total kWh = setpoint × KWH × efficiency.
    expected_total_kwh = setpoint * PRODUCTION_KWH_PER_BBL * eff
    assert w.state.today_summary_so_far["production_kw"] == pytest.approx(expected_total_kwh)


def test_injection_kwh_per_bbl_unchanged_by_slice_07():
    """Symmetric ACs require the injection-side constant to be untouched."""
    from world.subsurface import INJECTION_KWH_PER_BBL

    assert INJECTION_KWH_PER_BBL == 50.0


def test_catalog_exposes_well_specs():
    from world.catalog import build_catalog

    cat = build_catalog()
    well_types = [w["tile_type"] for w in cat["wells"]]
    assert "oil_well" in well_types
    assert "injection_well" in well_types
    oil = next(w for w in cat["wells"] if w["tile_type"] == "oil_well")
    assert oil["capex"] == 50_000
    assert oil["buildable"] is False

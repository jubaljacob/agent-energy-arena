"""Injection wells + demand-response (slice 08, brief §4.5 + PRD).

Covers per-hour DR behavior driven by the previous hour's balance state
(shed at brownout/blackout, ramp at curtailment, baseline at balanced),
the pressure_boost term in the production formula, and the integration
showing that injection support keeps a depleting pool's capacity high.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from world.api import create_app
from world.sim import World
from world.subsurface import (
    INJECTION_KWH_PER_BBL,
    PRESSURE_BOOST_MAX,
    Q_MAX_WELL_BBL_DAY,
    SubsurfaceGrid,
    Voxel,
    pools_intersect,
    well_production_bbl_day,
)


def _hc_voxel(world: World) -> Voxel:
    return next(iter(world.subsurface.voxels.values()))


def _make_voxel(x: int, y: int, z: int, *, perm: float = 1000.0, oil: float = 1000.0) -> Voxel:
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


# -- pools_intersect -------------------------------------------------------


def test_pools_intersect_centers_within_two():
    assert pools_intersect(5, 5, 5, 5, 5, 5) is True
    assert pools_intersect(5, 5, 5, 7, 5, 5) is True  # exactly 2 apart
    assert pools_intersect(5, 5, 5, 7, 7, 7) is True  # corner overlap (2,2,2)


def test_pools_dont_intersect_when_one_axis_exceeds_two():
    assert pools_intersect(5, 5, 5, 8, 5, 5) is False  # 3 apart in x
    assert pools_intersect(5, 5, 5, 5, 5, 8) is False  # 3 apart in z


# -- pressure_boost in well_production_bbl_day ----------------------------


def test_pressure_boost_lifts_effective_fraction():
    """A pool drained to fraction=0.1 receives a 0.4 boost when the
    qualifying injection rate is 0.4× the producer's yesterday rate."""
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    grid.voxels[(5, 5, 5)] = _make_voxel(5, 5, 5, perm=1000.0, oil=1000.0)
    voxel = grid.voxels[(5, 5, 5)]
    voxel.oil_remaining_bbl = 100.0  # fraction = 0.1

    q_no_boost = well_production_bbl_day(grid, 5, 5, 5, setpoint_rate_bbl_day=Q_MAX_WELL_BBL_DAY)
    voxel.oil_remaining_bbl = 100.0  # reset

    q_with_boost = well_production_bbl_day(
        grid,
        5,
        5,
        5,
        setpoint_rate_bbl_day=Q_MAX_WELL_BBL_DAY,
        qualifying_inj_rate_bbl_day=40.0,
        producer_yesterday_rate_bbl_day=100.0,
    )
    assert q_with_boost > q_no_boost


def test_pressure_boost_capped_at_max():
    """`pressure_boost` is bounded at 0.5 regardless of inj/prod ratio."""
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    grid.voxels[(5, 5, 5)] = _make_voxel(5, 5, 5, perm=1000.0, oil=1000.0)
    voxel = grid.voxels[(5, 5, 5)]
    voxel.oil_remaining_bbl = 100.0  # fraction = 0.1

    # Massive injection rate vs tiny producer rate → ratio = huge, but
    # pressure_boost caps at 0.5.
    q_huge_inj = well_production_bbl_day(
        grid,
        5,
        5,
        5,
        setpoint_rate_bbl_day=Q_MAX_WELL_BBL_DAY,
        qualifying_inj_rate_bbl_day=10_000.0,
        producer_yesterday_rate_bbl_day=10.0,
    )
    voxel.oil_remaining_bbl = 100.0  # reset
    q_at_cap = well_production_bbl_day(
        grid,
        5,
        5,
        5,
        setpoint_rate_bbl_day=Q_MAX_WELL_BBL_DAY,
        qualifying_inj_rate_bbl_day=PRESSURE_BOOST_MAX * 10.0,  # exactly 0.5 boost
        producer_yesterday_rate_bbl_day=10.0,
    )
    assert q_huge_inj == pytest.approx(q_at_cap)


def test_pressure_boost_clipped_when_combined_above_one():
    """`effective_fraction = min(1.0, fraction + pressure_boost)`."""
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    grid.voxels[(5, 5, 5)] = _make_voxel(5, 5, 5, perm=1000.0, oil=1000.0)
    # fraction = 1.0; with any boost effective_fraction stays at 1.0.
    q_no_boost = well_production_bbl_day(grid, 5, 5, 5, setpoint_rate_bbl_day=Q_MAX_WELL_BBL_DAY)
    grid.voxels[(5, 5, 5)] = _make_voxel(5, 5, 5, perm=1000.0, oil=1000.0)
    q_full_boost = well_production_bbl_day(
        grid,
        5,
        5,
        5,
        setpoint_rate_bbl_day=Q_MAX_WELL_BBL_DAY,
        qualifying_inj_rate_bbl_day=10_000.0,
        producer_yesterday_rate_bbl_day=100.0,
    )
    assert q_full_boost == pytest.approx(q_no_boost)


def test_pressure_boost_zero_when_qualifying_inj_rate_zero():
    """No qualifying injectors → pressure_boost = 0 regardless of
    producer_yesterday_rate value (matches the day-of-drill state)."""
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    grid.voxels[(5, 5, 5)] = _make_voxel(5, 5, 5, perm=1000.0, oil=1000.0)
    grid.voxels[(5, 5, 5)].oil_remaining_bbl = 100.0  # fraction = 0.1
    q_default = well_production_bbl_day(grid, 5, 5, 5, setpoint_rate_bbl_day=Q_MAX_WELL_BBL_DAY)
    grid.voxels[(5, 5, 5)].oil_remaining_bbl = 100.0
    q_explicit = well_production_bbl_day(
        grid,
        5,
        5,
        5,
        setpoint_rate_bbl_day=Q_MAX_WELL_BBL_DAY,
        qualifying_inj_rate_bbl_day=0.0,
        producer_yesterday_rate_bbl_day=100.0,
    )
    assert q_default == pytest.approx(q_explicit)


def test_depleted_pool_returns_zero_q_actual_with_injection():
    """Bug fix (reservoir-scale slice 02): a fully drained pool
    (`V_remain == 0`) must return `q_actual == 0.0` even when a
    qualifying injector would otherwise lift `effective_fraction` via
    `pressure_boost`. Without the guard, the producer prints oil from
    a dead reservoir because `effective_fraction = 0 + 0.5 = 0.5`
    keeps `q_potential > 0` while the per-voxel drain loop short-
    circuits on `W = 0` — net effect: `cumulative_produced_bbl`
    advances every day forever."""
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    grid.voxels[(5, 5, 5)] = _make_voxel(5, 5, 5, perm=1000.0, oil=1000.0)
    voxel = grid.voxels[(5, 5, 5)]
    voxel.oil_remaining_bbl = 0.0  # pool fully drained, V_remain == 0

    q_actual_day1 = well_production_bbl_day(
        grid,
        5,
        5,
        5,
        setpoint_rate_bbl_day=200.0,
        qualifying_inj_rate_bbl_day=100.0,
        producer_yesterday_rate_bbl_day=200.0,
    )
    assert q_actual_day1 == 0.0
    # Voxel remaining stays at zero day-over-day.
    assert voxel.oil_remaining_bbl == 0.0

    q_actual_day2 = well_production_bbl_day(
        grid,
        5,
        5,
        5,
        setpoint_rate_bbl_day=200.0,
        qualifying_inj_rate_bbl_day=100.0,
        producer_yesterday_rate_bbl_day=200.0,
    )
    assert q_actual_day2 == 0.0
    assert voxel.oil_remaining_bbl == 0.0


# -- /drill injection well -------------------------------------------------


def test_drill_injection_well_deducts_capex():
    from world.subsurface import drill_capex

    w = World()
    w.reset(seed=42)
    treasury_before = w.state.treasury
    res = w.drill(10, 10, 8, "injection")
    assert res["ok"] is True
    expected_capex = drill_capex(30_000.0, 8, w.config.world_d)
    assert w.state.treasury == treasury_before - expected_capex
    assert len(w.state.wells) == 1
    assert w.state.wells[0].type == "injection"
    assert w.state.wells[0].capex_paid == expected_capex


def test_control_well_clamps_injection_setpoint():
    w = World()
    w.reset(seed=42)
    w.drill(10, 10, 8, "injection")
    res = w.control_well(w.state.wells[0].id, 250.0)
    assert res["ok"] is True
    assert res["result"]["setpoint_rate_bbl_day"] == Q_MAX_WELL_BBL_DAY


# -- DR behavior driven by previous hour's balance state -------------------


def test_injection_baseline_when_balanced():
    """At fresh world hour 0 (prev_balance="balanced"), injection runs at
    `setpoint × INJECTION_KWH_PER_BBL / 24` baseline. Over a full day the
    well injects exactly `setpoint` bbl."""
    w = World()
    w.reset(seed=42)
    # Build enough generation to keep the grid balanced (population=100 has
    # ~33 kW base load × hourly factor; injection adds setpoint × 50 / 24
    # which is ~208 kW for setpoint=100). 2 coal plants give plenty of
    # headroom and stay in must-run + ramp.
    w.state.treasury = 10_000_000.0
    w.build("coal_plant", 5, 5)
    w.build("coal_plant", 6, 5)
    w.drill(10, 10, 8, "injection")
    w.control_well(w.state.wells[0].id, 100.0)
    w.step(days=1)
    iw = w.state.wells[0]
    # Day 1: hour 0 prev=balanced (default) → baseline. Subsequent hours
    # depend on whether dispatch holds balanced — with 1600 kW capacity vs
    # ~50-300 kW total demand, R >> 1 = curtailment from very early.
    # Tighter test: cumulative > 0 at minimum.
    assert iw.cumulative_injected_bbl > 0.0
    # current_rate_bbl_day is the day's actual delivered.
    assert iw.current_rate_bbl_day == iw.cumulative_injected_bbl


def test_injection_sheds_when_prev_balance_brownout():
    """Pre-set the prev-hour balance to 'brownout'. First hour the well
    delivers 0 power. Subsequent hours, since with no plants supply=0 and
    inj=0 keep balance=blackout/brownout, the well stays shed all day."""
    w = World()
    w.reset(seed=42)
    # No plants → blackout for civilian load. But we want to test brownout
    # specifically, so manually pin prev_balance.
    w.state.power_now["balance_state"] = "brownout"
    # No injection-well-on-hour-0 power — so demand stays civilian only.
    w.drill(10, 10, 8, "injection")
    w.control_well(w.state.wells[0].id, 100.0)
    w.step(days=1)
    iw = w.state.wells[0]
    # Hour 0 prev=brownout → power=0 → bbl=0. Hour 0 dispatch with no plants
    # gives blackout, so hour 1 prev=blackout → power=0. And so on. No bbl
    # injected at any hour.
    assert iw.cumulative_injected_bbl == 0.0
    assert w.state.today_summary_so_far["injection_kw"] == 0.0


def test_injection_sheds_when_prev_balance_blackout():
    w = World()
    w.reset(seed=42)
    w.state.power_now["balance_state"] = "blackout"
    w.drill(10, 10, 8, "injection")
    w.control_well(w.state.wells[0].id, 100.0)
    w.step(days=1)
    iw = w.state.wells[0]
    assert iw.cumulative_injected_bbl == 0.0


def test_injection_ramps_at_curtailment():
    """When prev_balance='curtailment', injection power = min(2 × baseline,
    cap). With setpoint=50, baseline_kw=104.17, 2× = 208.33; cap = 416.67;
    so power = 208.33 kW, bbl/hr = 208.33/50 = 4.166. Per day = 100 bbl."""
    w = World()
    w.reset(seed=42)
    # Pre-set prev_balance and rig a fully-renewable supply so dispatch
    # holds curtailment hour-after-hour. With population=0, civilian demand
    # is 0; any positive renewable supply > 1.15× triggers curtailment.
    w.state.population = 0
    w.state.treasury = 10_000_000.0
    w.build("coal_plant", 5, 5)  # forces some baseline
    coal = next(t for t in w.state.tiles if t.type == "coal_plant")
    coal.staffed_jobs = coal.jobs  # pop=0; force-staff so plant dispatches
    w.state.power_now["balance_state"] = "curtailment"
    w.drill(10, 10, 8, "injection")
    iw = w.state.wells[0]
    iw.staffed_jobs = 2  # injection_well jobs=2; force-staff under pop=0
    setpoint = 50.0
    w.control_well(iw.id, setpoint)
    w.step(days=1)
    # If curtailment held all 24 hours, cumulative = 2 × setpoint = 100 bbl.
    # In practice, supply is ~200 kW (coal must-run) and demand from
    # injection alone is ~200 kW too, so dispatch may move to balanced after
    # hour 0. The robust assertion: hour 0 ramp mode → first hour bbl > baseline.
    # First-hour bbl/hr = min(2×baseline, cap)/INJ. baseline_kw = 50×50/24 = 104.17;
    # 2×=208.33; cap=416.67. power=208.33, bbl=4.166.
    # Across 24 hours injection_kw = sum of hourly kW.
    # Lower bound: at least the first-hour ramp delivered.
    expected_first_hour_bbl = min(2 * setpoint, Q_MAX_WELL_BBL_DAY) / 24.0
    assert iw.cumulative_injected_bbl >= expected_first_hour_bbl - 1e-9


def test_injection_ramp_capped_at_hardware_max():
    """At setpoint=200 (max), 2 × baseline = 2 × 416.67 = 833.33 kW but
    cap = 416.67 kW. So curtailment doesn't exceed baseline."""
    w = World()
    w.reset(seed=42)
    w.state.population = 0
    w.state.power_now["balance_state"] = "curtailment"
    w.drill(10, 10, 8, "injection")
    w.control_well(w.state.wells[0].id, 200.0)
    # First-hour power should be exactly cap: 200 × 50 / 24 = 416.67 kW.
    # bbl/hr = 416.67 / 50 = 8.333.
    # Over 24 hours of curtailment: cumulative_injected_bbl <= 200.0 bbl/day.
    w.step(days=1)
    iw = w.state.wells[0]
    # Cap assertion: the day's injection bbl ≤ max bbl/day from hardware.
    assert iw.cumulative_injected_bbl <= Q_MAX_WELL_BBL_DAY + 1e-9


def test_prev_hour_lag_one_hour():
    """Hour 0 uses prev_balance from before /step started; hour 1 uses
    hour-0's actual balance. A single-day step exercises both transitions.

    Population stays at the default 100 so civilian demand > 0; otherwise
    an injection-only demand collapses to "balanced" the moment the well
    sheds (compute_balance_state returns "balanced" at demand=0), and the
    next hour pumps again — masking the lag we're trying to test.
    """
    w = World()
    w.reset(seed=42)
    # No plants → hour 0 prev=balanced → inj baseline draws against zero
    # supply with civilian load → blackout. Hours 1-23: prev=blackout,
    # inj sheds; civilian demand still > 0 with zero supply → blackout
    # holds, so injection stays shed for the rest of the day.
    w.state.power_now["balance_state"] = "balanced"
    w.drill(10, 10, 8, "injection")
    setpoint = 100.0
    w.control_well(w.state.wells[0].id, setpoint)
    w.step(days=1)
    iw = w.state.wells[0]
    # Hour-0 baseline: bbl = setpoint × 50 / 24 / 50 = setpoint / 24.
    expected_hour_0_bbl = setpoint / 24.0
    assert iw.cumulative_injected_bbl == pytest.approx(expected_hour_0_bbl)


def test_fresh_world_hour_0_treats_prev_as_balanced():
    """No prior /step has run; the default state.power_now['balance_state']
    is 'balanced'. Hour 0 should therefore run baseline injection."""
    w = World()
    w.reset(seed=42)
    assert w.state.power_now["balance_state"] == "balanced"


# -- injection_kw daily summary --------------------------------------------


def test_injection_kw_summary_accumulates_hourly_power():
    """`today_summary_so_far['injection_kw']` reports the sum of hourly
    injection kW values (kWh delivered). Over 1 day at baseline, this is
    24 × (setpoint × 50 / 24) = setpoint × 50 kWh."""
    w = World()
    w.reset(seed=42)
    w.state.population = 0
    w.state.treasury = 10_000_000.0
    # Build enough plant capacity to hold balanced.
    w.build("coal_plant", 5, 5)
    # Force the coal plant fully staffed even though pop=0; the workforce
    # module's max(0, pop-employed) clamp keeps the inconsistency benign.
    coal = next(t for t in w.state.tiles if t.type == "coal_plant")
    coal.staffed_jobs = coal.jobs
    w.state.power_now["balance_state"] = "balanced"
    w.drill(10, 10, 8, "injection")
    inj = w.state.wells[0]
    inj.staffed_jobs = 2  # injection_well jobs=2
    setpoint = 100.0
    w.control_well(inj.id, setpoint)
    w.step(days=1)
    # When the grid stays balanced for all 24 hours, injection_kw = 24 ×
    # (setpoint × 50 / 24) = setpoint × 50 = 5000 kWh.
    # If dispatch flips to curtailment partway, kWh climbs further.
    # Lower bound: baseline-only delivery.
    assert w.state.today_summary_so_far["injection_kw"] >= setpoint * INJECTION_KWH_PER_BBL - 1e-6


# -- Pressure-boost integration: production capacity rises with injection --


def test_pressure_boost_keeps_late_game_capacity_high():
    """Two identical depleted-pool worlds. World A has an injection well in
    the same reservoir as the producer, at Chebyshev distance 2 from the
    producer's target (outside the breakthrough gate); World B does not.
    After many days, A's production well delivers strictly more crude
    than B's because A's qualifying injection rate is non-zero."""

    def setup_world(with_injection: bool) -> World:
        w = World()
        w.reset(seed=42)
        w.state.treasury = 10_000_000.0
        # Build enough renewable + fossil capacity to keep grid healthy.
        w.build("coal_plant", 5, 5)
        w.build("coal_plant", 6, 5)
        # Pre-deplete the pool around a known HC voxel by overwriting
        # oil_remaining to 5% of OOIP.
        hc = _hc_voxel(w)
        for v in w.subsurface.voxels.values():
            if abs(v.x - hc.x) <= 1 and abs(v.y - hc.y) <= 1 and abs(v.z - hc.z) <= 1:
                v.oil_remaining_bbl = 0.05 * v.oil_in_place_bbl
        w.drill(hc.x, hc.y, hc.z, "production")
        w.control_well(w.state.wells[-1].id, Q_MAX_WELL_BBL_DAY)
        if with_injection:
            # Seed-42 reservoir-1 has a second voxel at (29, 20, 14),
            # Chebyshev 2 from the producer's (29, 18, 14) target — outside
            # the breakthrough gate (cheb > 1) and same reservoir_id.
            w.drill(hc.x, hc.y + 2, hc.z, "injection")
            w.control_well(w.state.wells[-1].id, Q_MAX_WELL_BBL_DAY)
        return w

    a = setup_world(True)
    b = setup_world(False)
    # 30 sim days, in 7-day chunks (step() caps at 7).
    for _ in range(5):
        a.step(days=6)
        b.step(days=6)
    # Pull the production well (the first well drilled in each).
    prod_a = next(ww for ww in a.state.wells if ww.type == "production")
    prod_b = next(ww for ww in b.state.wells if ww.type == "production")
    assert prod_a.cumulative_produced_bbl > prod_b.cumulative_produced_bbl


def test_injection_pool_must_intersect_production_pool():
    """An injection well outside the production well's 3×3×3 neighborhood
    (Chebyshev > 2 on any axis) contributes no pressure_boost."""
    w = World()
    w.reset(seed=42)
    w.state.treasury = 10_000_000.0
    hc = _hc_voxel(w)
    # Pre-deplete the neighborhood of hc so fraction is small.
    for v in w.subsurface.voxels.values():
        if abs(v.x - hc.x) <= 1 and abs(v.y - hc.y) <= 1 and abs(v.z - hc.z) <= 1:
            v.oil_remaining_bbl = 0.05 * v.oil_in_place_bbl
    w.drill(hc.x, hc.y, hc.z, "production")
    prod_id = w.state.wells[-1].id
    w.control_well(prod_id, Q_MAX_WELL_BBL_DAY)
    # Place injection well 5 cells away in x — well outside Chebyshev ≤ 2.
    far_x = hc.x + 5 if hc.x + 5 < w.config.world_w else hc.x - 5
    w.drill(far_x, hc.y, hc.z, "injection")
    inj_id = w.state.wells[-1].id
    w.control_well(inj_id, Q_MAX_WELL_BBL_DAY)
    for _ in range(2):
        w.step(days=5)
    # The injection has accumulated a lot of bbl, but it's NOT in the prod
    # pool. Compare against a control world with no injection well at all.
    b = World()
    b.reset(seed=42)
    b.state.treasury = 10_000_000.0
    for v in b.subsurface.voxels.values():
        if abs(v.x - hc.x) <= 1 and abs(v.y - hc.y) <= 1 and abs(v.z - hc.z) <= 1:
            v.oil_remaining_bbl = 0.05 * v.oil_in_place_bbl
    b.drill(hc.x, hc.y, hc.z, "production")
    b.control_well(b.state.wells[-1].id, Q_MAX_WELL_BBL_DAY)
    for _ in range(2):
        b.step(days=5)
    prod_a = next(ww for ww in w.state.wells if ww.id == prod_id)
    prod_b = next(ww for ww in b.state.wells if ww.type == "production")
    # Same production trajectory because the far-away injection contributes 0.
    assert prod_a.cumulative_produced_bbl == pytest.approx(prod_b.cumulative_produced_bbl)


# -- Rate-based pressure mechanics (oilfield-v2 slice 03) ------------------


def _setup_depleted_producer_world() -> World:
    """Build a fresh seed-42 world with the canonical hc voxel's pool
    pre-depleted to 5% OOIP and a coal-backed grid. Caller drills
    producer + (optional) injector and steps the sim."""
    w = World()
    w.reset(seed=42)
    w.state.treasury = 10_000_000.0
    w.build("coal_plant", 5, 5)
    w.build("coal_plant", 6, 5)
    hc = _hc_voxel(w)
    for v in w.subsurface.voxels.values():
        if abs(v.x - hc.x) <= 1 and abs(v.y - hc.y) <= 1 and abs(v.z - hc.z) <= 1:
            v.oil_remaining_bbl = 0.05 * v.oil_in_place_bbl
    return w


def test_producer_with_no_injector_has_zero_pressure_boost():
    """The qualifying_inj_rate sum is 0 → production matches the no-boost
    baseline (rate-pressure-physics AC: drilling a producer with no
    injector → pressure_boost = 0)."""
    a = _setup_depleted_producer_world()
    hc = _hc_voxel(a)
    a.drill(hc.x, hc.y, hc.z, "production")
    a.control_well(a.state.wells[-1].id, Q_MAX_WELL_BBL_DAY)
    for _ in range(2):
        a.step(days=5)

    b = _setup_depleted_producer_world()
    b.drill(hc.x, hc.y, hc.z, "production")
    b.control_well(b.state.wells[-1].id, Q_MAX_WELL_BBL_DAY)
    for _ in range(2):
        b.step(days=5)
    # Identical worlds, identical trajectories.
    a_prod = next(ww for ww in a.state.wells if ww.type == "production")
    b_prod = next(ww for ww in b.state.wells if ww.type == "production")
    assert a_prod.cumulative_produced_bbl == pytest.approx(b_prod.cumulative_produced_bbl)


def test_same_reservoir_chebyshev_two_lifts_production_from_day_two():
    """Day 1 both yesterday rates are 0 → boost=0 day 1; on day 2 the
    producer's and injector's yesterday rates are non-zero → boost > 0
    and the producer outproduces a no-injector control world."""
    a = _setup_depleted_producer_world()
    hc = _hc_voxel(a)
    a.drill(hc.x, hc.y, hc.z, "production")
    a.control_well(a.state.wells[-1].id, Q_MAX_WELL_BBL_DAY)
    # Same-reservoir injector at Chebyshev 2 (seed-42 R1: (29,18,14)+(29,20,14)).
    a.drill(hc.x, hc.y + 2, hc.z, "injection")
    a.control_well(a.state.wells[-1].id, Q_MAX_WELL_BBL_DAY)
    # Step 5 days so yesterday rates ramp up and boost kicks in.
    a.step(days=5)

    b = _setup_depleted_producer_world()
    b.drill(hc.x, hc.y, hc.z, "production")
    b.control_well(b.state.wells[-1].id, Q_MAX_WELL_BBL_DAY)
    b.step(days=5)

    a_prod = next(ww for ww in a.state.wells if ww.type == "production")
    b_prod = next(ww for ww in b.state.wells if ww.type == "production")
    assert a_prod.cumulative_produced_bbl > b_prod.cumulative_produced_bbl


def test_chebyshev_one_breakthrough_gate_yields_zero_boost():
    """Same-reservoir injector at Chebyshev 1 (inside the producer's
    3×3×3 pool) is filtered out by the breakthrough gate; production
    matches the no-injector control."""
    a = _setup_depleted_producer_world()
    hc = _hc_voxel(a)
    a.drill(hc.x, hc.y, hc.z, "production")
    a.control_well(a.state.wells[-1].id, Q_MAX_WELL_BBL_DAY)
    # Same reservoir voxel inside the 3×3×3 pool — seed-42 R1 has (28,18,15)
    # which is Chebyshev 1 from (29,18,14).
    a.drill(28, 18, 15, "injection")
    a.control_well(a.state.wells[-1].id, Q_MAX_WELL_BBL_DAY)
    a.step(days=5)

    b = _setup_depleted_producer_world()
    b.drill(hc.x, hc.y, hc.z, "production")
    b.control_well(b.state.wells[-1].id, Q_MAX_WELL_BBL_DAY)
    b.step(days=5)

    a_prod = next(ww for ww in a.state.wells if ww.type == "production")
    b_prod = next(ww for ww in b.state.wells if ww.type == "production")
    assert a_prod.cumulative_produced_bbl == pytest.approx(b_prod.cumulative_produced_bbl)


def test_different_reservoirs_yields_zero_boost():
    """An injector in a different reservoir at Chebyshev > 1 still
    contributes 0 to qualifying_inj_rate (the reservoir_id mismatch is
    the binding filter)."""
    a = _setup_depleted_producer_world()
    hc = _hc_voxel(a)
    a.drill(hc.x, hc.y, hc.z, "production")
    a.control_well(a.state.wells[-1].id, Q_MAX_WELL_BBL_DAY)
    # Seed-42 R2 starts at (28, 31, 4) — different reservoir from R1.
    a.drill(28, 31, 4, "injection")
    a.control_well(a.state.wells[-1].id, Q_MAX_WELL_BBL_DAY)
    a_prod_well = next(ww for ww in a.state.wells if ww.type == "production")
    a_inj_well = next(ww for ww in a.state.wells if ww.type == "injection")
    assert a_prod_well.reservoir_id == 1
    assert a_inj_well.reservoir_id == 2
    a.step(days=5)

    b = _setup_depleted_producer_world()
    b.drill(hc.x, hc.y, hc.z, "production")
    b.control_well(b.state.wells[-1].id, Q_MAX_WELL_BBL_DAY)
    b.step(days=5)

    a_prod = next(ww for ww in a.state.wells if ww.type == "production")
    b_prod = next(ww for ww in b.state.wells if ww.type == "production")
    assert a_prod.cumulative_produced_bbl == pytest.approx(b_prod.cumulative_produced_bbl)


def test_idled_injector_drops_boost_to_zero_next_day():
    """After running the producer + injector at steady state for several
    days, idling the injector (setpoint=0) leaves the boost engaged for
    exactly one more day (today's pressure_boost reads yesterday's inj
    rate, which is still > 0), then drops to 0 on the following day."""
    w = _setup_depleted_producer_world()
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    w.control_well(w.state.wells[-1].id, Q_MAX_WELL_BBL_DAY)
    w.drill(hc.x, hc.y + 2, hc.z, "injection")
    inj_id = w.state.wells[-1].id
    w.control_well(inj_id, Q_MAX_WELL_BBL_DAY)
    w.step(days=5)
    rate_with_boost = next(
        ww.current_rate_bbl_day for ww in w.state.wells if ww.type == "production"
    )
    # Idle the injector. Snapshot today's yesterday rate first so we can
    # confirm it drops on the subsequent day.
    w.control_well(inj_id, 0.0)
    w.step(days=1)
    inj_after_one_idle = next(ww for ww in w.state.wells if ww.id == inj_id)
    assert inj_after_one_idle.current_rate_bbl_day == 0.0
    # One more day: injector's yesterday_rate snapshot is now 0, so the
    # boost is gone and the producer matches the no-injector trajectory.
    w.step(days=3)
    rate_no_boost = next(ww.current_rate_bbl_day for ww in w.state.wells if ww.type == "production")
    assert rate_no_boost < rate_with_boost


def test_two_qualifying_injectors_sum_in_numerator():
    """Two injection wells in the same reservoir at Chebyshev > 1 from
    the producer both contribute to qualifying_inj_rate. The combined
    boost exceeds a single-injector world's boost (subject to the 0.5
    cap)."""
    # Use a constructed grid + direct call: integration in seed-42 is
    # constrained by reservoir geometry (R1 only exposes one Chebyshev-2
    # peer to (29,18,14)).
    grid = SubsurfaceGrid(width=10, height=10, depth=10)
    grid.voxels[(5, 5, 5)] = _make_voxel(5, 5, 5, perm=1000.0, oil=1000.0)
    grid.voxels[(5, 5, 5)].oil_remaining_bbl = 100.0  # fraction = 0.1
    q_single = well_production_bbl_day(
        grid,
        5,
        5,
        5,
        setpoint_rate_bbl_day=Q_MAX_WELL_BBL_DAY,
        qualifying_inj_rate_bbl_day=10.0,  # one injector at 10 bbl/day
        producer_yesterday_rate_bbl_day=100.0,
    )
    grid.voxels[(5, 5, 5)].oil_remaining_bbl = 100.0  # reset
    q_two = well_production_bbl_day(
        grid,
        5,
        5,
        5,
        setpoint_rate_bbl_day=Q_MAX_WELL_BBL_DAY,
        qualifying_inj_rate_bbl_day=20.0,  # two injectors summing to 20
        producer_yesterday_rate_bbl_day=100.0,
    )
    assert q_two > q_single


def test_yesterday_rate_snapshot_taken_before_production_loop():
    """`yesterday_rate_bbl_day` is updated at the top of each /step's
    daily loop, before the producer's pressure_boost is computed. After
    one step, the producer's yesterday rate equals the rate the well
    *just* produced (i.e. day-1's value)."""
    w = World()
    w.reset(seed=42)
    hc = _hc_voxel(w)
    w.drill(hc.x, hc.y, hc.z, "production")
    w.control_well(w.state.wells[-1].id, Q_MAX_WELL_BBL_DAY)
    # Day 0: yesterday_rate = 0 (drill-day).
    assert w.state.wells[0].yesterday_rate_bbl_day == 0.0
    w.step(days=1)
    # After day 1, current_rate_bbl_day is the day-1 actual; yesterday is
    # what was current at the start of day-1, which was 0 (the drill-day
    # value).
    well = w.state.wells[0]
    assert well.yesterday_rate_bbl_day == 0.0
    day1_rate = well.current_rate_bbl_day
    assert day1_rate > 0.0
    w.step(days=1)
    well = w.state.wells[0]
    # Day 2 starts by snapshotting current (=day1) into yesterday.
    assert well.yesterday_rate_bbl_day == pytest.approx(day1_rate)


def test_state_wells_exposes_yesterday_rate():
    """/state.wells stamps `yesterday_rate_bbl_day` so agents can audit
    the rate-based pressure calculation (PRD §"Agent author")."""
    w = World()
    w.reset(seed=42)
    w.drill(10, 10, 8, "production")
    w.step(days=2)
    s = w.state_dict()
    well = s["wells"][0]
    assert "yesterday_rate_bbl_day" in well


# -- /state.wells schema for injection wells -------------------------------


def test_state_wells_exposes_cumulative_injected_for_injection_wells():
    w = World()
    w.reset(seed=42)
    w.drill(10, 10, 8, "injection")
    s = w.state_dict()
    assert len(s["wells"]) == 1
    iw = s["wells"][0]
    assert iw["type"] == "injection"
    assert iw["cumulative_injected_bbl"] == 0.0


# -- Step-size invariance with injection wells ------------------------------


def test_step_size_invariance_with_injection_wells():
    """Determinism still holds with injection wells running."""
    a = World()
    b = World()
    a.reset(seed=42)
    b.reset(seed=42)
    a.state.treasury = 10_000_000.0
    b.state.treasury = 10_000_000.0
    a.build("coal_plant", 5, 5)
    b.build("coal_plant", 5, 5)
    hc = _hc_voxel(a)
    a.drill(hc.x, hc.y, hc.z, "production")
    b.drill(hc.x, hc.y, hc.z, "production")
    a.drill(hc.x + 1, hc.y, hc.z, "injection")
    b.drill(hc.x + 1, hc.y, hc.z, "injection")
    a.control_well("production-1", 150.0)
    b.control_well("production-1", 150.0)
    a.control_well("injection-2", 100.0)
    b.control_well("injection-2", 100.0)
    a.step(days=7)
    for _ in range(7):
        b.step(days=1)
    a_prod = next(ww for ww in a.state.wells if ww.type == "production")
    b_prod = next(ww for ww in b.state.wells if ww.type == "production")
    a_inj = next(ww for ww in a.state.wells if ww.type == "injection")
    b_inj = next(ww for ww in b.state.wells if ww.type == "injection")
    assert a_prod.cumulative_produced_bbl == pytest.approx(b_prod.cumulative_produced_bbl)
    assert a_inj.cumulative_injected_bbl == pytest.approx(b_inj.cumulative_injected_bbl)
    assert a.state.treasury == pytest.approx(b.state.treasury)


# -- Workforce slice 07: efficiency scales injection baseline + DR cap -----


def test_half_staffed_injection_baseline_halves():
    """At balanced, an injection well's hourly draw equals
    `setpoint × KWH_PER_BBL / 24 × efficiency`. Half-staffed (jobs=2,
    staffed=1) cuts that figure exactly in half."""
    w = World()
    w.reset(seed=42)
    w.state.population = 0
    w.state.treasury = 10_000_000.0
    w.build("coal_plant", 5, 5)
    coal = next(t for t in w.state.tiles if t.type == "coal_plant")
    coal.staffed_jobs = coal.jobs
    w.state.power_now["balance_state"] = "balanced"
    w.drill(10, 10, 8, "injection")
    inj = w.state.wells[0]
    inj.staffed_jobs = 1  # injection_well jobs=2
    setpoint = 100.0
    w.control_well(inj.id, setpoint)
    w.step(days=1)
    # Hour 0 prev=balanced, well delivers eff_baseline = setpoint×50/24×0.5 kW
    # → bbl/hr = setpoint/24×0.5. Over a day held at balanced, total ≈
    # setpoint × 0.5. Loose lower bound (only first hour is guaranteed
    # balanced from the pre-set prev_balance).
    expected_first_hour_bbl = setpoint / 24.0 * 0.5
    # The well injected at half-rate hour 0 at minimum.
    assert inj.cumulative_injected_bbl >= expected_first_hour_bbl - 1e-9
    # Upper bound: even if every hour stayed in curtailment mode
    # (max scaled DR cap), bbl/day cannot exceed setpoint × efficiency
    # (since 2×baseline gets capped by hardware cap, but cap is also
    # scaled by efficiency; min(2×base×eff, cap×eff) = eff × min(2×base, cap)
    # → bbl/day ≤ Q_MAX × eff = 100). With setpoint=100, 2×baseline=200×eff=100
    # so curtailment-bbl/hr = setpoint×eff/12, /day = setpoint×eff×2 = 100.
    assert inj.cumulative_injected_bbl <= setpoint * 2.0 * 0.5 + 1e-9


def test_idle_injection_draws_zero_baseline():
    """staffed_jobs=0 → injection draws 0 kW at every hour and injects 0 bbl."""
    w = World()
    w.reset(seed=42)
    w.state.population = 0
    w.state.treasury = 10_000_000.0
    w.build("coal_plant", 5, 5)
    coal = next(t for t in w.state.tiles if t.type == "coal_plant")
    coal.staffed_jobs = coal.jobs
    w.state.power_now["balance_state"] = "balanced"
    w.drill(10, 10, 8, "injection")
    inj = w.state.wells[0]
    inj.staffed_jobs = 0
    w.control_well(inj.id, 200.0)
    w.step(days=1)
    assert inj.cumulative_injected_bbl == 0.0
    assert w.state.today_summary_so_far["injection_kw"] == 0.0


def test_idle_injection_offers_zero_dr_headroom():
    """Even forced into curtailment, an idle well never ramps."""
    w = World()
    w.reset(seed=42)
    w.state.population = 0
    w.state.treasury = 10_000_000.0
    w.build("coal_plant", 5, 5)
    coal = next(t for t in w.state.tiles if t.type == "coal_plant")
    coal.staffed_jobs = coal.jobs
    w.state.power_now["balance_state"] = "curtailment"
    w.drill(10, 10, 8, "injection")
    inj = w.state.wells[0]
    inj.staffed_jobs = 0
    w.control_well(inj.id, 100.0)
    w.step(days=1)
    assert inj.cumulative_injected_bbl == 0.0


def test_half_staffed_injection_pool_intersection_still_recovers_pressure_proportionally():
    """A half-staffed injection well injects half the bbl per day, so its
    cumulative_injected_bbl feeding pressure_boost is exactly half what a
    fully-staffed well would contribute. Pool-intersection itself (geological)
    is unchanged — the proportionally reduced recovery follows from the
    halved injection rate, not from any intersection-mode toggle."""

    def setup(staffed: int) -> World:
        w = World()
        w.reset(seed=42)
        w.state.treasury = 10_000_000.0
        w.build("coal_plant", 5, 5)
        w.build("coal_plant", 6, 5)
        hc = _hc_voxel(w)
        for v in w.subsurface.voxels.values():
            if abs(v.x - hc.x) <= 1 and abs(v.y - hc.y) <= 1 and abs(v.z - hc.z) <= 1:
                v.oil_remaining_bbl = 0.05 * v.oil_in_place_bbl
        w.drill(hc.x, hc.y, hc.z, "production")
        w.control_well(w.state.wells[-1].id, Q_MAX_WELL_BBL_DAY)
        # Same-reservoir injector at Chebyshev 2 (outside breakthrough gate).
        # Setpoint is kept low so the half-staffed injection rate keeps
        # `qualifying_inj / max(prod_yest, 1)` below the 0.5 cap — without
        # this, both full and half deliver the same capped boost.
        w.drill(hc.x, hc.y + 2, hc.z, "injection")
        inj = w.state.wells[-1]
        w.control_well(inj.id, 8.0)
        inj.staffed_jobs = staffed
        return w

    full = setup(2)  # injection_well jobs=2 → fully staffed
    half = setup(1)
    for _ in range(3):
        full.step(days=5)
        half.step(days=5)
    full_inj = next(ww for ww in full.state.wells if ww.type == "injection")
    half_inj = next(ww for ww in half.state.wells if ww.type == "injection")
    # Half-staffed well injects strictly less than fully staffed (the linear
    # halving is approximate end-to-end because the production well consumes
    # crude, but the inequality is robust).
    assert half_inj.cumulative_injected_bbl < full_inj.cumulative_injected_bbl
    assert half_inj.cumulative_injected_bbl > 0.0
    # Production trajectory also strictly less for half-staffed inj → less
    # pressure_boost → less production.
    full_prod = next(ww for ww in full.state.wells if ww.type == "production")
    half_prod = next(ww for ww in half.state.wells if ww.type == "production")
    assert half_prod.cumulative_produced_bbl < full_prod.cumulative_produced_bbl


# -- API smoke -------------------------------------------------------------


def test_api_drill_injection_well():
    from world.subsurface import drill_capex

    w = World()
    w.reset(seed=42)
    client = TestClient(create_app(world=w))
    res = client.post(
        "/drill",
        json={"x": 10, "y": 10, "target_z": 8, "well_type": "injection"},
    ).json()
    assert res["ok"] is True
    assert res["result"]["type"] == "injection"
    expected = drill_capex(30_000.0, 8, w.config.world_d)
    assert w.state.treasury == 500_000.0 - expected


def test_api_control_injection_well():
    w = World()
    w.reset(seed=42)
    client = TestClient(create_app(world=w))
    drill = client.post(
        "/drill",
        json={"x": 10, "y": 10, "target_z": 8, "well_type": "injection"},
    ).json()
    well_id = drill["result"]["id"]
    res = client.post("/control/well", json={"well_id": well_id, "rate_bbl_day": 75.0}).json()
    assert res["ok"] is True
    assert res["result"]["setpoint_rate_bbl_day"] == 75.0

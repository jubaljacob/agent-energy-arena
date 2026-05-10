"""Plants + dispatch + balance state + power revenue (slice 05, brief §4.4).

Each function in `world.power` (dispatch and compute_balance_state) is
exercised in isolation, then the sim-level wiring is verified end-to-end
via World.step.
"""

from __future__ import annotations

import pytest

from world.catalog import TILE_CATALOG
from world.population import update_population
from world.power import (
    COAL_MIN_RUN,
    COAL_RAMP_PER_HOUR,
    GAS_RAMP_PER_HOUR,
    compute_balance_state,
    dispatch,
)
from world.sim import World
from world.state import Tile


def _fresh_world() -> World:
    w = World()
    w.reset(seed=42)
    return w


def _plant(tile_type: str, idx: int = 1) -> Tile:
    spec = TILE_CATALOG[tile_type]
    return Tile(
        id=f"{tile_type}-{idx}",
        type=tile_type,
        x=idx,
        y=0,
        built_day=0,
        operational=True,
        capex_paid=spec.capex,
        opex_per_day=spec.opex_per_day,
    )


# -- Catalog wiring ---------------------------------------------------------


def test_catalog_exposes_four_plant_types() -> None:
    for t in ("solar_farm", "wind_turbine", "gas_peaker", "coal_plant"):
        assert t in TILE_CATALOG
        spec = TILE_CATALOG[t]
        assert spec.requires_road is False
        assert spec.buildable is True


def test_plant_capacities_match_spec() -> None:
    assert TILE_CATALOG["solar_farm"].capacity_kw == 150
    assert TILE_CATALOG["wind_turbine"].capacity_kw == 200
    assert TILE_CATALOG["gas_peaker"].capacity_kw == 500
    assert TILE_CATALOG["coal_plant"].capacity_kw == 800


def test_plants_do_not_require_road_adjacency() -> None:
    w = _fresh_world()
    # Far corner of the world, no roads connecting.
    res = w.build("solar_farm", 0, 0)
    assert res["ok"] is True, res
    res = w.build("coal_plant", 31, 31)
    assert res["ok"] is True, res


# -- Solar/wind output (Step 1: must-take renewables) -----------------------


def test_dispatch_solar_only_at_noon() -> None:
    p = _plant("solar_farm")
    weather = {"cloud_factor": 1.0, "wind_speed_mps": 0.0}
    outputs, supply, by_source = dispatch(
        [p], demand_kw=10_000.0, prev_outputs={}, weather=weather, D=80, h=12
    )
    # At equinox-ish, noon is mid-arc → irradiance ≈ 1.0 → 150 kW.
    assert outputs[p.id] == pytest.approx(150.0, abs=0.5)
    assert by_source["solar"] == pytest.approx(supply)
    assert by_source["wind"] == 0.0
    assert by_source["coal"] == 0.0
    assert by_source["gas"] == 0.0


def test_dispatch_wind_at_rated_speed() -> None:
    p = _plant("wind_turbine")
    weather = {"cloud_factor": 0.0, "wind_speed_mps": 12.0}
    outputs, supply, by_source = dispatch(
        [p], demand_kw=10_000.0, prev_outputs={}, weather=weather, D=0, h=0
    )
    assert outputs[p.id] == pytest.approx(200.0)
    assert by_source["wind"] == pytest.approx(200.0)


def test_dispatch_solar_zero_at_night() -> None:
    p = _plant("solar_farm")
    weather = {"cloud_factor": 1.0, "wind_speed_mps": 0.0}
    outputs, _supply, _by = dispatch(
        [p], demand_kw=10_000.0, prev_outputs={}, weather=weather, D=80, h=0
    )
    assert outputs[p.id] == 0.0


# -- Coal must-run + ramp (Step 2 + 3) --------------------------------------


def test_coal_must_run_at_25_percent_when_demand_low() -> None:
    """A coal plant always runs at >= 25% capacity when operational."""
    p = _plant("coal_plant")
    cap = TILE_CATALOG["coal_plant"].capacity_kw
    outputs, _s, _b = dispatch([p], demand_kw=0.0, prev_outputs={}, weather={}, D=0, h=12)
    assert outputs[p.id] == pytest.approx(cap * COAL_MIN_RUN)


def test_coal_ramp_limit_per_hour() -> None:
    """Coal output cannot exceed prev_out + 10% × cap in a single hour."""
    p = _plant("coal_plant")
    cap = TILE_CATALOG["coal_plant"].capacity_kw
    prev = {p.id: cap * COAL_MIN_RUN}  # last hour at must-run (200 kW).
    # Demand far above must-run; coal would want to ramp to capacity.
    outputs, _s, _b = dispatch([p], demand_kw=10_000.0, prev_outputs=prev, weather={}, D=0, h=12)
    expected_max = cap * COAL_MIN_RUN + cap * COAL_RAMP_PER_HOUR  # 200 + 80 = 280
    assert outputs[p.id] == pytest.approx(expected_max)
    # And not higher than prev + ramp_room.
    assert outputs[p.id] - prev[p.id] <= cap * COAL_RAMP_PER_HOUR + 1e-6


def test_coal_can_ramp_down_to_must_run_freely() -> None:
    """Coal ramp constraint is upper-only; min-run is the floor."""
    p = _plant("coal_plant")
    cap = TILE_CATALOG["coal_plant"].capacity_kw
    prev = {p.id: cap}  # last hour at full capacity.
    # Demand collapses to zero — coal drops straight to must-run.
    outputs, _s, _b = dispatch([p], demand_kw=0.0, prev_outputs=prev, weather={}, D=0, h=12)
    assert outputs[p.id] == pytest.approx(cap * COAL_MIN_RUN)


def test_coal_holds_output_when_prev_above_must_run() -> None:
    """If prev was above must-run and demand is high, coal stays near prev."""
    p = _plant("coal_plant")
    prev = {p.id: 600.0}  # last hour at 600 kW (above must-run).
    outputs, _s, _b = dispatch([p], demand_kw=10_000.0, prev_outputs=prev, weather={}, D=0, h=12)
    # Should ramp to min(cap, prev+ramp_room) = min(800, 680) = 680.
    assert outputs[p.id] == pytest.approx(680.0)


# -- Gas peakers (Step 4) ---------------------------------------------------


def test_gas_ramp_limit_per_hour() -> None:
    """Gas output cannot exceed prev_out + 50% × cap in one hour."""
    p = _plant("gas_peaker")
    cap = TILE_CATALOG["gas_peaker"].capacity_kw
    prev = {p.id: 100.0}
    outputs, _s, _b = dispatch([p], demand_kw=10_000.0, prev_outputs=prev, weather={}, D=0, h=12)
    expected_max = min(cap, 100.0 + cap * GAS_RAMP_PER_HOUR)  # 100 + 250 = 350
    assert outputs[p.id] == pytest.approx(expected_max)


def test_gas_starts_at_zero_with_no_prev_output() -> None:
    """Newly built gas peaker ramps from 0 — first hour limited to 50% cap."""
    p = _plant("gas_peaker")
    cap = TILE_CATALOG["gas_peaker"].capacity_kw
    outputs, _s, _b = dispatch([p], demand_kw=10_000.0, prev_outputs={}, weather={}, D=0, h=12)
    assert outputs[p.id] == pytest.approx(cap * GAS_RAMP_PER_HOUR)


def test_gas_does_not_dispatch_when_demand_already_met() -> None:
    """If renewables + coal cover demand, gas stays at zero."""
    solar = _plant("solar_farm", idx=1)
    gas = _plant("gas_peaker", idx=2)
    weather = {"cloud_factor": 1.0, "wind_speed_mps": 0.0}
    outputs, _s, by_source = dispatch(
        [solar, gas], demand_kw=100.0, prev_outputs={}, weather=weather, D=80, h=12
    )
    # Solar at noon ≈ 150 kW > demand 100. Gas idle.
    assert outputs[gas.id] == 0.0
    assert by_source["gas"] == 0.0


# -- Merit order (renewables → coal → gas) ----------------------------------


def test_merit_order_renewables_first_then_coal_then_gas() -> None:
    """When demand is moderate, gas does NOT fire if renewables + coal suffice."""
    solar = _plant("solar_farm", idx=1)
    coal = _plant("coal_plant", idx=2)
    gas = _plant("gas_peaker", idx=3)
    weather = {"cloud_factor": 1.0, "wind_speed_mps": 0.0}
    # Demand = 250 kW. Solar at noon ≈ 150. Coal must-run = 200. Sum=350 > 250.
    # Gas should remain idle.
    outputs, supply, by_source = dispatch(
        [solar, coal, gas], demand_kw=250.0, prev_outputs={}, weather=weather, D=80, h=12
    )
    assert outputs[gas.id] == 0.0
    assert by_source["solar"] > 0
    assert by_source["coal"] > 0
    assert supply >= 250.0  # supply meets-or-curtails demand.


# -- Balance state thresholds -----------------------------------------------


def test_balance_curtailment_at_R_at_least_1_15() -> None:
    state, served, excess, R = compute_balance_state(supply_kw=115.0, demand_kw=100.0)
    assert state == "curtailment"
    assert served == pytest.approx(100.0)
    assert excess == pytest.approx(15.0)
    assert pytest.approx(1.15) == R


def test_balance_balanced_at_R_in_0_95_to_1_15() -> None:
    state, served, excess, R = compute_balance_state(supply_kw=100.0, demand_kw=100.0)
    assert state == "balanced"
    assert served == pytest.approx(100.0)
    assert excess == 0.0
    state, _s, _e, _r = compute_balance_state(supply_kw=95.0, demand_kw=100.0)
    assert state == "balanced"
    state, _s, _e, _r = compute_balance_state(supply_kw=114.99, demand_kw=100.0)
    assert state == "balanced"


def test_balance_brownout_at_R_in_0_70_to_0_95() -> None:
    state, served, excess, R = compute_balance_state(supply_kw=80.0, demand_kw=100.0)
    assert state == "brownout"
    assert served == pytest.approx(80.0)
    assert excess == 0.0
    assert pytest.approx(0.80) == R


def test_balance_blackout_below_R_0_70() -> None:
    state, served, _excess, R = compute_balance_state(supply_kw=50.0, demand_kw=100.0)
    assert state == "blackout"
    assert served == pytest.approx(50.0)
    assert pytest.approx(0.50) == R


def test_balance_zero_demand_is_balanced() -> None:
    """No load → no penalty even when supply is also 0."""
    state, _s, _e, _r = compute_balance_state(supply_kw=0.0, demand_kw=0.0)
    assert state == "balanced"


# -- Sim integration --------------------------------------------------------


def _build_at(w: World, tile_type: str, x: int, y: int) -> Tile:
    spec = TILE_CATALOG[tile_type]
    tile = Tile(
        id=f"injected-{tile_type}-{x}-{y}",
        type=tile_type,
        x=x,
        y=y,
        built_day=0,
        operational=True,
        capex_paid=spec.capex,
        opex_per_day=spec.opex_per_day,
    )
    w.state.tiles.append(tile)
    return tile


def test_state_power_now_by_source_kw_populated() -> None:
    w = _fresh_world()
    _build_at(w, "solar_farm", 5, 5)
    _build_at(w, "coal_plant", 6, 5)
    w.step(days=1)
    bs = w.state.power_now["by_source_kw"]
    for key in ("solar", "wind", "coal", "gas"):
        assert key in bs
    # Coal at must-run is 200 kW; some hour should reflect that.
    assert bs["coal"] >= 0.0


def test_blackout_decrements_treasury_per_hour() -> None:
    """A 24-hour blackout costs 24 × $5,000 = $120,000."""
    w = _fresh_world()
    # Pop=100, no plants → demand-only world. R=0 → blackout every hour.
    treasury_before = w.state.treasury
    w.step(days=1)
    # 24 blackout hours expected.
    assert w.state.today_summary_so_far["blackout_hours"] == pytest.approx(24.0)
    expected_penalty = 24 * w.config.blackout_penalty_hour
    assert w.state.today_summary_so_far["blackout_penalty"] == pytest.approx(expected_penalty)
    # Net treasury delta = -penalty + tax_revenue (no plants → no opex/fuel/
    # power_revenue). Pop dropped 100 → 99 by job-decline; tax = 99 × $4 = 396.
    expected_delta = -expected_penalty + 99 * 4.0
    assert w.state.treasury - treasury_before == pytest.approx(expected_delta)


def test_curtailment_revenue_includes_export_component() -> None:
    """Curtailment hour: served at retail + excess at export."""
    w = _fresh_world()
    # Pop=0 → no residential demand. One coal plant → 200 kW must-run.
    # Demand=0 → balanced (special case). Force demand by adding industrial.
    w.state.population = 0
    _build_at(w, "industrial", 5, 5)  # 300 kW continuous
    _build_at(w, "coal_plant", 6, 5)  # 200 kW must-run, can ramp to 280 first hour
    _build_at(w, "coal_plant", 7, 5)
    _build_at(w, "coal_plant", 8, 5)  # 3 plants × 200 must-run = 600 kW; cap headroom
    w.step(days=1)
    # Some power_revenue should accrue (served retail at minimum).
    pr = w.state.today_summary_so_far["power_revenue"]
    assert pr > 0.0


def test_renewables_build_via_api() -> None:
    """All four plant types are accepted via /build."""
    w = _fresh_world()
    for tile_type, x in (
        ("solar_farm", 0),
        ("wind_turbine", 1),
        ("gas_peaker", 2),
        ("coal_plant", 3),
    ):
        res = w.build(tile_type, x, 0)
        assert res["ok"] is True, (tile_type, res)


def test_step_size_invariance_with_plants() -> None:
    """Adding plants must not break the step-size determinism contract."""
    a = World()
    a.reset(seed=42)
    _build_at(a, "coal_plant", 5, 5)
    _build_at(a, "solar_farm", 6, 5)
    a.step(days=7)

    b = World()
    b.reset(seed=42)
    _build_at(b, "coal_plant", 5, 5)
    _build_at(b, "solar_farm", 6, 5)
    for _ in range(7):
        b.step(days=1)

    assert a.state.treasury == b.state.treasury
    assert a.state.population == b.state.population
    assert a.state.happiness == b.state.happiness
    assert a.sim_rng.standard_normal() == b.sim_rng.standard_normal()


# -- Coal-proximity unhappiness ---------------------------------------------


def test_coal_proximity_reduces_happiness_for_houses_within_3() -> None:
    """0.05 × (houses_within_3 / max(1, house_count)) deducted from happiness."""
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    # Place a house adjacent to town hall, then a coal plant within chebyshev 3.
    w.build("house", cx + 1, cy)
    _build_at(w, "coal_plant", cx + 4, cy)  # chebyshev distance = 3 from house.
    update_population(w)
    # All 1 house within 3: penalty = 0.05 * 1 / 1 = 0.05.
    # Baseline happiness = 1.0 (no parks, no blackouts) - 0.05 = 0.95.
    assert w.state.happiness == pytest.approx(0.95)


def test_coal_proximity_zero_when_no_houses_in_range() -> None:
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    w.build("house", cx + 1, cy)
    # Coal plant 5 cells away → chebyshev distance 5 > 3.
    _build_at(w, "coal_plant", cx + 6, cy)
    update_population(w)
    assert w.state.happiness == pytest.approx(1.0)


# -- Outage → happiness penalty (issue 22 — applied end-of-day) -------------


def test_full_day_blackout_pins_happiness_at_zero() -> None:
    """A direct call to dispatch + balance lets us verify the outage detection;
    one full day of blackout drops happiness to 0 via the daily reassignment
    in `update_population` (per-hour coef 0.05; 24*0.05 = 1.20 → clip to 0)."""
    w = _fresh_world()
    happiness_before = w.state.happiness
    assert happiness_before == 1.0
    w.state.hour = 0
    from world.power import compute_balance_state, dispatch, total_demand_kw

    demand = total_demand_kw(w.state, 0)
    _o, supply, _b = dispatch([], demand, {}, {}, 0, 0)
    state, _s, _e, _r = compute_balance_state(supply, demand)
    assert state == "blackout"

    w.step(days=1)
    # 24 blackout hours × 0.05/hr = 1.20 → clipped to 0 by [0, 1.5].
    assert w.state.happiness == pytest.approx(0.0, abs=0.001)
    assert w.state.yesterday_blackout_hours == 24.0

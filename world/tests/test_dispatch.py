"""Plants + dispatch + balance state + power revenue (slice 05, brief §4.4).

Each function in `world.power` (dispatch and compute_balance_state) is
exercised in isolation, then the sim-level wiring is verified end-to-end
via World.step.
"""

from __future__ import annotations

import math

import pytest

from world.catalog import TILE_CATALOG
from world.population import update_population
from world.power import (
    COAL_MIN_RUN,
    COAL_RAMP_PER_HOUR,
    GAS_RAMP_PER_HOUR,
    battery_charge_step,
    battery_discharge_step,
    compute_balance_state,
    dispatch,
)
from world.sim import World
from world.state import Tile


def _fresh_world() -> World:
    w = World()
    w.reset(seed=42)
    return w


def _plant(tile_type: str, idx: int = 1, staffed_jobs: int | None = None) -> Tile:
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
        jobs=spec.jobs,
        staffed_jobs=spec.jobs if staffed_jobs is None else staffed_jobs,
    )


# -- Catalog wiring ---------------------------------------------------------


def test_catalog_exposes_four_plant_types() -> None:
    for t in ("solar_farm", "wind_turbine", "gas_peaker", "coal_plant"):
        assert t in TILE_CATALOG
        spec = TILE_CATALOG[t]
        assert spec.buildable is True
    # Coal is the only plant with a road requirement (logistically heavy).
    for t in ("solar_farm", "wind_turbine", "gas_peaker"):
        assert TILE_CATALOG[t].requires_road is False
    assert TILE_CATALOG["coal_plant"].requires_road is True


def test_plant_capacities_match_spec() -> None:
    assert TILE_CATALOG["solar_farm"].capacity_kw == 150
    assert TILE_CATALOG["wind_turbine"].capacity_kw == 200
    assert TILE_CATALOG["gas_peaker"].capacity_kw == 500
    assert TILE_CATALOG["coal_plant"].capacity_kw == 1500


def test_coal_cheaper_per_mwh_than_gas_at_default_carbon() -> None:
    """All-in $/MWh = fuel + carbon × CO2 intensity. At the default carbon
    price ($25/t) coal should beat gas — the post-rebalance rule that
    positions coal as the baseload anchor.
    """
    from world.economy import CARBON_PRICE_USD_PER_TON

    coal_spec = TILE_CATALOG["coal_plant"]
    gas_spec = TILE_CATALOG["gas_peaker"]
    coal_all_in = coal_spec.fuel_cost_per_mwh + CARBON_PRICE_USD_PER_TON * coal_spec.co2_t_per_mwh
    gas_all_in = gas_spec.fuel_cost_per_mwh + CARBON_PRICE_USD_PER_TON * gas_spec.co2_t_per_mwh
    assert coal_all_in < gas_all_in


def test_renewable_and_gas_plants_do_not_require_road_adjacency() -> None:
    w = _fresh_world()
    # Far corner of the world, no roads connecting.
    res = w.build("solar_farm", 0, 0)
    assert res["ok"] is True, res
    res = w.build("wind_turbine", 1, 0)
    assert res["ok"] is True, res
    res = w.build("gas_peaker", 2, 0)
    assert res["ok"] is True, res


def test_coal_plant_requires_road_adjacency() -> None:
    w = _fresh_world()
    # Isolated corner — no road network reaches here.
    res = w.build("coal_plant", 31, 31)
    assert res["ok"] is False
    assert res["error"] == "no_road_adjacency"


def test_coal_plant_catalog_labor_and_road_requirements() -> None:
    """Coal is logistically heavy: 30 workers and a road-adjacent site."""
    spec = TILE_CATALOG["coal_plant"]
    assert spec.jobs == 30
    assert spec.requires_road is True
    # Catalog description is the in-game tooltip + tile-spec contract.
    assert "30" in spec.description
    assert "road" in spec.description.lower()


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


def test_solar_derate_during_heatwave() -> None:
    """Solar output drops 20% when `solar_derate=0.8`; unchanged at 1.0.

    AC pin (balance-upgrade-p0 issue 05): a heatwave-active dispatch call
    receives `solar_derate_multiplier(state) = 0.8`, which caps each solar
    plant at 80% of its effective capacity.
    """
    p = _plant("solar_farm")
    weather = {"cloud_factor": 1.0, "wind_speed_mps": 0.0}
    base_out, _, base_by = dispatch(
        [p], demand_kw=10_000.0, prev_outputs={}, weather=weather, D=80, h=12
    )
    derated_out, _, derated_by = dispatch(
        [p],
        demand_kw=10_000.0,
        prev_outputs={},
        weather=weather,
        D=80,
        h=12,
        solar_derate=0.8,
    )
    # Baseline noon solar ≈ 150 kW; derated ≈ 120 kW.
    assert base_out[p.id] == pytest.approx(150.0, abs=0.5)
    assert derated_out[p.id] == pytest.approx(120.0, abs=0.5)
    assert derated_by["solar"] == pytest.approx(0.8 * base_by["solar"])


def test_solar_derate_does_not_affect_wind() -> None:
    """Wind output is unchanged when `solar_derate < 1.0`."""
    p = _plant("wind_turbine")
    weather = {"cloud_factor": 0.0, "wind_speed_mps": 12.0}
    outputs, _supply, by_source = dispatch(
        [p],
        demand_kw=10_000.0,
        prev_outputs={},
        weather=weather,
        D=0,
        h=0,
        solar_derate=0.8,
    )
    assert outputs[p.id] == pytest.approx(200.0)
    assert by_source["wind"] == pytest.approx(200.0)


def test_solar_derate_defaults_to_one() -> None:
    """No-arg dispatch yields the same solar output as `solar_derate=1.0`."""
    p = _plant("solar_farm")
    weather = {"cloud_factor": 1.0, "wind_speed_mps": 0.0}
    default_out, _, _ = dispatch(
        [p], demand_kw=10_000.0, prev_outputs={}, weather=weather, D=80, h=12
    )
    explicit_out, _, _ = dispatch(
        [p],
        demand_kw=10_000.0,
        prev_outputs={},
        weather=weather,
        D=80,
        h=12,
        solar_derate=1.0,
    )
    assert default_out[p.id] == pytest.approx(explicit_out[p.id])


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
    cap = TILE_CATALOG["coal_plant"].capacity_kw
    prev = {p.id: 600.0}  # last hour at 600 kW (above must-run = cap × 0.25).
    outputs, _s, _b = dispatch([p], demand_kw=10_000.0, prev_outputs=prev, weather={}, D=0, h=12)
    # Should ramp to min(cap, prev + ramp_room) where ramp_room = cap × 0.10.
    expected = min(cap, 600.0 + cap * COAL_RAMP_PER_HOUR)
    assert outputs[p.id] == pytest.approx(expected)


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


def test_unsupplied_peaker_zeroed_like_plant_failure() -> None:
    """A peaker in `unsupplied_peaker_ids` outputs 0 and is excluded from gas
    by_source — same shape as `operational=False` (plant_failure)."""
    p = _plant("gas_peaker")
    outputs, supply, by_source = dispatch(
        [p],
        demand_kw=10_000.0,
        prev_outputs={},
        weather={},
        D=0,
        h=12,
        unsupplied_peaker_ids=frozenset({p.id}),
    )
    assert outputs[p.id] == 0.0
    assert by_source["gas"] == 0.0
    assert supply == 0.0


def test_unsupplied_peaker_matches_non_operational_peaker_outputs() -> None:
    """Filtering via `unsupplied_peaker_ids` is structurally equivalent to
    `operational=False` (plant_failure path) — same outputs, same by_source."""
    failed = _plant("gas_peaker", idx=1)
    failed.operational = False
    unsupplied = _plant("gas_peaker", idx=2)
    failed_out, failed_supply, failed_by = dispatch(
        [failed], demand_kw=10_000.0, prev_outputs={}, weather={}, D=0, h=12
    )
    unsupplied_out, unsupplied_supply, unsupplied_by = dispatch(
        [unsupplied],
        demand_kw=10_000.0,
        prev_outputs={},
        weather={},
        D=0,
        h=12,
        unsupplied_peaker_ids=frozenset({unsupplied.id}),
    )
    assert failed_out[failed.id] == unsupplied_out[unsupplied.id] == 0.0
    assert failed_supply == unsupplied_supply == 0.0
    assert failed_by == unsupplied_by


def test_supplied_peaker_dispatches_normally_when_demand_present() -> None:
    """An empty unsupplied set is a no-op; gas dispatches per ramp."""
    p = _plant("gas_peaker")
    outputs_default, _, _ = dispatch(
        [p], demand_kw=10_000.0, prev_outputs={}, weather={}, D=0, h=12
    )
    outputs_empty, _, _ = dispatch(
        [p],
        demand_kw=10_000.0,
        prev_outputs={},
        weather={},
        D=0,
        h=12,
        unsupplied_peaker_ids=frozenset(),
    )
    assert outputs_default[p.id] > 0.0
    assert outputs_empty[p.id] == pytest.approx(outputs_default[p.id])


def test_unsupplied_filter_does_not_affect_coal_or_renewables() -> None:
    """The filter only matches gas peakers — coal with the same id still
    dispatches. (Defensive: prevents an over-broad filter on the type check.)
    """
    coal = _plant("coal_plant", idx=1)
    outputs, _s, by_source = dispatch(
        [coal],
        demand_kw=10_000.0,
        prev_outputs={},
        weather={},
        D=0,
        h=12,
        unsupplied_peaker_ids=frozenset({coal.id}),
    )
    assert outputs[coal.id] > 0.0
    assert by_source["coal"] > 0.0


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


# -- daily_met_demand_fraction (issue 08) ----------------------------------


def test_daily_met_demand_fraction_full_supply_returns_one() -> None:
    from world.power import daily_met_demand_fraction

    supply = [1000.0] * 24
    demand = [1000.0] * 24
    assert daily_met_demand_fraction(supply, demand) == pytest.approx(1.0)


def test_daily_met_demand_fraction_oversupply_clamps_at_one() -> None:
    from world.power import daily_met_demand_fraction

    supply = [2000.0] * 24
    demand = [1000.0] * 24
    assert daily_met_demand_fraction(supply, demand) == pytest.approx(1.0)


def test_daily_met_demand_fraction_uniform_undersupply_at_half() -> None:
    from world.power import daily_met_demand_fraction

    supply = [500.0] * 24
    demand = [1000.0] * 24
    assert daily_met_demand_fraction(supply, demand) == pytest.approx(0.5)


def test_daily_met_demand_fraction_zero_supply_returns_zero() -> None:
    from world.power import daily_met_demand_fraction

    supply = [0.0] * 24
    demand = [1000.0] * 24
    assert daily_met_demand_fraction(supply, demand) == pytest.approx(0.0)


def test_daily_met_demand_fraction_hour_average_not_demand_weighted() -> None:
    """12 hours fully served, 12 hours zero served → 0.5 daily."""
    from world.power import daily_met_demand_fraction

    supply = [1000.0] * 12 + [0.0] * 12
    demand = [1000.0] * 24
    assert daily_met_demand_fraction(supply, demand) == pytest.approx(0.5)


def test_daily_met_demand_fraction_zero_demand_hour_counts_as_served() -> None:
    """An hour with no load is not a brownout — fraction is 1.0 for that hour."""
    from world.power import daily_met_demand_fraction

    supply = [0.0] * 24
    demand = [0.0] * 24
    assert daily_met_demand_fraction(supply, demand) == pytest.approx(1.0)


def test_daily_met_demand_fraction_empty_trace_returns_one() -> None:
    """Day 0 path: no day has completed yet — default to "no shortage"."""
    from world.power import daily_met_demand_fraction

    assert daily_met_demand_fraction([], []) == pytest.approx(1.0)


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
        jobs=spec.jobs,
        staffed_jobs=spec.jobs,
        demand_kw=spec.demand_kw,
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
    # power_revenue). Pop drops under the happiness-velocity model:
    # h=0 (24h blackout clipped) → velocity = 0.012·100·-1 = -1.2 → 98.8.
    # Then jobs floor fires: max(30/0.7, 98.8·0.99) = 97.812. int = 97.
    expected_delta = -expected_penalty + 97 * 4.0
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
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    for tile_type, x in (
        ("solar_farm", 0),
        ("wind_turbine", 1),
        ("gas_peaker", 2),
    ):
        res = w.build(tile_type, x, 0)
        assert res["ok"] is True, (tile_type, res)
    # Coal requires road; town hall participates in the road network for adjacency.
    res = w.build("coal_plant", th.x + 1, th.y)
    assert res["ok"] is True, res


def _build_pipeline_supplied_peaker(w: World) -> tuple[Tile, Tile]:
    """Place peaker — pipeline — refinery in a row, return (peaker, refinery).

    Forces the peaker to fire by zeroing pop, then adding industrial demand
    that exceeds the renewables (none built) + coal must-run (none built) =
    0 baseline supply.
    """
    w.state.population = 0
    _build_at(w, "industrial", 10, 10)  # 300 kW continuous demand
    peaker = _build_at(w, "gas_peaker", 4, 5)
    _build_at(w, "pipeline", 5, 5)
    refinery = _build_at(w, "refinery", 6, 5)
    return peaker, refinery


def test_peaker_with_pipeline_supply_dispatches_through_sim() -> None:
    """End-to-end: a peaker on a pipeline network with an operational refinery
    runs over a full day. Demand is industrial (no other supply), so any nonzero
    gas output proves the supply gate let the peaker through."""
    w = _fresh_world()
    peaker, _refinery = _build_pipeline_supplied_peaker(w)
    w.step(days=1)
    assert peaker.kwh_served_yesterday > 0.0


def test_peaker_zeroed_when_no_pipeline_adjacency() -> None:
    """A peaker built away from any pipeline outputs zero over the day."""
    w = _fresh_world()
    w.state.population = 0
    _build_at(w, "industrial", 10, 10)
    peaker = _build_at(w, "gas_peaker", 20, 20)  # no pipeline anywhere near
    w.step(days=1)
    assert peaker.kwh_served_yesterday == 0.0


def test_peaker_zeroed_after_refinery_destroyed_mid_game() -> None:
    """Destroying the only connected refinery cuts the peaker on the next day."""
    w = _fresh_world()
    peaker, refinery = _build_pipeline_supplied_peaker(w)
    w.step(days=1)
    served_before = peaker.kwh_served_yesterday
    assert served_before > 0.0

    refinery.operational = False
    w.step(days=1)
    assert peaker.kwh_served_yesterday == 0.0


def test_gas_peaker_catalog_mentions_pipeline_refinery_requirement() -> None:
    spec = TILE_CATALOG["gas_peaker"]
    desc = spec.description.lower()
    assert "pipeline" in desc
    assert "refinery" in desc


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


# -- Workforce efficiency scaling (slice 04) --------------------------------


def test_half_staffed_coal_ceiling_is_half_capacity() -> None:
    """A coal plant at 15/30 staff has effective capacity = 0.5 × catalog."""
    p = _plant("coal_plant", staffed_jobs=15)  # jobs=30 → efficiency=0.5
    cap = TILE_CATALOG["coal_plant"].capacity_kw
    eff_cap = cap * 0.5
    # Big prior to skip the ramp constraint — pin the ceiling alone.
    prev = {p.id: eff_cap}
    outputs, _s, _b = dispatch([p], demand_kw=10_000.0, prev_outputs=prev, weather={}, D=0, h=12)
    assert outputs[p.id] == pytest.approx(eff_cap)


def test_half_staffed_coal_must_run_is_half_floor() -> None:
    """Half-staffed coal's must-run floor scales with efficiency."""
    p = _plant("coal_plant", staffed_jobs=15)
    cap = TILE_CATALOG["coal_plant"].capacity_kw
    outputs, _s, _b = dispatch([p], demand_kw=0.0, prev_outputs={}, weather={}, D=0, h=12)
    assert outputs[p.id] == pytest.approx(cap * 0.5 * COAL_MIN_RUN)


def test_half_staffed_coal_ramp_scales() -> None:
    """Cold-start half-staffed coal: hour 1 cap = eff_floor + eff_ramp."""
    p = _plant("coal_plant", staffed_jobs=15)
    cap = TILE_CATALOG["coal_plant"].capacity_kw
    eff_cap = cap * 0.5
    # No prior output → warm-start at effective must-run.
    outputs, _s, _b = dispatch([p], demand_kw=10_000.0, prev_outputs={}, weather={}, D=0, h=12)
    expected = eff_cap * COAL_MIN_RUN + eff_cap * COAL_RAMP_PER_HOUR
    assert outputs[p.id] == pytest.approx(expected)


def test_idle_coal_plant_produces_no_output() -> None:
    """A 0-staffed coal plant: no output, no must-run, no ramp."""
    p = _plant("coal_plant", staffed_jobs=0)
    outputs, _s, by_source = dispatch(
        [p], demand_kw=10_000.0, prev_outputs={}, weather={}, D=0, h=12
    )
    assert outputs[p.id] == 0.0
    assert by_source["coal"] == 0.0


def test_idle_solar_farm_produces_no_output() -> None:
    """0-staffed solar farm under full sun produces 0 kW."""
    p = _plant("solar_farm", staffed_jobs=0)
    weather = {"cloud_factor": 1.0, "wind_speed_mps": 0.0}
    outputs, _s, by_source = dispatch(
        [p], demand_kw=10_000.0, prev_outputs={}, weather=weather, D=80, h=12
    )
    assert outputs[p.id] == 0.0
    assert by_source["solar"] == 0.0


def test_idle_wind_turbine_produces_no_output() -> None:
    """0-staffed wind turbine at rated speed produces 0 kW."""
    p = _plant("wind_turbine", staffed_jobs=0)
    weather = {"cloud_factor": 0.0, "wind_speed_mps": 12.0}
    outputs, _s, by_source = dispatch(
        [p], demand_kw=10_000.0, prev_outputs={}, weather=weather, D=0, h=0
    )
    assert outputs[p.id] == 0.0
    assert by_source["wind"] == 0.0


def test_half_staffed_wind_caps_at_half_capacity() -> None:
    """A 1/2-staffed wind turbine at rated speed produces half of catalog."""
    p = _plant("wind_turbine", staffed_jobs=1)  # jobs=2 → efficiency=0.5
    cap = TILE_CATALOG["wind_turbine"].capacity_kw
    weather = {"cloud_factor": 0.0, "wind_speed_mps": 12.0}
    outputs, _s, _b = dispatch([p], demand_kw=10_000.0, prev_outputs={}, weather=weather, D=0, h=0)
    assert outputs[p.id] == pytest.approx(cap * 0.5)


def test_half_staffed_solar_caps_at_half_capacity() -> None:
    """A 1/2-staffed solar farm at noon produces half of full-staff output."""
    full = _plant("solar_farm", idx=1)
    half = _plant("solar_farm", idx=2, staffed_jobs=1)
    weather = {"cloud_factor": 1.0, "wind_speed_mps": 0.0}
    out_full, _s, _b = dispatch(
        [full], demand_kw=10_000.0, prev_outputs={}, weather=weather, D=80, h=12
    )
    out_half, _s, _b = dispatch(
        [half], demand_kw=10_000.0, prev_outputs={}, weather=weather, D=80, h=12
    )
    assert out_half[half.id] == pytest.approx(out_full[full.id] * 0.5)


def test_half_staffed_gas_ramp_scales() -> None:
    """A 1/4-staffed gas peaker (1/4 jobs) has 1/4 ramp and ceiling."""
    p = _plant("gas_peaker", staffed_jobs=1)  # jobs=4 → efficiency=0.25
    cap = TILE_CATALOG["gas_peaker"].capacity_kw
    outputs, _s, _b = dispatch([p], demand_kw=10_000.0, prev_outputs={}, weather={}, D=0, h=12)
    # Cold-start: prev=0, eff_cap = 0.25 × 500 = 125, ramp room = 50% × 125 = 62.5.
    assert outputs[p.id] == pytest.approx(cap * 0.25 * GAS_RAMP_PER_HOUR)


def test_idle_coal_plant_zero_fuel_and_co2_through_sim() -> None:
    """End-to-end: a 0-staffed coal plant emits 0 fuel cost and 0 CO2."""
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    # Drop unemployed to 0 so the new coal plant is built unstaffed.
    w.state.population = 30  # exactly fills town hall
    res = w.build("coal_plant", cx + 1, cy)
    assert res["ok"] is True
    coal = next(t for t in w.state.tiles if t.type == "coal_plant")
    assert coal.staffed_jobs == 0
    # Restore population so demand exists and would normally trigger coal.
    w.state.population = 100
    w.step(days=1)
    summary = w.state.today_summary_so_far
    # Coal contributed 0 → fuel cost from coal = 0, CO2 from coal = 0.
    # The gas peaker doesn't exist; only the idle coal plant. Any fuel/CO2
    # would be from coal — both must be zero.
    assert summary.get("fuel_cost", 0.0) == pytest.approx(0.0)
    assert summary.get("co2_emitted_t", 0.0) == pytest.approx(0.0)


# -- Battery charge/discharge step (slice 02) -------------------------------


def _battery(
    idx: int = 1,
    soc_kwh: float = 0.0,
    charge_setpoint_kw: float = 0.0,
    operational: bool = True,
) -> Tile:
    spec = TILE_CATALOG["battery"]
    return Tile(
        id=f"battery-{idx}",
        type="battery",
        x=idx,
        y=0,
        built_day=0,
        operational=operational,
        capex_paid=spec.capex,
        opex_per_day=spec.opex_per_day,
        jobs=spec.jobs,
        staffed_jobs=spec.jobs,
        soc_kwh=soc_kwh,
        charge_setpoint_kw=charge_setpoint_kw,
    )


SQRT_ETA = math.sqrt(0.85)  # battery round_trip_efficiency = 0.85


def test_battery_charge_step_absorbs_surplus_at_rated_power() -> None:
    """With ample surplus and empty SoC, the battery pulls its rated 200 kW."""
    b = _battery(soc_kwh=0.0)
    charges, total, soc_deltas = battery_charge_step(
        [b], renewable_supply_kw=400.0, demand_kw=100.0
    )
    # Surplus = 300, rated = 200 → draw = 200 kW, SoC gain = 200 × sqrt(0.85).
    assert charges[b.id] == pytest.approx(200.0)
    assert total == pytest.approx(200.0)
    assert soc_deltas[b.id] == pytest.approx(200.0 * SQRT_ETA)


def test_battery_charge_step_respects_rated_power_below_surplus() -> None:
    """Surplus 250 > rated 200 → battery still capped at rated."""
    b = _battery(soc_kwh=0.0)
    charges, total, _ = battery_charge_step([b], renewable_supply_kw=300.0, demand_kw=50.0)
    assert charges[b.id] == pytest.approx(200.0)
    assert total == pytest.approx(200.0)


def test_battery_charge_step_respects_soc_cap() -> None:
    """Battery near full only draws what fits in remaining headroom."""
    spec = TILE_CATALOG["battery"]
    # Leave 50 kWh of room → max draw = 50/sqrt(eta).
    b = _battery(soc_kwh=spec.storage_kwh - 50.0)
    charges, _total, soc_deltas = battery_charge_step(
        [b], renewable_supply_kw=400.0, demand_kw=100.0
    )
    expected_draw = 50.0 / SQRT_ETA
    assert charges[b.id] == pytest.approx(expected_draw)
    assert soc_deltas[b.id] == pytest.approx(50.0)


def test_battery_charge_step_applies_sqrt_eta() -> None:
    """1 kWh drawn → sqrt(0.85) ≈ 0.922 kWh enters SoC."""
    b = _battery(soc_kwh=0.0, charge_setpoint_kw=1.0)
    _charges, _total, soc_deltas = battery_charge_step([b], renewable_supply_kw=10.0, demand_kw=0.0)
    assert soc_deltas[b.id] == pytest.approx(SQRT_ETA)


def test_battery_charge_step_no_op_when_supply_not_above_demand() -> None:
    """No renewable surplus → battery does not charge regardless of SoC."""
    b = _battery(soc_kwh=0.0)
    charges, total, soc_deltas = battery_charge_step(
        [b], renewable_supply_kw=100.0, demand_kw=100.0
    )
    assert charges[b.id] == 0.0
    assert total == 0.0
    assert soc_deltas[b.id] == 0.0


def test_battery_charge_step_clamps_manual_positive_to_surplus() -> None:
    """Manual setpoint = 500 with surplus = 80 → draw clamped to 80."""
    b = _battery(soc_kwh=0.0, charge_setpoint_kw=500.0)
    charges, _total, _socs = battery_charge_step([b], renewable_supply_kw=180.0, demand_kw=100.0)
    assert charges[b.id] == pytest.approx(80.0)


def test_battery_charge_step_skips_negative_setpoint() -> None:
    """Manual discharge mode → no charging this hour."""
    b = _battery(soc_kwh=0.0, charge_setpoint_kw=-100.0)
    charges, total, _ = battery_charge_step([b], renewable_supply_kw=400.0, demand_kw=0.0)
    assert charges[b.id] == 0.0
    assert total == 0.0


def test_battery_discharge_step_closes_residual_demand() -> None:
    """Battery with full SoC discharges to close 100 kW shortfall."""
    spec = TILE_CATALOG["battery"]
    b = _battery(soc_kwh=spec.storage_kwh)
    discharges, total, soc_deltas = battery_discharge_step([b], residual_demand_kw=100.0)
    assert discharges[b.id] == pytest.approx(100.0)
    assert total == pytest.approx(100.0)
    # SoC drains by 100 / sqrt(eta) kWh per hour.
    assert soc_deltas[b.id] == pytest.approx(-100.0 / SQRT_ETA)


def test_battery_discharge_step_respects_rated_power() -> None:
    """Battery capped at rated 200 kW even when shortfall and SoC are huge."""
    spec = TILE_CATALOG["battery"]
    b = _battery(soc_kwh=spec.storage_kwh)
    discharges, total, _ = battery_discharge_step([b], residual_demand_kw=10_000.0)
    assert discharges[b.id] == pytest.approx(200.0)
    assert total == pytest.approx(200.0)


def test_battery_discharge_step_respects_soc_floor() -> None:
    """Battery with 10 kWh SoC delivers up to 10 × sqrt(eta) before draining."""
    b = _battery(soc_kwh=10.0)
    discharges, _total, soc_deltas = battery_discharge_step([b], residual_demand_kw=1000.0)
    # Deliverable energy this hour = SoC × sqrt(eta) = 10 × sqrt(0.85).
    assert discharges[b.id] == pytest.approx(10.0 * SQRT_ETA)
    # SoC drains to ~0.
    assert b.soc_kwh + soc_deltas[b.id] == pytest.approx(0.0, abs=1e-9)


def test_battery_discharge_step_applies_sqrt_eta() -> None:
    """1 kWh delivered to load → 1/sqrt(eta) ≈ 1.085 kWh drained from SoC."""
    b = _battery(soc_kwh=100.0, charge_setpoint_kw=-1.0)
    _discharges, _total, soc_deltas = battery_discharge_step([b], residual_demand_kw=1.0)
    assert soc_deltas[b.id] == pytest.approx(-1.0 / SQRT_ETA)


def test_battery_discharge_step_no_op_when_residual_zero() -> None:
    """Grid is balanced → battery stays put."""
    spec = TILE_CATALOG["battery"]
    b = _battery(soc_kwh=spec.storage_kwh)
    discharges, total, soc_deltas = battery_discharge_step([b], residual_demand_kw=0.0)
    assert discharges[b.id] == 0.0
    assert total == 0.0
    assert soc_deltas[b.id] == 0.0


def test_battery_discharge_step_skips_positive_setpoint() -> None:
    """Manual charge mode → no discharge this hour."""
    spec = TILE_CATALOG["battery"]
    b = _battery(soc_kwh=spec.storage_kwh, charge_setpoint_kw=100.0)
    discharges, total, _ = battery_discharge_step([b], residual_demand_kw=200.0)
    assert discharges[b.id] == 0.0
    assert total == 0.0


def test_battery_charges_during_curtailment() -> None:
    """End-to-end: solar surplus during midday charges the battery."""
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    w.state.population = 0  # no residential demand
    # Multiple solar farms so surplus is comfortable at noon.
    for x in range(cx + 1, cx + 6):
        _build_at(w, "solar_farm", x, cy)
    _build_at(w, "battery", cx + 7, cy)
    bat = next(t for t in w.state.tiles if t.type == "battery")
    soc_before = bat.soc_kwh
    w.step(days=1)
    # Battery should have stored renewable surplus across midday hours.
    assert bat.soc_kwh > soc_before


def test_battery_discharges_to_avoid_brownout() -> None:
    """End-to-end: battery with SoC closes a residual that would otherwise brown out."""
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    # Pre-load a battery near full SoC.
    _build_at(w, "battery", cx + 1, cy)
    bat = next(t for t in w.state.tiles if t.type == "battery")
    spec = TILE_CATALOG["battery"]
    bat.soc_kwh = spec.storage_kwh
    # No plants → demand drives blackouts every hour without the battery.
    # With the battery, at most 200 kW of residual is covered for ~4 hours.
    w.step(days=1)
    # Battery should have discharged.
    assert bat.soc_kwh < spec.storage_kwh


def test_battery_round_trip_loses_15_percent() -> None:
    """1 kWh in via charge → ~0.85 kWh out via discharge."""
    spec = TILE_CATALOG["battery"]
    # Charge 1 kWh worth into the battery.
    _c, _t, soc_deltas_c = battery_charge_step(
        [_battery(soc_kwh=0.0, charge_setpoint_kw=1.0)],
        renewable_supply_kw=10.0,
        demand_kw=0.0,
    )
    energy_stored = list(soc_deltas_c.values())[0]
    # Now discharge that stored energy.
    b2 = _battery(soc_kwh=energy_stored)
    disch, total, _ = battery_discharge_step([b2], residual_demand_kw=10.0)
    # 1 kWh drawn from grid → sqrt(eta) stored → eta × 1 kWh delivered.
    assert total == pytest.approx(spec.round_trip_efficiency, abs=1e-6)
    assert disch[b2.id] == pytest.approx(0.85, abs=1e-6)


def test_battery_renewable_share_includes_discharge() -> None:
    """Battery discharge counts as renewable kWh in both numerator and denominator."""
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    # Setup: a battery preloaded with energy, an industrial load to draw on
    # the battery (no plants → battery is the only supply that turns up).
    _build_at(w, "industrial", cx + 1, cy)  # 300 kW demand
    _build_at(w, "battery", cx + 2, cy)
    bat = next(t for t in w.state.tiles if t.type == "battery")
    spec = TILE_CATALOG["battery"]
    bat.soc_kwh = spec.storage_kwh
    w.state.population = 0
    pre_total = w.state.cumulative_total_served_kwh
    pre_ren = w.state.cumulative_renewable_served_kwh
    w.step(days=1)
    # Battery delivered something → both accumulators advanced equally.
    delta_total = w.state.cumulative_total_served_kwh - pre_total
    delta_ren = w.state.cumulative_renewable_served_kwh - pre_ren
    assert delta_total > 0
    assert delta_ren == pytest.approx(delta_total, rel=1e-6)


def test_battery_manual_charge_clamped_to_renewable_surplus_via_api() -> None:
    """Manual positive setpoint > rated power → still clamped to renewable surplus."""
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    # No renewable plants → no surplus, ever.
    _build_at(w, "industrial", cx + 1, cy)
    _build_at(w, "coal_plant", cx + 2, cy)
    _build_at(w, "battery", cx + 3, cy)
    bat = next(t for t in w.state.tiles if t.type == "battery")
    # Crank the setpoint absurdly high; without renewable surplus the battery
    # must NOT charge from fossil supply.
    w.control_battery(bat.id, 9_999.0)
    soc_before = bat.soc_kwh
    w.step(days=1)
    # No solar/wind plant means no charging is possible.
    assert bat.soc_kwh == pytest.approx(soc_before)


def test_battery_dispatch_step_size_invariance() -> None:
    """Adding batteries preserves the step-size determinism contract."""
    a = World()
    a.reset(seed=42)
    _build_at(a, "solar_farm", 5, 5)
    _build_at(a, "battery", 6, 5)
    a.step(days=7)

    b = World()
    b.reset(seed=42)
    _build_at(b, "solar_farm", 5, 5)
    _build_at(b, "battery", 6, 5)
    for _ in range(7):
        b.step(days=1)

    assert a.state.treasury == b.state.treasury
    assert a.state.population == b.state.population
    # SoC trajectory identical too.
    a_bat = next(t for t in a.state.tiles if t.type == "battery")
    b_bat = next(t for t in b.state.tiles if t.type == "battery")
    assert a_bat.soc_kwh == b_bat.soc_kwh


def test_plant_failure_preserves_staffing() -> None:
    """operational=False does not perturb staffed_jobs; restore resumes crew."""
    w = _fresh_world()
    w.state.treasury = 1_000_000.0
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    # Coal is town-hall-adjacent so it sits inside the road network for free.
    w.build("coal_plant", cx + 1, cy)
    coal = next(t for t in w.state.tiles if t.type == "coal_plant")
    assert coal.staffed_jobs == 30
    # Simulate plant failure.
    coal.operational = False
    # Workforce module sees the staffing regardless of operational flag.
    assert coal.staffed_jobs == 30
    # Restore.
    coal.operational = True
    assert coal.staffed_jobs == 30

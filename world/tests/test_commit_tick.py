"""Tests for `world.hourly_tick.commit_tick` — the sim-only mutating peer.

`commit_tick` writes the per-hour mutations to ``WorldState`` that
``_advance_one_day`` used to inline. Each test crosses the commit_tick
seam directly: construct a minimal state + a hand-tuned ``TickResult``,
call ``commit_tick``, assert specific state deltas. Preview never calls
``commit_tick``, so these tests pin the sim-only contract independently
of the projection.

Treasury is *not* mutated here — outage penalty and power revenue
accumulate into ``state.today`` and the day loop settles them once at
EOD. The "balance accrues today, treasury moves at EOD" invariant is
asserted explicitly so it doesn't drift back to per-hour mutation.
"""

from __future__ import annotations

import pytest

from world.catalog import TILE_CATALOG
from world.hourly_tick import TickResult, commit_tick
from world.snapshots import BalanceState
from world.state import Tile, WorldState


def _state() -> WorldState:
    """Fresh WorldState with the pricing defaults the sim seeds."""
    return WorldState(
        seed=1,
        treasury=10_000.0,
        grid_price_retail=0.08,
        grid_price_export=0.04,
        outage_penalty_hour=4_000.0,
    )


def _result(
    *,
    balance: BalanceState = BalanceState.BALANCED,
    demand_kw: float = 0.0,
    civilian_demand_kw: float = 0.0,
    supply_kw: float = 0.0,
    served_kw: float = 0.0,
    excess_kw: float = 0.0,
    outputs: dict[str, float] | None = None,
    by_source: dict[str, float] | None = None,
    charge_socs: dict[str, float] | None = None,
    discharge_socs: dict[str, float] | None = None,
    total_charge_kw: float = 0.0,
    total_discharge_kw: float = 0.0,
    renewable_supply_after_battery: float = 0.0,
    inj_hour_assignments: dict[str, tuple[float, float]] | None = None,
    prod_hour_kwh: dict[str, float] | None = None,
) -> TickResult:
    """Build a TickResult with sensible per-test defaults."""
    return TickResult(
        demand_kw=demand_kw,
        civilian_demand_kw=civilian_demand_kw,
        supply_kw=supply_kw,
        balance=balance,
        served_kw=served_kw,
        excess_kw=excess_kw,
        outputs=outputs or {},
        by_source=by_source or {"solar": 0.0, "wind": 0.0, "coal": 0.0, "gas": 0.0},
        charge_socs=charge_socs or {},
        discharge_socs=discharge_socs or {},
        total_charge_kw=total_charge_kw,
        total_discharge_kw=total_discharge_kw,
        renewable_supply_after_battery=renewable_supply_after_battery,
        inj_hour_assignments=inj_hour_assignments or {},
        prod_hour_kwh=prod_hour_kwh or {},
    )


# --- Battery SoC ----------------------------------------------------------


def test_battery_soc_applies_charge_and_discharge_deltas() -> None:
    state = _state()
    storage = TILE_CATALOG["battery"].storage_kwh
    battery = Tile(id="b-1", type="battery", x=0, y=0, built_day=0, operational=True, soc_kwh=100.0)
    state.tiles.append(battery)
    result = _result(
        charge_socs={"b-1": 20.0},
        discharge_socs={"b-1": -5.0},
    )
    commit_tick(state, result)
    assert battery.soc_kwh == pytest.approx(100.0 + 20.0 - 5.0)
    assert battery.soc_kwh <= storage


def test_battery_soc_clamps_at_storage_kwh() -> None:
    state = _state()
    storage = TILE_CATALOG["battery"].storage_kwh
    battery = Tile(
        id="b-1", type="battery", x=0, y=0, built_day=0, operational=True, soc_kwh=storage - 1.0
    )
    state.tiles.append(battery)
    commit_tick(state, _result(charge_socs={"b-1": 999.0}))
    assert battery.soc_kwh == pytest.approx(storage)


def test_battery_soc_clamps_at_zero() -> None:
    state = _state()
    battery = Tile(id="b-1", type="battery", x=0, y=0, built_day=0, operational=True, soc_kwh=10.0)
    state.tiles.append(battery)
    commit_tick(state, _result(discharge_socs={"b-1": -999.0}))
    assert battery.soc_kwh == pytest.approx(0.0)


# --- Outage bookkeeping ---------------------------------------------------


def test_blackout_charges_full_outage_penalty_per_hour() -> None:
    """Blackout charges `outage_penalty_hour` flat, regardless of supply."""
    state = _state()
    treasury_before = state.treasury
    commit_tick(
        state,
        _result(balance=BalanceState.BLACKOUT, demand_kw=100.0, supply_kw=0.0),
    )
    assert state.today.blackout_hours == pytest.approx(1.0)
    assert state.today.outage_penalty == pytest.approx(state.outage_penalty_hour)
    # Treasury debit happens at EOD, not per hour.
    assert state.treasury == treasury_before


def test_brownout_charges_flat_plus_ramp_on_unserved_share() -> None:
    """20% unserved → flat + ramp·0.20.

    With defaults flat=$1000, cap=$4000, R_BROWNOUT=0.70, the ramp slope is
    (4000-1000)/(1-0.70) = 10_000, so a 20%-unserved brownout costs
    1000 + 10000·0.20 = $3000.
    """
    state = _state()
    commit_tick(
        state,
        _result(balance=BalanceState.BROWNOUT, demand_kw=100.0, supply_kw=80.0),
    )
    assert state.today.brownout_hours == pytest.approx(1.0)
    assert state.today.blackout_hours == 0.0
    assert state.today.outage_penalty == pytest.approx(3000.0)


def test_brownout_caps_at_outage_penalty_hour_near_blackout_boundary() -> None:
    """At the brownout→blackout boundary (R=R_BROWNOUT=0.70, 30% unserved),
    the ramp exactly reaches `outage_penalty_hour` — a deeper brownout
    never out-costs an actual blackout."""
    state = _state()
    commit_tick(
        state,
        _result(balance=BalanceState.BROWNOUT, demand_kw=100.0, supply_kw=70.0),
    )
    assert state.today.outage_penalty == pytest.approx(state.outage_penalty_hour)


def test_balanced_state_writes_no_outage_penalty() -> None:
    """Penalty only fires under blackout/brownout, never under balanced/curtailment."""
    state = _state()
    commit_tick(
        state,
        _result(balance=BalanceState.BALANCED, demand_kw=100.0, supply_kw=100.0),
    )
    assert state.today.outage_penalty == 0.0


# --- Power revenue --------------------------------------------------------


def test_power_revenue_bills_civilian_kwh_at_retail() -> None:
    state = _state()
    # 1000 kW supply, 800 kW civilian demand → billable = 800 (capped at civilian).
    commit_tick(
        state,
        _result(supply_kw=1000.0, civilian_demand_kw=800.0, served_kw=800.0),
    )
    assert state.today.power_revenue == pytest.approx(800.0 * state.grid_price_retail)


def test_power_revenue_caps_billable_at_supply_when_undersupplied() -> None:
    state = _state()
    # 500 kW supply, 800 kW civilian demand → billable = 500 (capped at supply).
    commit_tick(
        state,
        _result(supply_kw=500.0, civilian_demand_kw=800.0, served_kw=500.0),
    )
    assert state.today.power_revenue == pytest.approx(500.0 * state.grid_price_retail)


def test_curtailment_adds_export_revenue_on_excess() -> None:
    state = _state()
    commit_tick(
        state,
        _result(
            balance=BalanceState.CURTAILMENT,
            supply_kw=1000.0,
            civilian_demand_kw=600.0,
            served_kw=600.0,
            excess_kw=400.0,
        ),
    )
    expected = 600.0 * state.grid_price_retail + 400.0 * state.grid_price_export
    assert state.today.power_revenue == pytest.approx(expected)


# --- Renewable share -------------------------------------------------------


def test_renewable_share_accumulates_min_of_renewable_and_served() -> None:
    state = _state()
    # served=500, renewable_after_battery=300 → renewable_served=300.
    commit_tick(
        state,
        _result(served_kw=500.0, renewable_supply_after_battery=300.0),
    )
    assert state.cumulative_total_served_kwh == pytest.approx(500.0)
    assert state.cumulative_renewable_served_kwh == pytest.approx(300.0)


# --- Injection / production commits ---------------------------------------


def test_injection_commits_accumulate_per_well_and_total_kw() -> None:
    state = _state()
    commit_tick(
        state,
        _result(
            inj_hour_assignments={
                "inj-1": (120.0, 12.0),  # (power_kw, bbl_this_hour)
                "inj-2": (80.0, 8.0),
            },
        ),
    )
    assert state.today.inj_bbl_by_well == {
        "inj-1": pytest.approx(12.0),
        "inj-2": pytest.approx(8.0),
    }
    assert state.today.injection_kw == pytest.approx(200.0)


def test_injection_commits_sum_across_hours() -> None:
    state = _state()
    commit_tick(state, _result(inj_hour_assignments={"inj-1": (50.0, 5.0)}))
    commit_tick(state, _result(inj_hour_assignments={"inj-1": (30.0, 3.0)}))
    assert state.today.inj_bbl_by_well["inj-1"] == pytest.approx(8.0)
    assert state.today.injection_kw == pytest.approx(80.0)


def test_production_commits_accumulate_per_well_and_total_kw() -> None:
    state = _state()
    commit_tick(
        state,
        _result(prod_hour_kwh={"prod-1": 60.0, "prod-2": 40.0}),
    )
    assert state.today.prod_kwh_by_well == {
        "prod-1": pytest.approx(60.0),
        "prod-2": pytest.approx(40.0),
    }
    assert state.today.production_kw == pytest.approx(100.0)


# --- By-source kWh --------------------------------------------------------


def test_by_source_accumulates_coal_and_gas() -> None:
    state = _state()
    commit_tick(
        state,
        _result(by_source={"solar": 50.0, "wind": 30.0, "coal": 200.0, "gas": 100.0}),
    )
    assert state.today.coal_kwh == pytest.approx(200.0)
    assert state.today.gas_kwh == pytest.approx(100.0)


# --- Per-plant outputs ----------------------------------------------------


def test_per_plant_outputs_set_current_and_accumulate_kwh_today() -> None:
    state = _state()
    plant = Tile(id="coal-1", type="coal_plant", x=0, y=0, built_day=0, operational=True)
    state.tiles.append(plant)
    commit_tick(state, _result(outputs={"coal-1": 180.0}))
    commit_tick(state, _result(outputs={"coal-1": 200.0}))
    assert plant.current_output_kw == pytest.approx(200.0)  # last hour
    assert plant.kwh_served_today == pytest.approx(380.0)  # sum


def test_non_plant_tiles_untouched_by_outputs() -> None:
    state = _state()
    house = Tile(id="h-1", type="house", x=0, y=0, built_day=0, operational=True)
    state.tiles.append(house)
    commit_tick(state, _result(outputs={"h-1": 999.0}))
    assert house.current_output_kw == 0.0
    assert house.kwh_served_today == 0.0


# --- PowerNow snapshot ----------------------------------------------------


def test_power_now_snapshot_replaces_state_power_now() -> None:
    state = _state()
    commit_tick(
        state,
        _result(
            demand_kw=500.0,
            supply_kw=480.0,
            balance=BalanceState.BROWNOUT,
            by_source={"solar": 100.0, "wind": 50.0, "coal": 200.0, "gas": 130.0},
        ),
    )
    assert state.power_now.demand_kw == pytest.approx(500.0)
    assert state.power_now.supply_kw == pytest.approx(480.0)
    assert state.power_now.balance_state is BalanceState.BROWNOUT
    assert state.power_now.by_source_kw.solar == pytest.approx(100.0)
    assert state.power_now.by_source_kw.gas == pytest.approx(130.0)


# --- Hourly traces --------------------------------------------------------


def test_hourly_traces_append_in_order() -> None:
    state = _state()
    commit_tick(state, _result(supply_kw=10.0, demand_kw=20.0, balance=BalanceState.BLACKOUT))
    commit_tick(state, _result(supply_kw=30.0, demand_kw=40.0, balance=BalanceState.BROWNOUT))
    commit_tick(state, _result(supply_kw=50.0, demand_kw=50.0, balance=BalanceState.BALANCED))
    assert state.today.supply_kw_by_hour == [10.0, 30.0, 50.0]
    assert state.today.demand_kw_by_hour == [20.0, 40.0, 50.0]
    assert state.today.balance_state_by_hour == [
        BalanceState.BLACKOUT,
        BalanceState.BROWNOUT,
        BalanceState.BALANCED,
    ]

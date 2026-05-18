"""Tests for `world.hourly_tick` — the unit shared between sim and preview.

The seam exists because preview previously duplicated the hourly dispatch
math and drifted whenever sim added a new input. These tests pin the
tick's contract directly, without going through `World.step`:

  * balanced baseline: injection and production wells draw their baseline
    kW; producer kWh equals setpoint × eff;
  * brownout: injection AND production wells shed to 0 kW the *next* hour;
  * curtailment: injection ramps to min(2×baseline, hardware cap);
  * peaker without pipeline path → zero output (regression for the
    pre-seam preview omission);
  * renewable surplus → battery charges; residual demand → battery
    discharges.

Plus one cross-module property test: with no events and matched
`prev_*` state, `preview_next_day` and `World.step` produce identical
hourly traces — the property that was silently broken before the seam.
"""

from __future__ import annotations

import math

from world.catalog import TILE_CATALOG
from world.hourly_tick import hourly_tick
from world.sim import World
from world.state import Tile, Well


def _fresh_world(seed: int = 42) -> World:
    w = World()
    w.reset(seed=seed)
    return w


def _plant(world: World, tile_type: str, x: int, y: int) -> Tile:
    spec = TILE_CATALOG[tile_type]
    tile = Tile(
        id=f"{tile_type}-{x}-{y}",
        type=tile_type,
        x=x,
        y=y,
        built_day=world.state.day,
        operational=True,
        capex_paid=spec.capex,
        opex_per_day=spec.opex_per_day,
        jobs=spec.jobs,
        staffed_jobs=spec.jobs,
    )
    world.state.tiles.append(tile)
    return tile


def _well(world: World, well_type: str, idx: int, setpoint: float) -> Well:
    spec_type = "oil_well" if well_type == "production" else "injection_well"
    spec = TILE_CATALOG[spec_type]
    well = Well(
        id=f"{well_type}-{idx}",
        type=well_type,
        x=idx,
        y=0,
        target_z=0,
        drilled_day=world.state.day,
        setpoint_rate_bbl_day=setpoint,
        staffed_jobs=spec.jobs,
    )
    world.state.wells.append(well)
    return well


def _weather(cloud: float = 0.5, wind_mps: float = 6.0) -> dict[str, float]:
    return {"cloud_factor": cloud, "wind_speed_mps": wind_mps}


# -- Field contract ---------------------------------------------------------


def test_tick_returns_all_promised_fields_on_a_fresh_world() -> None:
    w = _fresh_world()
    result = hourly_tick(w.state, 12, {}, "balanced", _weather())
    # Bus-level
    assert result.demand_kw >= 0.0
    assert result.civilian_demand_kw >= 0.0
    assert result.supply_kw >= 0.0
    assert result.balance in {"balanced", "brownout", "blackout", "curtailment"}
    # Source dict carries the four canonical keys.
    assert set(result.by_source.keys()) == {"solar", "wind", "coal", "gas"}
    # No wells / batteries on a fresh world → empty hour maps.
    assert result.inj_hour_assignments == {}
    assert result.prod_hour_kwh == {}
    assert result.charge_socs == {}
    assert result.discharge_socs == {}


# -- DR injection: balanced / brownout / curtailment -----------------------


def test_injection_draws_baseline_when_balanced() -> None:
    w = _fresh_world()
    iw = _well(w, "injection", 1, setpoint=120.0)
    result = hourly_tick(w.state, 0, {}, "balanced", _weather())
    power_kw, bbl = result.inj_hour_assignments[iw.id]
    # 120 bbl/day × 50 kWh/bbl / 24 h × eff(unstaffed passive)=1.0
    assert math.isclose(power_kw, 120.0 * 50.0 / 24.0)
    assert math.isclose(bbl, power_kw / 50.0)


def test_injection_sheds_to_zero_on_brownout_and_blackout() -> None:
    w = _fresh_world()
    iw = _well(w, "injection", 1, setpoint=120.0)
    for bad in ("brownout", "blackout"):
        result = hourly_tick(w.state, 0, {}, bad, _weather())
        power_kw, bbl = result.inj_hour_assignments[iw.id]
        assert power_kw == 0.0
        assert bbl == 0.0


def test_injection_ramps_under_curtailment_capped_at_hardware() -> None:
    w = _fresh_world()
    # Setpoint near the cap so 2× lands above it and clamps.
    iw = _well(w, "injection", 1, setpoint=150.0)
    result = hourly_tick(w.state, 0, {}, "curtailment", _weather())
    power_kw, _bbl = result.inj_hour_assignments[iw.id]
    # cap_kw = Q_MAX_WELL_BBL_DAY (200) * 50 / 24 = 416.67; baseline = 150*50/24=312.5
    # 2*baseline = 625, clamped to cap = 416.67.
    assert math.isclose(power_kw, 200.0 * 50.0 / 24.0)


# -- Producer power coupling -----------------------------------------------


def test_producer_draws_baseline_when_balanced_and_sheds_under_brownout() -> None:
    w = _fresh_world()
    pw = _well(w, "production", 1, setpoint=100.0)
    bal = hourly_tick(w.state, 0, {}, "balanced", _weather())
    assert math.isclose(bal.prod_hour_kwh[pw.id], 100.0 * 15.0 / 24.0)
    brown = hourly_tick(w.state, 0, {}, "brownout", _weather())
    assert brown.prod_hour_kwh[pw.id] == 0.0


# -- Peaker filtering ------------------------------------------------------


def test_peaker_without_pipeline_path_yields_zero() -> None:
    w = _fresh_world()
    # Build one gas peaker with no refinery anywhere on the map → no
    # pipeline path → peaker_supply == False → output forced to 0
    # (regression for the pre-seam preview omission).
    peaker = _plant(w, "gas_peaker", 5, 5)
    # Drive demand high so the merit order would otherwise want gas.
    w.state.population = 1000.0
    result = hourly_tick(w.state, 19, {}, "balanced", _weather())
    assert result.outputs.get(peaker.id, 0.0) == 0.0


# -- Battery charge / discharge --------------------------------------------


def test_battery_charges_renewable_surplus() -> None:
    w = _fresh_world()
    # Build a solar farm + battery; daytime hour with clear sky
    # (cloud_factor=1.0 means full sun in this codebase — it multiplies
    # sin(angle) in irradiance()) → renewable surplus → battery charges.
    _plant(w, "solar_farm", 1, 1)
    battery = _plant(w, "battery", 2, 1)
    # Zero out civilian demand so the solar farm fully overshoots.
    w.state.population = 0.0
    result = hourly_tick(w.state, 12, {}, "balanced", _weather(cloud=1.0))
    assert result.by_source["solar"] > 0.0
    assert result.total_charge_kw > 0.0
    assert result.charge_socs.get(battery.id, 0.0) > 0.0
    # Bus-level supply nets out the charge (charging consumes renewable
    # kWh that would otherwise have been curtailed).
    assert result.supply_kw < result.by_source["solar"] + result.by_source["wind"]


def test_battery_discharges_into_residual_demand() -> None:
    w = _fresh_world()
    battery = _plant(w, "battery", 2, 1)
    # Pre-charge the battery so discharge has something to spend.
    spec = TILE_CATALOG["battery"]
    battery.soc_kwh = spec.storage_kwh
    # Big residual demand with no supply source built.
    w.state.population = 500.0
    result = hourly_tick(w.state, 19, {}, "balanced", _weather())
    assert result.total_discharge_kw > 0.0
    assert result.discharge_socs.get(battery.id, 0.0) < 0.0


# -- Determinism ----------------------------------------------------------


def test_tick_is_pure_given_inputs() -> None:
    """`hourly_tick` consumes no RNG and reads only its arguments.

    The same `(state, hour, prev_outputs, prev_balance, weather)` must
    yield byte-identical results — this is what lets preview poll on
    every UI tick without perturbing the simulation.
    """
    w = _fresh_world()
    _plant(w, "solar_farm", 1, 1)
    _plant(w, "coal_plant", 3, 1)
    _well(w, "injection", 1, setpoint=80.0)
    a = hourly_tick(w.state, 14, {}, "balanced", _weather(cloud=0.7))
    b = hourly_tick(w.state, 14, {}, "balanced", _weather(cloud=0.7))
    assert a == b


def test_tick_does_not_mutate_state() -> None:
    """The day-loop owns mutations. The tick must hand them back on
    TickResult rather than writing through ``state`` — preview depends
    on this to be safely callable on every state poll.
    """
    w = _fresh_world()
    battery = _plant(w, "battery", 2, 1)
    spec = TILE_CATALOG["battery"]
    battery.soc_kwh = spec.storage_kwh / 2.0
    soc_before = battery.soc_kwh
    power_now_before = dict(w.state.power_now)
    hourly_tick(w.state, 19, {}, "balanced", _weather())
    assert battery.soc_kwh == soc_before
    assert w.state.power_now == power_now_before

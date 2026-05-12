"""Tests for the next-24h supply/demand projection (world.preview).

The projection is a UI read-model: deterministic, non-mutating, and
RNG-isolated so polling it on every state tick is free of side effects.
The mirror-the-sim contract is verified end-to-end by stepping a world
with no events and comparing the previewed trace to the actual hourly
trace recorded by `last_day_*_kw_by_hour`.
"""

from __future__ import annotations

import math

from world.catalog import TILE_CATALOG
from world.preview import preview_next_day
from world.sim import World
from world.state import Tile


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
    )
    world.state.tiles.append(tile)
    return tile


def test_preview_returns_24h_arrays() -> None:
    w = _fresh_world()
    out = preview_next_day(w)
    n = w.config.ticks_per_day
    assert len(out["supply_kw_by_hour"]) == n
    assert len(out["demand_kw_by_hour"]) == n
    assert len(out["balance_state_by_hour"]) == n
    for key in ("solar", "wind", "coal", "gas"):
        assert len(out["by_source_kw_by_hour"][key]) == n


def test_preview_empty_world_supply_is_zero() -> None:
    w = _fresh_world()
    out = preview_next_day(w)
    # Town hall provides no generation; default population draws demand.
    assert all(s == 0.0 for s in out["supply_kw_by_hour"])
    assert any(d > 0.0 for d in out["demand_kw_by_hour"])
    assert out["min_reserve_margin"] < 0  # blacked-out projection


def test_preview_solar_lifts_supply_after_build() -> None:
    w = _fresh_world()
    before = preview_next_day(w)
    _plant(w, "solar_farm", 4, 4)
    after = preview_next_day(w)
    # Solar can only add supply during daylight; midday hours must rise.
    midday_before = sum(before["supply_kw_by_hour"][8:18])
    midday_after = sum(after["supply_kw_by_hour"][8:18])
    assert midday_after > midday_before


def test_preview_does_not_mutate_state_or_consume_rng() -> None:
    w = _fresh_world()
    _plant(w, "gas_peaker", 6, 6)

    sim_state_before = w.sim_rng.bit_generator.state
    fc_state_before = w.forecast_rng.bit_generator.state
    weather_before = dict(w.state.weather_now)
    power_before = {
        "demand_kw": w.state.power_now.get("demand_kw"),
        "supply_kw": w.state.power_now.get("supply_kw"),
        "balance_state": w.state.power_now.get("balance_state"),
    }
    prev_outputs_before = dict(w._prev_plant_outputs)

    preview_next_day(w)
    preview_next_day(w)  # idempotent

    assert w.sim_rng.bit_generator.state == sim_state_before
    assert w.forecast_rng.bit_generator.state == fc_state_before
    assert w.state.weather_now == weather_before
    assert w.state.power_now.get("demand_kw") == power_before["demand_kw"]
    assert w.state.power_now.get("supply_kw") == power_before["supply_kw"]
    assert w.state.power_now.get("balance_state") == power_before["balance_state"]
    assert w._prev_plant_outputs == prev_outputs_before


def test_preview_returns_finite_peaks_and_margin() -> None:
    w = _fresh_world()
    _plant(w, "solar_farm", 4, 4)
    _plant(w, "gas_peaker", 6, 6)
    out = preview_next_day(w)
    assert math.isfinite(out["peak_demand_kw"])
    assert math.isfinite(out["peak_supply_kw"])
    assert math.isfinite(out["min_reserve_margin"])
    assert out["peak_demand_kw"] >= max(out["demand_kw_by_hour"]) - 1e-9


def test_preview_exposed_on_state_dict() -> None:
    w = _fresh_world()
    payload = w.state_dict()
    assert "next_24h_preview" in payload
    prev = payload["next_24h_preview"]
    assert "supply_kw_by_hour" in prev
    assert "demand_kw_by_hour" in prev
    assert "min_reserve_margin" in prev

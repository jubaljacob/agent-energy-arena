"""Demand model (slice 04, brief §4.3 + PRD's split-scope event multipliers).

The PRD overrides the brief's bottom-line `× heatwave × demand_surprise`:

  * Heatwave (1.40) multiplies *residential demand only*.
  * Demand surprise (1.30) multiplies *commercial + industrial only*.
  * Process loads always pass through.

Tests force events on by injecting entries directly into
`state.active_events`, since the event-sampling pipeline doesn't fire
until slice 11.
"""

from __future__ import annotations

import pytest

from world.power import (
    DEMAND_SURPRISE_IC_MULT,
    HEATWAVE_RESIDENTIAL_MULT,
    PER_CAPITA_KW,
    commercial_factor,
    demand_surprise_multiplier,
    heatwave_multiplier,
    hourly_factor,
    residential_kw,
    total_demand_kw,
)
from world.sim import World
from world.state import Tile


def _fresh_world() -> World:
    w = World()
    w.reset(seed=42)
    return w


def _inject_tile(w: World, *, tile_type: str, x: int, y: int) -> None:
    """Bypass /build to plant a demand-bearing tile directly."""
    from world.catalog import TILE_CATALOG

    spec = TILE_CATALOG[tile_type]
    w.state.tiles.append(
        Tile(
            id=f"injected-{tile_type}-{x}-{y}",
            type=tile_type,
            x=x,
            y=y,
            built_day=0,
            operational=True,
            housing_capacity=spec.housing_capacity,
            jobs=spec.jobs,
        )
    )


# -- Hourly factor buckets ---------------------------------------------------


def test_hourly_factor_buckets_match_brief() -> None:
    # Spot-check each bucket boundary.
    assert hourly_factor(0) == 0.6
    assert hourly_factor(4) == 0.6
    assert hourly_factor(5) == 1.0
    assert hourly_factor(8) == 1.0
    assert hourly_factor(9) == 0.8
    assert hourly_factor(16) == 0.8
    assert hourly_factor(17) == 1.5
    assert hourly_factor(21) == 1.5
    assert hourly_factor(22) == 0.7
    assert hourly_factor(23) == 0.7


def test_residential_kw_zero_when_pop_zero() -> None:
    assert residential_kw(12, pop=0) == 0.0


def test_residential_kw_evening_peak() -> None:
    # pop=100, h=18 (evening peak) → 100 * 0.333 * 1.5 = 49.95
    assert residential_kw(18, pop=100) == pytest.approx(100 * PER_CAPITA_KW * 1.5)


# -- Commercial factor -------------------------------------------------------


def test_commercial_factor_full_during_business_hours() -> None:
    for h in range(8, 20):
        assert commercial_factor(h) == 1.0


def test_commercial_factor_quiet_off_hours() -> None:
    for h in (0, 7, 20, 23):
        assert commercial_factor(h) == 0.2


# -- Industrial passes through unchanged -------------------------------------


def test_industrial_continuous_demand() -> None:
    w = _fresh_world()
    _inject_tile(w, tile_type="industrial", x=5, y=5)
    # Drop population so residential is zero; isolate the industrial term.
    w.state.population = 0
    # 300 kW continuous; sample several hours.
    for h in (0, 6, 12, 18, 23):
        assert total_demand_kw(w.state, h) == pytest.approx(300.0)


def test_commercial_demand_swings_with_factor() -> None:
    w = _fresh_world()
    _inject_tile(w, tile_type="commercial", x=5, y=5)
    w.state.population = 0
    # 50 kW peak during 8-19h; 10 kW (20%) otherwise.
    assert total_demand_kw(w.state, 12) == pytest.approx(50.0)
    assert total_demand_kw(w.state, 0) == pytest.approx(10.0)


# -- Event multipliers (PRD split scope) -------------------------------------


def test_no_events_means_unit_multipliers() -> None:
    w = _fresh_world()
    assert heatwave_multiplier(w.state) == 1.0
    assert demand_surprise_multiplier(w.state) == 1.0


def test_heatwave_multiplies_residential_only() -> None:
    """Heatwave × 1.4 applies to residential demand and nothing else."""
    w = _fresh_world()
    _inject_tile(w, tile_type="industrial", x=5, y=5)
    _inject_tile(w, tile_type="commercial", x=6, y=6)
    w.state.population = 100

    h = 12  # midday: factor 0.8
    base_residential = residential_kw(h, w.state.population)
    base_industrial = 300.0
    base_commercial = 50.0  # full during 8-20h
    expected_no_event = base_residential + base_industrial + base_commercial
    assert total_demand_kw(w.state, h) == pytest.approx(expected_no_event)

    w.state.active_events = [{"type": "heatwave", "days_left": 5}]
    expected_heatwave = (
        base_residential * HEATWAVE_RESIDENTIAL_MULT + base_industrial + base_commercial
    )
    assert total_demand_kw(w.state, h) == pytest.approx(expected_heatwave)
    # Industrial + commercial untouched: difference == residential * 0.4.
    assert total_demand_kw(w.state, h) - expected_no_event == pytest.approx(
        base_residential * (HEATWAVE_RESIDENTIAL_MULT - 1.0)
    )


def test_demand_surprise_multiplies_industrial_and_commercial_only() -> None:
    """Demand surprise × 1.3 applies to I+C only, leaving residential alone."""
    w = _fresh_world()
    _inject_tile(w, tile_type="industrial", x=5, y=5)
    _inject_tile(w, tile_type="commercial", x=6, y=6)
    w.state.population = 100

    h = 14  # business hours
    base_residential = residential_kw(h, w.state.population)
    base_ic = 300.0 + 50.0
    expected_no_event = base_residential + base_ic
    assert total_demand_kw(w.state, h) == pytest.approx(expected_no_event)

    w.state.active_events = [{"type": "demand_surprise", "days_left": 10}]
    expected = base_residential + base_ic * DEMAND_SURPRISE_IC_MULT
    assert total_demand_kw(w.state, h) == pytest.approx(expected)
    # Residential untouched.
    assert total_demand_kw(w.state, h) - expected_no_event == pytest.approx(
        base_ic * (DEMAND_SURPRISE_IC_MULT - 1.0)
    )


def test_both_multipliers_compose_on_their_own_scopes() -> None:
    w = _fresh_world()
    _inject_tile(w, tile_type="industrial", x=5, y=5)
    _inject_tile(w, tile_type="commercial", x=6, y=6)
    w.state.population = 100
    w.state.active_events = [
        {"type": "heatwave", "days_left": 5},
        {"type": "demand_surprise", "days_left": 10},
    ]

    h = 14
    expected = (
        residential_kw(h, w.state.population) * HEATWAVE_RESIDENTIAL_MULT
        + (300.0 + 50.0) * DEMAND_SURPRISE_IC_MULT
    )
    assert total_demand_kw(w.state, h) == pytest.approx(expected)


# -- Sim integration ---------------------------------------------------------


def test_state_power_now_demand_populated_after_step() -> None:
    w = _fresh_world()
    w.step(days=1)
    # Demand at hour 23 (the last hour simulated of day 0): factor 0.7,
    # only town hall standing → no industrial/commercial demand. Pop changed
    # from 100 → 99 by end-of-day population update. Whatever the exact
    # value, it must be a non-negative finite float.
    val = w.state.power_now["demand_kw"]
    assert isinstance(val, float)
    assert val >= 0.0


def test_demand_includes_population_and_tiles() -> None:
    """A 1-industrial world's demand must exceed a 0-tile world's demand."""
    bare = _fresh_world()
    bare.step(days=1)
    bare_demand = bare.state.power_now["demand_kw"]

    big = _fresh_world()
    _inject_tile(big, tile_type="industrial", x=5, y=5)
    big.step(days=1)

    assert big.state.power_now["demand_kw"] > bare_demand + 200.0  # +300kW continuous

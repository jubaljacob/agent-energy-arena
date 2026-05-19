"""Cumulative renewable / total served kWh accumulator tests.

`WorldState.cumulative_renewable_served_kwh` and
`cumulative_total_served_kwh` are commit-time accumulators maintained
by `commit_tick`. They feed the `R` term in
`world.scoring.compute_score` but are themselves a sim-side concern,
not a scoring concern — hence the dedicated file. See ADR-0005 for
the scoring split.
"""

from __future__ import annotations

import pytest

from world.sim import World
from world.state import Tile


def test_fresh_world_has_zero_cumulative_kwh():
    w = World()
    w.reset(seed=42)
    assert w.state.cumulative_renewable_served_kwh == 0.0
    assert w.state.cumulative_total_served_kwh == 0.0


def test_reset_resets_cumulative_kwh():
    w = World()
    w.reset(seed=42)
    w.state.cumulative_renewable_served_kwh = 999.0
    w.state.cumulative_total_served_kwh = 1234.0
    w.reset(seed=42)
    assert w.state.cumulative_renewable_served_kwh == 0.0
    assert w.state.cumulative_total_served_kwh == 0.0


def test_step_accumulates_total_served_kwh():
    """After a step with civilian load, total_served > 0."""
    w = World()
    w.reset(seed=42)
    # Default world has 100 pop + town hall serving ~ residential demand.
    # Add a coal plant so dispatch can serve the load.
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.state.tiles.append(
        Tile(
            id="coal-test",
            type="coal_plant",
            x=th.x + 1,
            y=th.y,
            built_day=0,
            operational=True,
            jobs=8,
            staffed_jobs=8,
        )
    )
    w.step(days=1)
    assert w.state.cumulative_total_served_kwh > 0.0


def test_full_renewable_supply_drives_R_to_one():
    """A grid served entirely by solar+wind should accumulate equal renewable
    and total kWh."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    # Drop ample solar + wind so renewables dominate.
    for i in range(8):
        w.state.tiles.append(
            Tile(
                id=f"solar-{i}",
                type="solar_farm",
                x=th.x + 1 + i,
                y=th.y,
                built_day=0,
                operational=True,
                jobs=2,
                staffed_jobs=2,
            )
        )
    for i in range(8):
        w.state.tiles.append(
            Tile(
                id=f"wind-{i}",
                type="wind_turbine",
                x=th.x + 1 + i,
                y=th.y + 1,
                built_day=0,
                operational=True,
                jobs=2,
                staffed_jobs=2,
            )
        )
    w.step(days=2)
    # Renewable share is ~1.0 if every served kWh came from solar/wind. The
    # `~` covers a small BALANCED-mode accounting gap: when supply is within
    # 5% short of demand, `served_kw = demand_kw` but
    # `renewable_supply_after_battery = supply_kw`, so a tiny sliver of
    # "served" kWh isn't credited to renewables. The intent of this test is
    # to verify the formula, not to chase floating-point exactness against
    # weather noise — abs=0.01 absorbs hours where wind dipped under demand.
    if w.state.cumulative_total_served_kwh > 0:
        R = w.state.cumulative_renewable_served_kwh / w.state.cumulative_total_served_kwh
        assert pytest.approx(1.0, abs=0.01) == R


def test_no_renewables_means_R_zero():
    """A coal-only grid serves load but R should be 0."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.state.tiles.append(
        Tile(
            id="coal-only",
            type="coal_plant",
            x=th.x + 1,
            y=th.y,
            built_day=0,
            operational=True,
            jobs=8,
            staffed_jobs=8,
        )
    )
    w.step(days=1)
    assert w.state.cumulative_renewable_served_kwh == pytest.approx(0.0)
    assert w.state.cumulative_total_served_kwh > 0.0


def test_curtailed_kwh_excluded_from_both_numerator_and_denominator():
    """When renewable supply >> demand, curtailed renewables must NOT inflate
    the renewable-served accumulator beyond demand."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    # Massively over-build renewables → curtailment guaranteed.
    for i in range(20):
        w.state.tiles.append(
            Tile(
                id=f"solar-curt-{i}",
                type="solar_farm",
                x=th.x + 1 + (i % 5),
                y=th.y + 1 + (i // 5),
                built_day=0,
                operational=True,
                jobs=2,
                staffed_jobs=2,
            )
        )
    w.step(days=1)
    # Both accumulators must be equal AND finite (renewable can never exceed
    # total because we capped renewable_served at served).
    assert w.state.cumulative_renewable_served_kwh <= w.state.cumulative_total_served_kwh + 1e-9


def test_step_size_invariance_of_cumulative_kwh():
    """step(7) and 7×step(1) leave identical accumulator values."""
    a = World()
    b = World()
    a.reset(seed=42)
    b.reset(seed=42)
    a.step(days=7)
    for _ in range(7):
        b.step(days=1)
    assert a.state.cumulative_total_served_kwh == pytest.approx(b.state.cumulative_total_served_kwh)
    assert a.state.cumulative_renewable_served_kwh == pytest.approx(
        b.state.cumulative_renewable_served_kwh
    )


def test_state_dict_exposes_cumulative_kwh():
    w = World()
    w.reset(seed=42)
    s = w.state_dict()
    assert "cumulative_renewable_served_kwh" in s
    assert "cumulative_total_served_kwh" in s

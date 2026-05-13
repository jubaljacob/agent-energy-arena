"""Scenario protocol + loader + day-loop hook (open-source-arena slice 02)."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from world.scenario import NullScenario, Scenario, load_scenario
from world.sim import World

# -- Scenario base + NullScenario ------------------------------------------


def test_scenario_base_apply_is_noop() -> None:
    """The base `Scenario.apply` is a no-op so trivial subclasses don't have
    to redeclare an empty method."""
    s = Scenario()
    world = World()
    world.reset(seed=42)
    snapshot = (
        world.state.day,
        world.state.treasury,
        dict(world.state.weather_now),
        list(world.state.active_events),
        dict(world.state.weather_overrides),
        list(world.state.scenario_trace),
    )
    s.apply(world, world.state.day)
    assert (
        world.state.day,
        world.state.treasury,
        dict(world.state.weather_now),
        list(world.state.active_events),
        dict(world.state.weather_overrides),
        list(world.state.scenario_trace),
    ) == snapshot


def test_null_scenario_apply_does_not_mutate_state() -> None:
    s = NullScenario()
    world = World()
    world.reset(seed=42)
    weather_before = dict(world.state.weather_now)
    s.apply(world, 0)
    assert world.state.weather_overrides == {}
    assert world.state.scenario_trace == []
    assert world.state.weather_now == weather_before


# -- Loader ----------------------------------------------------------------


def test_load_scenario_returns_subclass_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    mod_name = "world.tests._scenario_fixture_valid"
    mod = types.ModuleType(mod_name)

    class FixtureScenario(Scenario):
        seed = 7

        def apply(self, world: World, day: int) -> None:  # pragma: no cover
            return None

    mod.FixtureScenario = FixtureScenario  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, mod_name, mod)

    instance = load_scenario(mod_name)
    assert isinstance(instance, Scenario)
    assert isinstance(instance, FixtureScenario)
    assert instance.seed == 7


def test_load_scenario_raises_for_invalid_dotted_path() -> None:
    with pytest.raises(ImportError):
        load_scenario("world.tests._does_not_exist_anywhere_12345")


def test_load_scenario_raises_when_module_has_no_subclass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod_name = "world.tests._scenario_fixture_empty"
    mod = types.ModuleType(mod_name)
    monkeypatch.setitem(sys.modules, mod_name, mod)
    with pytest.raises(ValueError, match="does not define a Scenario subclass"):
        load_scenario(mod_name)


def test_load_scenario_ignores_scenario_and_null_scenario_themselves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module that merely re-exports Scenario/NullScenario should not
    satisfy the loader — the loader looks for *user* subclasses."""
    mod_name = "world.tests._scenario_fixture_only_base"
    mod = types.ModuleType(mod_name)
    mod.Scenario = Scenario  # type: ignore[attr-defined]
    mod.NullScenario = NullScenario  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, mod_name, mod)
    with pytest.raises(ValueError):
        load_scenario(mod_name)


# -- Day-loop hook ---------------------------------------------------------


class _RecordingScenario(Scenario):
    """Records (day, world.state.active_events length) at apply time."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def apply(self, world: World, day: int) -> None:
        self.calls.append((day, len(world.state.active_events)))


def test_scenario_apply_called_once_per_day() -> None:
    s = _RecordingScenario()
    world = World(scenario=s)
    world.reset(seed=42, scenario=s)
    world.step(days=3)
    # Called for days 0, 1, 2 — one apply per simulated day.
    assert [c[0] for c in s.calls] == [0, 1, 2]


def test_scenario_apply_fires_before_stochastic_sampler() -> None:
    """A scenario-injected heatwave on day D should suppress today's
    stochastic heatwave roll via the existing "already active" guard.
    The hook must therefore fire BEFORE `sample_and_apply_events`. We
    verify this by injecting a heatwave and seeing it appear in
    active_events after step(1)."""

    class HeatwaveInjector(Scenario):
        def apply(self, world: World, day: int) -> None:
            if day == 0 and not any(e.get("type") == "heatwave" for e in world.state.active_events):
                world.state.active_events.append(
                    {
                        "type": "heatwave",
                        "started_day": day,
                        "ends_day": day + 5,
                        "severity": 1.4,
                    }
                )

    world = World(scenario=HeatwaveInjector())
    world.reset(seed=42, scenario=HeatwaveInjector())
    world.step(days=1)
    heatwaves = [e for e in world.state.active_events if e["type"] == "heatwave"]
    assert len(heatwaves) == 1
    assert heatwaves[0]["started_day"] == 0


# -- Weather override ------------------------------------------------------


def test_weather_override_wins_over_ar1_value() -> None:
    """A scenario writing `cloud_factor` and `wind_speed_mps` into
    state.weather_overrides should observably pin the next hour's
    weather_now to those values."""

    class ClipScenario(Scenario):
        def apply(self, world: World, day: int) -> None:
            world.state.weather_overrides["cloud_factor"] = 0.30
            world.state.weather_overrides["wind_speed_mps"] = 0.50

    world = World(scenario=ClipScenario())
    world.reset(seed=42, scenario=ClipScenario())
    world.step(days=1)
    assert world.state.weather_now["cloud_factor"] == pytest.approx(0.30)
    assert world.state.weather_now["wind_speed_mps"] == pytest.approx(0.50)


def test_weather_override_can_pin_solar_irradiance() -> None:
    class DarkScenario(Scenario):
        def apply(self, world: World, day: int) -> None:
            world.state.weather_overrides["solar_irradiance"] = 0.0

    world = World(scenario=DarkScenario())
    world.reset(seed=42, scenario=DarkScenario())
    world.step(days=1)
    assert world.state.weather_now["solar_irradiance"] == 0.0


def test_empty_weather_overrides_preserves_ar1_value() -> None:
    """With no overrides, the AR(1) value flows through unchanged. This
    pins the no-op contract that makes the determinism test pass."""
    a = World()
    b = World()
    a.reset(seed=42)
    b.reset(seed=42)
    a.step(days=1)
    b.step(days=1)
    assert a.state.weather_now == b.state.weather_now


# -- Determinism preservation ---------------------------------------------


def test_null_scenario_preserves_byte_identical_replay() -> None:
    """The hook call itself + NullScenario.apply (no-op) consume no RNG,
    so a baseline-seed run with NullScenario attached must produce the
    same RNG state as one without."""
    a = World()
    b = World(scenario=NullScenario())
    a.reset(seed=42)
    b.reset(seed=42)
    a.step(days=3)
    b.step(days=3)
    assert a.state.treasury == b.state.treasury
    assert int(a.state.population) == int(b.state.population)
    assert a.state.weather_now == b.state.weather_now
    # Identical RNG state: next draws must match.
    assert a.sim_rng.standard_normal() == b.sim_rng.standard_normal()


# -- WorldState fields -----------------------------------------------------


def test_world_state_has_weather_overrides_and_scenario_trace() -> None:
    world = World()
    world.reset(seed=42)
    assert isinstance(world.state.weather_overrides, dict)
    assert world.state.weather_overrides == {}
    assert isinstance(world.state.scenario_trace, list)
    assert world.state.scenario_trace == []
    # Both should accept the canonical mutation patterns scenarios use.
    world.state.weather_overrides["cloud_factor"] = 0.5
    world.state.scenario_trace.append({"day": 0, "event": "test"})
    assert world.state.weather_overrides["cloud_factor"] == 0.5
    assert world.state.scenario_trace == [{"day": 0, "event": "test"}]


# -- Helpers for type checker ---------------------------------------------


def _unused_any() -> Any:  # pragma: no cover
    return None

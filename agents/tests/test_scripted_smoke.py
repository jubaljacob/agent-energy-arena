"""Smoke + determinism + baseline-regression tests for the scripted agent."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from agents.api_client import ApiClient
from agents.scripted import ScriptedAgent
from world.api import create_app
from world.scoring import score as score_world
from world.sim import World

BASELINE_PATH = Path(__file__).resolve().parent.parent.parent / "baselines" / "seed_42.json"


def _make_client(world: World) -> TestClient:
    return TestClient(create_app(world=world))


def _play(seed: int = 42) -> World:
    """Run the scripted agent end-to-end on `seed` and return the final world."""
    world = World()
    api = ApiClient(transport=_make_client(world))
    ScriptedAgent(api, seed=seed).play_game()
    return world


def test_scripted_completes_full_game() -> None:
    """3650-day game runs to completion without crashing."""
    world = _play(seed=42)
    assert world.day == world.config.game_days
    # Pop and treasury are real numbers (no NaN bleed-through).
    # population is float-typed on WorldState (happiness-population-driver #01);
    # the /state wire surface casts to int.
    assert isinstance(world.state.population, float)
    assert world.state.population >= 0


def test_scripted_is_deterministic_across_runs() -> None:
    """Two runs on the same seed produce identical final state.

    This wraps both world-level determinism (no rogue RNG) and agent-level
    determinism (no time-dependent or hash-based ordering)."""
    a = _play(seed=42)
    b = _play(seed=42)
    assert a.state.population == b.state.population
    assert a.state.treasury == b.state.treasury
    assert a.state.cumulative_total_served_kwh == b.state.cumulative_total_served_kwh
    assert a.state.cumulative_renewable_served_kwh == b.state.cumulative_renewable_served_kwh
    # Tile + well counts match (full state-dict comparison would be brittle
    # against incidental field reordering; counts pin the structural result).
    assert len(a.state.tiles) == len(b.state.tiles)
    assert len(a.state.wells) == len(b.state.wells)


def test_scripted_matches_committed_baseline() -> None:
    """Re-run the scripted agent on seed 42 and assert (P, T) are within 5% of
    the committed `baselines/seed_42.json`. If the agent's strategy is changed
    intentionally, regenerate the baseline via:

        python -m agents.scripted --seed 42 --output baselines/seed_42.json
    """
    payload = json.loads(BASELINE_PATH.read_text())
    p_ref = float(payload["p_ref"])
    t_ref = float(payload["t_ref"])

    world = _play(seed=42)
    P_actual = float(world.state.population)
    T_actual = float(world.state.treasury) - float(world.config.starting_cash)

    # Population is float-typed (happiness-population-driver #01). Integer
    # equality on the wire-cast value is the right gate; fractional drift is
    # ignored.
    assert int(P_actual) == int(p_ref), f"population drift: actual={P_actual}, baseline={p_ref}"

    # Treasury is float; small numerical jitter is conceivable. 5% per AC.
    assert abs(T_actual - t_ref) / max(abs(t_ref), 1.0) < 0.05, (
        f"treasury delta drift: actual={T_actual}, baseline={t_ref}"
    )

    # Scoring formula is read-only over (P, T, R, baselines); pinning that
    # the score function still consumes (p_ref, t_ref) cleanly catches any
    # baseline-format regression.
    breakdown = score_world(world, p_ref, t_ref)
    assert "score" in breakdown
    assert breakdown["P"] == P_actual
    assert breakdown["T"] == T_actual


def test_scripted_builds_batteries_when_renewables_exist() -> None:
    """balance-upgrade-p0 #02: agent builds 2-4 batteries on a seed where
    solar/wind exists and treasury permits."""
    world = _play(seed=42)
    n_battery = sum(1 for t in world.state.tiles if t.type == "battery")
    n_renewable = sum(1 for t in world.state.tiles if t.type in ("solar_farm", "wind_turbine"))
    # Seed-42 bootstrap places 4 solar farms — battery target = min(4, 4//2) = 2.
    # Cap is 4 per PRD. We pin the [2, 4] band rather than exact count so a
    # later renewable build doesn't tip the test fragile.
    assert n_renewable >= 2, n_renewable
    assert 2 <= n_battery <= 4, n_battery

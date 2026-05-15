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

        python -m agents.scripted.agent --seed 42 --output baselines/seed_42.json
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


def test_bootstrap_places_park_within_cheb2_of_every_house() -> None:
    """happiness-population-driver #02 AC: after the bootstrap turn fires,
    every house placed during bootstrap has at least one park within
    Chebyshev radius 2 (the same window `world.population.update_population`
    uses for the spatial park benefit). Scoped to bootstrap because later
    phases add houses on-demand whose park coverage is the next slice's
    concern."""
    world = World()
    api = ApiClient(transport=_make_client(world))
    agent = ScriptedAgent(api, seed=42)
    api.reset(seed=42)
    state = api.state()
    agent.act(state)  # single bootstrap turn places the full minimum-viable city

    houses = [t for t in world.state.tiles if t.type == "house"]
    parks = [t for t in world.state.tiles if t.type == "park"]
    assert houses, "expected at least one house after bootstrap"
    assert parks, "expected at least one park after bootstrap"
    for h in houses:
        nearby = [p for p in parks if max(abs(h.x - p.x), abs(h.y - p.y)) <= 2]
        assert nearby, f"house at ({h.x}, {h.y}) has no park within cheb-2"


def test_scripted_pop_grows_above_starting_floor() -> None:
    """AC: P_ref reflects positive population growth over the 10-year game
    (final pop > starting pop = 100). The bootstrap park rule + velocity
    model from slice 01 should drive happiness above the 1.0 neutral
    anchor and unlock real growth. Smoke floor is 0.8 * committed P_ref
    so a minor calibration shift doesn't break CI."""
    payload = json.loads(BASELINE_PATH.read_text())
    p_ref = float(payload["p_ref"])
    starting_pop = 100.0  # cfg.starting_pop default
    assert p_ref > starting_pop, (
        f"committed baseline must show positive growth (p_ref={p_ref}, start={starting_pop})"
    )
    world = _play(seed=42)
    assert float(world.state.population) >= 0.8 * p_ref, (
        f"pop regression: actual={world.state.population}, floor={0.8 * p_ref}"
    )


def test_baseline_regeneration_byte_identical() -> None:
    """Two consecutive baseline-regeneration runs produce byte-identical
    JSON content. Guards the determinism contract that score reproduction
    rests on."""
    a = _play(seed=42)
    b = _play(seed=42)
    payload_a = {
        "seed": 42,
        "p_ref": float(a.state.population),
        "t_ref": float(a.state.treasury) - float(a.config.starting_cash),
    }
    payload_b = {
        "seed": 42,
        "p_ref": float(b.state.population),
        "t_ref": float(b.state.treasury) - float(b.config.starting_cash),
    }
    assert json.dumps(payload_a, indent=2) == json.dumps(payload_b, indent=2)

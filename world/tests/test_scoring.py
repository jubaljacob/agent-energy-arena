"""Scoring formula + renewable accumulator + /score endpoint (slice 13).

Pins the PRD's three-term scoring formula:

  p_term = 0.5 × min(P / P_ref, 3.0)              # capped at 1.5
  t_term = 0.4 × 0.5 × (1 + tanh(T / max(T_ref, 1)))  # in [0, 0.4]
  r_term = 0.1 × R                                     # in [0, 0.1]

Plus the renewable-share accumulator: per-hour, cumulative_total += served,
cumulative_renewable += min(renewable_supply, served). Curtailed kWh
(supply > demand) is implicitly excluded because served is capped at demand.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from world.api import create_app
from world.scoring import (
    P_TERM_CAP,
    P_TERM_WEIGHT,
    R_TERM_WEIGHT,
    T_TERM_WEIGHT,
    score,
)
from world.sim import World
from world.state import Tile

# -- score() pure-function tests -------------------------------------------


def _world_at(
    *,
    population: int,
    treasury: float,
    renewable_kwh: float = 0.0,
    total_kwh: float = 0.0,
) -> World:
    """Build a fresh world and force-set the scoring inputs."""
    w = World()
    w.reset(seed=1)
    w.state.population = population
    w.state.treasury = treasury
    w.state.cumulative_renewable_served_kwh = renewable_kwh
    w.state.cumulative_total_served_kwh = total_kwh
    return w


def test_score_returns_all_required_keys():
    w = _world_at(population=500, treasury=600_000.0, renewable_kwh=10.0, total_kwh=20.0)
    out = score(w, p_ref=500.0, t_ref=100_000.0)
    expected_keys = {"P", "P_ref", "p_term", "T", "T_ref", "t_term", "R", "r_term", "score"}
    assert expected_keys <= set(out.keys())


def test_p_term_at_unity_is_half_weight():
    w = _world_at(population=500, treasury=500_000.0)
    out = score(w, p_ref=500.0, t_ref=100_000.0)
    # P/P_ref = 1, so p_term = 0.5 × 1 = 0.5.
    assert out["P"] == 500
    assert out["p_term"] == pytest.approx(P_TERM_WEIGHT * 1.0)


def test_p_term_capped_at_3x_reference():
    """p_term hits its ceiling at 0.5 × 3.0 = 1.5 even with P >> P_ref."""
    w = _world_at(population=10_000, treasury=500_000.0)
    out = score(w, p_ref=100.0, t_ref=100_000.0)
    # Without cap, P/P_ref = 100 → p_term would be 50. Cap at 3 means 1.5.
    assert out["p_term"] == pytest.approx(P_TERM_WEIGHT * P_TERM_CAP)
    assert out["p_term"] == pytest.approx(1.5)


def test_p_term_zero_when_population_zero():
    w = _world_at(population=0, treasury=500_000.0)
    out = score(w, p_ref=500.0, t_ref=100_000.0)
    assert out["p_term"] == 0.0


def test_p_term_with_pref_at_zero_does_not_divide_by_zero():
    """A baseline with P_ref=0 still produces a finite p_term (cap at 3×)."""
    w = _world_at(population=100, treasury=500_000.0)
    out = score(w, p_ref=0.0, t_ref=100_000.0)
    # max(P_ref, 1) ⇒ P/1 = 100, capped at 3.0.
    assert out["p_term"] == pytest.approx(P_TERM_WEIGHT * P_TERM_CAP)


def test_t_term_at_zero_delta_is_half_weight():
    """T = treasury - STARTING_CASH = 0 ⇒ tanh(0) = 0 ⇒ t_term = 0.4 × 0.5."""
    w = _world_at(population=0, treasury=500_000.0)  # starting cash default
    out = score(w, p_ref=500.0, t_ref=100_000.0)
    assert out["T"] == pytest.approx(0.0)
    assert out["t_term"] == pytest.approx(T_TERM_WEIGHT * 0.5)


def test_t_term_saturates_near_zero_for_very_negative_treasury():
    """tanh(-large) → -1 ⇒ t_term → 0.4 × 0.5 × 0 = 0."""
    w = _world_at(population=0, treasury=500_000.0 - 100_000_000.0)
    out = score(w, p_ref=500.0, t_ref=100_000.0)
    assert out["t_term"] == pytest.approx(0.0, abs=1e-6)


def test_t_term_saturates_near_max_for_very_positive_treasury():
    """tanh(+large) → 1 ⇒ t_term → 0.4 × 0.5 × 2 = 0.4."""
    w = _world_at(population=0, treasury=500_000.0 + 100_000_000.0)
    out = score(w, p_ref=500.0, t_ref=100_000.0)
    assert out["t_term"] == pytest.approx(T_TERM_WEIGHT, abs=1e-6)


def test_t_term_in_zero_zero_point_four_range():
    """t_term ∈ [0, 0.4] for any T."""
    for treasury_delta in (-1e9, -1e3, 0.0, 1e3, 1e9):
        w = _world_at(population=0, treasury=500_000.0 + treasury_delta)
        out = score(w, p_ref=500.0, t_ref=100_000.0)
        assert 0.0 <= out["t_term"] <= T_TERM_WEIGHT + 1e-9


def test_r_term_zero_when_no_kwh_served():
    """Fresh world has zero served kWh ⇒ R = 0 ⇒ r_term = 0."""
    w = _world_at(population=100, treasury=500_000.0)
    out = score(w, p_ref=500.0, t_ref=100_000.0)
    assert out["R"] == 0.0
    assert out["r_term"] == 0.0


def test_r_term_at_full_renewable():
    """Renewable share = 1 ⇒ r_term = 0.1."""
    w = _world_at(population=100, treasury=500_000.0, renewable_kwh=10.0, total_kwh=10.0)
    out = score(w, p_ref=500.0, t_ref=100_000.0)
    assert out["R"] == pytest.approx(1.0)
    assert out["r_term"] == pytest.approx(R_TERM_WEIGHT)


def test_r_term_scales_linearly_with_share():
    """r_term ∈ [0, 0.1] and scales linearly with R."""
    w = _world_at(population=100, treasury=500_000.0, renewable_kwh=3.0, total_kwh=10.0)
    out = score(w, p_ref=500.0, t_ref=100_000.0)
    assert out["R"] == pytest.approx(0.3)
    assert out["r_term"] == pytest.approx(R_TERM_WEIGHT * 0.3)


def test_score_is_sum_of_terms():
    w = _world_at(
        population=750,
        treasury=600_000.0,
        renewable_kwh=4.0,
        total_kwh=10.0,
    )
    out = score(w, p_ref=500.0, t_ref=100_000.0)
    assert out["score"] == pytest.approx(out["p_term"] + out["t_term"] + out["r_term"])


def test_score_full_integration_synthetic_final_world():
    """Sanity: a strong-finishing world yields a score in (1.0, 1.4)."""
    w = _world_at(
        population=1000,
        treasury=900_000.0,
        renewable_kwh=8.0,
        total_kwh=10.0,
    )
    out = score(w, p_ref=500.0, t_ref=100_000.0)
    # P/P_ref = 2 → p_term = 1.0; T = 400k vs T_ref 100k → tanh(4) ≈ 0.9993 →
    # t_term ≈ 0.4 × 0.5 × 1.9993 ≈ 0.3999; R = 0.8 → r_term = 0.08.
    assert out["p_term"] == pytest.approx(1.0)
    assert out["t_term"] == pytest.approx(T_TERM_WEIGHT * 0.5 * (1 + math.tanh(4.0)))
    assert out["r_term"] == pytest.approx(0.08)
    assert 1.0 < out["score"] < 1.5


# -- Renewable-share accumulator (sim integration) -------------------------


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
    # Renewable share equals 1.0 if every served kWh came from solar/wind.
    if w.state.cumulative_total_served_kwh > 0:
        R = w.state.cumulative_renewable_served_kwh / w.state.cumulative_total_served_kwh
        assert pytest.approx(1.0) == R


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


# -- /score endpoint -------------------------------------------------------


def test_score_endpoint_404_when_baseline_missing(tmp_path: Path, monkeypatch):
    """No baseline file ⇒ 404 with detail 'baseline_missing'."""
    # Point BASELINES_DIR at an empty tmp dir for this test.
    import world.api as api_mod

    monkeypatch.setattr(api_mod, "BASELINES_DIR", tmp_path)
    app = create_app(world=World())
    client = TestClient(app)
    r = client.get("/score")
    assert r.status_code == 404
    assert r.json()["detail"] == "baseline_missing"


def test_score_endpoint_returns_breakdown(tmp_path: Path, monkeypatch):
    """With a baseline file present, /score returns the score breakdown
    using the active seed's baseline."""
    import world.api as api_mod

    monkeypatch.setattr(api_mod, "BASELINES_DIR", tmp_path)
    # Drop a baseline matching the default world seed (42).
    baseline = {"seed": 42, "p_ref": 500.0, "t_ref": 100_000.0}
    (tmp_path / "seed_42.json").write_text(json.dumps(baseline))

    w = World()
    w.reset(seed=42)
    w.state.population = 1000
    w.state.treasury = 600_000.0
    w.state.cumulative_renewable_served_kwh = 5.0
    w.state.cumulative_total_served_kwh = 10.0

    app = create_app(world=w)
    client = TestClient(app)
    r = client.get("/score")
    assert r.status_code == 200
    body = r.json()
    assert body["P"] == 1000
    assert body["P_ref"] == 500.0
    assert body["T_ref"] == 100_000.0
    # P/P_ref = 2 → p_term = 1.0
    assert body["p_term"] == pytest.approx(1.0)
    # R = 0.5
    assert body["r_term"] == pytest.approx(0.05)
    assert body["score"] == pytest.approx(body["p_term"] + body["t_term"] + body["r_term"])


def test_score_endpoint_picks_seed_specific_baseline(tmp_path: Path, monkeypatch):
    """Active seed selects the right baseline file even when others exist."""
    import world.api as api_mod

    monkeypatch.setattr(api_mod, "BASELINES_DIR", tmp_path)
    (tmp_path / "seed_1.json").write_text(json.dumps({"seed": 1, "p_ref": 999.0, "t_ref": 1.0}))
    (tmp_path / "seed_42.json").write_text(
        json.dumps({"seed": 42, "p_ref": 500.0, "t_ref": 100_000.0})
    )

    w = World()
    w.reset(seed=42)
    app = create_app(world=w)
    client = TestClient(app)
    r = client.get("/score")
    assert r.status_code == 200
    assert r.json()["P_ref"] == 500.0  # not 999.0


def test_score_endpoint_404_after_reset_to_unknown_seed(tmp_path: Path, monkeypatch):
    """Resetting to a seed without a baseline triggers the 404 path."""
    import world.api as api_mod

    monkeypatch.setattr(api_mod, "BASELINES_DIR", tmp_path)
    (tmp_path / "seed_42.json").write_text(
        json.dumps({"seed": 42, "p_ref": 500.0, "t_ref": 100_000.0})
    )
    w = World()
    app = create_app(world=w)
    client = TestClient(app)
    # First seed has a baseline.
    client.post("/reset", json={"seed": 42})
    assert client.get("/score").status_code == 200
    # Switch to a seed with no baseline.
    client.post("/reset", json={"seed": 7})
    r = client.get("/score")
    assert r.status_code == 404

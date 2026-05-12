"""Tests for /forecast (slice 12, brief §4.9).

Covers:
- record-shape and length AC
- σ growth from 0.05 (i=0) to ≈0.30 (i=23) for default hours=24
- forecast_rng isolation from sim_rng (calling /forecast many times
  does not perturb the next /step)
- two consecutive calls return different records (independent draws)
- mean of N resamples for a fixed future hour converges to truth
- sub-bounds on solar/wind clipping
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from fastapi.testclient import TestClient

from world.api import create_app
from world.forecast import (
    SIGMA_BASE,
    SIGMA_DEMAND_SCALE,
    SIGMA_RAMP,
    SIGMA_WIND_SCALE,
    forecast_records,
    sigma_at,
)
from world.power import total_demand_kw
from world.sim import World
from world.weather import irradiance, v_mean


def _fresh_world(seed: int = 42) -> World:
    w = World()
    w.reset(seed=seed)
    return w


# --- σ schedule ------------------------------------------------------------


def test_sigma_at_endpoints_default_24():
    assert sigma_at(0, 24) == pytest.approx(0.05)
    # i=23, hours=24: 0.05 + 0.25 × 23/24 ≈ 0.2896
    assert sigma_at(23, 24) == pytest.approx(SIGMA_BASE + SIGMA_RAMP * 23.0 / 24.0)
    # AC: σ at i=23 is "around 0.30" — within 0.02 of 0.30.
    assert abs(sigma_at(23, 24) - 0.30) < 0.02


def test_sigma_at_grows_monotonically():
    prev = -1.0
    for i in range(24):
        s = sigma_at(i, 24)
        assert s > prev
        prev = s


def test_sigma_records_match_schedule_default_24():
    w = _fresh_world()
    recs = forecast_records(w, 24)
    assert len(recs) == 24
    for i, r in enumerate(recs):
        assert r["sigma"] == pytest.approx(sigma_at(i, 24))


# --- length / shape --------------------------------------------------------


def test_default_24_records():
    w = _fresh_world()
    recs = forecast_records(w, 24)
    assert len(recs) == 24
    assert [r["hour_offset"] for r in recs] == list(range(24))


def test_arbitrary_hours():
    w = _fresh_world()
    assert len(forecast_records(w, 12)) == 12
    assert len(forecast_records(w, 1)) == 1
    assert len(forecast_records(w, 168)) == 168


def test_invalid_hours_raise():
    w = _fresh_world()
    with pytest.raises(ValueError):
        forecast_records(w, 0)
    with pytest.raises(ValueError):
        forecast_records(w, 169)


# --- forecast_rng isolation -----------------------------------------------


def test_forecast_does_not_advance_sim_rng_or_state():
    """Calling /forecast 100 times must not change the next /step result."""
    a = _fresh_world()
    b = _fresh_world()

    a.step(days=2)
    b.step(days=2)
    for _ in range(100):
        b.forecast(hours=24)

    a.step(days=1)
    b.step(days=1)

    # Treasury / population / weather all match.
    assert a.state.day == b.state.day
    assert a.state.treasury == b.state.treasury
    assert a.state.population == b.state.population
    assert a.state.weather_now == b.state.weather_now
    # Next sim_rng draw is byte-identical.
    assert a.sim_rng.standard_normal() == b.sim_rng.standard_normal()


def test_forecast_uses_forecast_rng_not_sim_rng():
    w = _fresh_world()
    sim_before = w.sim_rng.bit_generator.state
    forecast_before = w.forecast_rng.bit_generator.state
    w.forecast(hours=24)
    sim_after = w.sim_rng.bit_generator.state
    forecast_after = w.forecast_rng.bit_generator.state
    assert sim_before == sim_after  # untouched
    assert forecast_before != forecast_after  # advanced


# --- consecutive calls return different records ---------------------------


def test_consecutive_calls_yield_independent_samples():
    w = _fresh_world()
    a = forecast_records(w, 24)
    b = forecast_records(w, 24)
    # Same hour_offset but different noisy values — at least one of the
    # three quantities must differ at every hour where the truth is
    # non-zero.
    diffs = 0
    for ra, rb in zip(a, b, strict=True):
        if (
            ra["solar_irradiance"] != rb["solar_irradiance"]
            or ra["wind_speed_mps"] != rb["wind_speed_mps"]
            or ra["demand_factor"] != rb["demand_factor"]
        ):
            diffs += 1
    assert diffs >= 20  # virtually all hours should differ


# --- mean-of-resamples convergence ----------------------------------------


def test_mean_solar_converges_to_truth():
    """For a daytime future hour, mean(solar) over many resamples ≈ truth."""
    w = _fresh_world()
    # Step until midday (hour 11 ish) — we want a non-zero solar truth.
    # Default world starts at day=0, hour=0. Don't /step (which advances
    # day-by-day, hour stays 0). Just iterate forecast_rng many times.
    n = 4000
    solars = []
    target_offset = 11  # 12 hours ahead of hour 0 ⇒ noon
    for _ in range(n):
        recs = forecast_records(w, 24)
        solars.append(recs[target_offset]["solar_irradiance"])

    # Compute deterministic truth.
    cloud = w.state.weather_now["cloud_factor"]
    truth = irradiance(0, 0 + 1 + target_offset, cloud)
    mean = float(np.mean(solars))
    # σ at i=11 is 0.05 + 0.25 × 11/24 ≈ 0.165, clipping skews very
    # slightly. With n=4000 the SE of the mean is truth × σ / √n which
    # is comfortably under 0.01.
    assert abs(mean - truth) < 0.01


def test_mean_wind_converges_to_truth():
    w = _fresh_world()
    n = 4000
    target_offset = 5
    samples = []
    for _ in range(n):
        recs = forecast_records(w, 24)
        samples.append(recs[target_offset]["wind_speed_mps"])
    truth = v_mean(0, w.wind_phi_seed)
    mean = float(np.mean(samples))
    # σ × 5 ≈ 0.46 mps → SE of mean ≈ 0.46/√4000 ≈ 0.007. Plus a tiny
    # rectification bias from max(0, ...) clipping; truth is ~7-9 mps
    # so clipping rarely fires.
    assert abs(mean - truth) < 0.05


def test_mean_demand_converges_to_truth():
    w = _fresh_world()
    n = 4000
    target_offset = 8
    samples = []
    for _ in range(n):
        recs = forecast_records(w, 24)
        samples.append(recs[target_offset]["demand_factor"])
    target_h = (0 + 1 + target_offset) % 24
    truth = total_demand_kw(w.state, target_h)
    mean = float(np.mean(samples))
    # demand noise σ × 0.3 → relative stdev <0.1; multiplicative,
    # symmetric, no clipping; mean → truth.
    if truth == 0.0:
        assert abs(mean) < 1e-6
    else:
        assert abs(mean - truth) / max(truth, 1.0) < 0.02


# --- bounds ---------------------------------------------------------------


def test_solar_irradiance_within_unit_interval():
    w = _fresh_world()
    for _ in range(100):
        for r in forecast_records(w, 24):
            assert 0.0 <= r["solar_irradiance"] <= 1.0


def test_wind_speed_non_negative():
    w = _fresh_world()
    for _ in range(100):
        for r in forecast_records(w, 24):
            assert r["wind_speed_mps"] >= 0.0


def test_night_hours_have_zero_solar():
    """At hour 0 we forecast 24 future hours starting from hour 1.

    For the brief's sin-arc, irradiance is 0 outside (sunrise, sunset).
    At day=0, sunrise≈6, sunset≈18, so the first ~5 forecast hours are
    pre-dawn and the last ~6 are post-sunset. Truth is 0 ⇒ noisy
    output is 0 (anything × 0 = 0).
    """
    w = _fresh_world()
    recs = forecast_records(w, 24)
    # i=0 → h=1 (pre-dawn); i=22,23 → h=23,0 next day (post/pre-sunset).
    assert recs[0]["solar_irradiance"] == 0.0
    assert recs[1]["solar_irradiance"] == 0.0
    assert recs[2]["solar_irradiance"] == 0.0
    # i=4 → h=5; sunrise(0)=6 ⇒ still pre-dawn
    assert recs[4]["solar_irradiance"] == 0.0


# --- /forecast endpoint ---------------------------------------------------


def test_endpoint_returns_list_of_records(tmp_path):
    app = create_app()
    client = TestClient(app)
    r = client.get("/forecast", params={"hours": 12})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 12
    for i, rec in enumerate(body):
        assert rec["hour_offset"] == i
        assert "solar_irradiance" in rec
        assert "wind_speed_mps" in rec
        assert "demand_factor" in rec
        assert "sigma" in rec


def test_endpoint_default_hours_is_24():
    app = create_app()
    client = TestClient(app)
    r = client.get("/forecast")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 24


def test_endpoint_rejects_out_of_range_hours():
    app = create_app()
    client = TestClient(app)
    assert client.get("/forecast", params={"hours": 0}).status_code == 400
    assert client.get("/forecast", params={"hours": 169}).status_code == 400


# --- σ scale documentation -----------------------------------------------


def test_sigma_scales_match_brief():
    # Brief §4.9 wind noise stdev = σ × 5; demand noise stdev = σ × 0.3.
    assert SIGMA_WIND_SCALE == 5.0
    assert SIGMA_DEMAND_SCALE == 0.3


def test_sigma_grows_from_005_to_about_030():
    # Spec: σ at i=0 is exactly 0.05; at i=hours-1 with hours=24 is ~0.30.
    assert sigma_at(0, 24) == 0.05
    assert math.isclose(sigma_at(24, 24), SIGMA_BASE + SIGMA_RAMP, rel_tol=1e-9)


# --- workforce: staffing snapshot contract (PRD slice 03) -----------------


def _world_with_industrial(seed: int = 42) -> World:
    """Fresh world with one industrial tile next to the town hall."""
    w = _fresh_world(seed=seed)
    w.state.treasury = 1_000_000.0
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    res = w.build("industrial", th.x, th.y + 1)
    assert res["ok"] is True
    return w


def test_forecast_demand_drops_when_industrial_is_half_staffed():
    """Slice 03 AC: half-staffed producer → strictly-lower forecast demand."""
    w = _world_with_industrial()
    ind = next(t for t in w.state.tiles if t.type == "industrial")
    assert ind.staffed_jobs == ind.jobs == 30

    rng_state = w.forecast_rng.bit_generator.state
    full_recs = forecast_records(w, 24)

    # Re-seed the forecast RNG so the noise draws are identical.
    w.forecast_rng.bit_generator.state = rng_state
    ind.staffed_jobs = 15  # half-staffed → efficiency = 0.5
    half_recs = forecast_records(w, 24)

    # Industrial is 24/7 flat demand, so every hour must be strictly lower.
    for full, half in zip(full_recs, half_recs, strict=True):
        assert half["demand_factor"] < full["demand_factor"]


def test_forecast_uses_current_staffing_snapshot_not_future_projection():
    """`/forecast` consumes today's state; it does not simulate future hires.

    Build a partially-staffed industrial in a city poised for growth (capacity
    well above population, jobs above population, happiness=1.0). The mean of
    many forecast resamples for a future hour must match
    `total_demand_kw(current_state, future_h)` — NOT a hypothetical post-step
    state where update_population grew the city and auto-hired into the
    industrial's vacancies.
    """
    w = _world_with_industrial()
    ind = next(t for t in w.state.tiles if t.type == "industrial")

    # Drop the industrial to partial staffing and pin a "near growth gate"
    # state: total jobs (30 + 30 = 60) > population (50), so update_population
    # would grow the city and auto-hire toward the vacancies.
    ind.staffed_jobs = 25  # 5 vacancies
    w.state.population = 50
    w.state.happiness = 1.0

    pop_before = w.state.population
    staffed_before = {t.id: t.staffed_jobs for t in w.state.tiles}

    n = 4000
    target_offset = 8
    samples = []
    for _ in range(n):
        recs = forecast_records(w, 24)
        samples.append(recs[target_offset]["demand_factor"])

    # State must not have been mutated by /forecast — no future builds, no
    # future hires/fires, no population update.
    assert w.state.population == pop_before
    assert {t.id: t.staffed_jobs for t in w.state.tiles} == staffed_before

    target_h = (w.state.hour + 1 + target_offset) % 24
    truth = total_demand_kw(w.state, target_h)
    mean = float(np.mean(samples))
    if truth == 0.0:
        assert abs(mean) < 1e-6
    else:
        # demand noise σ × 0.3 → relative stdev <0.1 at i=8; multiplicative,
        # symmetric, no clipping; mean → truth on the current snapshot.
        assert abs(mean - truth) / max(truth, 1.0) < 0.02


def test_forecast_is_byte_identical_for_same_rng_state_and_staffing():
    """Determinism: same forecast_rng state + same staffing → identical output."""
    w = _world_with_industrial()
    ind = next(t for t in w.state.tiles if t.type == "industrial")
    ind.staffed_jobs = 18  # arbitrary partial staffing
    w.state.population = 60

    rng_state = w.forecast_rng.bit_generator.state
    first = forecast_records(w, 24)

    # Reset the forecast RNG and re-run with byte-identical staffing.
    w.forecast_rng.bit_generator.state = rng_state
    second = forecast_records(w, 24)

    for a, b in zip(first, second, strict=True):
        assert a == b


def test_forecast_records_consume_exactly_three_rng_draws_per_hour():
    """Determinism contract: three forecast_rng draws per hour (solar, wind, demand).

    The workforce module consumes no RNG, so this contract must remain true
    regardless of staffing. Re-deriving the noise draws by hand from the
    captured rng_state must reproduce the forecast's noise stream.
    """
    w = _world_with_industrial()
    ind = next(t for t in w.state.tiles if t.type == "industrial")
    ind.staffed_jobs = 10

    rng_state = w.forecast_rng.bit_generator.state
    forecast_records(w, 24)
    after_state = w.forecast_rng.bit_generator.state

    # Re-derive: consume 3 × 24 standard_normal draws from a fresh RNG seeded
    # to the same starting state. Final state must match.
    probe = np.random.default_rng()
    probe.bit_generator.state = rng_state
    for _ in range(3 * 24):
        probe.standard_normal()
    assert probe.bit_generator.state == after_state

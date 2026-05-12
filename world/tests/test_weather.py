"""Solar + wind models (slice 04, brief §4.1, §4.2).

These tests exercise `world.weather` formulas directly and the
sim-level integration that wires per-hour weather into `_advance_one_day`.
"""

from __future__ import annotations

import math

import pytest

from world.sim import World
from world.state import WorldState
from world.weather import (
    HEATWAVE_SOLAR_DERATE,
    WIND_RATED_KW,
    P_solar_kw,
    derive_phi_seed,
    irradiance,
    solar_derate_multiplier,
    sunrise,
    sunset,
    turbine_kw,
    update_cloud_factor,
    update_wind_direction,
    update_wind_speed,
    v_mean,
)

# -- Solar shape -------------------------------------------------------------


def test_solar_zero_at_midnight() -> None:
    # Hour 0 sits before any plausible sunrise across the year.
    for D in (0, 91, 182, 273):
        assert irradiance(D, 0, cloud_factor=1.0) == 0.0


def test_solar_zero_after_sunset() -> None:
    # Sunset is at most 20 across the year; hour 23 must always be dark.
    for D in (0, 91, 182, 273):
        assert irradiance(D, 23, cloud_factor=1.0) == 0.0


def test_solar_nonzero_near_noon() -> None:
    # Noon is inside [sunrise, sunset] for every day-of-year.
    for D in (0, 91, 182, 273):
        assert irradiance(D, 12, cloud_factor=1.0) > 0.0


def test_solar_peaks_near_solar_noon() -> None:
    """At solar noon (mid-arc), irradiance should hit its sin-peak of 1.0."""
    D = 80  # equinox-ish; sunrise ≈ 6, sunset ≈ 18, mid-arc ≈ 12
    sr = sunrise(D)
    ss = sunset(D)
    mid = int(round((sr + ss) / 2))
    assert mid == 12
    val = irradiance(D, 12, cloud_factor=1.0)
    # At mid-arc: angle = π/2 → sin = 1.
    assert val == pytest.approx(1.0, abs=0.05)


def test_p_solar_scales_with_peak() -> None:
    # At noon with cloud_factor=1, P_solar_kw ≈ SOLAR_PEAK_KW.
    val = P_solar_kw(80, 12, cloud_factor=1.0)
    assert val == pytest.approx(150.0, abs=10.0)


def test_cloud_factor_scales_irradiance_linearly() -> None:
    full = irradiance(80, 12, cloud_factor=1.0)
    half = irradiance(80, 12, cloud_factor=0.5)
    assert half == pytest.approx(full * 0.5)


# -- Sunrise / sunset across the year ----------------------------------------


def test_sunrise_within_brief_range() -> None:
    for D in range(0, 365, 7):
        sr = sunrise(D)
        assert 4.0 <= sr <= 8.0, (D, sr)


def test_sunset_within_brief_range() -> None:
    for D in range(0, 365, 7):
        ss = sunset(D)
        assert 16.0 <= ss <= 20.0, (D, ss)


# -- Cloud factor AR(1) ------------------------------------------------------


class _FixedRng:
    """Deterministic stand-in for np.random.Generator used in clipping tests."""

    def __init__(self, value: float) -> None:
        self._value = value

    def standard_normal(self) -> float:
        return self._value


def test_cloud_factor_clipped_below_at_0_1() -> None:
    # Pump a hugely negative noise; AR(1) target should clip to 0.1.
    out = update_cloud_factor(0.85, _FixedRng(-100.0))  # type: ignore[arg-type]
    assert out == pytest.approx(0.1)


def test_cloud_factor_clipped_above_at_1_0() -> None:
    out = update_cloud_factor(0.85, _FixedRng(+100.0))  # type: ignore[arg-type]
    assert out == pytest.approx(1.0)


def test_cloud_factor_recurrence_zero_noise() -> None:
    # cloud_factor(t+1) = 0.7*prev + 0.3*0.85 + 0 = 0.7*prev + 0.255
    out = update_cloud_factor(0.9, _FixedRng(0.0))  # type: ignore[arg-type]
    assert out == pytest.approx(0.7 * 0.9 + 0.3 * 0.85)


# -- Wind speed AR(1) and wind power curve -----------------------------------


def test_wind_below_cut_in_returns_zero() -> None:
    assert turbine_kw(2.9) == 0.0
    assert turbine_kw(0.0) == 0.0


def test_wind_above_cut_out_returns_zero() -> None:
    assert turbine_kw(25.1) == 0.0
    assert turbine_kw(40.0) == 0.0


def test_wind_at_or_above_rated_returns_full() -> None:
    assert turbine_kw(12.0) == WIND_RATED_KW
    assert turbine_kw(15.0) == WIND_RATED_KW
    assert turbine_kw(25.0) == WIND_RATED_KW


def test_wind_cubic_interpolation_at_intermediate_speed() -> None:
    # turbine_kw(7.5) = 200 * ((7.5 - 3) / 9)**3 = 200 * 0.5**3 = 25
    assert turbine_kw(7.5) == pytest.approx(25.0)


def test_wind_speed_clipped_to_zero_floor() -> None:
    out = update_wind_speed(0.0, D=0, phi_seed=0.0, rng=_FixedRng(-100.0))  # type: ignore[arg-type]
    assert out == 0.0


def test_wind_speed_clipped_at_30_ceiling() -> None:
    out = update_wind_speed(20.0, D=0, phi_seed=0.0, rng=_FixedRng(+100.0))  # type: ignore[arg-type]
    assert out == 30.0


def test_wind_direction_wraps_modulo_360() -> None:
    # Drift 700° in a single noisy step should wrap to 700 % 360 = 340.
    out = update_wind_direction(0.0, _FixedRng(700.0 / 5.0))  # type: ignore[arg-type]
    assert 0.0 <= out < 360.0
    assert out == pytest.approx(340.0)


def test_v_mean_seasonal_swing_in_brief_range() -> None:
    # 7 ± 2 m/s seasonal swing.
    samples = [v_mean(D, 0.0) for D in range(0, 365, 30)]
    assert min(samples) == pytest.approx(5.0, abs=0.2)
    assert max(samples) == pytest.approx(9.0, abs=0.2)


def test_phi_seed_is_deterministic_per_seed() -> None:
    assert derive_phi_seed(42) == derive_phi_seed(42)
    assert derive_phi_seed(42) != derive_phi_seed(43)
    # Range: [0, 2π).
    for s in (0, 1, 42, 999, 1_000_000):
        phi = derive_phi_seed(s)
        assert 0.0 <= phi < 2.0 * math.pi


# -- Heatwave solar derate (balance-upgrade-p0 issue 05) --------------------


def test_solar_derate_multiplier_returns_one_when_no_active_events() -> None:
    state = WorldState(seed=42)
    assert state.active_events == []
    assert solar_derate_multiplier(state) == 1.0


def test_solar_derate_multiplier_returns_0_8_during_heatwave() -> None:
    state = WorldState(seed=42)
    state.active_events.append({"type": "heatwave", "ends_day": 5})
    assert solar_derate_multiplier(state) == HEATWAVE_SOLAR_DERATE == 0.8


def test_solar_derate_multiplier_ignores_other_active_events() -> None:
    state = WorldState(seed=42)
    state.active_events.append({"type": "fuel_price_shock", "ends_day": 3})
    state.active_events.append({"type": "demand_surprise", "ends_day": 7})
    assert solar_derate_multiplier(state) == 1.0


# -- Sim integration ---------------------------------------------------------


def test_step_advances_24_hours_per_day() -> None:
    """One day of /step must run 24 internal hourly ticks.

    Per-hour weather consumes 3 sim_rng draws (cloud, wind speed, wind dir),
    so 7 days = 7 * 24 * 3 = 504 draws. Compare to a snapshot that advances
    its sim_rng by exactly 504 standard_normal draws.
    """
    w = World()
    w.reset(seed=42)
    w.step(days=7)

    snapshot = World()
    snapshot.reset(seed=42)
    for _ in range(7 * 24 * 3):
        snapshot.sim_rng.standard_normal()

    # Both RNGs should now be aligned.
    assert w.sim_rng.standard_normal() == snapshot.sim_rng.standard_normal()


def test_step_size_invariance_with_weather() -> None:
    """step(7) ≡ step(1)*7 with weather + demand wired in."""
    a = World()
    a.reset(seed=42)
    a.step(days=7)

    b = World()
    b.reset(seed=42)
    for _ in range(7):
        b.step(days=1)

    assert a.state.day == b.state.day == 7
    assert a.state.weather_now == b.state.weather_now
    assert a.state.power_now["demand_kw"] == b.state.power_now["demand_kw"]
    assert a.sim_rng.standard_normal() == b.sim_rng.standard_normal()


def test_weather_now_populated_after_step() -> None:
    w = World()
    w.reset(seed=42)
    w.step(days=1)
    wn = w.state.weather_now
    # Every brief field must be present and numeric.
    assert isinstance(wn["solar_irradiance"], float)
    assert isinstance(wn["wind_speed_mps"], float)
    assert isinstance(wn["wind_direction_deg"], float)
    assert isinstance(wn["cloud_factor"], float)
    # AR(1) bounds.
    assert 0.1 <= wn["cloud_factor"] <= 1.0
    assert 0.0 <= wn["wind_speed_mps"] <= 30.0
    assert 0.0 <= wn["wind_direction_deg"] < 360.0


def test_state_dict_exposes_weather_now() -> None:
    w = World()
    w.reset(seed=42)
    s = w.state_dict()
    for key in ("solar_irradiance", "wind_speed_mps", "wind_direction_deg", "cloud_factor"):
        assert key in s["weather_now"]


def test_forecast_does_not_perturb_weather_state() -> None:
    """Pounding /forecast must not advance the per-hour sim weather."""
    w = World()
    w.reset(seed=42)
    w.step(days=2)
    snapshot = dict(w.state.weather_now)

    for _ in range(50):
        w.forecast(hours=24)

    assert w.state.weather_now == snapshot

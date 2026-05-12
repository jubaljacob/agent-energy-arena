"""Solar + wind models (brief §4.1, §4.2).

All formulas keep the brief's variable names so the 1:1 mapping survives
into code review:

  * `sunrise(D)`, `sunset(D)`, `day_length(D)` — seasonal day-length swing
  * `irradiance(D, h, cloud_factor)` — sin-arc shape × cloud attenuation
  * `P_solar_kw(...)` — per-panel solar output (SOLAR_PEAK_KW = 150)
  * `cloud_factor` AR(1) recurrence with N(0, 0.10) noise, clipped [0.1, 1.0]
  * `v_mean(D, phi_seed)` — seasonal wind mean
  * `v(t+1)` AR(1) wind speed with N(0, 1.5) noise, clipped [0, 30]
  * `theta(t+1)` wind direction random walk mod 360°
  * `turbine_kw(v)` — rated 200 kW, cut-in 3, cut-out 25, cubic between
    3 and 12

`step_weather_one_hour(world)` is the single integration point: it consumes
exactly three draws from `world.sim_rng` per hour (cloud → wind speed →
wind direction, in that order) and writes the four observed fields into
`world.state.weather_now`.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

    from world.sim import World
    from world.state import WorldState

SOLAR_PEAK_KW: float = 150.0
WIND_RATED_KW: float = 200.0

# Heatwaves derate solar panel output by 20% (balance-upgrade-p0 §"Heatwave
# solar derate"): on top of the residential demand spike the existing event
# already applies, the panel-temperature efficiency loss closes the loophole
# where a solar-heavy fleet could ignore the event entirely.
HEATWAVE_SOLAR_DERATE: float = 0.8

# AR(1) noise standard deviations (brief §4.1, §4.2).
_CLOUD_NOISE_SIGMA: float = 0.10
_WIND_SPEED_NOISE_SIGMA: float = 1.5
_WIND_DIR_NOISE_SIGMA_DEG: float = 5.0

# Steady-state initialisation for the AR(1) processes; used at world reset.
INITIAL_CLOUD_FACTOR: float = 0.85
INITIAL_WIND_DIRECTION_DEG: float = 180.0


def sunrise(D: int) -> float:
    return 6.0 - 2.0 * math.sin(2.0 * math.pi * D / 365.0)


def sunset(D: int) -> float:
    return 18.0 + 2.0 * math.sin(2.0 * math.pi * D / 365.0)


def day_length(D: int) -> float:
    return sunset(D) - sunrise(D)


def irradiance(D: int, h: int, cloud_factor: float) -> float:
    sr = sunrise(D)
    ss = sunset(D)
    if h < sr or h > ss:
        return 0.0
    angle = math.pi * (h - sr) / (ss - sr)
    return math.sin(angle) * cloud_factor


def P_solar_kw(D: int, h: int, cloud_factor: float) -> float:
    return SOLAR_PEAK_KW * irradiance(D, h, cloud_factor)


def v_mean(D: int, phi_seed: float) -> float:
    return 7.0 + 2.0 * math.sin(2.0 * math.pi * D / 365.0 + phi_seed)


def turbine_kw(v: float) -> float:
    if v < 3.0 or v > 25.0:
        return 0.0
    if v >= 12.0:
        return WIND_RATED_KW
    return WIND_RATED_KW * ((v - 3.0) / 9.0) ** 3


def update_cloud_factor(prev: float, rng: np.random.Generator) -> float:
    nxt = 0.7 * prev + 0.3 * 0.85 + rng.standard_normal() * _CLOUD_NOISE_SIGMA
    return float(max(0.1, min(1.0, nxt)))


def update_wind_speed(prev: float, D: int, phi_seed: float, rng: np.random.Generator) -> float:
    nxt = 0.85 * prev + 0.15 * v_mean(D, phi_seed) + rng.standard_normal() * _WIND_SPEED_NOISE_SIGMA
    return float(max(0.0, min(30.0, nxt)))


def update_wind_direction(prev: float, rng: np.random.Generator) -> float:
    return float((prev + rng.standard_normal() * _WIND_DIR_NOISE_SIGMA_DEG) % 360.0)


def solar_derate_multiplier(state: WorldState) -> float:
    """Per-hour solar-output multiplier from active weather events.

    Returns `HEATWAVE_SOLAR_DERATE` (0.8) iff a heatwave sits in
    `state.active_events`, else 1.0. Wind is unaffected.
    """
    for e in state.active_events:
        if e.get("type") == "heatwave":
            return HEATWAVE_SOLAR_DERATE
    return 1.0


def derive_phi_seed(world_seed: int) -> float:
    """Deterministic per-seed phase for the seasonal wind cycle.

    Drawn outside the sim_rng stream so weather AR(1) draws stay aligned
    across resets that share a seed.
    """
    return (world_seed % 1000) / 1000.0 * 2.0 * math.pi


def step_weather_one_hour(world: World) -> None:
    """Advance the four weather observables by one hour.

    Consumes three sim_rng draws per call, in order: cloud_factor,
    wind_speed, wind_direction. The order is part of the determinism
    contract — do not reorder.
    """
    state = world.state
    D = state.day
    h = state.hour

    cloud = update_cloud_factor(state.weather_now["cloud_factor"], world.sim_rng)
    wind_v = update_wind_speed(
        state.weather_now["wind_speed_mps"], D, world.wind_phi_seed, world.sim_rng
    )
    wind_dir = update_wind_direction(state.weather_now["wind_direction_deg"], world.sim_rng)

    state.weather_now["cloud_factor"] = cloud
    state.weather_now["wind_speed_mps"] = wind_v
    state.weather_now["wind_direction_deg"] = wind_dir
    state.weather_now["solar_irradiance"] = irradiance(D, h, cloud)

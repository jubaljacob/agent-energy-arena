"""Tunable constants. Single source of definition; every module imports from here."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None and raw != "" else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw is not None and raw != "" else default


def _str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return raw if raw is not None and raw != "" else default


@dataclass(frozen=True)
class Config:
    world_seed: int
    world_w: int
    world_h: int
    world_d: int
    game_days: int
    manual_game_days: int
    ticks_per_day: int
    starting_cash: float
    starting_pop: int
    carbon_price: float
    grid_price_retail: float
    grid_price_export: float
    base_growth_rate: float
    outage_penalty_hour: float
    brownout_flat_penalty_hour: float
    industrial_process_co2_t_per_day: float
    api_port: int
    llm_base_url: str
    llm_model: str
    ui_play_ms: int
    ui_fast_play_ms: int


def load_config() -> Config:
    return Config(
        world_seed=_int("WORLD_SEED", 42),
        world_w=_int("WORLD_W", 32),
        world_h=_int("WORLD_H", 32),
        world_d=_int("WORLD_D", 16),
        game_days=_int("GAME_DAYS", 3650),
        manual_game_days=_int("MANUAL_GAME_DAYS", 365),
        ticks_per_day=_int("TICKS_PER_DAY", 24),
        starting_cash=_float("STARTING_CASH", 300_000),
        starting_pop=_int("STARTING_POP", 100),
        carbon_price=_float("CARBON_PRICE_USD_PER_TON", 25.0),
        grid_price_retail=_float("GRID_PRICE_RETAIL", 0.08),
        grid_price_export=_float("GRID_PRICE_EXPORT", 0.04),
        base_growth_rate=_float("BASE_GROWTH_RATE", 0.025),
        outage_penalty_hour=_float("OUTAGE_PENALTY_HOUR", 4000),
        brownout_flat_penalty_hour=_float("BROWNOUT_FLAT_PENALTY_HOUR", 1000),
        industrial_process_co2_t_per_day=_float("INDUSTRIAL_PROCESS_CO2_T_PER_DAY", 2.0),
        api_port=_int("API_PORT", 8000),
        llm_base_url=_str("LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_model=_str("LLM_MODEL", "gpt-4o-mini"),
        ui_play_ms=_int("UI_PLAY_MS", 1000),
        ui_fast_play_ms=_int("UI_FAST_PLAY_MS", 500),
    )

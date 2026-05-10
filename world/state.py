"""Core dataclasses for world state. Kept minimal in the skeleton slice."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Tile:
    id: str
    type: str
    x: int
    y: int
    built_day: int
    operational: bool = True
    current_output_kw: float = 0.0
    # Catalog snapshot at build time (kept on the tile so demolition refunds
    # the originally paid CAPEX even if the catalog is later retuned).
    capex_paid: float = 0.0
    opex_per_day: float = 0.0
    housing_capacity: int = 0
    jobs: int = 0
    # Refinery-specific (slice 09): setpoint and yesterday's actual throughput.
    # Hourly demand reads `current_throughput_bbl_day` (1-day lag) so the
    # process-load contribution to dispatch is well-defined; end-of-day
    # routing pins it after the production loop runs.
    setpoint_rate_bbl_day: float = 0.0
    current_throughput_bbl_day: float = 0.0


@dataclass
class Well:
    id: str
    type: str  # "production" | "injection"
    x: int
    y: int
    target_z: int
    drilled_day: int
    setpoint_rate_bbl_day: float = 0.0
    current_rate_bbl_day: float = 0.0
    cumulative_produced_bbl: float = 0.0
    cumulative_injected_bbl: float = 0.0
    # Catalog snapshot at drill time (kept on the well so the daily OPEX
    # accrual is independent of catalog retunes between sessions).
    capex_paid: float = 0.0
    opex_per_day: float = 0.0


@dataclass
class WorldState:
    seed: int
    day: int = 0
    hour: int = 0
    treasury: float = 0.0
    population: int = 0
    happiness: float = 1.0
    tiles: list[Tile] = field(default_factory=list)
    wells: list[Well] = field(default_factory=list)
    active_events: list[dict[str, Any]] = field(default_factory=list)
    historical_events: list[dict[str, Any]] = field(default_factory=list)
    # Cumulative count of regulatory-tightening events fired this game (capped
    # at REGULATORY_TIGHTENING_MAX_OCCURRENCES = 3). After the cap, additional
    # rolls are skipped.
    regulatory_tightenings_applied: int = 0

    # Outage hours from the previous simulated day. Both feed the happiness
    # penalty in `world.population.update_population`. Stay at 0.0 until the
    # power-dispatch slice (05) starts populating them.
    yesterday_blackout_hours: float = 0.0
    yesterday_brownout_hours: float = 0.0

    # Mutable carbon price ($/ton). Initialised to CARBON_PRICE_USD_PER_TON on
    # /reset; slice 11's regulatory-tightening events bump it (capped at 3
    # cumulative occurrences per game).
    carbon_price: float = 25.0

    # Lifetime served-kWh accumulators for the renewable share term in the
    # scoring formula (PRD §"Scoring"). Both are reset on /reset and updated
    # at the end of each hour; curtailed kWh (the post-demand surplus exported
    # to the external grid) is excluded from BOTH numerator and denominator.
    cumulative_renewable_served_kwh: float = 0.0
    cumulative_total_served_kwh: float = 0.0

    weather_now: dict[str, float] = field(
        default_factory=lambda: {
            "solar_irradiance": 0.0,
            "wind_speed_mps": 0.0,
            "wind_direction_deg": 0.0,
            "cloud_factor": 0.0,
        }
    )
    power_now: dict[str, Any] = field(
        default_factory=lambda: {
            "demand_kw": 0.0,
            "supply_kw": 0.0,
            "balance_state": "balanced",
            "by_source_kw": {"solar": 0.0, "wind": 0.0, "coal": 0.0, "gas": 0.0},
        }
    )
    # 24-element trace of the most recently completed day's hourly dispatch,
    # for the UI power tab. Empty until the first /step finishes.
    last_day_supply_kw_by_hour: list[float] = field(default_factory=list)
    last_day_demand_kw_by_hour: list[float] = field(default_factory=list)
    last_day_balance_state_by_hour: list[str] = field(default_factory=list)

    today_summary_so_far: dict[str, float] = field(
        default_factory=lambda: {
            "tax_revenue": 0.0,
            "power_revenue": 0.0,
            "oil_revenue": 0.0,
            "crude_revenue": 0.0,
            "refined_revenue": 0.0,
            "opex": 0.0,
            "fuel_cost": 0.0,
            "carbon_cost": 0.0,
            "co2_emitted_t": 0.0,
            "coal_kwh": 0.0,
            "gas_kwh": 0.0,
            "refined_bbl": 0.0,
            "blackout_hours": 0.0,
            "brownout_hours": 0.0,
            "blackout_penalty": 0.0,
            "renewable_share": 0.0,
            "injection_kw": 0.0,
        }
    )

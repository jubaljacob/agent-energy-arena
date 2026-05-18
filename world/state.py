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
    # Catalog-snapshotted continuous demand (commercial + industrial loads).
    # Read by power.py instead of TILE_CATALOG so catalog retunes do not change
    # behaviour of already-built tiles.
    demand_kw: float = 0.0
    # Workforce slice 01: hired headcount. Allocator fills/drains; defaults to
    # 0 until `world.workforce.hire_to_fill` runs.
    staffed_jobs: int = 0
    # Refinery-specific (slice 09): setpoint and yesterday's actual throughput.
    # Hourly demand reads `current_throughput_bbl_day` (1-day lag) so the
    # process-load contribution to dispatch is well-defined; end-of-day
    # routing pins it after the production loop runs.
    setpoint_rate_bbl_day: float = 0.0
    current_throughput_bbl_day: float = 0.0
    # Plant-specific (facility-economics-popup slice 03): per-day served-energy
    # accumulator. Reset at the start of each day, summed over the 24 hourly
    # dispatch outputs, and copied to `kwh_served_yesterday` at end-of-day so
    # per-plant revenue can be priced from yesterday's actual served energy
    # (no last-hour × 24 extrapolation). Non-plant tiles leave both at 0.0.
    kwh_served_today: float = 0.0
    kwh_served_yesterday: float = 0.0
    # Battery (balance-upgrade-p0 slice 01): stored energy and the manual
    # charge setpoint. Setpoint sign convention: >0 = charge, <0 = discharge,
    # 0 = auto. Both stay at 0 on non-battery tiles. Slice 02 lights them up
    # in dispatch; slice 01 only surfaces them through /state + /control.
    soc_kwh: float = 0.0
    charge_setpoint_kw: float = 0.0


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
    # Workforce slice 01: hired headcount. Allocator fills/drains; defaults to
    # 0 until `world.workforce.hire_to_fill` runs.
    staffed_jobs: int = 0
    # oilfield-v2 slice 01: 1-indexed reservoir tag resolved at drill time
    # from the target voxel. None when the target voxel is non-HC rock —
    # the well is still recorded, but it has no reservoir affiliation and
    # cannot participate in same-reservoir pressure pairing.
    reservoir_id: int | None = None
    # oilfield-v2 slice 03: per-day snapshot of `current_rate_bbl_day` taken
    # at the start of `_advance_one_day` (before production/injection
    # computation). Producers consume their own and qualifying injectors'
    # value to compute the rate-based pressure_boost. Day-0 / day-of-drill:
    # stays at 0 so a freshly-drilled well gets no boost on its first day.
    yesterday_rate_bbl_day: float = 0.0
    # oilfield-v2 slice 04: read-only telemetry for producers. Stamped in
    # the production loop of `_advance_one_day` with the values fed into
    # today's `well_production_bbl_day` call so popup/state consumers can
    # audit attribution without recomputing the reservoir/Chebyshev filter.
    # Both stay at 0 on injection wells.
    yesterday_inj_rate_bbl_day: float = 0.0
    pressure_boost: float = 0.0


@dataclass
class WorldState:
    seed: int
    day: int = 0
    hour: int = 0
    treasury: float = 0.0
    # Population is a float so sub-1/day deltas from the happiness-velocity
    # model accumulate across days; API/UI consumers see int(...) on the wire.
    population: float = 0.0
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

    # Mutable pricing/rate fields (open-source-arena slice 01). Defaults
    # mirror the module-level constants so a default-state world is
    # byte-identical to the pre-refactor behavior; World.reset is the
    # single point where defaults flow from constants/Config into state.
    # Scenarios mutate these in their `apply(world, day)` body to simulate
    # price shocks (crude collapse, fuel-cost spike, tax hike, ...).
    crude_price_usd_per_bbl: float = 40.0
    refined_price_usd_per_bbl: float = 90.0
    grid_price_retail: float = 0.08
    grid_price_export: float = 0.04
    industrial_revenue_per_day: float = 500.0
    commercial_revenue_per_resident_per_day: float = 2.0
    daily_tax_per_capita: float = 4.0
    blackout_penalty_hour: float = 5000.0
    plant_fuel_cost_per_mwh: dict[str, float] = field(
        default_factory=lambda: {"coal_plant": 12.0, "gas_peaker": 30.0}
    )

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

    # Scenario hook (open-source-arena slice 02). `weather_overrides` is a
    # transient per-hour dict consulted by `world.weather.step_weather_one_hour`
    # AFTER the AR(1) updates: any key present wins over the AR(1) value for
    # this hour. Scenarios re-write keys each day in their `apply` if they
    # want a sustained clip; an unset key falls through to the AR(1) value.
    # `scenario_trace` is an append-only log of what fired and when, for the
    # recorder; structure is owned by the scenario author.
    weather_overrides: dict[str, float] = field(default_factory=dict)
    scenario_trace: list[dict[str, Any]] = field(default_factory=list)

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
            "production_kw": 0.0,
            "industrial_revenue": 0.0,
            "commercial_revenue": 0.0,
        }
    )

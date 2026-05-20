"""Typed snapshots and the day-ledger.

These models replace the three string-keyed dicts that used to live on
``WorldState`` (``weather_now``, ``power_now``, ``today_summary_so_far``)
plus the three peer ``last_day_*_by_hour`` lists. They are pydantic
``BaseModel`` so the wire schema is one shape end-to-end: FastAPI
publishes them in OpenAPI, in-process callers read attributes, and tests
get structural equality instead of dict-key fishing. See ADR-0003 for the
"typed wire schema" decision.

Frozen vs mutable:
  * ``WeatherNow`` and ``PowerNow`` are **frozen**. The hourly tick
    produces a fresh value and the day loop *replaces* the field on
    ``WorldState``. There is no field-by-field mutation.
  * ``LastDayTrace`` and ``DayLedger`` are **mutable** because callers
    append/accumulate across the day. ``DayLedger`` disables
    ``validate_assignment`` so ``ledger.power_revenue += revenue``
    stays a single attribute write with no per-tick validation cost.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class BalanceState(StrEnum):
    """Bus-level outcome of one hourly_tick's dispatch + battery step."""

    BALANCED = "balanced"
    BROWNOUT = "brownout"
    BLACKOUT = "blackout"
    CURTAILMENT = "curtailment"


class BySourceKw(BaseModel):
    """Per-plant-type kW totals for the most recent hour.

    Taxonomy is fixed at the four plant types the dispatcher emits. A
    new plant type means updating this model in lockstep with
    ``world.power.PLANT_TYPES``.
    """

    model_config = ConfigDict(frozen=True)

    solar: float = 0.0
    wind: float = 0.0
    coal: float = 0.0
    gas: float = 0.0


class WeatherNow(BaseModel):
    """The four observable weather variables for the current hour.

    Replaced by ``step_weather_one_hour`` once per tick; never mutated
    in place. The AR(1) recurrences read the previous value off the
    incoming ``WeatherNow`` and return a new one.
    """

    model_config = ConfigDict(frozen=True)

    solar_irradiance: float = 0.0
    wind_speed_mps: float = 0.0
    wind_direction_deg: float = 0.0
    cloud_factor: float = 0.0


class PowerNow(BaseModel):
    """Bus-level snapshot after the most recent hourly_tick.

    Whole-value replacement: the day loop assigns ``state.power_now =
    PowerNow(...)`` after each tick. Field-by-field mutation is
    deliberately impossible (frozen).
    """

    model_config = ConfigDict(frozen=True)

    demand_kw: float = 0.0
    supply_kw: float = 0.0
    balance_state: BalanceState = BalanceState.BALANCED
    by_source_kw: BySourceKw = BySourceKw()


class LastDayTrace(BaseModel):
    """24-element traces of the most recently completed day, for the UI.

    Mutable because callers extend the lists tick by tick during the
    day loop and reset all three at the start of each new day.
    """

    supply_kw_by_hour: list[float] = []
    demand_kw_by_hour: list[float] = []
    balance_state_by_hour: list[BalanceState] = []

    def reset(self) -> None:
        self.supply_kw_by_hour = []
        self.demand_kw_by_hour = []
        self.balance_state_by_hour = []


class DayLedger(BaseModel):
    """Per-day bookkeeping. Holds two kinds of fields, both reset at the
    top of each simulated day:

      * **Rollups** — float fields summed across the day's 24 ticks and
        end-of-day phases (``power_revenue``, ``opex``, ``fuel_cost``,
        ``co2_emitted_t``, ``blackout_hours``, ``outage_penalty``, ...).
        The day's ``treasury`` delta is derived from these.
      * **Per-hour accumulators** — dict/list fields written by
        ``commit_tick`` across the 24 ticks and consumed by end-of-day
        phases (``inj_bbl_by_well``, ``prod_kwh_by_well``,
        ``coal_kwh_running``, ``gas_kwh_running``, ``supply_kw_by_hour``,
        ``demand_kw_by_hour``, ``balance_state_by_hour``). The traces
        are copied to ``LastDayTrace`` once the day completes.

    ``validate_assignment=False`` keeps the hot path
    (``ledger.power_revenue += revenue``, ``ledger.coal_kwh_running +=
    kwh``) a single attribute write with no validator dispatch.
    """

    model_config = ConfigDict(validate_assignment=False)

    # --- Rollups ----------------------------------------------------------
    tax_revenue: float = 0.0
    power_revenue: float = 0.0
    oil_revenue: float = 0.0
    crude_revenue: float = 0.0
    refined_revenue: float = 0.0
    opex: float = 0.0
    fuel_cost: float = 0.0
    carbon_cost: float = 0.0
    co2_emitted_t: float = 0.0
    coal_kwh: float = 0.0
    gas_kwh: float = 0.0
    refined_bbl: float = 0.0
    blackout_hours: float = 0.0
    brownout_hours: float = 0.0
    outage_penalty: float = 0.0
    renewable_share: float = 0.0
    injection_kw: float = 0.0
    production_kw: float = 0.0
    industrial_revenue: float = 0.0
    commercial_revenue: float = 0.0

    # --- Per-hour accumulators (written by commit_tick) -------------------
    # Several existing float rollups above are *also* per-hour accumulators
    # (``power_revenue``, ``coal_kwh``, ``gas_kwh``, ``injection_kw``,
    # ``production_kw``, ``blackout_hours``, ``brownout_hours``,
    # ``outage_penalty``). They live in the rollup section because the
    # end-of-day total equals the per-hour sum — no separate aggregation.
    # The fields below are dict/list shapes that have no equivalent float
    # rollup; they only exist as per-hour accumulators.

    # Per-well bbl injected today (sum of hourly DR assignments).
    inj_bbl_by_well: dict[str, float] = {}
    # Per-well kWh delivered to production today (caps daily throughput
    # when the grid sheds the well during brownout/blackout hours).
    prod_kwh_by_well: dict[str, float] = {}
    # 24-element traces of bus-level outcome per hour. Copied to
    # ``LastDayTrace`` at end-of-day for the UI's "yesterday" chart.
    supply_kw_by_hour: list[float] = []
    demand_kw_by_hour: list[float] = []
    balance_state_by_hour: list[BalanceState] = []

    def reset(self) -> None:
        """Zero every field. Called at the top of each simulated day.

        Floats reset to 0.0; dicts and lists clear in place. Pydantic
        ``model_fields`` is the single source of truth for the field
        set so adding a new accumulator only requires declaring it.
        """
        for name, info in self.__class__.model_fields.items():
            default = info.get_default(call_default_factory=True)
            setattr(self, name, default)

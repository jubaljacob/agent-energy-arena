"""One hour of simulated World time.

`hourly_tick(state, hour, prev_outputs, prev_balance, weather)` is the unit
shared between `World._advance_one_day` (which advances and mutates state)
and `world.preview.preview_next_day` (which projects the next 24 ticks
without mutating). It computes:

  1. Civilian demand (residential + commercial + industrial).
  2. Injection-well power draw, with one-hour-lagged DR shedding/ramping
     governed by ``prev_balance``.
  3. Production-well power draw, sheds to 0 on brownout/blackout.
  4. Refinery process load (kW from yesterday's pinned throughput).
  5. Plant `dispatch` against the combined demand, with gas peakers
     missing a pipeline path to a refinery filtered out and solar derated
     by the heatwave panel-temperature multiplier.
  6. Battery charge step against the renewable surplus, then discharge
     step against any residual demand.
  7. The bus-level balance state from the post-battery supply.

The function reads `state.plant_fuel_cost_per_mwh` for the dispatch merit
order. It does NOT mutate `state` — every value the caller might want to
commit comes back on `TickResult`. The caller is responsible for applying
SoC deltas, accumulating per-day kWh, recording per-plant outputs, etc.

This shape (Shape A from the design grill) makes drift impossible:
preview and sim see exactly the same hour because they call the same
function with the same signature. A change to any of the seven steps
above lands in both callers at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from world import workforce
from world.economy import refinery_process_kw
from world.pipelines import peaker_supply
from world.power import (
    PLANT_TYPES,
    battery_charge_step,
    battery_discharge_step,
    compute_balance_state,
    dispatch,
    total_demand_kw,
)
from world.subsurface import INJECTION_KWH_PER_BBL, PRODUCTION_KWH_PER_BBL, Q_MAX_WELL_BBL_DAY
from world.weather import solar_derate_multiplier

if TYPE_CHECKING:
    from world.state import WorldState


@dataclass(frozen=True)
class TickResult:
    """Everything one hourly_tick produces.

    Bus-level fields (`demand_kw`, `supply_kw`, `balance`, ...) drive the
    UI power-tab projection and the day-loop's revenue/blackout
    accounting. Plant-side (`outputs`, `by_source`) feeds the next hour's
    ramp limits and the day's renewable-share / fuel-cost accumulators.
    Battery-side (`charge_socs`, `discharge_socs`, `total_charge_kw`,
    `total_discharge_kw`, `renewable_supply_after_battery`) lets the
    caller commit SoC deltas and net charge/discharge into the
    renewable-share numerator. Well-side (`inj_hour_assignments`,
    `prod_hour_kwh`) carries the per-well power/bbl draws so the day
    loop's end-of-day production and injection bookkeeping can credit
    each well honestly.

    Fields the *day-level* loop computes (OPEX, fuel cost, production,
    population, scoring) deliberately stay out. The tick's scope is one
    hour, not one day.
    """

    # Bus-level
    demand_kw: float
    civilian_demand_kw: float  # for billable-served split: power revenue
    # bills civilian kWh only.
    supply_kw: float  # net of battery charge/discharge.
    balance: str  # "balanced" | "brownout" | "blackout" | "curtailment"
    served_kw: float
    excess_kw: float
    # Plant-side
    outputs: dict[str, float]  # per-plant kW this hour
    by_source: dict[str, float]  # {"solar","wind","coal","gas"}
    # Battery-side (caller applies SoC deltas to b.soc_kwh)
    charge_socs: dict[str, float]
    discharge_socs: dict[str, float]
    total_charge_kw: float
    total_discharge_kw: float
    renewable_supply_after_battery: float
    # Well-side (caller commits per-day accumulators)
    inj_hour_assignments: dict[str, tuple[float, float]]  # well_id -> (kW, bbl)
    prod_hour_kwh: dict[str, float]  # well_id -> kWh delivered this hour


def hourly_tick(
    state: WorldState,
    hour: int,
    prev_outputs: dict[str, float],
    prev_balance: str,
    weather: dict[str, float],
) -> TickResult:
    """Project one hour of bus-level state without mutating ``state``.

    Args:
        state: The world state. Read-only here — the tick treats it as
            an input even though Python can't enforce that. Callers that
            mutate (the day loop) do so after the tick returns, using
            the fields on TickResult.
        hour: Hour-of-day, 0..ticks_per_day-1.
        prev_outputs: The previous hour's `outputs` dict; warm-starts
            coal at must-run and cold-starts gas at 0 for plants absent
            from the dict.
        prev_balance: The previous hour's `balance` string; gates
            injection-well DR shedding (brownout/blackout → 0 kW) and
            ramping (curtailment → up to 2× baseline capped at hardware).
        weather: The hour's weather snapshot — `cloud_factor` and
            `wind_speed_mps` are read. The day loop passes
            `state.weather_now` after `step_weather_one_hour`; preview
            passes a deterministic projection.

    Returns:
        TickResult with every field both callers consume.
    """
    civilian_demand_kw = total_demand_kw(state, hour)

    # DR-on-injection (PRD §"Demand-response on injection wells"). Each
    # injection well's power for THIS hour is set by the PREVIOUS hour's
    # balance state, breaking the otherwise-circular dependency between
    # injection load and dispatch.
    inj_total_kw = 0.0
    inj_hour_assignments: dict[str, tuple[float, float]] = {}
    for iw in state.wells:
        if iw.type != "injection":
            continue
        eff = workforce.efficiency(iw)
        baseline_kw = iw.setpoint_rate_bbl_day * INJECTION_KWH_PER_BBL / 24.0 * eff
        cap_kw = Q_MAX_WELL_BBL_DAY * INJECTION_KWH_PER_BBL / 24.0 * eff
        if prev_balance in ("brownout", "blackout"):
            power_kw = 0.0
        elif prev_balance == "curtailment":
            power_kw = min(2.0 * baseline_kw, cap_kw)
        else:
            power_kw = baseline_kw
        bbl_this_hour = power_kw / INJECTION_KWH_PER_BBL
        inj_hour_assignments[iw.id] = (power_kw, bbl_this_hour)
        inj_total_kw += power_kw

    # Production-well power coupling (economy-rebalance slice 07): each
    # producer draws `setpoint × PRODUCTION_KWH_PER_BBL / 24 × eff` at
    # baseline, sheds to 0 on brownout/blackout. No curtailment ramp-up:
    # a producer cannot lift faster than its setpoint.
    prod_total_kw = 0.0
    prod_hour_kwh: dict[str, float] = {}
    for pw in state.wells:
        if pw.type != "production":
            continue
        eff = workforce.efficiency(pw)
        baseline_kw = pw.setpoint_rate_bbl_day * PRODUCTION_KWH_PER_BBL / 24.0 * eff
        power_kw = 0.0 if prev_balance in ("brownout", "blackout") else baseline_kw
        prod_hour_kwh[pw.id] = power_kw
        prod_total_kw += power_kw

    # Refinery process load (slice 09): hourly kW = yesterday's actual
    # throughput × KWH_PER_BBL / 24. The 1-day lag mirrors DR injection.
    refinery_process_load_kw = sum(
        refinery_process_kw(t.current_throughput_bbl_day)
        for t in state.tiles
        if t.type == "refinery" and t.operational
    )

    demand_kw = civilian_demand_kw + inj_total_kw + prod_total_kw + refinery_process_load_kw

    # Gas peakers must share a 4-connected pipeline network with at
    # least one operational refinery to dispatch this hour. Filtered
    # peakers are treated identically to plant_failure (zero output).
    plants = [t for t in state.tiles if t.type in PLANT_TYPES]
    unsupplied_peakers = frozenset(
        p.id for p in plants if p.type == "gas_peaker" and not peaker_supply(p, state.tiles)
    )

    outputs, supply_kw, by_source = dispatch(
        plants,
        demand_kw,
        prev_outputs,
        weather,
        state.day,
        hour,
        solar_derate=solar_derate_multiplier(state),
        fuel_cost_per_mwh=state.plant_fuel_cost_per_mwh,
        unsupplied_peaker_ids=unsupplied_peakers,
    )

    # Battery dispatch (balance-upgrade-p0 slice 02). Charging from
    # fossil is forbidden by construction: only renewable surplus
    # (solar+wind, after demand) enters batteries.
    batteries = [t for t in state.tiles if t.type == "battery"]
    renewable_supply_kw = by_source.get("solar", 0.0) + by_source.get("wind", 0.0)
    _charges, total_charge_kw, charge_socs = battery_charge_step(
        batteries, renewable_supply_kw, demand_kw
    )
    residual_demand_kw = max(0.0, demand_kw - supply_kw)
    _discharges, total_discharge_kw, discharge_socs = battery_discharge_step(
        batteries, residual_demand_kw
    )

    # Bus-level supply nets out battery flow: charging consumes
    # renewable kWh that would otherwise have been curtailed, discharge
    # adds delivered kWh to supply.
    net_supply_kw = supply_kw - total_charge_kw + total_discharge_kw
    balance, served_kw, excess_kw, _r = compute_balance_state(net_supply_kw, demand_kw)

    renewable_supply_after_battery = renewable_supply_kw - total_charge_kw + total_discharge_kw

    return TickResult(
        demand_kw=demand_kw,
        civilian_demand_kw=civilian_demand_kw,
        supply_kw=net_supply_kw,
        balance=balance,
        served_kw=served_kw,
        excess_kw=excess_kw,
        outputs=outputs,
        by_source=by_source,
        charge_socs=charge_socs,
        discharge_socs=discharge_socs,
        total_charge_kw=total_charge_kw,
        total_discharge_kw=total_discharge_kw,
        renewable_supply_after_battery=renewable_supply_after_battery,
        inj_hour_assignments=inj_hour_assignments,
        prod_hour_kwh=prod_hour_kwh,
    )

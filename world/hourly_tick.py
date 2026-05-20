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

`hourly_tick` is **pure** — every value the caller might want to commit
comes back on ``TickResult``. ``commit_tick(state, result)`` is the
sim-only mutating peer: it writes the hour's mutations to ``state``
(battery SoC, outage bookkeeping, revenue accrual, renewable share,
injection/production accumulators, by-source running totals, per-plant
outputs, ``PowerNow`` snapshot, hourly traces). Preview calls
``hourly_tick`` but never ``commit_tick`` — the two-function split is
what makes preview/sim drift-impossible: both run the same projection,
only one commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from world import workforce
from world.catalog import TILE_CATALOG
from world.economy import refinery_process_kw
from world.event_effects import heatwave_solar_derate
from world.power import (
    PLANT_TYPES,
    R_BROWNOUT,
    battery_charge_step,
    battery_discharge_step,
    compute_balance_state,
    dispatch,
    total_demand_kw,
)
from world.snapshots import BalanceState, BySourceKw, PowerNow, WeatherNow
from world.subsurface import INJECTION_KWH_PER_BBL, PRODUCTION_KWH_PER_BBL, Q_MAX_WELL_BBL_DAY

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
    balance: BalanceState
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
    prev_balance: BalanceState,
    weather: WeatherNow,
    peaker_supplied_ids: frozenset[str],
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
        prev_balance: The previous hour's `balance` state; gates
            injection-well DR shedding (brownout/blackout → 0 kW) and
            ramping (curtailment → up to 2× baseline capped at hardware).
        weather: The hour's weather snapshot — `cloud_factor` and
            `wind_speed_mps` are read. The day loop passes
            `state.weather_now` after `step_weather_one_hour`; preview
            passes a deterministic projection.
        peaker_supplied_ids: Gas peakers that share a pipeline network
            with an operational refinery this epoch. Precomputed once
            per day (or once per preview) by the caller from the same
            ``PipelineGraph`` ``route_oil`` consumes — refinery
            operational flags are day-stable, so the set does not
            change inside the 24-hour loop.

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
        if prev_balance in (BalanceState.BROWNOUT, BalanceState.BLACKOUT):
            power_kw = 0.0
        elif prev_balance is BalanceState.CURTAILMENT:
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
        power_kw = (
            0.0 if prev_balance in (BalanceState.BROWNOUT, BalanceState.BLACKOUT) else baseline_kw
        )
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
    # ``peaker_supplied_ids`` is precomputed once per epoch from the
    # day's ``PipelineGraph``; we invert it here to get the per-hour
    # zero-output set.
    plants = [t for t in state.tiles if t.type in PLANT_TYPES]
    unsupplied_peakers = frozenset(
        p.id for p in plants if p.type == "gas_peaker" and p.id not in peaker_supplied_ids
    )

    outputs, supply_kw, by_source = dispatch(
        plants,
        demand_kw,
        prev_outputs,
        weather,
        state.day,
        hour,
        solar_derate=heatwave_solar_derate(state),
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


def commit_tick(state: WorldState, result: TickResult) -> None:
    """Apply one ``TickResult`` to ``state`` — the sim-only mutating peer
    of ``hourly_tick``. Preview discards ``result`` instead.

    Every mutation the day used to inline in ``_advance_one_day``'s hourly
    loop lives here. The order matters: ``PowerNow`` is the last write,
    because the next hour reads ``state.power_now.balance_state`` to drive
    DR-on-injection and producer shedding.

    Treasury is *not* touched here. Per-hour outage penalties accumulate
    in ``state.today.outage_penalty`` and are settled once at end of day
    alongside ``power_revenue`` (same pattern: accumulate during the day,
    credit/debit treasury once when the day completes).
    """
    today = state.today

    # Battery SoC delta + clamp at storage bounds (absorbs float jitter).
    for b in state.tiles:
        if b.type != "battery":
            continue
        spec = TILE_CATALOG[b.type]
        delta = result.charge_socs.get(b.id, 0.0) + result.discharge_socs.get(b.id, 0.0)
        b.soc_kwh = max(0.0, min(spec.storage_kwh, b.soc_kwh + delta))

    # Outage bookkeeping.
    #   * Blackout charges the full `outage_penalty_hour` flat — supply
    #     is effectively zero, so the city has lost all power.
    #   * Brownout charges a `brownout_flat_penalty_hour` floor plus a
    #     ramp on `unserved_share = 1 - supply/demand`. The ramp is
    #     calibrated so a brownout right at the blackout boundary
    #     (R = R_BROWNOUT) costs the same as a blackout, then it caps
    #     there so a deeper brownout never out-costs an outright
    #     blackout.
    if result.balance is BalanceState.BLACKOUT:
        today.blackout_hours += 1.0
        today.outage_penalty += state.outage_penalty_hour
    elif result.balance is BalanceState.BROWNOUT:
        today.brownout_hours += 1.0
        unserved_share = 0.0
        if result.demand_kw > 0.0:
            unserved_share = max(0.0, 1.0 - result.supply_kw / result.demand_kw)
        flat = state.brownout_flat_penalty_hour
        cap = state.outage_penalty_hour
        ramp = (cap - flat) / (1.0 - R_BROWNOUT)
        today.outage_penalty += min(cap, flat + ramp * unserved_share)

    # Power revenue. Process loads (injection wells, refinery) are
    # unbilled; only civilian kWh × retail. Curtailment exports the
    # post-injection surplus at the export tariff.
    billable_served_kw = min(result.supply_kw, result.civilian_demand_kw)
    today.power_revenue += billable_served_kw * state.grid_price_retail
    if result.balance is BalanceState.CURTAILMENT and result.excess_kw > 0:
        today.power_revenue += result.excess_kw * state.grid_price_export

    # Renewable-share accumulator (PRD §"Scoring"). Battery accounting:
    # charged kWh subtracted from renewable supply (charge step happens
    # before discharge), discharged kWh added back as 100% renewable —
    # round-trip losses vanish from both numerator and denominator.
    renewable_served_kw = min(result.renewable_supply_after_battery, result.served_kw)
    state.cumulative_total_served_kwh += result.served_kw
    state.cumulative_renewable_served_kwh += renewable_served_kw

    # DR injection commits — bbl actually delivered and total kWh drawn.
    # If supply collapsed mid-hour, injectors still contributed their
    # pre-set baseline to this hour's demand; DR sheds the *next* hour,
    # when prev_balance reflects the bad state.
    for iw_id, (power_kw, bbl_this_hour) in result.inj_hour_assignments.items():
        today.inj_bbl_by_well[iw_id] = today.inj_bbl_by_well.get(iw_id, 0.0) + bbl_this_hour
        today.injection_kw += power_kw

    # Per-production-well kWh accumulator. Summed across 24 hours; the
    # end-of-day production loop divides by PRODUCTION_KWH_PER_BBL to get
    # the day's power-allocated bbl budget per well.
    for pw_id, power_kw in result.prod_hour_kwh.items():
        today.prod_kwh_by_well[pw_id] = today.prod_kwh_by_well.get(pw_id, 0.0) + power_kw
        today.production_kw += power_kw

    today.coal_kwh += result.by_source["coal"]
    today.gas_kwh += result.by_source["gas"]

    # Per-plant outputs feed the next hour's ramp-limit accounting AND
    # the day's per-plant served-energy total. ``current_output_kw`` IS
    # the prev_outputs source for the next tick — no parallel structure.
    for p in state.tiles:
        if p.type not in PLANT_TYPES:
            continue
        out_kw = result.outputs.get(p.id, 0.0)
        p.current_output_kw = out_kw
        p.kwh_served_today += out_kw

    # Hourly traces (24-element by end-of-day). Pinned to
    # ``state.last_day_trace`` once the day completes.
    today.supply_kw_by_hour.append(result.supply_kw)
    today.demand_kw_by_hour.append(result.demand_kw)
    today.balance_state_by_hour.append(result.balance)

    # PowerNow last — the next hour reads ``state.power_now.balance_state``
    # at the top of its tick to drive DR and producer shedding.
    state.power_now = PowerNow(
        demand_kw=result.demand_kw,
        supply_kw=result.supply_kw,
        balance_state=result.balance,
        by_source_kw=BySourceKw(**result.by_source),
    )

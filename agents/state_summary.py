"""Compress the world's `/state` + `/forecast` payloads into a compact
text summary for the LLM ReAct agent.

The PRD's canonical extension point — participants override this in
`submit/agent.py` to add domain knowledge or strip detail they don't
need. Target: ~1000 tokens for a typical mid-game state, well inside
the 2000-token max-tokens budget per response and the 1M cumulative
ceiling per game.

Compression strategy:
- Top-line: day, hour, treasury, population, happiness, carbon price.
- Tile inventory: counts by type (full per-tile list is too big once
  the city has 50+ tiles).
- Wells: compact one-row-per-well table (id, type, x, y, z, setpoint,
  cum-bbl).
- Reservoirs: top-K=30 voxels by oil×perm score (already compressed by
  /reservoirs).
- Power: yesterday's 24-hour supply/demand/balance traces as compact
  arrays.
- Forecast: next-24h as one line per hour (solar / wind / demand).
- Events: active list with countdown; historical count.
- Score-relevant cumulative kWh.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

TOP_K_VOXELS: int = 30
MAX_WELL_ROWS: int = 20
MAX_HISTORICAL_EVENTS: int = 4


def summarize_state(
    obs: dict[str, Any],
    forecast: list[dict[str, Any]] | None = None,
) -> str:
    """Produce a compact text summary of the world state for the LLM.

    `obs` is the parsed `/state` payload. `forecast` is the parsed
    `/forecast` payload (a list of next-hour records); pass None to
    skip the forecast block.
    """
    lines: list[str] = []
    cfg = obs.get("config", {})
    lines.append(
        f"DAY {obs.get('day')}/{cfg.get('active_game_days', cfg.get('game_days'))} "
        f"hour={obs.get('hour')} session={cfg.get('session', '?')}"
    )
    lines.append(
        f"treasury=${_fmt(obs.get('treasury', 0))} "
        f"pop={obs.get('population', 0)} happiness={_round(obs.get('happiness', 0), 2)} "
        f"carbon_price=${_round(cfg.get('carbon_price', 0), 2)}/t"
    )
    cumul_total = float(obs.get("cumulative_total_served_kwh", 0.0))
    cumul_renew = float(obs.get("cumulative_renewable_served_kwh", 0.0))
    r_share = cumul_renew / cumul_total if cumul_total > 0 else 0.0
    lines.append(
        f"served_kwh_total={_fmt(cumul_total)} renewable={_fmt(cumul_renew)} "
        f"R_share={_round(r_share, 3)}"
    )
    lines.append(
        f"world={cfg.get('world_w', '?')}x{cfg.get('world_h', '?')}x{cfg.get('world_d', '?')} "
        f"starting_cash=${_fmt(cfg.get('starting_cash', 0))}"
    )

    # --- Tile inventory: counts + town-hall location --------------------
    tiles = obs.get("tiles") or []
    counts = Counter(t.get("type", "?") for t in tiles)
    if counts:
        inv = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        lines.append(f"TILES ({len(tiles)}): {inv}")
    town_hall = next((t for t in tiles if t.get("type") == "town_hall"), None)
    if town_hall is not None:
        lines.append(f"town_hall@({town_hall.get('x')},{town_hall.get('y')})")
    # Plant positions are useful for the model when deciding where to
    # demolish / where to place renewables. Keep this compact.
    plants = [t for t in tiles if t.get("type") in _PLANT_TYPES]
    if plants:
        lines.append(
            "plants: "
            + " ".join(
                f"{t['type'][:4]}#{t['id']}@({t['x']},{t['y']})"
                + ("" if t.get("operational", True) else "[OFFLINE]")
                for t in plants[:24]
            )
        )

    # --- Wells -----------------------------------------------------------
    wells = obs.get("wells") or []
    if wells:
        lines.append(f"WELLS ({len(wells)}):")
        for w in wells[:MAX_WELL_ROWS]:
            cum = w.get("cumulative_produced_bbl", w.get("cumulative_injected_bbl", 0))
            lines.append(
                f"  id={w.get('id')} type={w.get('type')} "
                f"@({w.get('x')},{w.get('y')},z={w.get('target_z')}) "
                f"setpoint={_round(w.get('setpoint_rate_bbl_day', 0), 1)} "
                f"cum_bbl={_round(cum, 0)}"
            )
        if len(wells) > MAX_WELL_ROWS:
            lines.append(f"  ...{len(wells) - MAX_WELL_ROWS} more wells")

    # --- Reservoirs: top-K voxels ---------------------------------------
    reservoirs = obs.get("reservoirs_revealed") or {}
    top = reservoirs.get("top_k") or []
    if top:
        lines.append(f"RESERVOIRS top-{min(len(top), TOP_K_VOXELS)} revealed voxels:")
        for v in top[:TOP_K_VOXELS]:
            lines.append(
                f"  ({v.get('x')},{v.get('y')},{v.get('z')}) "
                f"oil={_round(v.get('oil_estimate_bbl', 0), 0)}bbl "
                f"perm={_round(v.get('perm_estimate_md', 0), 0)}mD"
            )

    # --- Power: now + yesterday's hourly traces -------------------------
    power = obs.get("power_now") or {}
    if power:
        lines.append(
            f"power_now: supply={_round(power.get('supply_kw', 0), 0)}kW "
            f"demand={_round(power.get('demand_kw', 0), 0)}kW "
            f"balance={power.get('balance_state', '?')}"
        )
    supply_h = obs.get("last_day_supply_kw_by_hour") or []
    demand_h = obs.get("last_day_demand_kw_by_hour") or []
    balance_h = obs.get("last_day_balance_state_by_hour") or []
    if supply_h:
        lines.append("last_day_supply_kw: " + _compact_floats(supply_h))
    if demand_h:
        lines.append("last_day_demand_kw: " + _compact_floats(demand_h))
    if balance_h:
        lines.append("last_day_balance: " + " ".join(s[:1] for s in balance_h))

    # --- Today P&L so far ----------------------------------------------
    today = obs.get("today_summary_so_far") or {}
    if today:
        # Drop zero-valued keys to keep the line short.
        nz = {k: v for k, v in today.items() if abs(float(v or 0)) > 0.01}
        if nz:
            lines.append(
                "today_so_far: " + " ".join(f"{k}={_round(v, 0)}" for k, v in sorted(nz.items()))
            )

    # --- Events ---------------------------------------------------------
    active = obs.get("active_events") or []
    if active:
        lines.append(
            "ACTIVE_EVENTS: "
            + "; ".join(
                f"{e.get('type')}(ends_day={e.get('ends_day')},severity={e.get('severity', '?')})"
                for e in active
            )
        )
    historical = obs.get("historical_events") or []
    if historical:
        recent = historical[-MAX_HISTORICAL_EVENTS:]
        lines.append(
            f"recent_events({len(historical)} total): "
            + "; ".join(f"{e.get('type')}@d{e.get('started_day')}" for e in recent)
        )

    # --- Forecast block -------------------------------------------------
    if forecast:
        lines.append(f"FORECAST next {len(forecast)}h (h_offset solar wind_mps demand σ):")
        for f in forecast:
            lines.append(
                f"  +{f.get('hour_offset', 0):>2}h "
                f"solar={_round(f.get('solar_irradiance', 0), 2)} "
                f"wind={_round(f.get('wind_speed_mps', 0), 1)} "
                f"demand={_round(f.get('demand_factor', 0), 0)} "
                f"σ={_round(f.get('sigma', 0), 2)}"
            )

    return "\n".join(lines)


_PLANT_TYPES = {"solar_farm", "wind_turbine", "gas_peaker", "coal_plant"}


def _fmt(n: Any) -> str:
    """Render a number with thousand separators, no decimals."""
    try:
        return f"{int(float(n)):,}"
    except (TypeError, ValueError):
        return str(n)


def _round(n: Any, places: int) -> float | str:
    try:
        return round(float(n), places)
    except (TypeError, ValueError):
        return str(n)


def _compact_floats(arr: list[Any]) -> str:
    """24-entry float array → space-separated integers (kW rounded)."""
    parts: list[str] = []
    for v in arr:
        try:
            parts.append(str(int(round(float(v)))))
        except (TypeError, ValueError):
            parts.append("?")
    return " ".join(parts)

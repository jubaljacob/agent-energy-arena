"""Hourly demand model (brief §4.3, with PRD's split-scope multipliers).

`total_demand_kw(state, h)` is the per-hour total electric load. Sources:

  * Residential: `pop * PER_CAPITA_KW * hourly_factor(h)`
  * Commercial: full demand 8 ≤ h < 20, 20% otherwise
  * Industrial: continuous full demand
  * Process loads (refinery + injection wells): always pass through

Event multipliers, per the PRD's correction to the brief's bottom-line
multipliers:

  * Heatwave (1.40) multiplies *residential demand only* — A/C drives it.
  * Demand surprise (1.30) multiplies *commercial + industrial only*.
  * Process loads are unaffected by either multiplier.

In slice 04 events are stubbed; both `heatwave_active` and
`demand_surprise_active` return False unless a test injects a matching
entry into `state.active_events`. The flags go live in slice 11.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from world.catalog import TILE_CATALOG

if TYPE_CHECKING:
    from world.state import WorldState

PER_CAPITA_KW: float = 0.333  # 8 kWh/day continuous; brief §4.3
HEATWAVE_RESIDENTIAL_MULT: float = 1.40
DEMAND_SURPRISE_IC_MULT: float = 1.30


def hourly_factor(h: int) -> float:
    if h < 5:
        return 0.6  # night
    if h < 9:
        return 1.0  # morning
    if h < 17:
        return 0.8  # midday
    if h < 22:
        return 1.5  # evening peak
    return 0.7  # late night


def residential_kw(h: int, pop: int) -> float:
    return pop * PER_CAPITA_KW * hourly_factor(h)


def commercial_factor(h: int) -> float:
    return 1.0 if 8 <= h < 20 else 0.2


def heatwave_active(state: WorldState) -> bool:
    return any(e.get("type") == "heatwave" for e in state.active_events)


def demand_surprise_active(state: WorldState) -> bool:
    return any(e.get("type") == "demand_surprise" for e in state.active_events)


def heatwave_multiplier(state: WorldState) -> float:
    return HEATWAVE_RESIDENTIAL_MULT if heatwave_active(state) else 1.0


def demand_surprise_multiplier(state: WorldState) -> float:
    return DEMAND_SURPRISE_IC_MULT if demand_surprise_active(state) else 1.0


def _industrial_kw(state: WorldState) -> float:
    return sum(TILE_CATALOG[t.type].demand_kw for t in state.tiles if t.type == "industrial")


def _commercial_peak_kw(state: WorldState) -> float:
    return sum(TILE_CATALOG[t.type].demand_kw for t in state.tiles if t.type == "commercial")


def _process_loads_kw(state: WorldState) -> float:
    # Refineries (slice 09) and injection wells (slice 08) will contribute here.
    # The slice-04 world has neither, so this is always 0.0.
    return 0.0


def total_demand_kw(state: WorldState, h: int) -> float:
    res = residential_kw(h, state.population) * heatwave_multiplier(state)
    ic = (_industrial_kw(state) + _commercial_peak_kw(state) * commercial_factor(h)) * (
        demand_surprise_multiplier(state)
    )
    process = _process_loads_kw(state)
    return float(res + ic + process)

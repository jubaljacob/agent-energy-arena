"""Event sampling and application (slice 11, brief §4.11 + PRD §Events).

Five event types are rolled at the start of each simulated day from a
dedicated `event_rng` stream (a third child of the master seed alongside
`sim_rng` and `forecast_rng`). The dedicated stream keeps the slice-04
weather-draw budget pinned at 3 sim_rng draws per hour — events draws
don't interleave with the deterministic weather sequence.

| Event                  | Probability       | Duration (days) | Effect                            |
|-----------------------|-------------------|-----------------|-----------------------------------|
| heatwave              | 0.003 daily       | 5 fixed         | residential demand × 1.40         |
| plant_failure         | 0.001 per fossil  | 3-7 uniform     | affected plant outputs 0 kW       |
| fuel_price_shock      | 0.002 daily       | 30 fixed        | gas + coal fuel cost × 2          |
| demand_surprise       | 0.003 daily       | 10 fixed        | I+C demand × 1.30                 |
| regulatory_tightening | 0.001 daily (cap 3) | permanent     | carbon_price × 1.5                |

Sampling order (RNG draw stability across replays):
1. heatwave roll (skipped if active)
2. fuel_price_shock roll (skipped if active)
3. demand_surprise roll (skipped if active)
4. regulatory_tightening roll (skipped if cap reached)
5. for each fossil plant (sorted by id): plant_failure roll; if hit,
   1 duration draw is consumed unconditionally so RNG state stays stable
   even if the plant is already failed.

Plant_failure: a hit on an already-failed plant consumes the duration
draw but creates no new event entry. Multiple plant_failures can be
active concurrently — the "at most one" rule applies only to
heatwave / fuel_price_shock / demand_surprise.

Regulatory_tightening events go directly to `historical_events` since
they have no expiry; the carbon-price bump persists in
`state.carbon_price`. The other event types live in `active_events`
until their `ends_day` is reached, then move to `historical_events`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

    from world.sim import World
    from world.state import WorldState

# Probabilities (per day unless noted).
HEATWAVE_PROB: float = 0.003
PLANT_FAILURE_PROB_PER_PLANT: float = 0.001
FUEL_PRICE_SHOCK_PROB: float = 0.002
DEMAND_SURPRISE_PROB: float = 0.003
REGULATORY_TIGHTENING_PROB: float = 0.001

# Durations (days).
HEATWAVE_DURATION: int = 5
PLANT_FAILURE_DURATION_MIN: int = 3
PLANT_FAILURE_DURATION_MAX: int = 7  # inclusive
FUEL_PRICE_SHOCK_DURATION: int = 30
DEMAND_SURPRISE_DURATION: int = 10

# Multipliers / constants.
HEATWAVE_RESIDENTIAL_MULT: float = 1.40
FUEL_PRICE_SHOCK_MULT: float = 2.0
DEMAND_SURPRISE_IC_MULT: float = 1.30
REGULATORY_TIGHTENING_MULT: float = 1.5
REGULATORY_TIGHTENING_MAX_OCCURRENCES: int = 3

# Plant-failure target set + finite-duration whitelist.
FOSSIL_PLANT_TYPES: frozenset[str] = frozenset({"coal_plant", "gas_peaker"})
FINITE_DURATION_TYPES: frozenset[str] = frozenset(
    {"heatwave", "fuel_price_shock", "demand_surprise", "plant_failure"}
)


def _has_active(state: WorldState, event_type: str) -> bool:
    return any(e.get("type") == event_type for e in state.active_events)


def fuel_price_shock_active(state: WorldState) -> bool:
    """True iff a fuel_price_shock is currently in `active_events`."""
    return _has_active(state, "fuel_price_shock")


def fuel_price_shock_multiplier(state: WorldState) -> float:
    """Return 2.0 when a fuel_price_shock is active, else 1.0."""
    return FUEL_PRICE_SHOCK_MULT if fuel_price_shock_active(state) else 1.0


def expire_finite_events(world: World) -> None:
    """Move events whose `ends_day` <= today from `active_events` to
    `historical_events`. Plant failures restore the affected plant's
    `operational` flag if the plant still exists.

    Should be called at the start of each day BEFORE
    `sample_and_apply_events`, so a brand-new event that happens to fire
    today doesn't get its 1-day-window stomped.
    """
    today = world.state.day
    still_active: list[dict[str, Any]] = []
    for e in world.state.active_events:
        ends = e.get("ends_day")
        if ends is not None and ends <= today:
            if e.get("type") == "plant_failure":
                pid = e.get("plant_id")
                if pid is not None:
                    for t in world.state.tiles:
                        if t.id == pid and t.type in FOSSIL_PLANT_TYPES:
                            t.operational = True
                            break
            world.state.historical_events.append(e)
        else:
            still_active.append(e)
    world.state.active_events = still_active


def sample_and_apply_events(world: World) -> None:
    """Roll today's events from `world.event_rng` and apply effects.

    Mutates: `state.active_events`, `state.historical_events`,
    `state.carbon_price`, `state.regulatory_tightenings_applied`, and
    plant `operational` flags. Order of draws is fixed (see module
    docstring) so replays are byte-stable.
    """
    rng: np.random.Generator = world.event_rng
    today = world.state.day
    state = world.state

    # 1. Heatwave (residential ×1.4 for 5 days).
    if not _has_active(state, "heatwave") and float(rng.random()) < HEATWAVE_PROB:
        state.active_events.append(
            {
                "type": "heatwave",
                "started_day": today,
                "ends_day": today + HEATWAVE_DURATION,
                "severity": HEATWAVE_RESIDENTIAL_MULT,
            }
        )

    # 2. Fuel price shock (gas + coal ×2 for 30 days).
    if not _has_active(state, "fuel_price_shock") and float(rng.random()) < FUEL_PRICE_SHOCK_PROB:
        state.active_events.append(
            {
                "type": "fuel_price_shock",
                "started_day": today,
                "ends_day": today + FUEL_PRICE_SHOCK_DURATION,
                "severity": FUEL_PRICE_SHOCK_MULT,
            }
        )

    # 3. Demand surprise (I+C ×1.3 for 10 days).
    if not _has_active(state, "demand_surprise") and float(rng.random()) < DEMAND_SURPRISE_PROB:
        state.active_events.append(
            {
                "type": "demand_surprise",
                "started_day": today,
                "ends_day": today + DEMAND_SURPRISE_DURATION,
                "severity": DEMAND_SURPRISE_IC_MULT,
            }
        )

    # 4. Regulatory tightening (carbon_price × 1.5, permanent, capped at 3).
    if (
        state.regulatory_tightenings_applied < REGULATORY_TIGHTENING_MAX_OCCURRENCES
        and float(rng.random()) < REGULATORY_TIGHTENING_PROB
    ):
        state.carbon_price *= REGULATORY_TIGHTENING_MULT
        state.regulatory_tightenings_applied += 1
        state.historical_events.append(
            {
                "type": "regulatory_tightening",
                "started_day": today,
                "ends_day": today,
                "severity": state.carbon_price,
                "occurrences_after": state.regulatory_tightenings_applied,
            }
        )

    # 5. Plant failure rolls — one per fossil plant in id-ascending order.
    fossil_plants = sorted(
        (t for t in state.tiles if t.type in FOSSIL_PLANT_TYPES),
        key=lambda t: t.id,
    )
    for plant in fossil_plants:
        if float(rng.random()) < PLANT_FAILURE_PROB_PER_PLANT:
            # Always consume the duration draw so the RNG sequence is stable
            # regardless of whether this plant is already in a failed state.
            duration = int(
                rng.integers(
                    PLANT_FAILURE_DURATION_MIN,
                    PLANT_FAILURE_DURATION_MAX + 1,
                )
            )
            already_failed = any(
                e.get("type") == "plant_failure" and e.get("plant_id") == plant.id
                for e in state.active_events
            )
            if already_failed:
                continue
            plant.operational = False
            state.active_events.append(
                {
                    "type": "plant_failure",
                    "plant_id": plant.id,
                    "started_day": today,
                    "ends_day": today + duration,
                    "severity": 1.0,
                }
            )

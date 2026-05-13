"""Population dynamics + daily tax revenue.

A single end-of-day routine, `update_population(world)`, that:

  1. Sums housing capacity and jobs from the current tile set.
  2. Computes happiness from a spatial park benefit (averaged over
     houses), a noise penalty from industrial/refinery tiles near
     houses (halved by an intervening park), prior-day blackout +
     brownout hours, and a coal-proximity term.
  3. Calls `happiness_velocity` (signed daily delta around the
     neutral happiness fixed-point of 1.0) and `apply_structural_clamps`
     (housing exodus + jobs floor backstops) as pure helpers.
  4. Persists the new float population, triggers workforce churn on
     integer transitions, accrues `DAILY_TAX_PER_CAPITA × int(pop)` to
     the treasury.

Population is stored as a float on `WorldState` so fractional deltas
accumulate across days; the `/state` and `/step` API serializers cast
to int on the way out. No RNG is consumed here; the determinism
contract in `sim._advance_one_day` is unaffected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from world import workforce

if TYPE_CHECKING:
    from world.sim import World

DAILY_TAX_PER_CAPITA: float = 4.0

# Per-hour outage penalties feeding the happiness term.
BLACKOUT_HAPPINESS_PER_HOUR: float = 0.05
BROWNOUT_HAPPINESS_PER_HOUR: float = 0.02

# Velocity model anchors (PRD §"Implementation Decisions").
HAPPINESS_NEUTRAL: float = 1.0


def happiness_velocity(
    pop: float,
    happiness: float,
    capacity: int,
    jobs: int,
    b: float = 0.012,
    h_neutral: float = HAPPINESS_NEUTRAL,
) -> float:
    """Signed daily population delta from the happiness-velocity model.

    `delta = b * pop * (happiness - h_neutral)`; positive deltas are
    clamped by available housing and jobs headroom, negative deltas
    are not clamped by structural state (an unhappy city sheds people
    regardless of how much housing or how many jobs it has).
    """
    raw = b * pop * (happiness - h_neutral)
    if raw <= 0:
        return raw
    cap_headroom = max(0.0, capacity - pop)
    jobs_headroom = max(0.0, jobs - pop)
    return min(raw, cap_headroom, jobs_headroom)


def apply_structural_clamps(pop: float, capacity: int, jobs: int) -> float:
    """Gradual housing exodus and jobs floor backstops.

    Both clamps fire in sequence. Housing exodus caps the post-velocity
    population at `max(capacity, pop - 5)` when `pop > capacity`. Jobs
    floor caps at `max(jobs / 0.7, pop * 0.99)` when `jobs < 0.7 * pop`.
    """
    if pop > capacity:
        pop = max(float(capacity), pop - 5.0)
    if jobs < 0.7 * pop:
        pop = max(jobs / 0.7, pop * 0.99)
    return pop


def update_population(world: World) -> None:
    state = world.state
    config = world.config

    capacity = sum(t.housing_capacity for t in state.tiles)
    jobs = sum(t.jobs for t in state.tiles)
    parks = [t for t in state.tiles if t.type == "park"]
    houses = [t for t in state.tiles if t.type == "house"]
    house_count = len(houses)
    noise_sources = [t for t in state.tiles if t.type in ("industrial", "refinery")]

    coal_plants = [t for t in state.tiles if t.type == "coal_plant" and t.operational]
    coal_houses_within_3 = sum(
        1 for h in houses if any(max(abs(h.x - c.x), abs(h.y - c.y)) <= 3 for c in coal_plants)
    )

    park_benefit = 0.0
    noise_penalty = 0.0
    if house_count > 0:
        bonus_total = 0.0
        penalty_total = 0.0
        for h in houses:
            nearby_parks = [p for p in parks if max(abs(h.x - p.x), abs(h.y - p.y)) <= 2]
            bonus_total += min(0.30, 0.10 * len(nearby_parks))
            for src in noise_sources:
                if max(abs(h.x - src.x), abs(h.y - src.y)) > 2:
                    continue
                shielded = any(
                    max(abs(h.x - p.x), abs(h.y - p.y)) <= 2
                    and max(abs(src.x - p.x), abs(src.y - p.y)) <= 2
                    for p in parks
                )
                penalty_total += 0.015 if shielded else 0.03
        park_benefit = bonus_total / house_count
        noise_penalty = penalty_total / house_count

    happiness = 1.0
    happiness += park_benefit
    happiness -= noise_penalty
    happiness -= BLACKOUT_HAPPINESS_PER_HOUR * state.yesterday_blackout_hours
    happiness -= BROWNOUT_HAPPINESS_PER_HOUR * state.yesterday_brownout_hours
    happiness -= 0.05 * coal_houses_within_3 / max(1, house_count)
    happiness = max(0.0, min(1.5, happiness))

    pop_before = float(state.population)
    pop_before_int = int(pop_before)

    delta = happiness_velocity(pop_before, happiness, capacity, jobs, b=config.base_growth_rate)
    pop_after = pop_before + delta
    pop_after = apply_structural_clamps(pop_after, capacity, jobs)
    pop_after = max(0.0, pop_after)

    pop_after_int = int(pop_after)
    delta_int = pop_after_int - pop_before_int

    # Workforce hooks fire only on integer transitions. drain_n decrements
    # state.population internally; reset to the int-valued float first so
    # the silent/firing accounting reads cleanly, then overwrite with the
    # precise float so fractional residue accumulates day-to-day.
    if delta_int < 0:
        state.population = float(pop_before_int)
        workforce.drain_n(state, -delta_int)
    state.population = pop_after
    if delta_int > 0:
        workforce.hire_to_fill(state)

    state.happiness = happiness

    tax = DAILY_TAX_PER_CAPITA * int(state.population)
    state.treasury += tax
    state.today_summary_so_far["tax_revenue"] = tax

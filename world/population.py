"""Population dynamics + daily tax revenue.

A single end-of-day routine, `update_population(world)`, that:

  1. Sums housing capacity and jobs from the current tile set.
  2. Computes happiness from a spatial park benefit (averaged over
     residential tiles — both `town_hall` and `house` count), a noise
     penalty from industrial/refinery tiles near those residences
     (halved by an intervening park), prior-day blackout + brownout
     hours, a coal-proximity term (residences within cheb-5 of an
     operational coal plant), a coal-share-of-generation term (today's
     coal kWh as a fraction of total served), a mild unemployment
     drag, a flat no-parks penalty, and a flat negative-treasury
     penalty.
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

# Flat daily happiness penalty when the day closes with treasury < 0.
# Does not scale with depth or duration; disappears the day treasury
# returns non-negative. Sized at 0.20 — larger than a single park's
# +0.10 contribution and capable of beating the max park benefit
# (+0.30/house) down to a near-neutral +0.10 net, so debt cannot be
# silently cancelled by minimal park placement.
NEGATIVE_TREASURY_HAPPINESS_PENALTY: float = 0.20

# Mild happiness drag scaling with unemployment rate `(pop - jobs)/pop`.
# Caps at the coefficient when there are zero jobs; pulls happiness
# below the neutral fixed-point even before the idle out-migration
# clamp fires, so a jobs deficit shows up in the velocity model.
UNEMPLOYMENT_HAPPINESS_COEF: float = 0.15

# Flat penalty when the city has zero parks anywhere on the map. A
# minimum civic-services prod that breaks the "do nothing" equilibrium
# without changing the velocity neutral point. Placing a single park
# (anywhere) clears it.
NO_PARKS_HAPPINESS_PENALTY: float = 0.05

# Per-day coefficient on the coal share of today's served energy. At
# 100% coal generation this contributes -0.05; combined with the spatial
# coal-proximity term (also up to -0.05) the worst case is ~-0.10.
COAL_GENERATION_HAPPINESS_COEF: float = 0.05

# Chebyshev radius for the spatial coal-proximity term. Widened from 3
# so the starter coal_plant (which sits ~8 tiles from town_hall on the
# default grid) actually fires on at least nearby residences as the
# city grows past the town hall.
COAL_PROXIMITY_RADIUS: int = 5

# Velocity model anchors (PRD §"Implementation Decisions").
HAPPINESS_NEUTRAL: float = 1.0


def happiness_velocity(
    pop: float,
    happiness: float,
    capacity: int,
    jobs: int,
    b: float = 0.025,
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
    """Housing exodus and idle out-migration backstops.

    Both clamps fire in sequence. Housing exodus caps the post-velocity
    population at `max(capacity, pop - 5)` when `pop > capacity`. Idle
    out-migration shrinks pop by 0.3%/day toward `jobs` when `pop > jobs`:
    people leave when there is no work for them, even if housing is
    available. Equilibrium is `pop == jobs` (zero idle); the production
    starter (pop=100, jobs=60) settles at pop=60 over ~170 days. The
    rate is deliberately slow so that happiness-driven in-migration
    (`happiness_velocity` with b=0.025) can outpace it at any happiness
    above ~1.12.
    """
    if pop > capacity:
        pop = max(float(capacity), pop - 5.0)
    if pop > jobs:
        pop = max(float(jobs), pop * 0.997)
    return pop


def update_population(world: World) -> None:
    state = world.state
    config = world.config

    capacity = sum(t.housing_capacity for t in state.tiles)
    jobs = workforce.total_jobs(state)
    parks = [t for t in state.tiles if t.type == "park"]
    # The town_hall holds the starter pop before any dedicated house exists.
    # The previous formula iterated only over `house` tiles, which left
    # early-game happiness pinned at 1.0 regardless of parks built (no
    # houses → no spatial bonus). Both residential tile types now contribute.
    residences = [t for t in state.tiles if t.type in ("house", "town_hall")]
    residence_count = len(residences)
    noise_sources = [t for t in state.tiles if t.type in ("industrial", "refinery")]

    coal_plants = [t for t in state.tiles if t.type == "coal_plant" and t.operational]
    coal_residences_in_range = sum(
        1
        for h in residences
        if any(max(abs(h.x - c.x), abs(h.y - c.y)) <= COAL_PROXIMITY_RADIUS for c in coal_plants)
    )

    park_benefit = 0.0
    noise_penalty = 0.0
    if residence_count > 0:
        bonus_total = 0.0
        penalty_total = 0.0
        for h in residences:
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
        park_benefit = bonus_total / residence_count
        noise_penalty = penalty_total / residence_count

    pop_int = int(state.population)
    unemployment_rate = max(0.0, (pop_int - jobs)) / pop_int if pop_int > 0 else 0.0

    # `today.coal_kwh` is the day's coal generation; total served kWh is
    # summed across plant tiles. Both are valid at this point in the
    # day-loop (after pin_yesterday, before the day-counter increment).
    total_kwh_today = sum(t.kwh_served_today for t in state.tiles)
    coal_share_today = state.today.coal_kwh / total_kwh_today if total_kwh_today > 0 else 0.0

    happiness = 1.0
    happiness += park_benefit
    happiness -= noise_penalty
    happiness -= BLACKOUT_HAPPINESS_PER_HOUR * state.yesterday_blackout_hours
    happiness -= BROWNOUT_HAPPINESS_PER_HOUR * state.yesterday_brownout_hours
    happiness -= 0.05 * coal_residences_in_range / max(1, residence_count)
    happiness -= COAL_GENERATION_HAPPINESS_COEF * coal_share_today
    happiness -= UNEMPLOYMENT_HAPPINESS_COEF * unemployment_rate
    if not parks:
        happiness -= NO_PARKS_HAPPINESS_PENALTY
    if state.treasury < 0:
        happiness -= NEGATIVE_TREASURY_HAPPINESS_PENALTY
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

    tax = state.daily_tax_per_capita * int(state.population)
    state.treasury += tax
    state.today.tax_revenue = tax

"""Population dynamics and daily tax revenue (slice 03).

Each cascading branch of `update_population` is exercised in isolation by
manipulating tiles/population/blackout-hours directly, so the test asserts
on the algebra from §4.8 of the brief rather than going end-to-end through
multiple build calls.
"""

from __future__ import annotations

import pytest

from world.population import DAILY_TAX_PER_CAPITA, update_population
from world.sim import World
from world.state import Tile


def _fresh_world() -> World:
    w = World()
    w.reset(seed=42)
    return w


def _inject_tile(
    w: World,
    *,
    type: str,
    x: int,
    y: int,
    jobs: int = 0,
    housing_capacity: int = 0,
    staffed_jobs: int | None = None,
    built_day: int = 0,
    operational: bool = True,
) -> None:
    """Bypass /build's adjacency/funds checks to set up arbitrary aggregates.

    Workforce slice 01: defaults ``staffed_jobs`` to ``jobs`` so injected
    producer tiles look fully staffed. Tests that want a partially-staffed
    tile pass ``staffed_jobs=N`` explicitly.
    """
    w.state.tiles.append(
        Tile(
            id=f"injected-{x}-{y}",
            type=type,
            x=x,
            y=y,
            built_day=built_day,
            operational=operational,
            jobs=jobs,
            housing_capacity=housing_capacity,
            staffed_jobs=jobs if staffed_jobs is None else staffed_jobs,
        )
    )


# -- Branch 1: growth --------------------------------------------------------


def test_grow_branch_applies_base_rate_capped_by_headroom():
    """jobs >= pop AND capacity > pop AND happiness >= 0.5 → grow."""
    w = _fresh_world()
    # Town hall already gives capacity=100, jobs=30. Inject a synthetic block
    # with abundant headroom so the cap on growth is the base rate, not
    # capacity/jobs headroom.
    _inject_tile(w, type="commercial", x=5, y=5, jobs=1000, housing_capacity=1000)
    w.state.population = 500

    update_population(w)

    # happiness = 1.0 (no parks, no blackouts, no coal).
    # growth = min(0.012 * 500 * 1.0 = 6.0, cap-pop=600, jobs-pop=530) = 6.0.
    assert w.state.population == 506
    assert w.state.happiness == pytest.approx(1.0)


def test_grow_branch_capped_by_jobs_headroom():
    w = _fresh_world()
    # capacity = 100 + 1000 = 1100; jobs = 30 + (1000) tied at +1 above pop.
    _inject_tile(w, type="commercial", x=5, y=5, jobs=71, housing_capacity=1000)
    # pop=100, jobs=101. jobs - pop = 1, far below 0.012*100=1.2.
    w.state.population = 100

    update_population(w)
    # growth = min(1.2, 1100-100=1000, 101-100=1) = 1. New pop = 101.
    assert w.state.population == 101


# -- Branch 2: housing exodus ------------------------------------------------


def test_exodus_when_capacity_drops_below_pop():
    """capacity < pop → pop = max(capacity, pop - 5)."""
    w = _fresh_world()
    # Town hall capacity=100. Set pop=110 (above capacity).
    w.state.population = 110

    update_population(w)
    # max(100, 110 - 5) = 105.
    assert w.state.population == 105


def test_exodus_floors_at_capacity():
    w = _fresh_world()
    # Capacity = 100. Pop just above; pop - 5 < capacity.
    w.state.population = 102

    update_population(w)
    # max(100, 102 - 5 = 97) = 100.
    assert w.state.population == 100


# -- Branch 3: job-driven decline --------------------------------------------


def test_job_decline_one_day_from_fresh_world():
    """Fresh world: pop=100, jobs=30. jobs < 0.7*pop=70 → pop=max(42.857, 99.0)=99."""
    w = _fresh_world()
    update_population(w)
    assert w.state.population == 99
    assert w.state.happiness == pytest.approx(1.0)


def test_job_decline_70_days_approaches_equilibrium():
    """After 70 simulated days, pop floors at jobs/0.7 ≈ 42-43.

    A gas peaker is force-placed so the fresh world isn't blacking out
    every hour — without it, the issue-22 happiness-decline branch
    cascades pop past the job-floor down toward 0."""
    w = _fresh_world()
    _inject_tile(w, type="gas_peaker", x=0, y=0)
    w.state.tiles[-1].current_output_kw = 0.0
    # Set the catalog-driven capacity field so dispatch sees a usable plant.
    w.state.tiles[-1].opex_per_day = 150.0
    # Mark capacity_kw via the tile-spec is implicit (catalog read on dispatch);
    # the gas peaker spec already provides 500 kW @ 50%/h ramp.
    w.step(days=7)
    for _ in range(9):
        w.step(days=7)
    # int truncation lands the equilibrium at floor(30/0.7) = 42.
    assert 40 <= w.state.population <= 45


# -- Branch 4: happiness decline ---------------------------------------------


def test_happiness_decline_when_below_threshold():
    """jobs >= pop AND cap > pop but happiness < 0.5 → pop *= 0.99."""
    w = _fresh_world()
    # Inject abundant capacity+jobs so the first three branches are skipped.
    _inject_tile(w, type="commercial", x=5, y=5, jobs=1000, housing_capacity=1000)
    w.state.population = 100
    # Per-hour blackout coefficient is BLACKOUT_HAPPINESS_PER_HOUR = 0.05.
    # 24h+ of blackouts saturates happiness at 0.0 (after [0, 1.5] clip).
    w.state.yesterday_blackout_hours = 200.0

    update_population(w)
    # happiness ≈ max(0, 1 - 0.05 * 200) = max(0, -9) = 0.
    assert w.state.happiness < 0.5
    # pop = 100 * 0.99 = 99.
    assert w.state.population == 99


def test_full_day_blackout_drops_happiness_below_threshold():
    """24h of blackout in a single day pins happiness at 0 (clipped)."""
    w = _fresh_world()
    _inject_tile(w, type="commercial", x=5, y=5, jobs=1000, housing_capacity=1000)
    w.state.population = 100
    w.state.yesterday_blackout_hours = 24.0

    update_population(w)
    # 1.0 - 0.05 * 24 = -0.20 → clipped to 0.0.
    assert w.state.happiness == pytest.approx(0.0)
    # 100 * 0.99 = 99 → int → 99.
    assert w.state.population == 99


def test_eleven_hour_blackout_crosses_decline_threshold():
    """The threshold is exactly `< 0.5`; with coef 0.05/h, 10h leaves happiness
    at 0.5 (no decline) and 11h drops it below 0.5 (decline fires)."""
    w = _fresh_world()
    _inject_tile(w, type="commercial", x=5, y=5, jobs=1000, housing_capacity=1000)
    w.state.population = 100
    w.state.yesterday_blackout_hours = 10.0

    update_population(w)
    # 1.0 - 0.5 = 0.5 → not less-than-0.5 → no decline branch.
    assert w.state.happiness == pytest.approx(0.5)
    # First three branches are also satisfied for growth; check that
    # neither growth nor decline overshoots: pop should be unchanged or
    # grow modestly (jobs-pop=900, capacity-pop=900).
    assert w.state.population >= 100

    # Re-run with 11h: should decline.
    w2 = _fresh_world()
    _inject_tile(w2, type="commercial", x=5, y=5, jobs=1000, housing_capacity=1000)
    w2.state.population = 100
    w2.state.yesterday_blackout_hours = 11.0
    update_population(w2)
    assert w2.state.happiness < 0.5
    assert w2.state.population == 99


def test_brownout_hours_also_dent_happiness():
    """Brownout coefficient is lighter than blackout (0.02/h) but still
    accumulates: 24h brownout drops happiness by 0.48."""
    w = _fresh_world()
    _inject_tile(w, type="commercial", x=5, y=5, jobs=1000, housing_capacity=1000)
    w.state.population = 100
    w.state.yesterday_brownout_hours = 24.0

    update_population(w)
    # 1.0 - 0.02 * 24 = 0.52. Still ≥ 0.5 so no decline; verifies the term.
    assert w.state.happiness == pytest.approx(0.52)


def test_zero_blackout_no_pop_decline():
    """No blackout, jobs/capacity sufficient → pop grows; happiness stays at 1.0."""
    w = _fresh_world()
    _inject_tile(w, type="commercial", x=5, y=5, jobs=1000, housing_capacity=1000)
    w.state.population = 100
    # Default: yesterday_blackout_hours = yesterday_brownout_hours = 0.

    update_population(w)
    assert w.state.happiness == pytest.approx(1.0)
    assert w.state.population > 100  # growth branch fires


# -- Tax revenue -------------------------------------------------------------


def test_tax_revenue_accrues_to_treasury_and_summary():
    """Tax = $4 × end-of-day population, accrued to treasury + summary.

    Calls update_population directly so the assertion stays focused on the
    population module's contract (slice 03). Going through step would now
    mix in dispatch-driven blackout penalties from slice 05.
    """
    w = _fresh_world()
    treasury_before = w.state.treasury
    update_population(w)
    # pop went 100 → 99 (job-decline branch).
    assert w.state.population == 99
    assert w.state.today_summary_so_far["tax_revenue"] == pytest.approx(99 * 4.0)
    assert w.state.treasury == pytest.approx(treasury_before + 99 * 4.0)


def test_tax_revenue_constant_per_capita():
    """DAILY_TAX_PER_CAPITA is the brief's named constant ($4)."""
    assert DAILY_TAX_PER_CAPITA == 4.0


# -- Happiness composition ---------------------------------------------------


def test_park_count_bonus_kicks_in_after_first_park():
    """Happiness gains 0.05 per park beyond the first."""
    w = _fresh_world()
    _inject_tile(w, type="park", x=1, y=1)
    _inject_tile(w, type="park", x=2, y=2)
    _inject_tile(w, type="park", x=3, y=3)

    update_population(w)
    # park_count=3 → bonus = 0.05 * (3-1) = 0.10. happiness = 1.10.
    assert w.state.happiness == pytest.approx(1.10)


def test_happiness_clipped_above_at_1_5():
    w = _fresh_world()
    # Stuff in 50 parks → bonus = 0.05*49 = 2.45 → clipped at 1.5.
    for i in range(50):
        _inject_tile(w, type="park", x=i, y=0)

    update_population(w)
    assert w.state.happiness == pytest.approx(1.5)


def test_happiness_clipped_below_at_0_0():
    w = _fresh_world()
    # Crank blackout hours absurdly high; happiness would go very negative.
    w.state.yesterday_blackout_hours = 10_000.0

    update_population(w)
    assert w.state.happiness == pytest.approx(0.0)


# -- State surface -----------------------------------------------------------


def test_state_dict_exposes_population_and_happiness():
    w = _fresh_world()
    s = w.state_dict()
    assert "population" in s
    assert "happiness" in s
    assert s["population"] == 100
    assert s["happiness"] == pytest.approx(1.0)


def test_sustained_blackout_declines_population_through_step():
    """Integration: a world with insufficient generation runs daily blackouts;
    pop bleeds via the happiness branch within a week. This is the bug from
    issue 22 — without the fix, pop is invariant under continuous blackouts."""
    w = World()
    w.reset(seed=42)
    # Inject abundant capacity + jobs (so pop doesn't decline via housing or
    # job branches), and a high baseline pop. NO power plants → every hour
    # is a blackout.
    w.state.tiles.append(
        Tile(
            id="injected-jobs",
            type="commercial",
            x=5,
            y=5,
            built_day=0,
            operational=True,
            jobs=1000,
            housing_capacity=1000,
        )
    )
    w.state.population = 200
    pop_start = w.state.population

    w.step(days=7)

    # 24 blackout hours/day × 7 days. Happiness pinned at 0 → decline branch.
    assert w.state.population < pop_start
    assert w.state.happiness < 0.5


def test_step_size_invariance_with_population_dynamics():
    """Slice-01 determinism contract holds with population update wired in."""
    a = World()
    a.reset(seed=42)
    a.step(days=7)

    b = World()
    b.reset(seed=42)
    for _ in range(7):
        b.step(days=1)

    assert a.state.treasury == b.state.treasury
    assert a.state.population == b.state.population
    assert a.state.happiness == b.state.happiness
    assert a.sim_rng.standard_normal() == b.sim_rng.standard_normal()


# -- Workforce wiring (slice 02) --------------------------------------------


def _find_tile(w: World, type: str) -> Tile:
    for t in w.state.tiles:
        if t.type == type:
            return t
    raise AssertionError(f"no {type} tile in world")


def test_growth_branch_hires_into_open_vacancies_oldest_first():
    """Growth branch → ``hire_to_fill`` auto-fills the unemployed pool."""
    w = _fresh_world()
    # Town hall (day 0) is already 30/30. Older industrial fully staffed,
    # younger industrial with 30 open vacancies. Plenty of capacity + jobs.
    _inject_tile(w, type="industrial", x=2, y=2, jobs=30, built_day=1)
    _inject_tile(w, type="industrial", x=3, y=3, jobs=30, staffed_jobs=0, built_day=2)
    _inject_tile(w, type="house", x=4, y=4, housing_capacity=200)
    w.state.population = 84  # employed = 30+30 = 60, unemployed = 24

    update_population(w)

    # growth = 0.012 * 84 * 1.0 = 1.008 → +1. New pop = 85.
    assert w.state.population == 85
    # Unemployed post-growth = 85 - 60 = 25. The younger industrial (day 2)
    # absorbs all 25 since the older industrial (day 1) is already full.
    older = w.state.tiles[1]
    younger = w.state.tiles[2]
    assert older.staffed_jobs == 30  # day 1, untouched
    assert younger.staffed_jobs == 25  # day 2, filled oldest-first


def test_exodus_branch_fires_newest_when_unemployed_is_zero():
    """capacity < pop → drain via ``drain_n``; with unemployed=0 the newest
    producer loses staff."""
    w = _fresh_world()
    # Shrink town hall housing so capacity drops below pop.
    town_hall = _find_tile(w, "town_hall")
    town_hall.housing_capacity = 50
    _inject_tile(w, type="industrial", x=2, y=2, jobs=30, built_day=1)
    w.state.population = 60  # employed = 30 + 30 = 60, unemployed = 0

    update_population(w)

    # max(50, 60-5) = 55 → delta = 5. All 5 fire newest-first = industrial.
    assert w.state.population == 55
    industrial = w.state.tiles[1]
    assert industrial.staffed_jobs == 25
    assert town_hall.staffed_jobs == 30


def test_exodus_branch_drains_unemployed_first_when_buffer_exists():
    """capacity < pop with unemployed buffer → no producer fires."""
    w = _fresh_world()
    town_hall = _find_tile(w, "town_hall")
    town_hall.housing_capacity = 50
    _inject_tile(w, type="industrial", x=2, y=2, jobs=30, built_day=1)
    _inject_tile(w, type="house", x=4, y=4, housing_capacity=20)
    # employed = 30 + 30 = 60, unemployed = 20, capacity = 50+20 = 70
    w.state.population = 80

    update_population(w)

    # max(70, 80-5) = 75 → delta = 5. All 5 from unemployed; staffing untouched.
    assert w.state.population == 75
    assert town_hall.staffed_jobs == 30
    assert w.state.tiles[1].staffed_jobs == 30


def test_job_decline_branch_drains_unemployed_silently():
    """jobs < 0.7 × pop → drain comes from the unemployed pool."""
    w = _fresh_world()
    # Only the town hall (jobs=30). pop=100. unemployed=70.
    w.state.population = 100

    update_population(w)

    # max(30/0.7=42.86, 99) = 99 → delta = 1. Drained from unemployed.
    assert w.state.population == 99
    town_hall = _find_tile(w, "town_hall")
    assert town_hall.staffed_jobs == 30


def test_happiness_decline_branch_fires_newest_when_unemployed_zero():
    """happiness < 0.5 → drain via ``drain_n``; newest producer loses staff."""
    w = _fresh_world()
    _inject_tile(w, type="industrial", x=2, y=2, jobs=30, built_day=1)
    w.state.population = 60  # employed = 60, unemployed = 0
    # 12h blackout → happiness = 1 - 0.05*12 = 0.4 < 0.5
    w.state.yesterday_blackout_hours = 12.0

    update_population(w)

    assert w.state.happiness < 0.5
    # 60 * 0.99 = 59.4 → 59. delta=1. Newest = industrial.
    assert w.state.population == 59
    industrial = w.state.tiles[1]
    town_hall = _find_tile(w, "town_hall")
    assert industrial.staffed_jobs == 29
    assert town_hall.staffed_jobs == 30


def test_drain_fires_newest_producer_first_with_multiple_young_producers():
    """Fire order respects ``(creation_day, id_string)`` ascending, drained
    in reverse — the youngest producer loses staff first."""
    w = _fresh_world()
    town_hall = _find_tile(w, "town_hall")
    town_hall.housing_capacity = 50  # force exodus
    _inject_tile(w, type="coal_plant", x=2, y=2, jobs=8, built_day=5)
    _inject_tile(w, type="industrial", x=3, y=3, jobs=30, built_day=10)
    _inject_tile(w, type="refinery", x=4, y=4, jobs=25, staffed_jobs=22, built_day=15)
    # employed = 30+8+30+22 = 90, unemployed = 0
    w.state.population = 90

    update_population(w)

    # max(50, 85) = 85 → delta = 5. All 5 come from refinery (newest, day 15).
    assert w.state.population == 85
    refinery = w.state.tiles[3]
    industrial = w.state.tiles[2]
    coal_plant = w.state.tiles[1]
    assert refinery.staffed_jobs == 17
    assert industrial.staffed_jobs == 30  # untouched
    assert coal_plant.staffed_jobs == 8  # untouched
    assert town_hall.staffed_jobs == 30  # untouched


def test_mixed_drain_drains_unemployed_then_fires_newest():
    """Drain order: unemployed pool first, then newest producer."""
    w = _fresh_world()
    town_hall = _find_tile(w, "town_hall")
    town_hall.housing_capacity = 50  # force exodus
    _inject_tile(w, type="industrial", x=2, y=2, jobs=30, built_day=1)
    # employed = 30+30 = 60. population = 65 → unemployed = 5.
    w.state.population = 65

    update_population(w)

    # max(50, 60) = 60 → delta = 5. Take all 5 from unemployed; staffing intact.
    assert w.state.population == 60
    assert w.state.tiles[1].staffed_jobs == 30
    assert town_hall.staffed_jobs == 30

    # Run again: cap still 50, pop=60 → max(50, 55) = 55 → delta=5.
    # Unemployed = 60-60 = 0, so all 5 fire from industrial (newest).
    update_population(w)
    assert w.state.population == 55
    assert w.state.tiles[1].staffed_jobs == 25
    assert town_hall.staffed_jobs == 30


def test_tax_base_uses_post_drain_population_not_employed():
    """Tax = $4 × state.population (post-drain), not $4 × employed."""
    w = _fresh_world()
    town_hall = _find_tile(w, "town_hall")
    town_hall.housing_capacity = 50  # force exodus
    _inject_tile(w, type="industrial", x=2, y=2, jobs=30, built_day=1)
    w.state.population = 100  # employed=60, unemployed=40
    treasury_before = w.state.treasury

    update_population(w)

    # max(50, 95) = 95 → delta=5. Drained from unemployed.
    assert w.state.population == 95
    # Tax = $4 × 95 = $380; NOT $4 × 60 = $240.
    assert w.state.today_summary_so_far["tax_revenue"] == pytest.approx(380.0)
    assert w.state.treasury == pytest.approx(treasury_before + 380.0)


def test_failed_plant_still_drained_by_workforce():
    """Non-operational plants stay in ``producers`` — they can lose workers."""
    w = _fresh_world()
    _inject_tile(w, type="coal_plant", x=2, y=2, jobs=8, built_day=1, operational=False)
    # employed = 30+8 = 38, unemployed = 0
    w.state.population = 38
    w.state.yesterday_blackout_hours = 12.0  # happiness 0.4 < 0.5

    update_population(w)

    # 38 * 0.99 = 37.62 → 37. delta=1.
    # Newest producer is the failed coal plant; it loses 1 worker even
    # though operational=False.
    assert w.state.population == 37
    coal_plant = w.state.tiles[1]
    assert coal_plant.staffed_jobs == 7
    assert coal_plant.operational is False  # unchanged
    town_hall = _find_tile(w, "town_hall")
    assert town_hall.staffed_jobs == 30

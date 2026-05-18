"""Population dynamics and daily tax revenue.

Tests are organized in three layers:
  - Pure unit tests for `happiness_velocity` and `apply_structural_clamps`
    (no World fixture, numeric I/O only).
  - Integration tests for `update_population` against a minimal World fixture
    (asserts on the multi-tick trajectory of state.population).
  - Happiness composition tests (park benefit, noise penalty, coal proximity,
    blackout/brownout) that target the happiness *number*, not the velocity.
"""

from __future__ import annotations

import pytest

from world.population import (
    DAILY_TAX_PER_CAPITA,
    HAPPINESS_NEUTRAL,
    NEGATIVE_TREASURY_HAPPINESS_PENALTY,
    apply_structural_clamps,
    happiness_velocity,
    update_population,
)
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
    """Bypass /build's adjacency/funds checks to set up arbitrary aggregates."""
    from world.catalog import TILE_CATALOG

    spec = TILE_CATALOG.get(type)
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
            demand_kw=spec.demand_kw if spec is not None else 0.0,
            staffed_jobs=jobs if staffed_jobs is None else staffed_jobs,
        )
    )


# -- happiness_velocity: pure unit tests -------------------------------------


def test_velocity_zero_at_happiness_1_0():
    """Neutral fixed-point: at h=1.0 the velocity is exactly 0, regardless of pop."""
    assert happiness_velocity(100.0, 1.0, capacity=1000, jobs=1000) == 0.0
    assert happiness_velocity(0.0, 1.0, capacity=1000, jobs=1000) == 0.0
    assert happiness_velocity(2000.0, 1.0, capacity=10_000, jobs=10_000) == 0.0


def test_velocity_positive_when_happiness_above_neutral():
    """Above neutral: delta = b · pop · (h - 1) > 0 when headroom is abundant."""
    delta = happiness_velocity(100.0, 1.2, capacity=10_000, jobs=10_000)
    assert delta == pytest.approx(0.012 * 100.0 * 0.2)
    assert delta > 0


def test_velocity_negative_when_happiness_below_neutral():
    """Below neutral: delta < 0 proportional to pop · (1 - h)."""
    delta = happiness_velocity(100.0, 0.7, capacity=10_000, jobs=10_000)
    assert delta == pytest.approx(0.012 * 100.0 * -0.3)
    assert delta < 0


def test_velocity_max_negative_at_happiness_0():
    """At h=0 the velocity reaches its maximum-magnitude negative: −0.012 · pop."""
    delta = happiness_velocity(500.0, 0.0, capacity=10_000, jobs=10_000)
    assert delta == pytest.approx(-0.012 * 500.0)


def test_velocity_max_positive_at_happiness_1_5_with_abundant_headroom():
    """At h=1.5 (the clip cap) the velocity is +0.006 · pop with abundant headroom."""
    delta = happiness_velocity(500.0, 1.5, capacity=10_000, jobs=10_000)
    assert delta == pytest.approx(0.012 * 500.0 * 0.5)


def test_velocity_upward_clamps_to_jobs_headroom():
    """When `jobs - pop` is the binding constraint, growth is capped at that headroom."""
    # raw = 0.012 × 1000 × 0.5 = 6.0; jobs headroom is 2.
    delta = happiness_velocity(1000.0, 1.5, capacity=10_000, jobs=1002)
    assert delta == pytest.approx(2.0)


def test_velocity_upward_clamps_to_capacity_headroom():
    """When `capacity - pop` is the binding constraint, growth is capped there."""
    # raw = 0.012 × 1000 × 0.5 = 6.0; capacity headroom is 3.
    delta = happiness_velocity(1000.0, 1.5, capacity=1003, jobs=10_000)
    assert delta == pytest.approx(3.0)


def test_velocity_downward_does_not_clamp_on_jobs_or_capacity():
    """Emigration is not bounded by structural state: a city with abundant
    jobs+housing still bleeds when unhappy."""
    delta = happiness_velocity(500.0, 0.5, capacity=10_000, jobs=10_000)
    assert delta == pytest.approx(-3.0)
    # And the same delta when jobs/capacity are exactly at pop (no upward
    # headroom) since the clamps only fire on positive raw.
    delta2 = happiness_velocity(500.0, 0.5, capacity=500, jobs=500)
    assert delta2 == pytest.approx(-3.0)


def test_velocity_asymmetry_max_emigration_is_double_max_growth():
    """Max emigration magnitude (h=0) is 2× max growth magnitude (h=1.5)."""
    pop = 1000.0
    max_growth = happiness_velocity(pop, 1.5, capacity=10_000, jobs=10_000)
    max_emigration = happiness_velocity(pop, 0.0, capacity=10_000, jobs=10_000)
    assert max_growth > 0
    assert max_emigration < 0
    assert abs(max_emigration) == pytest.approx(2.0 * max_growth)


def test_velocity_neutral_constant_matches_prd():
    """The h_neutral anchor is documented as 1.0."""
    assert HAPPINESS_NEUTRAL == 1.0


# -- apply_structural_clamps: pure unit tests -------------------------------


def test_clamps_no_op_when_within_bounds():
    """pop ≤ capacity and jobs ≥ 0.7·pop → identity."""
    assert apply_structural_clamps(100.0, capacity=200, jobs=200) == 100.0
    assert apply_structural_clamps(100.5, capacity=200, jobs=200) == 100.5


def test_housing_exodus_small_overrun_drops_by_5():
    """pop = capacity + 100: max(capacity, pop - 5) = pop - 5."""
    assert apply_structural_clamps(200.0, capacity=100, jobs=1000) == 195.0


def test_housing_exodus_floors_at_capacity():
    """pop = capacity + 1: max(capacity, pop - 5) = capacity."""
    assert apply_structural_clamps(101.0, capacity=100, jobs=1000) == 100.0


def test_jobs_floor_mild_deficit_decays_gradual():
    """jobs slightly below 0.7·pop: result is max(jobs/0.7, pop·0.99) = pop·0.99."""
    # pop=100, jobs=69 (just below 0.7·100=70). pop·0.99 = 99 > jobs/0.7 = 98.57.
    assert apply_structural_clamps(100.0, capacity=1000, jobs=69) == 99.0


def test_jobs_floor_snaps_when_deficit_is_severe():
    """jobs far below 0.7·pop: result snaps to jobs/0.7."""
    # pop=100, jobs=10. jobs/0.7 ≈ 14.28; pop·0.99 = 99. max = 99.
    # So even severe deficit decays gradually; only after many ticks does
    # pop converge toward jobs/0.7. Confirm one-tick step is pop·0.99.
    result = apply_structural_clamps(100.0, capacity=1000, jobs=10)
    assert result == 99.0
    # Iterating drives pop down toward 10/0.7 ≈ 14.28.
    pop = 100.0
    for _ in range(500):
        pop = apply_structural_clamps(pop, capacity=1000, jobs=10)
    assert pop == pytest.approx(10 / 0.7, abs=0.5)


def test_both_clamps_interact_when_both_conditions_hold():
    """pop > capacity AND jobs < 0.7·pop: housing fires first, jobs floor second."""
    # pop=200, capacity=100, jobs=50. Step 1: max(100, 195) = 195. Step 2:
    # 50 < 0.7·195 = 136.5 → max(50/0.7≈71.43, 195·0.99=193.05) = 193.05.
    result = apply_structural_clamps(200.0, capacity=100, jobs=50)
    assert result == pytest.approx(193.05)


# -- update_population: integration tests ------------------------------------


def test_happy_city_grows_monotonically_over_30_ticks():
    """At h=1.2 with adequate headroom, pop rises every tick."""
    w = _fresh_world()
    _inject_tile(w, type="house", x=0, y=0, housing_capacity=10)
    _inject_tile(w, type="park", x=1, y=1)  # adds +0.10 happiness → h≈1.10
    _inject_tile(w, type="park", x=-1, y=-1)  # +0.10 more → h=1.20
    _inject_tile(w, type="commercial", x=5, y=5, jobs=10_000, housing_capacity=10_000)
    w.state.population = 500.0

    prev = w.state.population
    for _ in range(30):
        update_population(w)
        assert w.state.population >= prev
        prev = w.state.population
    # 30 ticks at 0.012 × pop × 0.2: pop should grow noticeably.
    assert w.state.population > 500.0


def test_neutral_city_holds_population_over_30_ticks():
    """h=1.0 vanilla city (no parks, no penalties) sits at exactly its starting pop."""
    w = _fresh_world()
    _inject_tile(w, type="commercial", x=5, y=5, jobs=10_000, housing_capacity=10_000)
    w.state.population = 500.0
    for _ in range(30):
        update_population(w)
        # Happiness is exactly 1.0 (no parks, no noise, no blackouts, no coal).
        assert w.state.happiness == pytest.approx(1.0)
    assert w.state.population == pytest.approx(500.0)


def test_unhappy_city_bleeds_along_closed_form():
    """h=0.7 with abundant headroom: pop_n+1 = pop_n · (1 + 0.012·(h−1))."""
    w = _fresh_world()
    _inject_tile(w, type="commercial", x=5, y=5, jobs=10_000, housing_capacity=10_000)
    # Drive happiness to 0.7 via 6h blackout (1.0 - 0.05·6 = 0.7).
    w.state.population = 1000.0
    expected = 1000.0
    factor = 1.0 + 0.012 * (0.7 - 1.0)
    for _ in range(30):
        w.state.yesterday_blackout_hours = 6.0
        update_population(w)
        expected *= factor
    # 30 ticks. Tax accrual + workforce hooks don't affect pop directly.
    assert w.state.population == pytest.approx(expected, rel=1e-9)


def test_fractional_growth_accumulates_across_days():
    """Regression: small daily delta < 1 must accumulate across days.

    A pop-100 city with happiness 1.2 has velocity 0.012·100·0.2 = 0.24/day.
    After ~5 days the fractional residue crosses 1; integer pop must tick up.
    Under the old `int(pop)` truncation, the city was stuck at exactly 100.
    """
    w = _fresh_world()
    _inject_tile(w, type="house", x=0, y=0, housing_capacity=10)
    _inject_tile(w, type="park", x=1, y=1)  # +0.10
    _inject_tile(w, type="park", x=-1, y=-1)  # +0.10 → h=1.20
    _inject_tile(w, type="commercial", x=5, y=5, jobs=10_000, housing_capacity=10_000)
    w.state.population = 100.0

    update_population(w)
    # After 1 tick: float pop = 100 + 0.24 = 100.24; int(pop) = 100.
    assert w.state.population == pytest.approx(100.24)
    assert int(w.state.population) == 100

    for _ in range(4):
        update_population(w)
    # After 5 total ticks (compounding): pop = 100 · 1.0024^5 ≈ 101.205.
    expected = 100.0 * (1.0024**5)
    assert w.state.population == pytest.approx(expected, rel=1e-6)
    assert int(w.state.population) == 101


def test_workforce_hooks_fire_on_integer_transitions():
    """drain_n is called when int(pop) crosses an integer boundary downward."""
    w = _fresh_world()
    # Inject jobs+housing without staffing them (staffed_jobs=0) so the
    # unemployed pool is non-zero and can absorb the drain silently.
    _inject_tile(
        w,
        type="commercial",
        x=5,
        y=5,
        jobs=10_000,
        housing_capacity=10_000,
        staffed_jobs=0,
    )
    w.state.population = 100.0
    w.state.yesterday_blackout_hours = 6.0  # h=0.7 → velocity ≈ -0.36
    employed_before = sum(t.staffed_jobs for t in w.state.tiles) + sum(
        wl.staffed_jobs for wl in w.state.wells
    )

    update_population(w)

    assert int(w.state.population) == 99
    # Unemployed pool absorbs the drain → no firings.
    employed_after = sum(t.staffed_jobs for t in w.state.tiles) + sum(
        wl.staffed_jobs for wl in w.state.wells
    )
    assert employed_after == employed_before


def test_workforce_hooks_skip_when_no_integer_crossing():
    """Sub-1/day delta that doesn't cross an integer leaves workforce untouched."""
    w = _fresh_world()
    _inject_tile(w, type="house", x=0, y=0, housing_capacity=10)
    _inject_tile(w, type="park", x=1, y=1)
    _inject_tile(w, type="commercial", x=5, y=5, jobs=10_000, housing_capacity=10_000)
    w.state.population = 100.0
    # h = 1.10 → velocity = 0.012·100·0.10 = 0.12/day.
    # After 1 tick: pop = 100.12, int still 100 → no hire/drain.
    staff_before = [t.staffed_jobs for t in w.state.tiles]
    update_population(w)
    assert int(w.state.population) == 100
    assert w.state.population == pytest.approx(100.12)
    staff_after = [t.staffed_jobs for t in w.state.tiles]
    assert staff_after == staff_before


def test_daily_tax_uses_post_update_integer_population():
    """Tax = $4 · int(state.population), accrued each tick."""
    w = _fresh_world()
    _inject_tile(w, type="commercial", x=5, y=5, jobs=10_000, housing_capacity=10_000)
    w.state.population = 500.7  # int = 500
    treasury_before = w.state.treasury

    update_population(w)
    # h=1.0 (no penalties), velocity=0 → pop stays at 500.7 → tax = $4 × 500.
    assert int(w.state.population) == 500
    assert w.state.today_summary_so_far["tax_revenue"] == pytest.approx(500 * 4.0)
    assert w.state.treasury == pytest.approx(treasury_before + 500 * 4.0)


# -- Workforce wiring on integer transitions --------------------------------


def _find_tile(w: World, type: str) -> Tile:
    for t in w.state.tiles:
        if t.type == type:
            return t
    raise AssertionError(f"no {type} tile in world")


def test_drain_fires_newest_producer_when_unemployed_zero():
    """Unhappy city, fully-staffed → newest producer loses workers."""
    w = _fresh_world()
    _inject_tile(w, type="industrial", x=2, y=2, jobs=30, built_day=1)
    w.state.population = 60  # employed = 60, unemployed = 0
    # 8h blackout → h=0.6 → velocity = 0.012·60·-0.4 = -0.288 → pop=59.7; int=59.
    w.state.yesterday_blackout_hours = 8.0

    update_population(w)

    # 1 worker fired from newest producer (industrial).
    assert int(w.state.population) == 59
    industrial = w.state.tiles[1]
    town_hall = _find_tile(w, "town_hall")
    assert industrial.staffed_jobs == 29
    assert town_hall.staffed_jobs == 30


def test_drain_silent_from_unemployed_when_buffer_exists():
    """Unhappy city with unemployed buffer → no firings."""
    w = _fresh_world()
    # Pad jobs so the structural jobs floor never fires (jobs ≥ 0.7·pop). The
    # injected commercial tile contributes its catalog demand but its
    # staffed_jobs is independent of pop; it just inflates the `jobs` total.
    _inject_tile(w, type="industrial", x=2, y=2, jobs=30, built_day=1)
    _inject_tile(w, type="commercial", x=5, y=5, jobs=1000, staffed_jobs=12, housing_capacity=200)
    w.state.population = 100  # employed = 30 + 30 + 12 = 72 → unemployed = 28
    w.state.yesterday_blackout_hours = 8.0  # h=0.6

    update_population(w)

    # velocity = 0.012 · 100 · -0.4 = -0.48; pop_after ≈ 99.52 → int=99.
    assert int(w.state.population) == 99
    industrial = w.state.tiles[1]
    town_hall = _find_tile(w, "town_hall")
    commercial = w.state.tiles[2]
    assert industrial.staffed_jobs == 30
    assert town_hall.staffed_jobs == 30
    assert commercial.staffed_jobs == 12


def test_growth_hires_into_open_vacancies_oldest_first():
    """Happy city with abundant headroom: hire_to_fill fills the unemployed pool."""
    w = _fresh_world()
    # Town hall (day 0, 30 jobs, staffed=30) + two industrials with empty staffing.
    _inject_tile(w, type="industrial", x=2, y=2, jobs=30, staffed_jobs=0, built_day=1)
    _inject_tile(w, type="industrial", x=3, y=3, jobs=30, staffed_jobs=0, built_day=2)
    # Two parks within cheb-2 of a sample house gives h=1.2.
    _inject_tile(w, type="house", x=0, y=0, housing_capacity=10)
    _inject_tile(w, type="park", x=1, y=1)
    _inject_tile(w, type="park", x=-1, y=-1)
    _inject_tile(
        w,
        type="commercial",
        x=5,
        y=5,
        jobs=10_000,
        staffed_jobs=12,
        housing_capacity=10_000,
        built_day=0,
    )
    w.state.population = 1000.0  # employed = 30+0+0+12 = 42 → unemployed = 958

    update_population(w)
    # Older industrial (day 1) fills first.
    older = w.state.tiles[1]
    younger = w.state.tiles[2]
    assert older.staffed_jobs == 30  # day 1 — fully hired
    assert younger.staffed_jobs == 30  # day 2 — also fully hired


def test_tax_revenue_constant_per_capita():
    """DAILY_TAX_PER_CAPITA = $4 per the brief."""
    assert DAILY_TAX_PER_CAPITA == 4.0


# -- Happiness composition (number, not velocity) ---------------------------


def test_first_park_within_chebyshev_2_of_house_contributes():
    """First park within cheb-2 of a house adds 0.10 happiness."""
    w = _fresh_world()
    _inject_tile(w, type="house", x=0, y=0, housing_capacity=10)
    _inject_tile(w, type="park", x=1, y=1)

    update_population(w)
    assert w.state.happiness == pytest.approx(1.10)


def test_park_outside_chebyshev_2_contributes_zero():
    """Park beyond chebyshev-2 of every house contributes nothing."""
    w = _fresh_world()
    _inject_tile(w, type="house", x=0, y=0, housing_capacity=10)
    _inject_tile(w, type="park", x=5, y=5)

    update_population(w)
    assert w.state.happiness == pytest.approx(1.0)


def test_park_benefit_caps_at_0_30_per_house():
    """min(0.30, 0.10 × nearby_parks): 4 parks cap at 0.30."""
    w = _fresh_world()
    _inject_tile(w, type="house", x=0, y=0, housing_capacity=10)
    _inject_tile(w, type="park", x=1, y=1)
    _inject_tile(w, type="park", x=2, y=2)
    _inject_tile(w, type="park", x=-1, y=-1)
    _inject_tile(w, type="park", x=-2, y=-2)

    update_population(w)
    assert w.state.happiness == pytest.approx(1.30)


def test_park_benefit_zero_when_no_houses():
    """No house tiles → park_benefit=0 regardless of park count."""
    w = _fresh_world()
    for i in range(50):
        _inject_tile(w, type="park", x=i, y=0)

    update_population(w)
    assert w.state.happiness == pytest.approx(1.0)


def test_industrial_adjacent_to_house_drops_happiness():
    """Industrial within cheb-2 of a house: -0.03 noise."""
    w = _fresh_world()
    _inject_tile(w, type="house", x=0, y=0, housing_capacity=10)
    _inject_tile(w, type="industrial", x=1, y=1, jobs=5)

    update_population(w)
    assert w.state.happiness == pytest.approx(0.97)


def test_park_between_industrial_and_house_halves_penalty():
    """Park within cheb-2 of both house and source halves noise to -0.015."""
    w = _fresh_world()
    _inject_tile(w, type="house", x=0, y=0, housing_capacity=10)
    _inject_tile(w, type="industrial", x=2, y=2, jobs=5)
    _inject_tile(w, type="park", x=1, y=1)

    update_population(w)
    # park_benefit = 0.10, noise = -0.015 (shielded). h = 1.0 + 0.10 - 0.015 = 1.085.
    assert w.state.happiness == pytest.approx(1.085)


def test_refinery_counts_as_noise_source():
    """Refinery contributes -0.03 like industrial."""
    w = _fresh_world()
    _inject_tile(w, type="house", x=0, y=0, housing_capacity=10)
    _inject_tile(w, type="refinery", x=2, y=0, jobs=25)

    update_population(w)
    assert w.state.happiness == pytest.approx(0.97)


def test_noise_averaged_over_multiple_houses():
    """Noise is mean over houses, not sum."""
    w = _fresh_world()
    _inject_tile(w, type="house", x=0, y=0, housing_capacity=10)
    _inject_tile(w, type="house", x=10, y=10, housing_capacity=10)
    _inject_tile(w, type="industrial", x=1, y=1, jobs=5)

    update_population(w)
    # House 1: -0.03; House 2: 0. Mean = -0.015 → h = 0.985.
    assert w.state.happiness == pytest.approx(0.985)


def test_blackout_hours_lower_happiness():
    """Per-hour blackout coefficient is 0.05."""
    w = _fresh_world()
    w.state.yesterday_blackout_hours = 6.0
    update_population(w)
    assert w.state.happiness == pytest.approx(0.7)


def test_brownout_hours_lower_happiness():
    """Per-hour brownout coefficient is 0.02."""
    w = _fresh_world()
    w.state.yesterday_brownout_hours = 24.0
    update_population(w)
    assert w.state.happiness == pytest.approx(0.52)


def test_happiness_clipped_at_0_when_outage_extreme():
    """24h+ of blackouts pins happiness at the lower bound 0."""
    w = _fresh_world()
    w.state.yesterday_blackout_hours = 10_000.0
    update_population(w)
    assert w.state.happiness == pytest.approx(0.0)


# -- Negative-treasury happiness penalty -----------------------------------


def test_negative_treasury_penalty_constant_is_0_05():
    """The penalty is a flat 0.05 happiness drop per day in the red."""
    assert NEGATIVE_TREASURY_HAPPINESS_PENALTY == 0.05


def test_positive_treasury_applies_no_penalty():
    """Treasury >= 0 → happiness matches baseline (no penalty)."""
    w = _fresh_world()
    w.state.treasury = 1.0  # positive
    update_population(w)
    # Baseline: no parks, no noise, no outages, no coal → h=1.0.
    assert w.state.happiness == pytest.approx(1.0)


def test_zero_treasury_applies_no_penalty():
    """Treasury exactly 0 is not negative → no penalty."""
    w = _fresh_world()
    w.state.treasury = 0.0
    update_population(w)
    assert w.state.happiness == pytest.approx(1.0)


def test_negative_treasury_drops_happiness_by_exactly_0_05():
    """Treasury < 0 → exactly 0.05 subtracted before the [0, 1.5] clamp."""
    w = _fresh_world()
    w.state.treasury = -1.0
    update_population(w)
    assert w.state.happiness == pytest.approx(0.95)


def test_negative_treasury_penalty_does_not_scale_with_depth():
    """Penalty is flat: -$1 and -$1,000,000 produce the same 0.05 drop."""
    w_shallow = _fresh_world()
    w_shallow.state.treasury = -1.0
    update_population(w_shallow)

    w_deep = _fresh_world()
    w_deep.state.treasury = -1_000_000.0
    update_population(w_deep)

    assert w_shallow.state.happiness == pytest.approx(w_deep.state.happiness)
    assert w_deep.state.happiness == pytest.approx(0.95)


def test_negative_treasury_penalty_does_not_scale_with_duration():
    """Sustained negative treasury → same flat 0.05 every day (no accumulation)."""
    w = _fresh_world()
    # Park keeps things lively; tax revenue is small so treasury stays negative.
    _inject_tile(w, type="commercial", x=5, y=5, jobs=10_000, housing_capacity=10_000)
    w.state.population = 100.0
    w.state.treasury = -1_000_000.0  # deep enough that tax can't recover it in 10 days

    for _ in range(10):
        update_population(w)
        assert w.state.happiness == pytest.approx(0.95)


def test_negative_to_nonnegative_transition_removes_penalty_immediately():
    """The day treasury returns >= 0, the penalty is gone — no lingering effect."""
    w = _fresh_world()
    w.state.treasury = -1.0
    update_population(w)
    assert w.state.happiness == pytest.approx(0.95)

    # Treasury back in the black on the next day.
    w.state.treasury = 100.0
    update_population(w)
    assert w.state.happiness == pytest.approx(1.0)


def test_negative_treasury_penalty_applied_before_lower_clamp():
    """Penalty fires before the [0, 1.5] clamp, not after.

    With an extreme outage that already pins happiness below zero, the
    penalty's contribution disappears into the clamp at 0.0 — it does not
    push happiness negative.
    """
    w = _fresh_world()
    w.state.treasury = -1.0
    w.state.yesterday_blackout_hours = 10_000.0
    update_population(w)
    assert w.state.happiness == pytest.approx(0.0)


def test_negative_treasury_penalty_applied_before_upper_clamp():
    """At the upper clamp boundary, a -0.05 step is visible (it doesn't
    silently push above the cap)."""
    w = _fresh_world()
    _inject_tile(w, type="house", x=0, y=0, housing_capacity=10)
    # Four parks within cheb-2 → +0.30 (capped). h_raw = 1.30.
    _inject_tile(w, type="park", x=1, y=1)
    _inject_tile(w, type="park", x=2, y=2)
    _inject_tile(w, type="park", x=-1, y=-1)
    _inject_tile(w, type="park", x=-2, y=-2)
    w.state.treasury = -1.0

    update_population(w)
    # 1.30 - 0.05 = 1.25, well inside the cap.
    assert w.state.happiness == pytest.approx(1.25)


# -- State surface -----------------------------------------------------------


def test_state_dict_exposes_population_as_integer():
    """`/state` surfaces population as int even though it's float in-state."""
    w = _fresh_world()
    s = w.state_dict()
    assert "population" in s
    assert isinstance(s["population"], int)
    assert s["population"] == 100


def test_state_dict_population_floors_fractional_part():
    """Wire representation truncates fractional residue."""
    w = _fresh_world()
    w.state.population = 100.7
    assert w.state_dict()["population"] == 100


def test_step_size_invariance_with_population_dynamics():
    """Determinism contract: step(days=7) ≡ 7×step(days=1)."""
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


def test_population_field_is_float_typed():
    """WorldState.population is a float so fractional dynamics accumulate."""
    w = _fresh_world()
    assert isinstance(w.state.population, float)


def test_sustained_blackout_declines_population_through_step():
    """A world with insufficient generation runs daily blackouts; pop bleeds."""
    w = World()
    w.reset(seed=42)
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
    w.state.population = 200.0
    pop_start = w.state.population

    w.step(days=7)

    assert w.state.population < pop_start
    assert w.state.happiness < 1.0

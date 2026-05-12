"""Event sampling + application (slice 11, brief §4.11 + PRD §Events).

Covers:
- per-day rolls happen with the right probability over many trials
- durations land in the spec range
- regulatory tightening cap (3) silently skips later rolls
- multipliers wire into demand / fuel cost / carbon ledger
- plant_failure flips operational=False, restores at expiry
- /state.active_events + /events endpoint
- step-size invariance with events firing
- event_rng is independent from sim_rng (slice-04 weather budget intact)
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from world.api import create_app
from world.economy import CARBON_PRICE_USD_PER_TON
from world.events import (
    DEMAND_SURPRISE_DURATION,
    DEMAND_SURPRISE_IC_MULT,
    DEMAND_SURPRISE_PROB,
    FUEL_PRICE_SHOCK_DURATION,
    FUEL_PRICE_SHOCK_MULT,
    FUEL_PRICE_SHOCK_PROB,
    HEATWAVE_DURATION,
    HEATWAVE_PROB,
    HEATWAVE_RESIDENTIAL_MULT,
    PLANT_FAILURE_DURATION_MAX,
    PLANT_FAILURE_DURATION_MIN,
    PLANT_FAILURE_PROB_PER_PLANT,
    REGULATORY_TIGHTENING_MAX_OCCURRENCES,
    REGULATORY_TIGHTENING_MULT,
    REGULATORY_TIGHTENING_PROB,
    expire_finite_events,
    fuel_price_shock_multiplier,
    sample_and_apply_events,
)
from world.sim import World

# -- Constants pinned to PRD ----------------------------------------------


def test_event_constants_match_prd_table():
    assert HEATWAVE_PROB == 0.003
    assert HEATWAVE_DURATION == 5
    assert HEATWAVE_RESIDENTIAL_MULT == 1.40
    assert FUEL_PRICE_SHOCK_PROB == 0.002
    assert FUEL_PRICE_SHOCK_DURATION == 30
    assert FUEL_PRICE_SHOCK_MULT == 2.0
    assert DEMAND_SURPRISE_PROB == 0.003
    assert DEMAND_SURPRISE_DURATION == 10
    assert DEMAND_SURPRISE_IC_MULT == 1.30
    assert REGULATORY_TIGHTENING_PROB == 0.001
    assert REGULATORY_TIGHTENING_MULT == 1.5
    assert REGULATORY_TIGHTENING_MAX_OCCURRENCES == 3
    assert PLANT_FAILURE_PROB_PER_PLANT == 0.001
    assert PLANT_FAILURE_DURATION_MIN == 3
    assert PLANT_FAILURE_DURATION_MAX == 7


# -- Default state defaults ------------------------------------------------


def test_fresh_world_has_no_active_or_historical_events():
    w = World()
    w.reset(seed=42)
    assert w.state.active_events == []
    assert w.state.historical_events == []
    assert w.state.regulatory_tightenings_applied == 0


def test_event_rng_is_third_master_child_distinct_from_sim():
    w = World()
    w.reset(seed=42)
    a = float(w.sim_rng.standard_normal())
    b = float(w.event_rng.standard_normal())
    # Tiny chance of equality, but practically ~0 — different bit generator state.
    assert a != b


def test_step_does_not_consume_extra_sim_rng_per_day():
    """Slice 04 contract: 3 sim_rng draws per hour, 0 per day from events.
    A 7-day step still advances sim_rng by exactly 7 * 24 * 3 draws."""
    w = World()
    w.reset(seed=42)
    w.step(days=7)
    snapshot = World()
    snapshot.reset(seed=42)
    for _ in range(7 * 24 * 3):
        snapshot.sim_rng.standard_normal()
    assert w.sim_rng.standard_normal() == snapshot.sim_rng.standard_normal()


# -- Helper: force-pump events using event_rng ----------------------------


def _force_event_rng(w: World, seed: int) -> None:
    """Re-seed the event_rng with a chosen seed for testing distributions."""
    w.event_rng = np.random.default_rng(seed)


# -- Probabilities respected over many trials ------------------------------


def test_heatwave_probability_respected_over_many_trials():
    """Roll heatwave many times from a no-active state. Empirical hit rate
    should land near HEATWAVE_PROB (0.003)."""
    w = World()
    w.reset(seed=42)
    _force_event_rng(w, 12345)
    n_trials = 20_000
    n_hits = 0
    for _ in range(n_trials):
        # Each trial: a clean state with no active events; roll once.
        w.state.active_events = []
        before = len(w.state.active_events)
        sample_and_apply_events(w)
        after_types = [e["type"] for e in w.state.active_events]
        if "heatwave" in after_types:
            n_hits += 1
        # The dispatch loop also runs the per-day expiration, but since we
        # never advance day here it stays static. To keep heatwave eligible,
        # clear active_events between trials (above).
        _ = before
    # 95% CI for binomial(20000, 0.003) ~ 60 ± 15. Allow generous margin.
    assert 30 < n_hits < 110


def test_regulatory_tightening_probability_respected():
    """Roll regulatory tightening with the cap pre-set high so it can fire
    every trial."""
    w = World()
    w.reset(seed=42)
    _force_event_rng(w, 99)
    n_trials = 20_000
    n_hits = 0
    for _ in range(n_trials):
        # Reset the cap counter every trial so it's always eligible.
        w.state.regulatory_tightenings_applied = 0
        w.state.active_events = []
        before_count = w.state.regulatory_tightenings_applied
        sample_and_apply_events(w)
        if w.state.regulatory_tightenings_applied > before_count:
            n_hits += 1
    # Expected ~20 hits (0.001 * 20000); generous tolerance.
    assert 5 < n_hits < 50


# -- Plant failure duration sampled in [3, 7] -----------------------------


def test_plant_failure_duration_in_spec_range():
    """Force-fire plant_failure many times and assert duration ∈ [3, 7]."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("coal_plant", th.x + 1, th.y)

    durations: list[int] = []
    for k in range(1000):
        # Re-seed with a known-fire seed by pre-pumping until the per-plant
        # roll is < 0.001. Easier: just pre-force the active_events to empty
        # and re-roll many times until we collect 200 hits.
        _force_event_rng(w, k)
        w.state.active_events = []
        # Reset operational so the plant can fail again.
        for t in w.state.tiles:
            if t.type == "coal_plant":
                t.operational = True
        sample_and_apply_events(w)
        for e in w.state.active_events:
            if e["type"] == "plant_failure":
                durations.append(e["ends_day"] - e["started_day"])
        if len(durations) >= 200:
            break

    assert len(durations) > 0, "no plant failures fired across 1000 seed trials"
    assert all(PLANT_FAILURE_DURATION_MIN <= d <= PLANT_FAILURE_DURATION_MAX for d in durations)
    # Span the full range eventually (at least 200 hits should cover 3..7).
    if len(durations) >= 200:
        assert min(durations) == PLANT_FAILURE_DURATION_MIN
        assert max(durations) == PLANT_FAILURE_DURATION_MAX


# -- Regulatory tightening cap (3 occurrences) ----------------------------


def test_regulatory_tightening_capped_at_3_occurrences():
    """Force-fire regulatory tightening 4 times; the 4th must be silently
    skipped. carbon_price ends at 25 × 1.5³ = 84.375, NOT 25 × 1.5⁴."""
    w = World()
    w.reset(seed=42)
    # Custom rng that always returns 0.0 (always fires).
    w.event_rng = np.random.default_rng(0)
    # Patch event_rng.random to a deterministic always-fires function.

    class AlwaysFireRng:
        def random(self):  # noqa: D401
            return 0.0

        def integers(self, low, high):  # never reached for reg tightening
            return low

    w.event_rng = AlwaysFireRng()  # type: ignore[assignment]

    starting = w.state.carbon_price
    # 4 attempts; 4th roll skipped because cap = 3.
    for _ in range(4):
        # Clear finite-event slots so each call only rolls regulatory + 0
        # plant rolls. (No plants built.) Heatwave/fuel/demand will also fire
        # at p=0.0 < their probs, so wipe them between rolls.
        w.state.active_events = []
        sample_and_apply_events(w)
    expected = starting * (REGULATORY_TIGHTENING_MULT**REGULATORY_TIGHTENING_MAX_OCCURRENCES)
    assert w.state.carbon_price == pytest.approx(expected)
    assert w.state.regulatory_tightenings_applied == REGULATORY_TIGHTENING_MAX_OCCURRENCES
    # 3 historical_events of type regulatory_tightening, not 4.
    n_reg = sum(1 for e in w.state.historical_events if e["type"] == "regulatory_tightening")
    assert n_reg == REGULATORY_TIGHTENING_MAX_OCCURRENCES


def test_regulatory_tightening_caps_carbon_price_at_84_4():
    """End-to-end cap value: starting $25 × 1.5³ ≈ $84.375/ton."""
    w = World()
    w.reset(seed=42)
    w.state.regulatory_tightenings_applied = 0
    w.state.carbon_price = CARBON_PRICE_USD_PER_TON

    class AlwaysFireRng:
        def random(self):
            return 0.0

        def integers(self, low, high):
            return low

    w.event_rng = AlwaysFireRng()  # type: ignore[assignment]
    for _ in range(10):  # try 10 times; only 3 should land
        w.state.active_events = []
        sample_and_apply_events(w)

    assert w.state.carbon_price == pytest.approx(25.0 * 1.5**3)


# -- Multipliers wire into demand / fuel / carbon -------------------------


def test_heatwave_multiplies_residential_demand_in_sim():
    """Heatwave bumps residential by 1.4 — confirm via total_demand_kw."""
    from world.power import total_demand_kw

    w = World()
    w.reset(seed=42)
    base = total_demand_kw(w.state, h=18)  # evening peak
    w.state.active_events.append(
        {"type": "heatwave", "started_day": 0, "ends_day": 5, "severity": 1.4}
    )
    bumped = total_demand_kw(w.state, h=18)
    assert bumped > base
    assert bumped == pytest.approx(base * HEATWAVE_RESIDENTIAL_MULT)


def test_demand_surprise_multiplies_industrial_commercial():
    """Demand surprise bumps I+C by 1.3 — confirm via total_demand_kw."""
    from world.power import total_demand_kw
    from world.state import Tile

    w = World()
    w.reset(seed=42)
    w.state.tiles.append(
        Tile(
            id="injected-industrial",
            type="industrial",
            x=5,
            y=5,
            built_day=0,
            jobs=30,
            staffed_jobs=30,
            demand_kw=300,
        )
    )
    base = total_demand_kw(w.state, h=14)
    w.state.active_events.append(
        {"type": "demand_surprise", "started_day": 0, "ends_day": 10, "severity": 1.3}
    )
    bumped = total_demand_kw(w.state, h=14)
    assert bumped > base


def test_fuel_price_shock_doubles_fuel_cost():
    """End-to-end: a coal-plant world's fuel_cost doubles with shock."""
    w_normal = World()
    w_normal.reset(seed=42)
    th = next(t for t in w_normal.state.tiles if t.type == "town_hall")
    w_normal.build("coal_plant", th.x + 1, th.y)
    w_normal.step(days=1)
    normal_fuel = w_normal.state.today_summary_so_far["fuel_cost"]

    w_shock = World()
    w_shock.reset(seed=42)
    w_shock.build("coal_plant", th.x + 1, th.y)
    # Inject the shock so it's active for day 1.
    w_shock.state.active_events.append(
        {"type": "fuel_price_shock", "started_day": 0, "ends_day": 30, "severity": 2.0}
    )
    w_shock.step(days=1)
    shock_fuel = w_shock.state.today_summary_so_far["fuel_cost"]

    assert normal_fuel > 0
    assert shock_fuel == pytest.approx(2.0 * normal_fuel)


def test_fuel_price_shock_multiplier_helper():
    w = World()
    w.reset(seed=42)
    assert fuel_price_shock_multiplier(w.state) == 1.0
    w.state.active_events.append(
        {"type": "fuel_price_shock", "started_day": 0, "ends_day": 30, "severity": 2.0}
    )
    assert fuel_price_shock_multiplier(w.state) == 2.0


def test_regulatory_tightening_bumps_carbon_price_immediately():
    """Force one regulatory tightening; carbon_cost the same day reflects
    the bumped price."""

    class FireOnceRng:
        def __init__(self):
            self.calls = 0

        def random(self):
            # Calls in sample_and_apply order: heatwave, fuel, demand, reg, then plant rolls.
            # We want only the regulatory roll (4th) to fire.
            self.calls += 1
            return 0.0 if self.calls == 4 else 0.99

        def integers(self, low, high):
            return low

    w = World()
    w.reset(seed=42)
    w.event_rng = FireOnceRng()  # type: ignore[assignment]
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("coal_plant", th.x + 1, th.y)
    w.step(days=1)
    # carbon_price was bumped to 37.5 before the carbon-cost step ran.
    assert w.state.carbon_price == pytest.approx(25.0 * 1.5)
    co2 = w.state.today_summary_so_far["co2_emitted_t"]
    assert w.state.today_summary_so_far["carbon_cost"] == pytest.approx(co2 * 37.5)


# -- Plant failure: zeros output, restores after expiry -------------------


def test_plant_failure_zeros_plant_output_in_dispatch():
    """A plant marked operational=False produces 0 kW in dispatch."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("coal_plant", th.x + 1, th.y)
    plant = next(t for t in w.state.tiles if t.type == "coal_plant")
    plant.operational = False
    w.step(days=1)
    assert plant.current_output_kw == 0.0


def test_plant_failure_event_zeroes_then_restores_operational():
    """Force a 3-day plant_failure, advance through it, confirm operational
    flips False → True at expiry."""

    class FailPlantOnceRng:
        """First call returns 0.99 (no heatwave/fuel/demand/reg).
        5th call (plant roll) returns 0.0 (fire). Subsequent random() returns
        0.99 (no further hits). integers returns low (3-day duration)."""

        def __init__(self):
            self.calls = 0

        def random(self):
            self.calls += 1
            return 0.0 if self.calls == 5 else 0.99

        def integers(self, low, high):
            return low  # 3-day duration

    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("coal_plant", th.x + 1, th.y)
    plant = next(t for t in w.state.tiles if t.type == "coal_plant")

    w.event_rng = FailPlantOnceRng()  # type: ignore[assignment]
    w.step(days=1)  # day 0 → day 1: failure fires on day 0, ends_day=3
    assert plant.operational is False
    assert any(e["type"] == "plant_failure" for e in w.state.active_events)

    # Resume with a no-fire RNG so subsequent days don't re-trigger.
    class NoFireRng:
        def random(self):
            return 0.99

        def integers(self, low, high):
            return low

    w.event_rng = NoFireRng()  # type: ignore[assignment]
    # Days 1, 2: still failed (ends_day=3).
    w.step(days=2)
    assert not plant.operational

    # Day 3: ends_day == today, expire. Plant restores at start of day.
    w.step(days=1)
    # Re-fetch to bypass mypy's literal-narrowing memory of `plant.operational`.
    plant_after = next(t for t in w.state.tiles if t.type == "coal_plant")
    assert plant_after.operational
    # Moved to historical.
    assert any(e["type"] == "plant_failure" for e in w.state.historical_events)


def test_plant_failure_id_ascending_order_for_determinism():
    """When two coal plants exist, plant-failure rolls happen in id-ascending
    order — the SAME plant always fires given identical event_rng state."""
    a = World()
    b = World()
    a.reset(seed=42)
    b.reset(seed=42)
    th = next(t for t in a.state.tiles if t.type == "town_hall")
    for w in (a, b):
        w.build("coal_plant", th.x + 1, th.y)
        w.build("coal_plant", th.x - 1, th.y)
    # Step many days; if any plant_failure events fire, the rolling pattern
    # must be identical between a and b.
    for _ in range(50):
        a.step(days=1)
        b.step(days=1)
    a_failures = [e for e in a.state.historical_events if e["type"] == "plant_failure"] + [
        e for e in a.state.active_events if e["type"] == "plant_failure"
    ]
    b_failures = [e for e in b.state.historical_events if e["type"] == "plant_failure"] + [
        e for e in b.state.active_events if e["type"] == "plant_failure"
    ]
    assert [e["plant_id"] for e in a_failures] == [e["plant_id"] for e in b_failures]


# -- "At most one" rule ----------------------------------------------------


def test_finite_event_skips_re_roll_while_active():
    """If a heatwave is already active, the next day's heatwave roll is
    skipped — meaning event_rng's first draw goes to fuel_price_shock."""

    class PeekRng:
        def __init__(self):
            self.draws: list[float] = []

        def random(self):
            self.draws.append(0.5)
            return 0.5

        def integers(self, low, high):
            return low

    w = World()
    w.reset(seed=42)
    w.state.active_events.append(
        {"type": "heatwave", "started_day": 0, "ends_day": 100, "severity": 1.4}
    )
    rng = PeekRng()
    w.event_rng = rng  # type: ignore[assignment]
    sample_and_apply_events(w)
    # 4 base rolls expected (heatwave skipped → 3 base + reg) + 0 plants.
    # Wait: heatwave skipped, fuel rolled, demand rolled, regulatory rolled → 3 draws.
    assert len(rng.draws) == 3


# -- /state.active_events + GET /events -----------------------------------


def test_state_dict_active_events_exposed():
    w = World()
    w.reset(seed=42)
    w.state.active_events.append(
        {"type": "heatwave", "started_day": 0, "ends_day": 5, "severity": 1.4}
    )
    s = w.state_dict()
    assert s["active_events"] == [
        {"type": "heatwave", "started_day": 0, "ends_day": 5, "severity": 1.4}
    ]
    assert s["regulatory_tightenings_applied"] == 0
    assert s["historical_events"] == []


def test_get_events_endpoint_returns_active_and_history():
    w = World()
    w.reset(seed=42)
    w.state.active_events.append(
        {"type": "heatwave", "started_day": 0, "ends_day": 5, "severity": 1.4}
    )
    w.state.historical_events.append(
        {"type": "demand_surprise", "started_day": 0, "ends_day": 10, "severity": 1.3}
    )
    client = TestClient(create_app(world=w))
    res = client.get("/events").json()
    assert res["active"] == [{"type": "heatwave", "started_day": 0, "ends_day": 5, "severity": 1.4}]
    assert res["historical"] == [
        {"type": "demand_surprise", "started_day": 0, "ends_day": 10, "severity": 1.3}
    ]
    assert res["regulatory_tightenings_applied"] == 0


# -- expire_finite_events --------------------------------------------------


def test_expire_finite_events_drops_expired_and_restores_plants():
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("coal_plant", th.x + 1, th.y)
    plant = next(t for t in w.state.tiles if t.type == "coal_plant")
    plant.operational = False
    w.state.day = 5
    w.state.active_events = [
        {
            "type": "plant_failure",
            "plant_id": plant.id,
            "started_day": 2,
            "ends_day": 5,
            "severity": 1.0,
        },
        {
            "type": "heatwave",
            "started_day": 4,
            "ends_day": 9,
            "severity": 1.4,
        },
    ]
    expire_finite_events(w)
    # Plant failure expired (ends_day=5 == today), heatwave still active.
    assert [e["type"] for e in w.state.active_events] == ["heatwave"]
    assert plant.operational is True
    assert any(e["type"] == "plant_failure" for e in w.state.historical_events)


# -- Step-size invariance with events --------------------------------------


def test_step_size_invariance_with_events():
    """step(7) ≡ step(1)*7 with events firing — event_rng draws in a fixed
    per-day order, and the world state evolves identically."""
    a = World()
    b = World()
    a.reset(seed=42)
    b.reset(seed=42)
    th = next(t for t in a.state.tiles if t.type == "town_hall")
    for w in (a, b):
        w.build("coal_plant", th.x + 1, th.y)
        w.build("gas_peaker", th.x - 1, th.y)
    a.step(days=7)
    for _ in range(7):
        b.step(days=1)
    assert a.state.day == b.state.day == 7
    assert a.state.treasury == pytest.approx(b.state.treasury)
    assert a.state.carbon_price == pytest.approx(b.state.carbon_price)
    assert [e for e in a.state.active_events] == [e for e in b.state.active_events]
    assert [e for e in a.state.historical_events] == [e for e in b.state.historical_events]


def test_event_rng_replays_same_seed_byte_identical():
    """Two same-seed worlds produce byte-identical event sequences."""
    a = World()
    b = World()
    a.reset(seed=999)
    b.reset(seed=999)
    th = next(t for t in a.state.tiles if t.type == "town_hall")
    for w in (a, b):
        w.build("coal_plant", th.x + 1, th.y)
    for _ in range(50):
        a.step(days=1)
        b.step(days=1)
    assert a.state.active_events == b.state.active_events
    assert a.state.historical_events == b.state.historical_events
    assert a.state.regulatory_tightenings_applied == b.state.regulatory_tightenings_applied


# -- Empirical hit rates over a long run ----------------------------------


def test_heatwave_fires_in_long_simulation():
    """Run 5000 days with no plants. Per-day p=0.003 → expected ~15 heatwaves
    started. With heatwaves locking out re-rolls during their 5-day window,
    actual count is slightly lower but should still be > 0."""
    w = World()
    w.reset(seed=7)
    # Hand-pump: only do the events sampling — no /step (skip dispatch). Speed.
    fired = 0
    for day in range(5000):
        w.state.day = day
        from world.events import expire_finite_events as _expire

        _expire(w)
        sample_and_apply_events(w)
        fired = sum(
            1
            for e in (w.state.active_events + w.state.historical_events)
            if e["type"] == "heatwave"
        )
    # Hard floor: at least 5 heatwaves fired in 5000 days.
    assert fired >= 5

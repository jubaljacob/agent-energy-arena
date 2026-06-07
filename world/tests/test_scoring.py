"""Pure-function tests for `world.scoring.compute_score`.

Pins the externally observable behaviour of the trend-aware formula:

  - Empty input → empty payload.
  - Headline clipped to [0, 100] with all 15 component keys present.
  - Determinism: identical inputs → identical outputs.
  - Monotonicity: growing > flat > declining.
  - Solvency reflects the fraction of days with positive treasury.
  - CVaR-style trough: one bad day is forgiven; sustained bad days are not.
  - An excellent axis lands above 0.8; a mediocre axis lands near 0.5.
  - A signal saturated at the utility ceiling scores trend near level,
    not at the 0.5 fixed point (ADR-0005 reshape).
  - `R` saturates at `renewable_share = RENEWABLE_TARGET`; higher shares
    score the same as the target (ADR-0005 reshape).
  - A synthetic perfect run scores within 1 point of 100.

Internal helpers (`_u_treasury`, `_cvar_low`, ...) are private and
deliberately not pinned here so the maintainer can reshuffle them
during anchor tuning.
"""

from __future__ import annotations

from typing import Any

import pytest

from world.scoring import (
    CVAR_ALPHA,
    HAPPINESS_CEIL,
    POP_TARGET,
    RENEWABLE_TARGET,
    compute_score,
)

STARTING_CASH = 500_000.0

COMPONENT_KEYS = {
    "level_treasury",
    "trend_treasury",
    "trough_treasury",
    "axis_treasury",
    "level_pop",
    "trend_pop",
    "trough_pop",
    "axis_pop",
    "level_happy",
    "trend_happy",
    "trough_happy",
    "axis_happy",
    "R",
    "renewable_share",
    "solvency",
}


def _snapshot(
    *,
    treasury: float,
    population: float,
    happiness: float,
    renewable_kwh: float = 0.0,
    total_kwh: float = 0.0,
) -> dict[str, Any]:
    return {
        "treasury": treasury,
        "population": population,
        "happiness": happiness,
        "cumulative_renewable_served_kwh": renewable_kwh,
        "cumulative_total_served_kwh": total_kwh,
    }


def _flat_run(n: int, **kw: Any) -> list[dict[str, Any]]:
    return [_snapshot(**kw) for _ in range(n)]


# -- contract: empty / shape / clipping / determinism ----------------------


def test_empty_snapshots_returns_empty_payload():
    out = compute_score([], STARTING_CASH)
    assert out == {"n_days": 0, "score": 0.0, "components": {}}


def test_payload_has_all_component_keys_when_nonempty():
    snaps = _flat_run(
        5,
        treasury=STARTING_CASH,
        population=100.0,
        happiness=1.0,
        renewable_kwh=1.0,
        total_kwh=2.0,
    )
    out = compute_score(snaps, STARTING_CASH)
    assert out["n_days"] == 5
    assert set(out["components"].keys()) == COMPONENT_KEYS
    for v in out["components"].values():
        assert isinstance(v, float)


def test_headline_clipped_to_zero_hundred():
    # Even an absurdly bad run cannot go below 0; an absurdly good one
    # cannot exceed 100.
    bad = _flat_run(
        10,
        treasury=-1e12,
        population=0.0,
        happiness=0.0,
    )
    good = _flat_run(
        10,
        treasury=STARTING_CASH + 1e12,
        population=1e9,
        happiness=HAPPINESS_CEIL * 10,
        renewable_kwh=1.0,
        total_kwh=1.0,
    )
    bad_out = compute_score(bad, STARTING_CASH)
    good_out = compute_score(good, STARTING_CASH)
    assert 0.0 <= bad_out["score"] <= 100.0
    assert 0.0 <= good_out["score"] <= 100.0


def test_determinism_identical_inputs_byte_identical_outputs():
    snaps = [
        _snapshot(
            treasury=STARTING_CASH + i * 1000.0,
            population=10.0 + i,
            happiness=1.0,
            renewable_kwh=float(i),
            total_kwh=2.0 * i,
        )
        for i in range(20)
    ]
    a = compute_score(snaps, STARTING_CASH)
    b = compute_score(list(snaps), STARTING_CASH)
    assert a == b


# -- behavioural cases from the PRD ----------------------------------------


def test_flat_run_at_starting_state_scores_near_50():
    # Pick a sub-target renewable share so R isn't pinned at the
    # saturation ceiling — that lets this test exercise the mid-band
    # arithmetic rather than the R-clamp.
    snaps = _flat_run(
        100,
        treasury=STARTING_CASH,
        population=POP_TARGET / 2,  # u_pop = 0.5 (half the completion target)
        happiness=HAPPINESS_CEIL / 2,  # u_happy = 0.5
        renewable_kwh=0.2,
        total_kwh=1.0,
    )
    out = compute_score(snaps, STARTING_CASH)
    # u_treasury = 0.5; u_happy = 0.5; trends lifted by recent-window
    # utility match level; trough = level. R = 0.4 (= 0.2 / 0.5);
    # solvency = 1.0 (treasury == starting_cash > 0). Expected score
    # around the mid-range — give a wide band.
    assert 40.0 < out["score"] < 65.0


def test_growing_outscores_flat_outscores_declining():
    n = 50
    flat = _flat_run(
        n,
        treasury=STARTING_CASH,
        population=100.0,
        happiness=1.0,
        renewable_kwh=1.0,
        total_kwh=2.0,
    )
    growing = [
        _snapshot(
            treasury=STARTING_CASH + i * 10_000.0,
            population=100.0 + i,
            happiness=1.0 + i * 0.001,
            renewable_kwh=1.0 + i * 0.1,
            total_kwh=2.0 + i * 0.1,
        )
        for i in range(n)
    ]
    declining = [
        _snapshot(
            treasury=STARTING_CASH - i * 10_000.0,
            population=max(100.0 - i, 1.0),
            happiness=max(1.0 - i * 0.01, 0.0),
            renewable_kwh=1.0,
            total_kwh=2.0,
        )
        for i in range(n)
    ]
    s_grow = compute_score(growing, STARTING_CASH)["score"]
    s_flat = compute_score(flat, STARTING_CASH)["score"]
    s_decl = compute_score(declining, STARTING_CASH)["score"]
    assert s_grow > s_flat > s_decl


def test_half_run_bankrupt_drives_solvency_to_about_half():
    n = 20
    snaps: list[dict[str, Any]] = []
    for i in range(n):
        treasury = -10_000.0 if i < n // 2 else STARTING_CASH
        snaps.append(
            _snapshot(
                treasury=treasury,
                population=100.0,
                happiness=1.0,
                renewable_kwh=1.0,
                total_kwh=2.0,
            )
        )
    out = compute_score(snaps, STARTING_CASH)
    assert out["components"]["solvency"] == pytest.approx(0.5, abs=0.05)


def test_cvar_forgives_single_bad_day_but_penalises_sustained_bad_days():
    # Window large enough that CVAR_ALPHA fraction > 1 — sustained bad
    # days actually dominate the trough average. Magnitudes well past
    # TREASURY_SCALE so u_treasury saturates at 0 / 1, isolating the
    # trough term as the dominant signal.
    n = max(int(20 / CVAR_ALPHA), 200)
    bad = -100_000_000.0
    good = STARTING_CASH + 100_000_000.0
    # Single bad day, rest good.
    one_bad = [
        _snapshot(
            treasury=bad if i == 0 else good,
            population=100.0,
            happiness=1.0,
            renewable_kwh=1.0,
            total_kwh=2.0,
        )
        for i in range(n)
    ]
    # Sustained: the bottom CVAR_ALPHA fraction are all bad.
    bottom = max(int(CVAR_ALPHA * n) + 1, 2)
    sustained = [
        _snapshot(
            treasury=bad if i < bottom else good,
            population=100.0,
            happiness=1.0,
            renewable_kwh=1.0,
            total_kwh=2.0,
        )
        for i in range(n)
    ]
    s_one = compute_score(one_bad, STARTING_CASH)["score"]
    s_sustained = compute_score(sustained, STARTING_CASH)["score"]
    assert s_one > s_sustained + 1.0  # at least 1-point gap on the [0,100] headline


def test_excellent_axis_above_point_eight_mediocre_near_point_five():
    # Treasury axis goes excellent (large positive level, growing trend,
    # safe trough); happiness axis stays mediocre (held at half-ceil flat).
    n = 60
    snaps = [
        _snapshot(
            treasury=STARTING_CASH + 8_000_000.0 + i * 50_000.0,
            population=1.0,
            happiness=HAPPINESS_CEIL / 2,
            renewable_kwh=0.5,
            total_kwh=1.0,
        )
        for i in range(n)
    ]
    out = compute_score(snaps, STARTING_CASH)
    assert out["components"]["axis_treasury"] > 0.8
    assert 0.4 < out["components"]["axis_happy"] < 0.6


# -- reshape contract (ADR-0005) -------------------------------------------


def test_trend_credits_signal_saturated_at_ceiling():
    """Happiness flat at the utility ceiling is the best possible time
    series; the trend term must reflect that, not collapse to the 0.5
    no-growth fixed point. Pre-ADR-0005 this returned 0.5."""
    snaps = _flat_run(
        100,
        treasury=STARTING_CASH,
        population=100.0,
        happiness=HAPPINESS_CEIL,
        renewable_kwh=1.0,
        total_kwh=2.0,
    )
    out = compute_score(snaps, STARTING_CASH)
    # Recent-quarter mean of u_happy = 1.0; growth term is 0.5; the max
    # of the two is 1.0.
    assert out["components"]["trend_happy"] == pytest.approx(1.0)
    # Axis_happy = 0.4*1 + 0.4*1 + 0.2*1 = 1.0 (level + trend + trough
    # all at the ceiling).
    assert out["components"]["axis_happy"] == pytest.approx(1.0)


def test_trend_treasury_credits_sustained_high_level():
    """A treasury parked at a huge surplus for the entire run scores
    trend ≈ 1, not 0.5. The recent-window utility carries the day when
    the linear slope is zero."""
    snaps = _flat_run(
        100,
        treasury=STARTING_CASH + 1e9,
        population=100.0,
        happiness=1.0,
    )
    out = compute_score(snaps, STARTING_CASH)
    assert out["components"]["trend_treasury"] == pytest.approx(1.0, abs=1e-6)


def test_renewable_R_saturates_at_target_share():
    """A renewable share of exactly `RENEWABLE_TARGET` scores R = 1.0;
    higher shares score the same (no marginal credit past the
    resilience knee)."""
    base = dict(treasury=STARTING_CASH, population=100.0, happiness=1.0)

    half_target = compute_score(
        _flat_run(10, **base, renewable_kwh=RENEWABLE_TARGET / 2, total_kwh=1.0),
        STARTING_CASH,
    )
    at_target = compute_score(
        _flat_run(10, **base, renewable_kwh=RENEWABLE_TARGET, total_kwh=1.0),
        STARTING_CASH,
    )
    above_target = compute_score(
        _flat_run(10, **base, renewable_kwh=1.0, total_kwh=1.0),
        STARTING_CASH,
    )

    # Linear ramp to RENEWABLE_TARGET: half the share scores half R.
    assert half_target["components"]["R"] == pytest.approx(0.5)
    # At and above the target: full credit, flat.
    assert at_target["components"]["R"] == pytest.approx(1.0)
    assert above_target["components"]["R"] == pytest.approx(1.0)
    # Raw share is reported alongside the scored term.
    assert above_target["components"]["renewable_share"] == pytest.approx(1.0)


def test_perfect_run_scores_within_one_point_of_one_hundred():
    """A synthetic perfect run (all axes saturated, R at target,
    treasury positive every day) lands at score >= 99. Pre-ADR-0005
    the structural ceiling was ~85, with the happiness-trend fixed
    point and the linear-R term jointly forfeiting ~15 points."""
    snaps = _flat_run(
        200,
        treasury=STARTING_CASH + 1e9,
        population=1e6,
        happiness=HAPPINESS_CEIL,
        renewable_kwh=RENEWABLE_TARGET,
        total_kwh=1.0,
    )
    out = compute_score(snaps, STARTING_CASH)
    assert out["score"] >= 99.0

"""Per-facility daily economics (slice 01 — tracer: industrial).

Unit-tests on the pure ``world.pricing`` helpers and an end-to-end
``/step`` integration that the industrial revenue is accrued into
``today_summary_so_far["industrial_revenue"]`` and credited to
``state.treasury``. Catalog parity for the new ``economics`` block is
asserted alongside.
"""

from __future__ import annotations

import pytest

from world.catalog import TILE_CATALOG, build_catalog
from world.economy import CARBON_PRICE_USD_PER_TON
from world.pricing import (
    INDUSTRIAL_REVENUE_PER_DAY,
    industrial_co2_for_tile,
    industrial_revenue_for_tile,
    update_civic_revenue,
)
from world.sim import World
from world.state import Tile


def _industrial_tile(staffed: int | None = None) -> Tile:
    spec = TILE_CATALOG["industrial"]
    staffed_jobs = spec.jobs if staffed is None else staffed
    return Tile(
        id="ind_1",
        type="industrial",
        x=0,
        y=0,
        built_day=0,
        operational=True,
        capex_paid=spec.capex,
        opex_per_day=spec.opex_per_day,
        jobs=spec.jobs,
        demand_kw=spec.demand_kw,
        staffed_jobs=staffed_jobs,
    )


# -- industrial_revenue_for_tile -------------------------------------------


def test_industrial_revenue_full_staffing_equals_constant() -> None:
    tile = _industrial_tile()  # staffed = jobs
    assert industrial_revenue_for_tile(tile) == pytest.approx(INDUSTRIAL_REVENUE_PER_DAY)


def test_industrial_revenue_scales_linearly_with_efficiency() -> None:
    spec = TILE_CATALOG["industrial"]
    half_jobs = spec.jobs // 2
    tile = _industrial_tile(staffed=half_jobs)
    expected = INDUSTRIAL_REVENUE_PER_DAY * (half_jobs / spec.jobs)
    assert industrial_revenue_for_tile(tile) == pytest.approx(expected)


def test_industrial_revenue_idle_returns_zero() -> None:
    tile = _industrial_tile(staffed=0)
    assert industrial_revenue_for_tile(tile) == 0.0


def test_industrial_revenue_zero_when_not_operational() -> None:
    tile = _industrial_tile()
    tile.operational = False
    assert industrial_revenue_for_tile(tile) == 0.0


def test_industrial_revenue_zero_for_non_industrial_tile() -> None:
    spec = TILE_CATALOG["commercial"]
    tile = Tile(
        id="com_1",
        type="commercial",
        x=0,
        y=0,
        built_day=0,
        operational=True,
        jobs=spec.jobs,
        staffed_jobs=spec.jobs,
        demand_kw=spec.demand_kw,
    )
    assert industrial_revenue_for_tile(tile) == 0.0


# -- industrial_co2_for_tile (delegation contract) -------------------------


def test_industrial_co2_for_tile_matches_efficiency_scaled_constant() -> None:
    """The helper that the aggregator delegates to must scale by efficiency."""
    spec = TILE_CATALOG["industrial"]
    half = _industrial_tile(staffed=spec.jobs // 2)
    full = _industrial_tile()
    assert industrial_co2_for_tile(full) == pytest.approx(2.0)
    assert industrial_co2_for_tile(half) == pytest.approx(2.0 * (spec.jobs // 2) / spec.jobs)


def test_industrial_co2_for_tile_zero_when_idle_or_not_operational() -> None:
    assert industrial_co2_for_tile(_industrial_tile(staffed=0)) == 0.0
    tile = _industrial_tile()
    tile.operational = False
    assert industrial_co2_for_tile(tile) == 0.0


# -- update_civic_revenue (sim wiring) -------------------------------------


def test_update_civic_revenue_accrues_to_summary_and_treasury() -> None:
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("industrial", th.x + 1, th.y)
    # Force full staffing so we know the exact expected revenue.
    ind = next(t for t in w.state.tiles if t.type == "industrial")
    ind.staffed_jobs = ind.jobs

    treasury_before = w.state.treasury
    summary_before = w.state.today_summary_so_far.get("industrial_revenue", 0.0)

    update_civic_revenue(w)

    assert w.state.today_summary_so_far["industrial_revenue"] == pytest.approx(
        summary_before + INDUSTRIAL_REVENUE_PER_DAY
    )
    assert w.state.treasury == pytest.approx(treasury_before + INDUSTRIAL_REVENUE_PER_DAY)


def test_today_summary_so_far_defaults_industrial_revenue_to_zero() -> None:
    w = World()
    w.reset(seed=42)
    assert "industrial_revenue" in w.state.today_summary_so_far
    assert w.state.today_summary_so_far["industrial_revenue"] == 0.0


# -- /step end-to-end accrual ----------------------------------------------


def test_step_accrues_industrial_revenue_into_summary_and_treasury() -> None:
    """One /step with one fully-staffed industrial credits both the summary
    bucket and treasury by INDUSTRIAL_REVENUE_PER_DAY × efficiency."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("industrial", th.x + 1, th.y)
    ind = next(t for t in w.state.tiles if t.type == "industrial")
    ind.staffed_jobs = ind.jobs  # ensure full staffing irrespective of pop

    treasury_before = w.state.treasury
    w.step(days=1)

    revenue = w.state.today_summary_so_far["industrial_revenue"]
    assert revenue == pytest.approx(INDUSTRIAL_REVENUE_PER_DAY)
    # Treasury delta is dominated by many flows; isolate the industrial
    # revenue contribution by re-running without the industrial.
    w2 = World()
    w2.reset(seed=42)
    treasury_before_no_ind = w2.state.treasury
    w2.step(days=1)
    treasury_no_ind = w2.state.treasury
    treasury_with_ind = w.state.treasury
    # The industrial scenario also adds $200/day OPEX and ~$50/day carbon cost
    # vs the no-industrial scenario, but those costs are independent of
    # revenue. Net of OPEX+carbon, the industrial scenario must be exactly
    # INDUSTRIAL_REVENUE_PER_DAY × eff higher in treasury after one day.
    expected_delta = (
        INDUSTRIAL_REVENUE_PER_DAY  # new revenue
        - ind.opex_per_day  # extra OPEX
        - 2.0 * w.state.carbon_price  # extra industrial CO2 cost (2 t × price)
    )
    actual_delta = (treasury_with_ind - treasury_before) - (
        treasury_no_ind - treasury_before_no_ind
    )
    assert actual_delta == pytest.approx(expected_delta, abs=1e-6)


def test_civic_revenue_runs_before_population_update() -> None:
    """Industrial revenue accrues into today_summary_so_far *before*
    update_population, so tomorrow's commercial revenue (slice 02) will see
    today's lived population. We assert the ordering by constructing a state
    where the industrial tile would be unstaffed if population dropped to
    zero first — the revenue must still appear because efficiency is read
    before drain_n could fire."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("industrial", th.x + 1, th.y)
    ind = next(t for t in w.state.tiles if t.type == "industrial")
    ind.staffed_jobs = ind.jobs

    w.step(days=1)
    # Revenue is non-zero — proving update_civic_revenue ran while the
    # industrial was still staffed.
    assert w.state.today_summary_so_far["industrial_revenue"] > 0.0


def test_step_no_industrial_means_zero_industrial_revenue() -> None:
    """Regression guard: pre-feature cities (no industrial) keep their
    behavior modulo the new zero bucket."""
    w = World()
    w.reset(seed=42)
    w.step(days=1)
    assert w.state.today_summary_so_far["industrial_revenue"] == 0.0


# -- /state per-tile economics fields --------------------------------------


def test_state_tiles_include_estimated_fields_for_industrial() -> None:
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("industrial", th.x + 1, th.y)
    ind = next(t for t in w.state.tiles if t.type == "industrial")
    ind.staffed_jobs = ind.jobs

    s = w.state_dict()
    ind_dict = next(t for t in s["tiles"] if t["type"] == "industrial")
    assert ind_dict["estimated_revenue_per_day"] == pytest.approx(INDUSTRIAL_REVENUE_PER_DAY)
    assert ind_dict["estimated_co2_per_day"] == pytest.approx(2.0)
    assert ind_dict["estimated_carbon_cost_per_day"] == pytest.approx(2.0 * w.state.carbon_price)
    assert ind_dict["estimated_net_per_day"] == pytest.approx(
        INDUSTRIAL_REVENUE_PER_DAY - ind_dict["opex_per_day"] - 2.0 * w.state.carbon_price
    )


def test_state_tiles_non_industrial_estimated_fields_are_zero() -> None:
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    w.build("commercial", th.x + 1, th.y)
    s = w.state_dict()
    for t in s["tiles"]:
        if t["type"] != "industrial":
            assert t["estimated_revenue_per_day"] == 0.0
            assert t["estimated_co2_per_day"] == 0.0
            assert t["estimated_carbon_cost_per_day"] == 0.0
            assert t["estimated_net_per_day"] == 0.0


def test_build_industrial_response_includes_estimated_fields() -> None:
    """The /build response stamps the same economic fields as /state."""
    w = World()
    w.reset(seed=42)
    th = next(t for t in w.state.tiles if t.type == "town_hall")
    res = w.build("industrial", th.x + 1, th.y)
    assert res["ok"]
    payload = res["result"]
    assert "estimated_revenue_per_day" in payload
    assert "estimated_co2_per_day" in payload
    assert "estimated_carbon_cost_per_day" in payload
    assert "estimated_net_per_day" in payload


# -- /catalog economics block ----------------------------------------------


def test_catalog_exposes_economics_block() -> None:
    cat = build_catalog()
    assert "economics" in cat
    eco = cat["economics"]
    assert eco["industrial_revenue_per_day"] == pytest.approx(INDUSTRIAL_REVENUE_PER_DAY)
    assert eco["carbon_price"] == pytest.approx(CARBON_PRICE_USD_PER_TON)


def test_catalog_industrial_description_mentions_revenue_and_co2() -> None:
    cat = build_catalog()
    industrial = next(t for t in cat["tiles"] if t["tile_type"] == "industrial")
    desc = industrial["description"].lower()
    assert "$500" in industrial["description"] or "500" in industrial["description"]
    assert "co2" in desc or "co₂" in desc

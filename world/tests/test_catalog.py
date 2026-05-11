"""Catalog-shape tests focused on the subsurface block (issue 21).

Existing per-tile and per-well assertions live next to the slice that
introduced them (`test_build_api.py`, `test_production.py`,
`test_economy.py`). This module pins the API-parity surface that the
manual-play UI and a participant's agent both read from `/catalog`.
"""

from __future__ import annotations

from world.catalog import build_catalog
from world.subsurface import (
    CRUDE_PRICE_USD_PER_BBL,
    INJECTION_KWH_PER_BBL,
    SEISMIC_BASE_COST,
    SEISMIC_DEFAULT_SIZE,
    SEISMIC_MAX_SIZE,
    SEISMIC_MIN_SIZE,
    survey_cost,
)


def test_catalog_top_level_keys() -> None:
    cat = build_catalog()
    assert set(cat.keys()) == {"tiles", "wells", "subsurface"}


def test_catalog_subsurface_survey_block_matches_constants() -> None:
    cat = build_catalog()
    survey = cat["subsurface"]["survey"]
    assert survey["base_cost"] == SEISMIC_BASE_COST
    assert survey["base_size"] == SEISMIC_DEFAULT_SIZE
    assert survey["min_size"] == SEISMIC_MIN_SIZE
    assert survey["max_size"] == SEISMIC_MAX_SIZE
    assert survey["default_size"] == SEISMIC_DEFAULT_SIZE
    # cost_formula is descriptive metadata; clients must not eval it.
    assert survey["cost_formula"] == "base_cost * (size / base_size) ** 2"


def test_catalog_subsurface_drill_production_matches_constants() -> None:
    cat = build_catalog()
    production = cat["subsurface"]["drill"]["production"]
    assert production["capex"] == 50_000
    assert production["opex_per_day"] == 100
    assert production["max_rate_bbl_day"] == 200
    assert production["crude_price_usd_per_bbl"] == CRUDE_PRICE_USD_PER_BBL


def test_catalog_subsurface_drill_injection_matches_constants() -> None:
    cat = build_catalog()
    injection = cat["subsurface"]["drill"]["injection"]
    assert injection["capex"] == 30_000
    assert injection["opex_per_day"] == 50
    assert injection["max_rate_bbl_day"] == 200
    assert injection["kwh_per_bbl"] == INJECTION_KWH_PER_BBL


def test_catalog_subsurface_survey_cost_derivation_matches_helper() -> None:
    """The descriptive formula must agree with subsurface.survey_cost() for
    every legal size. UI / agent clients compute the preview arithmetically."""
    cat = build_catalog()
    survey = cat["subsurface"]["survey"]
    base = float(survey["base_cost"])
    base_size = float(survey["base_size"])
    for size in range(SEISMIC_MIN_SIZE, SEISMIC_MAX_SIZE + 1):
        derived = base * (size / base_size) ** 2
        assert derived == survey_cost(size), size


def test_catalog_wells_array_unchanged_by_subsurface_extension() -> None:
    """The new subsurface block is additive — the wells list must still
    expose the same two well types so existing readers keep working."""
    cat = build_catalog()
    well_types = {w["tile_type"] for w in cat["wells"]}
    assert well_types == {"oil_well", "injection_well"}

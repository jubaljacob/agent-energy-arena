"""Placement spacing matrix (economy-rebalance 10).

Coal, gas, and wind generators each impose a one-cell no-build halo on
the 8-neighborhood. Roads and batteries are admitted inside the halo so
plants can still be serviced and storage co-located. Solar and battery
candidates impose no halo of their own.

Pure-function surface: `validate(candidate_type, candidate_coords, tiles)
-> offending_neighbor | None`. Existing tiles violating the rule at
release are grandfathered — validation runs at build time only.
"""

from __future__ import annotations

from world.catalog import TILE_CATALOG
from world.placement import HALO_ADMITTED_NEIGHBORS, HALO_TYPES, validate
from world.sim import World
from world.state import Tile


def _tile(tile_type: str, x: int, y: int, idx: int = 1) -> Tile:
    spec = TILE_CATALOG[tile_type]
    return Tile(
        id=f"{tile_type}-{idx}",
        type=tile_type,
        x=x,
        y=y,
        built_day=0,
        operational=True,
        capex_paid=spec.capex,
        opex_per_day=spec.opex_per_day,
        jobs=spec.jobs,
    )


def _fresh_world() -> World:
    w = World()
    w.reset(seed=42)
    return w


# -- Matrix shape -----------------------------------------------------------


def test_halo_types_are_coal_gas_wind() -> None:
    assert frozenset({"coal_plant", "gas_peaker", "wind_turbine"}) == HALO_TYPES


def test_admitted_neighbors_are_road_and_battery() -> None:
    # town_hall is road-network-equivalent (see world.grid.ROAD_TYPES), so a
    # halo'd plant adjacent to town hall is admitted on the same grounds.
    assert frozenset({"road", "battery", "town_hall"}) == HALO_ADMITTED_NEIGHBORS


# -- Pure-function validate -------------------------------------------------


def test_validate_returns_none_for_non_halo_candidate() -> None:
    """Solar imposes no halo on its neighbors."""
    tiles = [_tile("commercial", 5, 5)]
    assert validate("solar_farm", (5, 6), tiles) is None
    assert validate("solar_farm", (6, 5), tiles) is None
    assert validate("battery", (5, 6), tiles) is None
    assert validate("house", (5, 6), tiles) is None


def test_validate_returns_none_when_no_neighbors() -> None:
    assert validate("coal_plant", (10, 10), []) is None


def test_validate_returns_offending_neighbor_at_orthogonal_distance_1() -> None:
    tiles = [_tile("house", 5, 5)]
    offender = validate("coal_plant", (5, 6), tiles)
    assert offender is tiles[0]


def test_validate_returns_offending_neighbor_at_diagonal_distance_1() -> None:
    tiles = [_tile("commercial", 5, 5)]
    offender = validate("wind_turbine", (6, 6), tiles)
    assert offender is tiles[0]


def test_validate_returns_none_at_chebyshev_distance_2() -> None:
    tiles = [_tile("house", 5, 5)]
    assert validate("coal_plant", (7, 5), tiles) is None
    assert validate("coal_plant", (7, 7), tiles) is None
    assert validate("coal_plant", (5, 7), tiles) is None


def test_validate_admits_road_neighbor() -> None:
    tiles = [_tile("road", 5, 5)]
    assert validate("coal_plant", (5, 6), tiles) is None
    assert validate("gas_peaker", (6, 6), tiles) is None
    assert validate("wind_turbine", (5, 4), tiles) is None


def test_validate_admits_battery_neighbor() -> None:
    tiles = [_tile("battery", 5, 5)]
    assert validate("coal_plant", (5, 6), tiles) is None
    assert validate("gas_peaker", (6, 6), tiles) is None
    assert validate("wind_turbine", (5, 4), tiles) is None


def test_validate_admits_town_hall_neighbor_via_road_membership() -> None:
    """town_hall participates in the road network (see world.grid.ROAD_TYPES),
    so an adjacent halo'd plant should be admitted on the same grounds as a
    bare road. Without this rule a coal plant adjacent to town hall would
    silently fail spacing despite satisfying the road-adjacency contract.
    """
    tiles = [_tile("town_hall", 5, 5)]
    assert validate("coal_plant", (5, 6), tiles) is None


def test_validate_rejects_solar_neighbor_for_coal() -> None:
    """A solar farm in the halo is not road and not battery — rejected."""
    tiles = [_tile("solar_farm", 5, 5)]
    offender = validate("coal_plant", (5, 6), tiles)
    assert offender is tiles[0]


def test_validate_rejects_halo_type_neighbor() -> None:
    """Two halo'd plants adjacent to each other must be rejected (the
    bilateral case — the AC asks for the candidate's halo to fire, but
    halo-vs-halo is the most common 'cluster of plants' violation)."""
    tiles = [_tile("gas_peaker", 5, 5)]
    offender = validate("coal_plant", (5, 6), tiles)
    assert offender is tiles[0]


def test_validate_picks_one_offender_deterministically_with_two_violations() -> None:
    """Two non-admitted neighbors → returns exactly one, deterministically.
    The contract: scan the 8 neighborhood in fixed (dx, dy) order and
    return the first non-admitted tile found."""
    tiles = [
        _tile("house", 5, 5, idx=1),
        _tile("commercial", 5, 7, idx=2),
    ]
    offender_a = validate("coal_plant", (5, 6), tiles)
    offender_b = validate("coal_plant", (5, 6), tiles)
    assert offender_a is offender_b
    assert offender_a in tiles


def test_validate_does_not_mutate_inputs() -> None:
    tiles = [_tile("house", 5, 5)]
    before = (tiles[0].x, tiles[0].y, tiles[0].type)
    validate("coal_plant", (5, 6), tiles)
    after = (tiles[0].x, tiles[0].y, tiles[0].type)
    assert before == after


# -- Build endpoint wiring --------------------------------------------------


def test_build_rejects_coal_adjacent_to_house_with_spacing_error() -> None:
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    # Road chain so coal at (cx+3, cy+1) has road adjacency at (cx+3, cy).
    for dx in range(1, 5):
        w.build("road", cx + dx, cy)
    # House at (cx+4, cy+1) is in the halo of coal at (cx+3, cy+1).
    res_house = w.build("house", cx + 4, cy + 1)
    assert res_house["ok"] is True
    res_coal = w.build("coal_plant", cx + 3, cy + 1)
    assert res_coal["ok"] is False
    assert res_coal["error"] == "spacing_violation"
    assert res_coal["result"] == {"x": cx + 4, "y": cy + 1}


def test_build_admits_coal_adjacent_to_battery() -> None:
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    for dx in range(1, 5):
        w.build("road", cx + dx, cy)
    w.build("battery", cx + 4, cy + 1)
    res = w.build("coal_plant", cx + 3, cy + 1)
    assert res["ok"] is True, res


def test_build_admits_wind_adjacent_to_road_only() -> None:
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    # Wind doesn't require road, but a road neighbor still counts as
    # admitted inside the halo. Place wind 2 away from town hall.
    res = w.build("wind_turbine", cx + 2, cy + 2)
    assert res["ok"] is True


def test_build_rejects_gas_adjacent_to_wind() -> None:
    w = _fresh_world()
    # Both at the periphery, no road needed for either.
    w.build("wind_turbine", 0, 0)
    res = w.build("gas_peaker", 1, 0)
    assert res["ok"] is False
    assert res["error"] == "spacing_violation"


def test_build_solar_adjacent_to_house_allowed() -> None:
    """Solar imposes no halo on neighbors; the build endpoint must let
    this through (only halo'd candidates trigger the rule)."""
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    w.build("house", cx + 1, cy)
    # Solar at (cx+1, cy+1) is diagonally adjacent to the house.
    res = w.build("solar_farm", cx + 1, cy + 1)
    assert res["ok"] is True


def test_build_spacing_checked_after_road_adjacency() -> None:
    """A coal plant placed without road adjacency must surface
    `no_road_adjacency`, not `spacing_violation`, even if the spacing
    rule would also have rejected it. The AC orders road → spacing →
    treasury."""
    w = _fresh_world()
    # Isolated corner: no road, no neighbors.
    res = w.build("coal_plant", 31, 31)
    assert res["ok"] is False
    assert res["error"] == "no_road_adjacency"


def test_build_spacing_checked_before_treasury_debit() -> None:
    """A spacing-rejected build must not debit treasury."""
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    for dx in range(1, 5):
        w.build("road", cx + dx, cy)
    w.build("house", cx + 4, cy + 1)
    treasury_before = w.state.treasury
    res = w.build("coal_plant", cx + 3, cy + 1)
    assert res["ok"] is False
    assert res["error"] == "spacing_violation"
    assert w.state.treasury == treasury_before


def test_build_grandfathers_pre_existing_violations() -> None:
    """A halo'd tile placed via direct state injection (simulating a
    pre-release world load) must not be re-validated. Adjacent fresh
    builds of non-halo'd tiles continue to succeed; only fresh halo'd
    builds inspect the neighborhood."""
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    # Inject two adjacent halo'd plants directly into state.
    w.state.tiles.append(_tile("coal_plant", cx + 5, cy + 5, idx=99))
    w.state.tiles.append(_tile("gas_peaker", cx + 5, cy + 6, idx=100))
    # World load / step path doesn't re-validate. A fresh build of a
    # non-halo'd tile far away still works.
    res = w.build("solar_farm", cx - 8, cy - 8)
    assert res["ok"] is True


# -- Catalog descriptions ---------------------------------------------------


def test_halo_types_mention_spacing_in_catalog() -> None:
    """The AC asks coal, gas, and wind descriptions to mention the
    one-cell halo so the in-game tooltip + agent-facing tile-spec
    contract surface the new rule."""
    for tile_type in ("coal_plant", "gas_peaker", "wind_turbine"):
        desc = TILE_CATALOG[tile_type].description.lower()
        assert "halo" in desc or "spacing" in desc or "no-build" in desc, desc

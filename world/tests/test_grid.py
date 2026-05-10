"""Tile placement, adjacency flood-fill, and treasury accounting."""

from __future__ import annotations

import pytest

from world.grid import road_connected_set
from world.sim import World


def _fresh_world() -> World:
    w = World()
    w.reset(seed=42)
    return w


# -- Adjacency flood-fill ----------------------------------------------------


def test_town_hall_counts_as_road():
    """A house orthogonally adjacent to the town hall (without any roads) is valid."""
    w = _fresh_world()
    cx = w.config.world_w // 2
    cy = w.config.world_h // 2
    res = w.build("house", cx + 1, cy)
    assert res["ok"] is True, res
    assert any(t.type == "house" and t.x == cx + 1 and t.y == cy for t in w.state.tiles)


def test_road_chain_extends_network():
    """A house adjacent to a road that is itself connected (via roads) to the town hall is valid."""
    w = _fresh_world()
    cx = w.config.world_w // 2
    cy = w.config.world_h // 2
    # Lay roads stepping east of town hall.
    for dx in range(1, 4):
        r = w.build("road", cx + dx, cy)
        assert r["ok"] is True, r
    # House adjacent to the far end of the road chain.
    res = w.build("house", cx + 3, cy + 1)
    assert res["ok"] is True


def test_house_without_road_adjacency_rejected():
    w = _fresh_world()
    res = w.build("house", 0, 0)  # corner; town hall is at center.
    assert res["ok"] is False
    assert res["error"] == "no_road_adjacency"
    # World unchanged.
    assert all(t.type != "house" for t in w.state.tiles)


def test_island_road_does_not_count_as_network():
    """A road not connected to the town hall via roads cannot anchor a house."""
    w = _fresh_world()
    # Place a single isolated road in the corner.
    res_road = w.build("road", 0, 0)
    assert res_road["ok"] is True
    # House next to that island road should be rejected: the road is not
    # connected to the town hall network.
    res_house = w.build("house", 1, 0)
    assert res_house["ok"] is False
    assert res_house["error"] == "no_road_adjacency"


def test_road_connected_set_includes_town_hall_only_at_start():
    w = _fresh_world()
    cx = w.config.world_w // 2
    cy = w.config.world_h // 2
    s = road_connected_set(w.state.tiles, w.config.world_w, w.config.world_h)
    assert (cx, cy) in s
    assert len(s) == 1


def test_park_does_not_require_road_adjacency():
    w = _fresh_world()
    res = w.build("park", 0, 0)
    assert res["ok"] is True


def test_pipeline_does_not_require_road_adjacency():
    w = _fresh_world()
    res = w.build("pipeline", 5, 5)
    assert res["ok"] is True


# -- Treasury accounting -----------------------------------------------------


def test_build_deducts_capex():
    w = _fresh_world()
    treasury_before = w.state.treasury
    res = w.build("road", 16, 17)
    assert res["ok"] is True
    assert w.state.treasury == treasury_before - 500
    assert res["treasury_after"] == w.state.treasury


def test_insufficient_funds_rejected_world_unchanged():
    w = _fresh_world()
    w.state.treasury = 100  # less than road CAPEX.
    n_tiles = len(w.state.tiles)
    res = w.build("road", 16, 17)
    assert res["ok"] is False
    assert res["error"] == "insufficient_funds"
    assert w.state.treasury == 100
    assert len(w.state.tiles) == n_tiles


def test_tile_occupied_rejected():
    w = _fresh_world()
    cx = w.config.world_w // 2
    cy = w.config.world_h // 2
    # Town hall is at (cx, cy).
    res = w.build("road", cx, cy)
    assert res["ok"] is False
    assert res["error"] == "tile_occupied"
    # And a freshly placed road is also occupied.
    w.build("road", cx + 1, cy)
    again = w.build("road", cx + 1, cy)
    assert again["ok"] is False
    assert again["error"] == "tile_occupied"


def test_unknown_tile_type_rejected():
    w = _fresh_world()
    res = w.build("not_a_tile", 1, 1)
    assert res["ok"] is False
    assert res["error"] == "unknown_tile_type"


def test_build_oil_well_rejected_via_build_endpoint():
    """Wells are exclusively created via /drill (PRD)."""
    w = _fresh_world()
    res = w.build("oil_well", 1, 1)
    assert res["ok"] is False
    assert res["error"] == "unknown_tile_type"


def test_out_of_bounds_rejected():
    w = _fresh_world()
    res = w.build("road", -1, 0)
    assert res["ok"] is False
    assert res["error"] == "out_of_bounds"
    res = w.build("road", w.config.world_w, 0)
    assert res["ok"] is False
    assert res["error"] == "out_of_bounds"


# -- Demolition --------------------------------------------------------------


def test_demolish_refunds_25_percent():
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    # Build a house ($3000) next to the town hall.
    w.build("house", cx + 1, cy)
    treasury_after_build = w.state.treasury
    res = w.demolish(cx + 1, cy)
    assert res["ok"] is True
    assert w.state.treasury == pytest.approx(treasury_after_build + 0.25 * 3000)


def test_demolish_empty_tile_rejected():
    w = _fresh_world()
    res = w.demolish(0, 0)
    assert res["ok"] is False
    assert res["error"] == "no_tile"


def test_demolish_townhall_rejected():
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    res = w.demolish(cx, cy)
    assert res["ok"] is False
    assert res["error"] == "cannot_demolish_townhall"
    # Town hall is still there.
    assert any(t.type == "town_hall" for t in w.state.tiles)


# -- Reset -------------------------------------------------------------------


def test_reset_places_town_hall_at_center():
    w = _fresh_world()
    cx = w.config.world_w // 2
    cy = w.config.world_h // 2
    halls = [t for t in w.state.tiles if t.type == "town_hall"]
    assert len(halls) == 1
    th = halls[0]
    assert th.x == cx and th.y == cy
    assert th.housing_capacity == 100
    assert th.jobs == 30


def test_reset_clears_previous_tiles():
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    w.build("road", cx + 1, cy)
    w.build("house", cx + 2, cy)
    w.reset(seed=42)
    # Only the town hall remains.
    assert len(w.state.tiles) == 1
    assert w.state.tiles[0].type == "town_hall"


# -- Daily OPEX accrual ------------------------------------------------------


def test_daily_opex_deducted_during_step():
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    # House: $20/day OPEX. Road: $0.
    w.build("road", cx + 1, cy)
    w.build("house", cx + 2, cy)
    treasury_before = w.state.treasury
    w.step(days=1)
    # OPEX = 20 (house) + 0 (road) + 0 (town_hall) = 20.
    assert w.state.treasury == pytest.approx(treasury_before - 20.0)


def test_daily_opex_summary_field_populated():
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    w.build("road", cx + 1, cy)
    w.build("house", cx + 2, cy)  # $20/day
    summary = w.step(days=3)
    # 3 days × $20 = $60.
    assert summary.summary["delta"] == pytest.approx(-60.0)


def test_step_size_invariance_with_tiles():
    """Adding tiles must not break the determinism contract from slice 01."""
    a = World()
    a.reset(seed=42)
    cx, cy = a.config.world_w // 2, a.config.world_h // 2
    a.build("road", cx + 1, cy)
    a.build("house", cx + 2, cy)
    a.step(days=7)

    b = World()
    b.reset(seed=42)
    b.build("road", cx + 1, cy)
    b.build("house", cx + 2, cy)
    for _ in range(7):
        b.step(days=1)

    assert a.state.treasury == b.state.treasury
    assert a.state.day == b.state.day == 7
    # And both RNG streams match.
    assert a.sim_rng.standard_normal() == b.sim_rng.standard_normal()

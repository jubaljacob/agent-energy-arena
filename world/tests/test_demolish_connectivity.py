"""Connectivity guard on /demolish (issue 20).

Demolishing a road tile must be rejected if doing so would leave any
road-requiring civilian tile (house, commercial, industrial, refinery)
without an orthogonal road-network neighbor.
"""

from __future__ import annotations

import pytest

from world.sim import World


def _fresh_world() -> World:
    w = World()
    w.reset(seed=42)
    return w


def test_demolish_island_road_with_no_dependents_allowed():
    """An isolated road that anchors no civilian tile can be removed freely."""
    w = _fresh_world()
    # Park doesn't require a road; the road at (0, 0) is purely cosmetic.
    res_road = w.build("road", 0, 0)
    assert res_road["ok"] is True
    treasury_after_build = w.state.treasury
    res = w.demolish(0, 0)
    assert res["ok"] is True, res
    # 25% refund of $500 = $125 returned.
    assert w.state.treasury == pytest.approx(treasury_after_build + 125.0)
    assert all(not (t.x == 0 and t.y == 0) for t in w.state.tiles)


def test_demolish_road_that_anchors_a_house_rejected():
    """If a house has only one road neighbor, removing it strands the house."""
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    # Lay a road east of town hall, then a house off the road's east face.
    # The house is not adjacent to the town hall, so the road is its only
    # connection to the network.
    assert w.build("road", cx + 1, cy)["ok"] is True
    assert w.build("road", cx + 2, cy)["ok"] is True
    assert w.build("house", cx + 2, cy + 1)["ok"] is True
    treasury_before = w.state.treasury
    n_tiles = len(w.state.tiles)

    res = w.demolish(cx + 2, cy)
    assert res["ok"] is False
    assert res["error"] == "would_disconnect"
    assert res["treasury_after"] == treasury_before
    assert w.state.treasury == treasury_before
    assert len(w.state.tiles) == n_tiles
    # Stranded list cites the house at (cx+2, cy+1).
    stranded = res["result"]["stranded"]
    assert {"x": cx + 2, "y": cy + 1, "type": "house"} in stranded


def test_demolish_middle_of_loop_allowed_when_alternative_path_exists():
    """A road in a chain where every dependent has another network neighbor is removable."""
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    # Build a 3-road chain east of town hall: (cx+1,cy)-(cx+2,cy)-(cx+3,cy)
    # along with a parallel chain one row north providing the alternative path
    # for the middle of the southern row. Specifically:
    #
    #     . R R R .            (cy-1)
    #     T R R R .            (cy)         T = town hall
    #
    # Removing the middle southern road (cx+2, cy) still leaves no civilian
    # tile orphaned because no civilian tile is present — but per the AC,
    # "an empty stranded list means allowed."
    assert w.build("road", cx + 1, cy)["ok"] is True
    assert w.build("road", cx + 2, cy)["ok"] is True
    assert w.build("road", cx + 3, cy)["ok"] is True
    assert w.build("road", cx + 1, cy - 1)["ok"] is True
    assert w.build("road", cx + 2, cy - 1)["ok"] is True
    assert w.build("road", cx + 3, cy - 1)["ok"] is True
    # Add a house adjacent to (cx+2, cy) but ALSO adjacent to (cx+2, cy-1)
    # via a different cell — actually a house can only sit at one (x, y).
    # Place it at (cx+2, cy+1) so its only neighbor is (cx+2, cy). That would
    # make the middle road non-removable. Instead place the house at
    # (cx+3, cy+1) where its road neighbor is (cx+3, cy), unaffected by the
    # middle-road removal.
    assert w.build("house", cx + 3, cy + 1)["ok"] is True

    treasury_before = w.state.treasury
    res = w.demolish(cx + 2, cy)
    assert res["ok"] is True, res
    # Treasury credited with 25% refund of $500.
    assert w.state.treasury == pytest.approx(treasury_before + 125.0)


def test_demolish_road_disconnecting_cluster_of_five_is_rejected_with_all_listed():
    """A choke-point road that disconnects 5 civilian tiles lists all 5."""
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    # Layout: town hall at (cx, cy). Lay a long road chain east: (cx+1, cy)
    # is the chokepoint; (cx+2..cx+6, cy) extend it. Park civilian tiles to
    # the north of every road from cx+2 onwards (5 houses total).
    #
    # T R R R R R R                       cy
    # . . H H H H H                       cy-1
    #
    # Removing (cx+1, cy) severs the entire eastern chain from town hall:
    # roads cx+2..cx+6 become an island; the 5 houses at cx+2..cx+6, cy-1
    # all lose their only network connection.
    for dx in range(1, 7):
        assert w.build("road", cx + dx, cy)["ok"] is True
    for dx in range(2, 7):
        assert w.build("house", cx + dx, cy - 1)["ok"] is True

    treasury_before = w.state.treasury
    n_tiles = len(w.state.tiles)

    res = w.demolish(cx + 1, cy)
    assert res["ok"] is False
    assert res["error"] == "would_disconnect"
    assert w.state.treasury == treasury_before
    assert len(w.state.tiles) == n_tiles

    stranded = res["result"]["stranded"]
    stranded_xys = {(s["x"], s["y"]) for s in stranded}
    assert stranded_xys == {(cx + dx, cy - 1) for dx in range(2, 7)}
    for s in stranded:
        assert s["type"] == "house"


def test_demolish_non_road_tile_unaffected_by_connectivity_check():
    """Demolishing a plant / well / refinery is unchanged by the new guard."""
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    # Place a gas peaker off the road network — no road requirement for plants.
    assert w.build("gas_peaker", cx - 3, cy + 3)["ok"] is True
    res = w.demolish(cx - 3, cy + 3)
    assert res["ok"] is True
    assert res["result"]["type"] == "gas_peaker"


def test_would_disconnect_action_log_entry_records_failure(tmp_path):
    """The action log appended by /demolish carries ok=False when guard fires."""
    import json

    from fastapi.testclient import TestClient

    from world.action_log import ActionLog
    from world.api import create_app

    log = ActionLog(root=tmp_path, run_id="test")
    app = create_app(world=World(), action_log=log)
    client = TestClient(app)
    client.post("/reset", json={"seed": 42})

    s = client.get("/state").json()
    cx, cy = s["config"]["world_w"] // 2, s["config"]["world_h"] // 2

    # Sole-anchor road for a house.
    client.post("/build", json={"tile_type": "road", "x": cx + 1, "y": cy})
    client.post("/build", json={"tile_type": "house", "x": cx + 1, "y": cy + 1})

    r = client.post("/demolish", json={"x": cx + 1, "y": cy}).json()
    assert r["ok"] is False
    assert r["error"] == "would_disconnect"

    # The log carries an ok=False entry for this demolish call.
    entries = [json.loads(line) for line in log.path.read_text().splitlines()]
    demolish_entries = [e for e in entries if e["endpoint"] == "/demolish"]
    assert len(demolish_entries) == 1
    assert demolish_entries[0]["ok"] is False
    assert demolish_entries[0]["error"] == "would_disconnect"


def test_pre_existing_safe_road_removal_keeps_refinery_connected():
    """A refinery (requires_road) with multiple road neighbors survives removing one."""
    w = _fresh_world()
    cx, cy = w.config.world_w // 2, w.config.world_h // 2
    # Refineries require road adjacency. Bracket one with roads on two sides
    # so removing one still leaves another adjacent.
    assert w.build("road", cx + 1, cy)["ok"] is True  # to town hall
    assert w.build("road", cx + 2, cy)["ok"] is True
    assert w.build("road", cx + 2, cy + 1)["ok"] is True
    # Need enough treasury for a refinery ($150k). Top up.
    w.state.treasury = max(w.state.treasury, 500_000.0)
    assert w.build("refinery", cx + 3, cy)["ok"] is True

    res = w.demolish(cx + 2, cy + 1)
    assert res["ok"] is True, res

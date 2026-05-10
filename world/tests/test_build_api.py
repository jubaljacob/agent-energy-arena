"""HTTP-level coverage of /build, /demolish, /catalog, /state.tiles."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from world.action_log import ActionLog
from world.api import create_app
from world.sim import World


def _client(tmp_path: Path) -> tuple[TestClient, ActionLog]:
    log = ActionLog(root=tmp_path / "runs")
    app = create_app(world=World(), action_log=log)
    return TestClient(app), log


def test_catalog_lists_civilian_tiles(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    catalog = client.get("/catalog").json()
    types = {entry["tile_type"] for entry in catalog["tiles"]}
    # Civilian tiles required by issue 02.
    for required in ("road", "house", "commercial", "industrial", "park", "pipeline"):
        assert required in types, types
    by_type = {entry["tile_type"]: entry for entry in catalog["tiles"]}
    assert by_type["road"]["capex"] == 500
    assert by_type["house"]["capex"] == 3000
    assert by_type["house"]["requires_road"] is True
    assert by_type["park"]["requires_road"] is False
    assert "description" in by_type["road"]


def test_state_tiles_lists_town_hall_after_reset(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    client.post("/reset", json={"seed": 42})
    s = client.get("/state").json()
    halls = [t for t in s["tiles"] if t["type"] == "town_hall"]
    assert len(halls) == 1
    th = halls[0]
    assert th["x"] == s["config"]["world_w"] // 2
    assert th["y"] == s["config"]["world_h"] // 2
    assert "id" in th
    assert "built_day" in th
    assert th["operational"] is True


def test_build_road_and_demolish_round_trip(tmp_path: Path) -> None:
    client, log = _client(tmp_path)
    client.post("/reset", json={"seed": 42})
    cx = 16
    cy = 16
    treasury0 = client.get("/state").json()["treasury"]

    r = client.post("/build", json={"tile_type": "road", "x": cx + 1, "y": cy})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["treasury_after"] == treasury0 - 500

    # Demolish.
    r = client.post("/demolish", json={"x": cx + 1, "y": cy})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # 25% refund of 500 = 125.
    assert body["treasury_after"] == treasury0 - 500 + 125

    # Action log captured both calls.
    entries = [json.loads(line) for line in log.path.read_text().splitlines()]
    endpoints = [e["endpoint"] for e in entries]
    assert "/build" in endpoints
    assert "/demolish" in endpoints


def test_build_rejection_returns_200_with_error_and_logs(tmp_path: Path) -> None:
    client, log = _client(tmp_path)
    client.post("/reset", json={"seed": 42})
    # House without road adjacency.
    r = client.post("/build", json={"tile_type": "house", "x": 0, "y": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "no_road_adjacency"

    entries = [json.loads(line) for line in log.path.read_text().splitlines()]
    build_failures = [e for e in entries if e["endpoint"] == "/build" and e["ok"] is False]
    assert len(build_failures) == 1
    assert build_failures[0]["error"] == "no_road_adjacency"


def test_build_townhall_via_endpoint_rejected(tmp_path: Path) -> None:
    """`town_hall` is auto-placed; not buildable via /build."""
    client, _ = _client(tmp_path)
    client.post("/reset", json={"seed": 42})
    r = client.post("/build", json={"tile_type": "town_hall", "x": 0, "y": 0})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["error"] == "unknown_tile_type"

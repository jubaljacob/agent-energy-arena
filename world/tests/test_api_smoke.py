"""End-to-end smoke test: boot the API and walk reset → step → state → reset."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from world.action_log import ActionLog
from world.api import create_app
from world.sim import World
from world.subsurface import SEISMIC_DEFAULT_SIZE


def _client(tmp_path: Path) -> tuple[TestClient, ActionLog]:
    log = ActionLog(root=tmp_path / "runs")
    app = create_app(world=World(), action_log=log)
    return TestClient(app), log


def test_smoke_reset_step_state_reset(tmp_path: Path) -> None:
    client, log = _client(tmp_path)

    # Reset to a known seed.
    r = client.post("/reset", json={"seed": 42})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # /seed returns the active seed.
    assert client.get("/seed").json() == {"seed": 42}

    # /state on a fresh world matches the spec defaults.
    s = client.get("/state").json()
    assert s["day"] == 0
    assert s["treasury"] == 300_000
    assert s["population"] == 100
    # Town hall (slice 02) is auto-placed at the world center on /reset.
    assert [t["type"] for t in s["tiles"]] == ["town_hall"]
    assert s["wells"] == []
    assert s["config"]["world_w"] == 32
    assert s["config"]["world_h"] == 32
    assert s["config"]["game_days"] == 3650
    assert s["config"]["manual_game_days"] == 365
    assert s["config"]["ticks_per_day"] == 24

    # /step advances day.
    r = client.post("/step", json={"days": 7})
    assert r.status_code == 200
    assert r.json()["day_completed"] == 7

    # /step rejects out-of-range days with 422 (Pydantic validation).
    r = client.post("/step", json={"days": 8})
    assert r.status_code == 422

    # /forecast returns a list of records of the requested length.
    r = client.get("/forecast", params={"hours": 24})
    assert r.status_code == 200
    payload = r.json()
    assert isinstance(payload, list) and len(payload) == 24
    assert payload[0]["hour_offset"] == 0
    assert {"solar_irradiance", "wind_speed_mps", "demand_factor", "sigma"} <= set(
        payload[0].keys()
    )

    # /catalog is wired (empty in this slice).
    r = client.get("/catalog")
    assert r.status_code == 200
    assert "tiles" in r.json()

    # Reset back to day 0.
    client.post("/reset", json={"seed": 42})
    assert client.get("/state").json()["day"] == 0

    # Action log must contain entries for every mutating call.
    lines = log.path.read_text().splitlines()
    assert len(lines) >= 3
    entries = [json.loads(line) for line in lines]
    endpoints = [e["endpoint"] for e in entries]
    assert "/reset" in endpoints
    assert "/step" in endpoints


def test_step_failure_is_logged(tmp_path: Path) -> None:
    client, log = _client(tmp_path)
    client.post("/reset", json={"seed": 1})
    # Pydantic rejects days=0 before reaching the endpoint, so use the World
    # directly to confirm rejected calls are still logged from the API path.
    # Use a body the validator accepts but the world rejects: monkey-patch.
    # Easiest: bypass validation by sending days at the top of the legal range
    # then expect failure-free behavior. To exercise the failure-log path,
    # call the world's underlying method through a malformed JSON body.
    r = client.post("/step", json={"days": -1})
    assert r.status_code == 422
    # Pydantic rejection happens before our handler, so it's not logged here.
    # The failure-log path is exercised when world.step() raises after the
    # body is accepted — see test_world_step_failure_logged.


def test_post_survey_size_field_is_optional_and_defaults_to_seismic_default(
    tmp_path: Path,
) -> None:
    """Issue 21 + oilfield-v2 §"Survey rescale": `size` is optional on POST
    /survey; the server uses SEISMIC_DEFAULT_SIZE (now 4) when it's omitted.
    The voxel count must match an explicit `{size: SEISMIC_DEFAULT_SIZE}`
    request when called from a fresh world at the same anchor."""
    client_a, _ = _client(tmp_path / "a")
    client_b, _ = _client(tmp_path / "b")
    client_a.post("/reset", json={"seed": 42})
    client_b.post("/reset", json={"seed": 42})

    r_default = client_a.post("/survey", json={"x": 16, "y": 16})
    r_explicit = client_b.post("/survey", json={"x": 16, "y": 16, "size": SEISMIC_DEFAULT_SIZE})
    assert r_default.status_code == 200
    assert r_explicit.status_code == 200
    body_default = r_default.json()
    body_explicit = r_explicit.json()
    assert body_default["ok"] is True
    assert body_explicit["ok"] is True
    assert body_default["result"]["size"] == SEISMIC_DEFAULT_SIZE
    assert body_default["result"]["cost"] == body_explicit["result"]["cost"]
    assert len(body_default["result"]["voxels"]) == len(body_explicit["result"]["voxels"])


def test_world_step_failure_logged(tmp_path: Path) -> None:
    """If the world rejects a step that passed body validation, the rejection
    must still be appended to the action log."""
    log = ActionLog(root=tmp_path / "runs")
    world = World()

    # Patch the world to raise on step.
    def boom(days: int = 7) -> None:
        raise ValueError("synthetic failure")

    world.step = boom  # type: ignore[assignment]
    app = create_app(world=world, action_log=log)
    client = TestClient(app)
    r = client.post("/step", json={"days": 7})
    assert r.status_code == 400

    entries = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert any(e["endpoint"] == "/step" and e["ok"] is False for e in entries)


def test_state_history_returns_recorded_day(tmp_path: Path) -> None:
    """`GET /state/history?day=N` returns the recorder entry for that day.

    The UI's "previous day" peek in live mode reads from this endpoint to
    render past days without moving the server's simulation state.
    """
    log = ActionLog(root=tmp_path / "runs")
    world = World(runs_root=str(tmp_path / "runs"))
    app = create_app(world=world, action_log=log)
    client = TestClient(app)

    client.post("/reset", json={"seed": 42})
    # Step a few days so the recorder has multiple entries to choose from.
    client.post("/step", json={"days": 3})

    # Recorder writes day N before incrementing state.day to N+1, so after
    # three steps the entries are for days 0, 1, 2. The embedded
    # state_dict() also shows the just-completed day (pre-increment).
    for d in (0, 1, 2):
        r = client.get("/state/history", params={"day": d})
        assert r.status_code == 200, r.text
        entry = r.json()
        assert entry["day"] == d
        assert "state" in entry and "summary" in entry
        assert entry["state"]["day"] == d

    # Day 99 was never recorded.
    r = client.get("/state/history", params={"day": 99})
    assert r.status_code == 404


def test_reset_with_empty_body_preserves_seed_and_resets_day(tmp_path: Path) -> None:
    """The UI's top-bar Reset button POSTs `/reset` with an empty body. The
    backend must treat the absent `seed` field as "preserve the active seed"
    and still drop the day counter to 0."""
    client, _ = _client(tmp_path)

    client.post("/reset", json={"seed": 123})
    client.post("/step", json={"days": 5})
    assert client.get("/state").json()["day"] == 5
    seed_before = client.get("/seed").json()["seed"]

    r = client.post("/reset", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["result"]["day"] == 0
    assert body["result"]["seed"] == seed_before

    assert client.get("/state").json()["day"] == 0
    assert client.get("/seed").json()["seed"] == seed_before


def test_state_history_404s_without_recorder(tmp_path: Path) -> None:
    """When the world has no recorder (tests, embedded use), /state/history
    returns 404 with a clear detail string rather than raising 500."""
    log = ActionLog(root=tmp_path / "runs")
    world = World()  # no runs_root → recorder is None
    app = create_app(world=world, action_log=log)
    client = TestClient(app)
    r = client.get("/state/history", params={"day": 0})
    assert r.status_code == 404
    assert "no recorded history" in r.json()["detail"]

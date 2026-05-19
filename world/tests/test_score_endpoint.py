"""End-to-end tests for the repurposed `GET /score` endpoint.

The endpoint reads the active recorder's per-day JSONL log from disk
and returns the `compute_score` result. Empty recorder / missing file /
empty file all return the empty-response payload with HTTP 200; never
404. See `.scratch/scoring/PRD.md` for the contract.

The pure-function formula tests live in `test_scoring.py`;
this file pins only the HTTP wiring and the on-disk-log read path.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from world.api import create_app
from world.sim import World

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


def test_score_endpoint_after_seven_steps_returns_full_breakdown(tmp_path: Path):
    w = World(runs_root=str(tmp_path))
    app = create_app(world=w)
    client = TestClient(app)
    client.post("/reset", json={"seed": 42})
    client.post("/step", json={"days": 7})
    r = client.get("/score")
    assert r.status_code == 200
    body = r.json()
    assert body["n_days"] == 7
    assert 0.0 <= body["score"] <= 100.0
    assert set(body["components"].keys()) == COMPONENT_KEYS


def test_score_endpoint_after_reset_with_no_steps_returns_empty_payload(tmp_path: Path):
    """Reset allocates a fresh recorder but writes no state lines yet —
    the per-day JSONL is empty, so /score returns the empty payload
    with HTTP 200."""
    w = World(runs_root=str(tmp_path))
    app = create_app(world=w)
    client = TestClient(app)
    client.post("/reset", json={"seed": 42})
    r = client.get("/score")
    assert r.status_code == 200
    assert r.json() == {"n_days": 0, "score": 0.0, "components": {}}


def test_score_endpoint_without_recorder_returns_empty_payload():
    """A test fixture world constructed with `runs_root=None` has no
    recorder. /score must return the empty payload, not 500."""
    w = World(runs_root=None)
    app = create_app(world=w)
    client = TestClient(app)
    r = client.get("/score")
    assert r.status_code == 200
    assert r.json() == {"n_days": 0, "score": 0.0, "components": {}}


def test_score_endpoint_with_empty_states_file_returns_empty_payload(tmp_path: Path):
    """If the recorder exists but `states.jsonl` is present-and-empty
    (no completed days yet, edge case), the handler still returns the
    empty payload."""
    w = World(runs_root=str(tmp_path))
    app = create_app(world=w)
    client = TestClient(app)
    # Recorder created at construction; force a freshly-empty states.jsonl
    # by touching the file without writing any lines.
    assert w.recorder is not None
    w.recorder.states_path.write_text("")
    r = client.get("/score")
    assert r.status_code == 200
    assert r.json() == {"n_days": 0, "score": 0.0, "components": {}}


def test_score_endpoint_reads_log_from_disk_not_in_memory_state(tmp_path: Path):
    """Mutating in-memory `WorldState` after the recorder has written
    lines must not change the /score response — the formula reads from
    disk, not from memory."""
    w = World(runs_root=str(tmp_path))
    app = create_app(world=w)
    client = TestClient(app)
    client.post("/reset", json={"seed": 42})
    client.post("/step", json={"days": 3})
    first = client.get("/score").json()
    # Stomp on in-memory state. The disk log is unchanged.
    w.state.treasury = -1e12
    w.state.population = -1e12
    w.state.happiness = -1e12
    second = client.get("/score").json()
    assert first == second

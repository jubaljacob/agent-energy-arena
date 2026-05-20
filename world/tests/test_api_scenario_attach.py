"""API scenario attach + inspect + run-folder id (open-source-arena slice 04).

ACs covered:
  * POST /scenario attaches by dotted path, returns the resolved name,
    400 on a bad path, writes to the action log on both branches.
  * GET /scenario returns the currently-attached dotted path (None for
    NullScenario).
  * GET /run returns the recorder's run_id + dir, or None/None when the
    world has no recorder (test default).
  * POST /reset accepts an optional `scenario` field; invalid path
    surfaces 400 + log; valid path attaches.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from world.action_log import ActionLog
from world.api import create_app
from world.scenario import NullScenario
from world.sim import World

FIXTURE_PATH = "world.tests.scenario_fixture"


def _client(tmp_path: Path) -> tuple[TestClient, FastAPI, World, ActionLog]:
    world = World(runs_root=str(tmp_path / "runs"))
    run_id = world.recorder.run_id if world.recorder is not None else None
    log = ActionLog(root=str(tmp_path / "runs"), run_id=run_id)
    app = create_app(world=world, action_log=log, runs_root=str(tmp_path / "runs"))
    return TestClient(app), app, world, log


# -- GET /scenario ----------------------------------------------------------


def test_get_scenario_returns_null_on_fresh_world(tmp_path: Path) -> None:
    client, _app, _world, _log = _client(tmp_path)
    r = client.get("/scenario")
    assert r.status_code == 200
    assert r.json() == {"dotted_path": None, "description": None, "source": None}


# -- GET /scenarios ---------------------------------------------------------


def test_get_scenarios_returns_discovered_dotted_paths(tmp_path: Path) -> None:
    """Symmetric to `GET /agent/folders`: returns `{scenarios: [...]}`
    enumerated under the repo's `scenarios/` package. `scenarios.baseline`
    is the canonical shipped scenario and must always appear."""
    client, _app, _world, _log = _client(tmp_path)
    r = client.get("/scenarios")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"scenarios"}
    assert isinstance(body["scenarios"], list)
    assert "scenarios.baseline" in body["scenarios"]


# -- POST /scenario ---------------------------------------------------------


def test_post_scenario_attaches_by_dotted_path(tmp_path: Path) -> None:
    client, _app, world, log = _client(tmp_path)

    r = client.post("/scenario", json={"dotted_path": FIXTURE_PATH})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "dotted_path": FIXTURE_PATH}
    assert not isinstance(world.scenario, NullScenario)
    assert world.scenario_dotted_path == FIXTURE_PATH

    # GET /scenario now surfaces the attached path. TraceScenario has no
    # class docstring, so `description` is null. `source` is the
    # fixture's full module file so the UI can render it in the source
    # box — assert the bytes match what's on disk.
    fixture_path = Path(__file__).resolve().parent / "scenario_fixture.py"
    expected_source = fixture_path.read_text(encoding="utf-8")
    assert client.get("/scenario").json() == {
        "dotted_path": FIXTURE_PATH,
        "description": None,
        "source": expected_source,
    }

    # Action log captured the success.
    entries = [json.loads(line) for line in log.path.read_text().splitlines()]
    attach = [e for e in entries if e["endpoint"] == "/scenario"]
    assert len(attach) == 1
    assert attach[0]["ok"] is True
    assert attach[0]["params"] == {"dotted_path": FIXTURE_PATH}


def test_get_scenario_surfaces_class_docstring_as_description(tmp_path: Path) -> None:
    """When the attached scenario class has a docstring, GET /scenario
    returns it stripped as `description`. The UI prints this as the
    scenario's "plan" once loaded. `scenarios.baseline` extends
    NullScenario, so we use a non-Null shipped scenario here. `source`
    is the full module file so the UI source box can show what the
    scenario actually does."""
    client, _app, _world, _log = _client(tmp_path)
    r = client.post("/scenario", json={"dotted_path": "scenarios.grid_stress"})
    assert r.status_code == 200, r.text
    body = client.get("/scenario").json()
    assert body["dotted_path"] == "scenarios.grid_stress"
    assert isinstance(body["description"], str)
    assert body["description"].startswith("Stress the grid")
    assert isinstance(body["source"], str)
    assert "class GridStress" in body["source"]


def test_post_scenario_bad_path_returns_400_and_logs(tmp_path: Path) -> None:
    client, _app, _world, log = _client(tmp_path)

    r = client.post("/scenario", json={"dotted_path": "no.such.module._xyz"})
    assert r.status_code == 400
    assert "no.such.module._xyz" in r.json()["detail"]

    entries = [json.loads(line) for line in log.path.read_text().splitlines()]
    failed = [e for e in entries if e["endpoint"] == "/scenario"]
    assert len(failed) == 1
    assert failed[0]["ok"] is False
    assert "no.such.module._xyz" in failed[0]["error"]


def test_post_scenario_module_without_subclass_returns_400(tmp_path: Path) -> None:
    client, _app, _world, _log = _client(tmp_path)
    # `world.scenario` itself only re-exports Scenario/NullScenario; the
    # loader rejects it.
    r = client.post("/scenario", json={"dotted_path": "world.scenario"})
    assert r.status_code == 400


# -- POST /reset with scenario ---------------------------------------------


def test_reset_with_scenario_field(tmp_path: Path) -> None:
    client, app, world, _log = _client(tmp_path)

    r = client.post("/reset", json={"seed": 42, "scenario": FIXTURE_PATH})
    assert r.status_code == 200
    assert not isinstance(world.scenario, NullScenario)
    assert world.scenario_dotted_path == FIXTURE_PATH

    # Recorder metadata reflects the attached scenario at reset time.
    # Recorder allocation is now lazy — step once so the new
    # recorder writes metadata.json before we read it.
    assert world.recorder is not None
    client.post("/step", json={"days": 1})
    meta = json.loads(world.recorder.metadata_path.read_text())
    assert meta["scenario"] == FIXTURE_PATH

    # /reset rebinds the action log to the new recorder's folder, so
    # the captured entry lives at the post-reset log path.
    active_log = app.state.action_log
    entries = [json.loads(line) for line in active_log.path.read_text().splitlines()]
    reset_entries = [e for e in entries if e["endpoint"] == "/reset"]
    assert reset_entries[-1]["params"]["scenario"] == FIXTURE_PATH


def test_reset_with_invalid_scenario_returns_400(tmp_path: Path) -> None:
    client, _app, _world, log = _client(tmp_path)
    r = client.post("/reset", json={"seed": 42, "scenario": "no.such._x"})
    assert r.status_code == 400
    # On the failure branch the world's reset never runs, so the action
    # log stays at its pre-call path — the entry lands in the same file
    # the test was given at boot.
    entries = [json.loads(line) for line in log.path.read_text().splitlines()]
    failed = [e for e in entries if e["endpoint"] == "/reset" and not e["ok"]]
    assert len(failed) == 1


def test_reset_without_scenario_is_backwards_compatible(tmp_path: Path) -> None:
    client, app, world, _log = _client(tmp_path)
    r = client.post("/reset", json={"seed": 7})
    assert r.status_code == 200
    assert isinstance(world.scenario, NullScenario)
    assert client.get("/scenario").json() == {
        "dotted_path": None,
        "description": None,
        "source": None,
    }


# -- GET /run ---------------------------------------------------------------


def test_get_run_returns_recorder_id_and_dir(tmp_path: Path) -> None:
    client, app, world, _log = _client(tmp_path)
    r = client.get("/run")
    assert r.status_code == 200
    payload = r.json()
    assert world.recorder is not None
    assert payload["run_id"] == world.recorder.run_id
    assert payload["dir"] == str(world.recorder.dir)


def test_get_run_returns_nulls_when_no_recorder(tmp_path: Path) -> None:
    world = World()  # no runs_root → no recorder
    log = ActionLog(root=str(tmp_path / "runs"))
    app = create_app(world=world, action_log=log)
    client = TestClient(app)
    assert client.get("/run").json() == {"run_id": None, "dir": None}


def test_get_run_reflects_reset_reallocation(tmp_path: Path) -> None:
    client, app, world, _log = _client(tmp_path)
    assert world.recorder is not None
    first_id = world.recorder.run_id
    # Step at least once so the first recorder materializes — a
    # zero-day recorder no longer leaves a folder behind (slice 03
    # invariant relaxed to "preserve *recorded* runs").
    client.post("/step", json={"days": 1})

    client.post("/reset", json={"seed": 1})
    payload = client.get("/run").json()
    assert payload["run_id"] != first_id
    # The previous run folder is preserved on disk.
    assert (Path(world.runs_root or "runs") / first_id).exists()


# -- Hygiene ---------------------------------------------------------------


def test_post_scenario_is_idempotent_for_same_path(tmp_path: Path) -> None:
    client, _app, world, _log = _client(tmp_path)
    client.post("/scenario", json={"dotted_path": FIXTURE_PATH})
    first_class = type(world.scenario)
    r = client.post("/scenario", json={"dotted_path": FIXTURE_PATH})
    assert r.status_code == 200
    # Re-attaching the same scenario is a fresh instance of the same class.
    assert type(world.scenario) is first_class

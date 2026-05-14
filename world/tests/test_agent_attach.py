"""Agent Play attach/detach + per-turn callback (agent-play slice 01).

ACs covered:
  * GET /agent reports the attached folder (or `None` on a fresh world).
  * POST /agent/attach loads a `BaseAgent` subclass from the named folder.
  * POST /agent/detach clears the attached agent.
  * Attaching repo-relative folders only; absolute paths and paths that
    resolve outside the configured `agent_repo_root` are rejected (this
    slice ships the security floor only; richer error surfacing is
    slice #2).
  * Missing folder / missing `agent.py` / no `BaseAgent` subclass each
    return 400.
  * When attached, `POST /step` invokes `agent.act(state)` and any
    action the agent submits lands in `actions.jsonl` for the slice
    the step terminates — indistinguishable from a human action.
  * `/agent/attach` and `/agent/detach` themselves do NOT append to
    `actions.jsonl`.
  * `POST /reset` auto-detaches.
  * A scenario attached before the agent attach survives the agent
    attach (`GET /scenario` still reports it).

Slice #4 adds:
  * `UiAgentApiClient.{step, reset, attach_scenario}` raise RuntimeError
    client-side (before any TestClient call).
  * `POST /step` returns 500 with `detail` containing `"agent.act raised:"`
    when the attached agent's `act()` raises; day unchanged; agent stays
    attached.
  * An agent that accidentally calls `self.api.step()` from `act()` hits
    the same 500 path via the client-side guard — the action log never
    sees the rejected request.

Slice #5 adds:
  * Re-attaching a folder reloads `agent.py` *and* its sibling helpers
    (`helpers.py`, ...) from disk — the new constant is visible to the
    next `act()` call.
  * Attach folder A, then attach folder B: `sys.path` contains B's
    folder, not A's.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.api_client import UiAgentApiClient
from world.action_log import ActionLog
from world.api import create_app
from world.sim import World

SCENARIO_FIXTURE_PATH = "world.tests.scenario_fixture"


def _client(
    tmp_path: Path,
    *,
    agent_repo_root: Path | None = None,
) -> tuple[TestClient, FastAPI, World, ActionLog]:
    """Mirror `world.tests.test_api_scenario_attach._client` plus an optional
    `agent_repo_root` so tests can drop a throwaway agent folder under
    `tmp_path` and have the attach handler accept it."""
    world = World(runs_root=str(tmp_path / "runs"))
    run_id = world.recorder.run_id if world.recorder is not None else None
    log = ActionLog(root=str(tmp_path / "runs"), run_id=run_id)
    app = create_app(
        world=world,
        action_log=log,
        runs_root=str(tmp_path / "runs"),
        agent_repo_root=agent_repo_root,
    )
    return TestClient(app), app, world, log


def _write_agent(folder: Path, body: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "agent.py").write_text(textwrap.dedent(body).lstrip())


def _entries(active_log: ActionLog) -> list[dict[str, Any]]:
    if not active_log.path.exists():
        return []
    return [json.loads(line) for line in active_log.path.read_text().splitlines() if line.strip()]


# -- GET /agent ------------------------------------------------------------


def test_get_agent_returns_null_on_fresh_world(tmp_path: Path) -> None:
    client, _app, _world, _log = _client(tmp_path)
    r = client.get("/agent")
    assert r.status_code == 200
    assert r.json() == {"folder": None}


# -- POST /agent/attach ---------------------------------------------------


def test_attach_loads_baseagent_subclass(tmp_path: Path) -> None:
    _write_agent(
        tmp_path / "myagent",
        """
        from agents.base import BaseAgent
        class Agent(BaseAgent):
            pass
        """,
    )
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)

    r = client.post("/agent/attach", json={"folder": "myagent"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "folder": "myagent"}
    assert client.get("/agent").json() == {"folder": "myagent"}


def test_attach_finds_baseagent_subclass_when_class_name_is_not_agent(tmp_path: Path) -> None:
    _write_agent(
        tmp_path / "myagent",
        """
        from agents.base import BaseAgent
        class MyCustomAgent(BaseAgent):
            pass
        """,
    )
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    r = client.post("/agent/attach", json={"folder": "myagent"})
    assert r.status_code == 200, r.text


def test_attach_rejects_absolute_path(tmp_path: Path) -> None:
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    r = client.post("/agent/attach", json={"folder": "/etc"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "failed to load agent from '/etc'" in detail
    assert "bad input" in detail


def test_attach_rejects_path_outside_repo_root(tmp_path: Path) -> None:
    # `agent_repo_root` is a child of tmp_path; `..` escapes it.
    inside_root = tmp_path / "inside"
    inside_root.mkdir()
    _write_agent(
        tmp_path / "outside", "from agents.base import BaseAgent\nclass Agent(BaseAgent): pass\n"
    )
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=inside_root)
    r = client.post("/agent/attach", json={"folder": "../outside"})
    assert r.status_code == 400
    assert "bad input" in r.json()["detail"]


def test_attach_rejects_dotted_python_path(tmp_path: Path) -> None:
    """`submit.agent` is a Python dotted path — reject before any filesystem
    access so the developer is not silently confused about path semantics."""
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    r = client.post("/agent/attach", json={"folder": "submit.agent"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "failed to load agent from 'submit.agent'" in detail
    assert "bad input" in detail


def test_attach_rejects_dot_dot(tmp_path: Path) -> None:
    """`..` is caught by the dot-rejection rule, same as `submit.agent`."""
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    r = client.post("/agent/attach", json={"folder": ".."})
    assert r.status_code == 400
    assert "bad input" in r.json()["detail"]


def test_attach_rejects_hidden_folder(tmp_path: Path) -> None:
    """`.hidden` is caught by the dot-rejection rule, same as `submit.agent`."""
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    r = client.post("/agent/attach", json={"folder": ".hidden"})
    assert r.status_code == 400
    assert "bad input" in r.json()["detail"]


def test_attach_rejects_symlink_escape(tmp_path: Path) -> None:
    """A symlink inside the repo root that resolves outside the root is
    rejected by the same `is_relative_to(repo_root.resolve())` boundary
    check that catches absolute paths and `..`."""
    inside_root = tmp_path / "inside"
    inside_root.mkdir()
    outside = tmp_path / "outside"
    _write_agent(outside, "from agents.base import BaseAgent\nclass Agent(BaseAgent): pass\n")
    (inside_root / "escape").symlink_to(outside, target_is_directory=True)

    client, _app, _world, _log = _client(tmp_path, agent_repo_root=inside_root)
    r = client.post("/agent/attach", json={"folder": "escape"})
    assert r.status_code == 400
    assert "bad input" in r.json()["detail"]


def test_attach_rejects_missing_folder(tmp_path: Path) -> None:
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    r = client.post("/agent/attach", json={"folder": "does_not_exist"})
    assert r.status_code == 400
    assert "bad input" in r.json()["detail"]


def test_attach_rejects_folder_without_agent_py(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    r = client.post("/agent/attach", json={"folder": "empty"})
    assert r.status_code == 400
    assert "bad input" in r.json()["detail"]


def test_attach_surfaces_import_error_in_detail(tmp_path: Path) -> None:
    """A module that imports a missing dependency should surface the
    ImportError verbatim in the detail so the developer can diagnose
    without grepping server logs."""
    _write_agent(
        tmp_path / "brokenimport",
        """
        import nonexistent_module_xyz  # noqa: F401
        from agents.base import BaseAgent
        class Agent(BaseAgent):
            pass
        """,
    )
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    r = client.post("/agent/attach", json={"folder": "brokenimport"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "failed to load agent from 'brokenimport'" in detail
    # ImportError / ModuleNotFoundError name + message surface verbatim.
    assert "nonexistent_module_xyz" in detail
    assert "ModuleNotFoundError" in detail or "ImportError" in detail


def test_attach_rejects_module_without_baseagent_subclass(tmp_path: Path) -> None:
    _write_agent(
        tmp_path / "barebones",
        """
        # No Agent class, no BaseAgent subclass anywhere.
        VALUE = 42
        """,
    )
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    r = client.post("/agent/attach", json={"folder": "barebones"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "failed to load agent from 'barebones'" in detail


def test_attach_surfaces_init_error_in_detail(tmp_path: Path) -> None:
    """When the agent's `__init__` raises, the exception type and message
    surface verbatim in the detail so the developer can debug missing API
    keys, bad imports in __init__, or signature mistakes."""
    _write_agent(
        tmp_path / "badinit",
        """
        from agents.base import BaseAgent
        class Agent(BaseAgent):
            def __init__(self, api, *, seed=None):
                raise RuntimeError("missing API key XYZ")
        """,
    )
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    r = client.post("/agent/attach", json={"folder": "badinit"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "failed to load agent from 'badinit'" in detail
    assert "RuntimeError" in detail
    assert "missing API key XYZ" in detail


# -- POST /agent/detach ---------------------------------------------------


def test_detach_clears_attached_agent(tmp_path: Path) -> None:
    _write_agent(
        tmp_path / "myagent",
        """
        from agents.base import BaseAgent
        class Agent(BaseAgent):
            pass
        """,
    )
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    client.post("/agent/attach", json={"folder": "myagent"})
    r = client.post("/agent/detach", json={})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "folder": None}
    assert client.get("/agent").json() == {"folder": None}


def test_detach_when_not_attached_is_noop(tmp_path: Path) -> None:
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    r = client.post("/agent/detach", json={})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "folder": None}


# -- /agent/* does not append to actions.jsonl ----------------------------


def test_agent_endpoints_do_not_append_to_actions_log(tmp_path: Path) -> None:
    _write_agent(
        tmp_path / "myagent",
        """
        from agents.base import BaseAgent
        class Agent(BaseAgent):
            pass
        """,
    )
    client, app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    client.post("/agent/attach", json={"folder": "myagent"})
    client.post("/agent/detach", json={})

    active_log: ActionLog = app.state.action_log
    eps = {e["endpoint"] for e in _entries(active_log)}
    assert "/agent/attach" not in eps
    assert "/agent/detach" not in eps


# -- /step invokes agent.act, action lands in actions.jsonl ----------------


def test_step_invokes_agent_act_and_action_lands_in_log(tmp_path: Path) -> None:
    """Tracer bullet: an attached agent submits an action via its api in
    `act()` and that action lands in the slice the step terminates,
    indistinguishable from a human-submitted action."""
    _write_agent(
        tmp_path / "buildagent",
        """
        from agents.base import BaseAgent
        class Agent(BaseAgent):
            def act(self, state):
                th = next(t for t in state["tiles"] if t["type"] == "town_hall")
                self.api.build("road", th["x"], th["y"] + 1)
            def next_step_days(self, state):
                return 1
        """,
    )
    client, app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    client.post("/reset", json={"seed": 42})
    r = client.post("/agent/attach", json={"folder": "buildagent"})
    assert r.status_code == 200, r.text

    r = client.post("/step", json={"days": 1})
    assert r.status_code == 200, r.text

    active_log: ActionLog = app.state.action_log
    entries = _entries(active_log)
    builds = [
        e
        for e in entries
        if e["endpoint"] == "/build" and e["ok"] and e["params"]["tile_type"] == "road"
    ]
    assert len(builds) == 1, entries


# -- /reset auto-detaches --------------------------------------------------


def test_reset_auto_detaches_agent(tmp_path: Path) -> None:
    _write_agent(
        tmp_path / "myagent",
        """
        from agents.base import BaseAgent
        class Agent(BaseAgent):
            pass
        """,
    )
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    client.post("/agent/attach", json={"folder": "myagent"})
    assert client.get("/agent").json() == {"folder": "myagent"}

    client.post("/reset", json={"seed": 42})
    assert client.get("/agent").json() == {"folder": None}


# -- Scenario coexistence --------------------------------------------------


def test_scenario_persists_through_agent_attach(tmp_path: Path) -> None:
    _write_agent(
        tmp_path / "myagent",
        """
        from agents.base import BaseAgent
        class Agent(BaseAgent):
            pass
        """,
    )
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    r = client.post("/scenario", json={"dotted_path": SCENARIO_FIXTURE_PATH})
    assert r.status_code == 200
    assert client.get("/scenario").json() == {"dotted_path": SCENARIO_FIXTURE_PATH}

    r = client.post("/agent/attach", json={"folder": "myagent"})
    assert r.status_code == 200, r.text
    assert client.get("/scenario").json() == {"dotted_path": SCENARIO_FIXTURE_PATH}


# -- Slice #4: act() crash safety + clock-violation guard -----------------


class _RecordingTransport:
    """Asserts the transport is never touched.

    Used to prove the `UiAgentApiClient` clock-method guards raise
    *before* the TestClient call, so no row can land in `actions.jsonl`.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def get(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(("GET", url, params))
        raise AssertionError("transport should not be reached")

    def post(self, url: str, *, json: dict[str, Any] | None = None) -> Any:
        self.calls.append(("POST", url, json))
        raise AssertionError("transport should not be reached")


def test_uiagentapiclient_step_raises_client_side() -> None:
    transport = _RecordingTransport()
    client = UiAgentApiClient(transport=transport)
    with pytest.raises(RuntimeError, match="human drives"):
        client.step(days=1)
    assert transport.calls == []


def test_uiagentapiclient_reset_raises_client_side() -> None:
    transport = _RecordingTransport()
    client = UiAgentApiClient(transport=transport)
    with pytest.raises(RuntimeError, match="human"):
        client.reset(seed=42)
    assert transport.calls == []


def test_uiagentapiclient_attach_scenario_raises_client_side() -> None:
    transport = _RecordingTransport()
    client = UiAgentApiClient(transport=transport)
    with pytest.raises(RuntimeError, match="human"):
        client.attach_scenario("some.dotted.path")
    assert transport.calls == []


def test_step_returns_500_when_act_raises(tmp_path: Path) -> None:
    """An attached agent whose `act()` raises must surface a 500 whose
    detail contains both `agent.act raised:` and the exception's repr —
    so the developer reads the proximate cause in the UI toast."""
    _write_agent(
        tmp_path / "crasher",
        """
        from agents.base import BaseAgent
        class Agent(BaseAgent):
            def act(self, state):
                raise RuntimeError("boom: kapow")
        """,
    )
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    client.post("/reset", json={"seed": 42})
    assert client.post("/agent/attach", json={"folder": "crasher"}).status_code == 200

    r = client.post("/step", json={"days": 1})
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "agent.act raised:" in detail
    assert "RuntimeError" in detail
    assert "boom: kapow" in detail


def test_act_raise_does_not_advance_day(tmp_path: Path) -> None:
    _write_agent(
        tmp_path / "crasher",
        """
        from agents.base import BaseAgent
        class Agent(BaseAgent):
            def act(self, state):
                raise RuntimeError("nope")
        """,
    )
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    client.post("/reset", json={"seed": 42})
    day_before = client.get("/state").json()["day"]
    client.post("/agent/attach", json={"folder": "crasher"})

    r = client.post("/step", json={"days": 1})
    assert r.status_code == 500
    assert client.get("/state").json()["day"] == day_before


def test_act_raise_keeps_agent_attached(tmp_path: Path) -> None:
    _write_agent(
        tmp_path / "crasher",
        """
        from agents.base import BaseAgent
        class Agent(BaseAgent):
            def act(self, state):
                raise RuntimeError("nope")
        """,
    )
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    client.post("/agent/attach", json={"folder": "crasher"})

    client.post("/step", json={"days": 1})
    assert client.get("/agent").json() == {"folder": "crasher"}


def test_agent_calling_api_step_returns_500_and_stays_attached(tmp_path: Path) -> None:
    """`act()` that calls `self.api.step(days=1)` hits the
    `UiAgentApiClient` client-side guard; the RuntimeError propagates
    through the /step handler's act-wrapper, returning 500. The day
    does not advance; the agent stays attached; nothing from the
    agent's `api.step()` reaches the action log (the transport guard
    raises before the TestClient call).
    """
    _write_agent(
        tmp_path / "clockcheater",
        """
        from agents.base import BaseAgent
        class Agent(BaseAgent):
            def act(self, state):
                self.api.step(days=1)
        """,
    )
    client, app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    client.post("/reset", json={"seed": 42})
    day_before = client.get("/state").json()["day"]
    client.post("/agent/attach", json={"folder": "clockcheater"})

    r = client.post("/step", json={"days": 1})
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "agent.act raised:" in detail
    assert "RuntimeError" in detail

    assert client.get("/state").json()["day"] == day_before
    assert client.get("/agent").json() == {"folder": "clockcheater"}

    # The agent's rejected api.step() must not have left a row in the
    # action log — the UiAgentApiClient guard raises before transport.
    active_log: ActionLog = app.state.action_log
    step_entries = [e for e in _entries(active_log) if e["endpoint"] == "/step" and e["ok"]]
    assert step_entries == []


# -- Slice #5: hot-reload loop for edited code ----------------------------


def _write_file(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip())


def test_reattach_reloads_sibling_helper_from_disk(tmp_path: Path) -> None:
    """The edit → Detach → Attach → see-new-behavior loop covers helpers,
    not just `agent.py`. Without the `__file__` walk in `_purge_modules_under`,
    `import_module("agent")` re-uses the cached `helpers` module and the
    second `act()` still sees the *old* constant — silently breaking the
    promise the PRD makes.

    The agent surfaces the helper's constant as the `x` parameter of a
    `/build` call. We inspect `actions.jsonl` rather than world state so
    the assertion does not depend on the build being placeable at the
    chosen coordinate — the *param* is the witness.
    """
    import sys

    folder = tmp_path / "helperagent"
    _write_file(
        folder / "helpers.py",
        """
        ROAD_X = 5
        """,
    )
    _write_file(
        folder / "agent.py",
        """
        from agents.base import BaseAgent
        from helpers import ROAD_X
        class Agent(BaseAgent):
            def act(self, state):
                self.api.build("road", ROAD_X, 0)
        """,
    )
    client, app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)
    client.post("/reset", json={"seed": 42})

    assert client.post("/agent/attach", json={"folder": "helperagent"}).status_code == 200
    assert client.post("/step", json={"days": 1}).status_code == 200

    builds_v1 = [e for e in _entries(app.state.action_log) if e["endpoint"] == "/build"]
    assert len(builds_v1) == 1
    assert builds_v1[0]["params"]["x"] == 5

    # Rewrite the helper, detach + re-attach, step again. The second build's
    # `x` param must reflect the *new* helper constant — proof the helper
    # reloaded from disk.
    _write_file(folder / "helpers.py", "ROAD_X = 9\n")
    # Force a future mtime so importlib's `.pyc` invalidation triggers
    # even when both writes land in the same wall-clock second. In real
    # usage a human takes seconds between edits, so the file's mtime
    # advances naturally; the test reproduces that explicitly.
    import os
    import time

    future = time.time() + 10
    os.utime(folder / "helpers.py", (future, future))
    assert client.post("/agent/detach", json={}).status_code == 200
    assert client.post("/agent/attach", json={"folder": "helperagent"}).status_code == 200
    assert client.post("/step", json={"days": 1}).status_code == 200

    builds_v2 = [e for e in _entries(app.state.action_log) if e["endpoint"] == "/build"]
    assert len(builds_v2) == 2
    assert builds_v2[1]["params"]["x"] == 9

    # Detach scrubs the sys.path entry the matching attach added.
    client.post("/agent/detach", json={})
    assert str(folder) not in sys.path


def test_attach_switches_sys_path_when_folder_changes(tmp_path: Path) -> None:
    """Attach folder A, then attach folder B without detaching first:
    `sys.path` reflects B and contains no entry for A. Without the
    `_detach_agent` call at the top of `post_agent_attach`, A's path
    would linger and contend with B for module lookups."""
    import sys

    _write_agent(
        tmp_path / "alpha",
        """
        from agents.base import BaseAgent
        class Agent(BaseAgent): pass
        """,
    )
    _write_agent(
        tmp_path / "beta",
        """
        from agents.base import BaseAgent
        class Agent(BaseAgent): pass
        """,
    )
    client, _app, _world, _log = _client(tmp_path, agent_repo_root=tmp_path)

    assert client.post("/agent/attach", json={"folder": "alpha"}).status_code == 200
    alpha_path = str((tmp_path / "alpha").resolve())
    assert alpha_path in sys.path

    assert client.post("/agent/attach", json={"folder": "beta"}).status_code == 200
    beta_path = str((tmp_path / "beta").resolve())
    assert beta_path in sys.path
    assert alpha_path not in sys.path

    client.post("/agent/detach", json={})
    assert beta_path not in sys.path

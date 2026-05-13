"""Replay roundtrip + evaluate.py CLI smoke tests.

The replay contract: given the same seed and the same logged action
sequence, a fresh world ends up byte-identical to the recorded final
state.  These tests pin that contract on a short scripted run so the
suite stays fast (~2 s per test).

The scripted agent's strategy is mostly bootstrap-only on a 30-day
window — that's the point: a small but non-trivial action log
(builds + steps) is enough to exercise dispatch through evaluate.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import evaluate
from agents.api_client import ApiClient
from agents.scripted import ScriptedAgent
from world.action_log import ActionLog
from world.api import create_app
from world.sim import World


def _short_game(monkeypatch: pytest.MonkeyPatch, days: int = 30) -> None:
    """Cap the active game length so the scripted agent finishes fast."""
    monkeypatch.setenv("GAME_DAYS", str(days))
    monkeypatch.setenv("MANUAL_GAME_DAYS", str(days))


def _run_scripted(runs_root: Path, seed: int = 42) -> tuple[Path, dict]:
    """Run the scripted agent in-process and return (run_dir, final_state)."""
    world = World()
    log = ActionLog(root=runs_root)
    app = create_app(world=world, action_log=log)
    api = ApiClient(transport=TestClient(app))
    agent = ScriptedAgent(api, seed=seed)
    final = agent.play_game()
    return log.dir, final


def test_replay_roundtrip_short_game(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run scripted for 30 days, replay the action log, assert state equality."""
    _short_game(monkeypatch, days=30)
    runs_root = tmp_path / "runs"

    run_dir, original_final = _run_scripted(runs_root, seed=42)
    (run_dir / "final_state.json").write_text(
        json.dumps(original_final, sort_keys=True, default=str) + "\n"
    )

    actions = (run_dir / "actions.jsonl").read_text().splitlines()
    assert any('"endpoint": "/reset"' in line for line in actions)
    assert any('"endpoint": "/step"' in line for line in actions)
    assert any('"endpoint": "/build"' in line for line in actions)

    rc = evaluate.cmd_replay(run_dir)
    assert rc == 0


def test_replay_detects_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If final_state.json is mutated, cmd_replay returns 1."""
    _short_game(monkeypatch, days=30)
    runs_root = tmp_path / "runs"

    run_dir, final = _run_scripted(runs_root, seed=42)
    drifted = dict(final)
    drifted["population"] = int(drifted["population"]) + 999
    (run_dir / "final_state.json").write_text(json.dumps(drifted, sort_keys=True, default=str))

    assert evaluate.cmd_replay(run_dir) == 1


def test_evaluate_cli_runs_submit_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`python evaluate.py --agent submit.agent --seed 42` runs end-to-end.

    Output is a single JSON line carrying the score breakdown (or null
    if no baseline exists for the seed); exit code is 0 on a clean run.
    """
    _short_game(monkeypatch, days=30)
    monkeypatch.chdir(tmp_path)

    rc = evaluate.main(["--agent", "submit.agent", "--seed", "42"])
    assert rc == 0

    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["agent"] == "submit.agent"
    assert payload["seed"] == 42
    assert "run_id" in payload

    run_dir = tmp_path / "runs" / payload["run_id"]
    assert (run_dir / "actions.jsonl").exists()
    assert (run_dir / "final_state.json").exists()

    # Smoke: round-trip the just-written run via evaluate.cmd_replay.
    assert evaluate.cmd_replay(run_dir) == 0


def test_evaluate_replay_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`python evaluate.py --replay <run_dir>` mirrors cmd_replay's exit code."""
    _short_game(monkeypatch, days=30)
    monkeypatch.chdir(tmp_path)

    rc = evaluate.main(["--agent", "submit.agent", "--seed", "42"])
    assert rc == 0
    line = capsys.readouterr().out.strip().splitlines()[-1]
    run_dir = tmp_path / "runs" / json.loads(line)["run_id"]

    rc2 = evaluate.main(["--replay", str(run_dir)])
    assert rc2 == 0


def test_evaluate_requires_agent_or_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        evaluate.main([])


def test_evaluate_cli_with_scenario_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--scenario scenarios.grid_stress` attaches the scenario for the run.

    The scenario dotted path lands in `metadata.json` for the post-reset
    recorder; `--replay` reads the field and re-attaches before driving
    the action log so the replay matches the recorded final state.
    """
    _short_game(monkeypatch, days=30)
    monkeypatch.chdir(tmp_path)

    rc = evaluate.main(
        [
            "--agent",
            "submit.agent",
            "--seed",
            "42",
            "--scenario",
            "scenarios.grid_stress",
        ]
    )
    assert rc == 0

    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    run_dir = tmp_path / "runs" / payload["run_id"]
    metadata = json.loads((run_dir / "metadata.json").read_text())
    assert metadata["scenario"] == "scenarios.grid_stress"

    # The grid-stress scenario fires a heatwave on day 10 with a 5-day
    # duration (see scenarios/grid_stress.py). Over a 30-day run that
    # event ends and lands in `historical_events`, so its presence in
    # the recorded final state is observable proof that the day-loop
    # hook actually ran the scenario.
    final = json.loads((run_dir / "final_state.json").read_text())
    historical = final.get("historical_events", [])
    assert any(ev.get("type") == "heatwave" and ev.get("started_day") == 10 for ev in historical)

    assert evaluate.cmd_replay(run_dir) == 0


def test_evaluate_cli_without_scenario_records_null(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Omitting `--scenario` leaves the metadata scenario field null.

    The default `NullScenario` is observably a no-op (slice 02), so the
    behavior matches a bare run.
    """
    _short_game(monkeypatch, days=30)
    monkeypatch.chdir(tmp_path)

    rc = evaluate.main(["--agent", "submit.agent", "--seed", "42"])
    assert rc == 0

    line = capsys.readouterr().out.strip().splitlines()[-1]
    run_dir = tmp_path / "runs" / json.loads(line)["run_id"]
    metadata = json.loads((run_dir / "metadata.json").read_text())
    assert metadata["scenario"] is None

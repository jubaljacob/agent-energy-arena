"""evaluate.py CLI smoke tests.

The scripted agent's strategy is mostly bootstrap-only on a 30-day
window — that's the point: a small but non-trivial run is enough to
exercise the CLI end-to-end without inflating suite runtime.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import evaluate


def _short_game(monkeypatch: pytest.MonkeyPatch, days: int = 30) -> None:
    """Cap the active game length so the scripted agent finishes fast."""
    monkeypatch.setenv("GAME_DAYS", str(days))
    monkeypatch.setenv("MANUAL_GAME_DAYS", str(days))


def test_evaluate_cli_runs_submit_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`python evaluate.py --agent agents.scripted --seed 42` runs end-to-end.

    Output is a single JSON line carrying the score breakdown (or null
    if no baseline exists for the seed); exit code is 0 on a clean run.
    """
    _short_game(monkeypatch, days=30)
    monkeypatch.chdir(tmp_path)

    rc = evaluate.main(["--agent", "agents.scripted", "--seed", "42"])
    assert rc == 0

    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["agent"] == "agents.scripted"
    assert payload["seed"] == 42
    assert "run_id" in payload

    run_dir = tmp_path / "runs" / payload["run_id"]
    assert (run_dir / "actions.jsonl").exists()
    assert (run_dir / "final_state.json").exists()
    assert (run_dir / "states.jsonl").exists()


def test_evaluate_score_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`python evaluate.py --score <run_dir>` prints the score breakdown."""
    _short_game(monkeypatch, days=30)
    monkeypatch.chdir(tmp_path)

    rc = evaluate.main(["--agent", "agents.scripted", "--seed", "42"])
    assert rc == 0
    line = capsys.readouterr().out.strip().splitlines()[-1]
    run_dir = tmp_path / "runs" / json.loads(line)["run_id"]

    rc2 = evaluate.main(["--score", str(run_dir)])
    assert rc2 == 0
    payload = json.loads(capsys.readouterr().out)
    assert "score" in payload
    assert "n_days" in payload


def test_evaluate_requires_agent_or_score(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    recorder, and the heatwave it schedules shows up in the recorded
    final state — observable proof that the day-loop hook fired.
    """
    _short_game(monkeypatch, days=30)
    monkeypatch.chdir(tmp_path)

    rc = evaluate.main(
        [
            "--agent",
            "agents.scripted",
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
    # event ends and lands in `historical_events`.
    final = json.loads((run_dir / "final_state.json").read_text())
    historical = final.get("historical_events", [])
    assert any(ev.get("type") == "heatwave" and ev.get("started_day") == 10 for ev in historical)


def test_evaluate_cli_with_time_budget_finishes_under_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A scripted-agent 30-day run completes well under a 60s budget.

    The output line carries the four budget-feature keys; days_advanced
    equals game_days; time_scaled_score equals the raw score (× 1.0).
    """
    _short_game(monkeypatch, days=30)
    monkeypatch.chdir(tmp_path)

    rc = evaluate.main(["--agent", "agents.scripted", "--seed", "42", "--time-budget", "60"])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["time_budget_seconds"] == 60
    assert payload["days_advanced"] == 30
    assert payload["wall_time_seconds"] > 0.0
    assert payload["wall_time_seconds"] < 60.0
    raw = float(payload["score"]["score"])
    assert payload["time_scaled_score"] == pytest.approx(raw)


def test_evaluate_cli_zero_time_budget_cuts_run_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--time-budget 0` trips the deadline on the first ApiClient call.

    The agent's play_game starts with /reset which raises BudgetExpired;
    evaluate.py catches it, reads the un-reset world state, and emits
    days_advanced=0 with time_scaled_score=0.0.
    """
    _short_game(monkeypatch, days=30)
    monkeypatch.chdir(tmp_path)

    rc = evaluate.main(["--agent", "agents.scripted", "--seed", "42", "--time-budget", "0"])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["time_budget_seconds"] == 0
    assert payload["days_advanced"] == 0
    assert payload["time_scaled_score"] == 0.0


def test_evaluate_cli_without_scenario_records_null(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Omitting `--scenario` leaves the metadata scenario field null.

    The default `NullScenario` is observably a no-op, so the behavior
    matches a bare run.
    """
    _short_game(monkeypatch, days=30)
    monkeypatch.chdir(tmp_path)

    rc = evaluate.main(["--agent", "agents.scripted", "--seed", "42"])
    assert rc == 0

    line = capsys.readouterr().out.strip().splitlines()[-1]
    run_dir = tmp_path / "runs" / json.loads(line)["run_id"]
    metadata = json.loads((run_dir / "metadata.json").read_text())
    assert metadata["scenario"] is None

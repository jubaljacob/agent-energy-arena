"""evaluate.py — CLI driver for AFK evaluation and scoring.

Two modes:

    python evaluate.py --agent agents.scripted --seed 42
        Loads <module>.Agent (a class with __init__(api, *, seed) and a
        .play_game() method, e.g. agents.scripted.ScriptedAgent), plays
        a full game on the given seed, writes the final state alongside
        the action log at runs/{run_id}/final_state.json, and prints a
        JSON breakdown line.  Exit 0 on success, 1 on agent crash.

        Pass ``--scenario <dotted_path>`` (e.g. ``scenarios.grid_stress``)
        to attach a stress scenario before the agent runs.

    python evaluate.py --score runs/{run_id}
        Reads ``states.jsonl`` from the run folder (or a direct path to
        a ``states.jsonl``) and prints the score breakdown that
        ``compute_score`` produces — the same payload ``GET /score``
        returns. Pass ``--starting-cash`` to override the cash anchor
        when scoring a run played under a non-default config.

By default the agent runs against an in-process FastAPI TestClient — no
uvicorn boot, no port management — matching the pattern in
``agents.scripted``.  Pass ``--api-url`` (or set ``WORLD_API_URL``) to
point at a live world (used by ``docker compose --profile eval run
agent``, where the URL is ``http://world:8000``).
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load `.env` from the repo root before importing anything that reads
# env vars. This lets users keep LLM_PROVIDER / LLM_API_KEY /
# NVIDIA_API_KEY etc. in `.env` instead of exporting them in every shell.
load_dotenv(Path(__file__).resolve().parent / ".env")

from agents.api_client import ApiClient, BudgetExpired  # noqa: E402
from agents.base import BaseAgent  # noqa: E402
from world.api import create_app  # noqa: E402
from world.config import load_config  # noqa: E402
from world.scoring import compute_score  # noqa: E402
from world.sim import World  # noqa: E402


def _load_agent_class(module_name: str) -> type[BaseAgent]:
    """Resolve the submitted agent class.

    Convention: the module exposes a top-level ``Agent`` symbol bound to
    a class with ``__init__(api, *, seed)`` and ``.play_game()``.  We
    also walk module attributes for any concrete ``BaseAgent`` subclass
    so a participant can re-export their class under any name.
    """
    mod = importlib.import_module(module_name)
    cls = getattr(mod, "Agent", None)
    if cls is None:
        for value in vars(mod).values():
            if isinstance(value, type) and issubclass(value, BaseAgent) and value is not BaseAgent:
                cls = value
                break
    if cls is None or not isinstance(cls, type):
        raise ValueError(f"{module_name} does not expose an `Agent` class or BaseAgent subclass")
    return cls


def _make_inprocess_client() -> tuple[ApiClient, World]:
    """Build an in-process API client + world.  No uvicorn, no socket."""
    from fastapi.testclient import TestClient

    world = World(runs_root="runs", seed_starter_grid=True)
    app = create_app(world=world)
    return ApiClient(transport=TestClient(app)), world


def _resolve_states_path(arg: Path) -> Path:
    if arg.is_dir():
        return arg / "states.jsonl"
    return arg


def _load_snapshots(path: Path) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            state = json.loads(line).get("state", {})
            snapshots.append(
                {
                    "treasury": state.get("treasury", 0.0),
                    "population": state.get("population", 0.0),
                    "happiness": state.get("happiness", 0.0),
                    "cumulative_renewable_served_kwh": state.get(
                        "cumulative_renewable_served_kwh", 0.0
                    ),
                    "cumulative_total_served_kwh": state.get("cumulative_total_served_kwh", 0.0),
                }
            )
    return snapshots


# --- Commands ---------------------------------------------------------------


def cmd_eval(
    module_name: str,
    seed: int,
    api_url: str | None,
    scenario: str | None = None,
    time_budget: int | None = None,
) -> int:
    AgentCls = _load_agent_class(module_name)
    world: World | None = None
    if api_url:
        api = ApiClient(base_url=api_url)
        # When running against a live world we don't own the action log
        # directory; final_state lands next to evaluate.py's cwd under a
        # synthetic run_id so the path printed in the result line points
        # somewhere meaningful.
        run_dir = Path("runs") / f"live-{seed}"
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        api, world = _make_inprocess_client()
        run_dir = world.recorder.dir if world.recorder is not None else Path("runs")

    # Attach the scenario BEFORE the agent's first reset so the scenario
    # survives into the post-reset recorder's metadata.json (the agent
    # always calls /reset without a scenario arg, and reset preserves
    # the currently-attached scenario when none is passed).
    if scenario is not None:
        api.attach_scenario(scenario)

    agent = AgentCls(api, seed=seed)

    # T2 clock: start counting once the agent class is in hand. Excludes
    # Python/import startup; includes agent __init__ (graph build for
    # langgraph) and the agent's own /reset inside play_game().
    started_at = time.monotonic()
    if time_budget is not None:
        api._deadline_monotonic = started_at + float(time_budget)

    final_state: dict[str, Any]
    try:
        final_state = agent.play_game()
    except BudgetExpired:
        # Stop the watchdog so the post-game state/score reads below
        # don't trip the deadline check on their own calls.
        api._deadline_monotonic = None
        final_state = api.state()
    finally:
        api._deadline_monotonic = None
    wall_time_seconds = time.monotonic() - started_at

    if world is not None and world.recorder is not None:
        run_dir = world.recorder.dir

    # The recorder lazily materializes its directory on the first
    # /step. A budget that fires before /reset (or any other path
    # that never advances time) leaves the run folder uncreated, so
    # ensure it exists before writing the harness-side snapshot.
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "final_state.json").write_text(
        json.dumps(final_state, sort_keys=True, default=str) + "\n"
    )

    score_payload = api.score()
    line: dict[str, Any] = {
        "agent": module_name,
        "seed": seed,
        "run_id": run_dir.name,
        "score": score_payload,
    }
    if time_budget is not None:
        # `active_game_days` reflects scenario overrides; falls back to
        # the static config when no scenario shrinks the horizon.
        config = final_state.get("config", {})
        game_days = int(config.get("active_game_days") or config.get("game_days") or 1)
        days_advanced = int(final_state.get("day", 0))
        raw_score = float(score_payload.get("score", 0.0))
        line["time_budget_seconds"] = int(time_budget)
        line["wall_time_seconds"] = wall_time_seconds
        line["days_advanced"] = days_advanced
        line["time_scaled_score"] = raw_score * days_advanced / game_days
    print(json.dumps(line))
    return 0


def cmd_score(run: Path, starting_cash: float | None) -> int:
    states_path = _resolve_states_path(run)
    if not states_path.exists():
        print(f"not found: {states_path}", file=sys.stderr)
        return 1

    cash = float(starting_cash) if starting_cash is not None else float(load_config().starting_cash)
    snapshots = _load_snapshots(states_path)
    print(json.dumps(compute_score(snapshots, cash), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", help="Module path to the submitted agent (e.g. submit.agent).")
    parser.add_argument("--seed", type=int, default=42, help="Seed to play (default 42).")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("WORLD_API_URL"),
        help="Talk to a live world at this URL (default: in-process TestClient).",
    )
    parser.add_argument(
        "--score",
        type=Path,
        metavar="RUN",
        help="Score a recorded run: path to runs/{run_id} or to a states.jsonl.",
    )
    parser.add_argument(
        "--starting-cash",
        type=float,
        default=None,
        help="Override starting cash for --score. Defaults to Config.starting_cash.",
    )
    parser.add_argument(
        "--scenario",
        default=None,
        help=(
            "Dotted path to a Scenario subclass (e.g. scenarios.grid_stress). "
            "Attached to the world before the agent runs."
        ),
    )
    parser.add_argument(
        "--time-budget",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "Wall-clock budget (int seconds). When the budget elapses the "
            "next ApiClient call raises BudgetExpired; evaluate.py reads "
            "the world's current state and emits time_scaled_score = "
            "score * days_advanced / game_days alongside the regular "
            "score payload. Omitted = no cap."
        ),
    )
    args = parser.parse_args(argv)

    if args.score is not None:
        return cmd_score(args.score, args.starting_cash)

    if not args.agent:
        parser.error("either --agent or --score is required")
    return cmd_eval(
        args.agent,
        int(args.seed),
        args.api_url,
        args.scenario,
        time_budget=args.time_budget,
    )


if __name__ == "__main__":
    raise SystemExit(main())

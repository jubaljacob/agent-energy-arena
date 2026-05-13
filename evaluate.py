"""evaluate.py — CLI driver for AFK evaluation and replay.

Two modes per the brief §11.2 / issue 18:

    python evaluate.py --agent submit.agent --seed 42
        Loads <module>.Agent (a class with __init__(api, *, seed) and a
        .play_game() method, e.g. agents.scripted.ScriptedAgent), plays
        a full game on the given seed, writes the final state alongside
        the action log at runs/{run_id}/final_state.json, and prints a
        JSON breakdown line.  Exit 0 on success, 1 on agent crash.

    python evaluate.py --replay runs/{run_id}
        Re-runs the action log byte-for-byte against a fresh in-process
        world, then asserts json.dumps(state) equals the recorded
        final_state.json.  Exit 0 on match, 1 on drift.

By default the agent runs against an in-process FastAPI TestClient — no
uvicorn boot, no port management — matching the pattern in
``agents.scripted``.  Pass ``--api-url`` (or set ``WORLD_API_URL``) to
point at a live world (used by ``docker compose --profile eval run
agent``, where the URL is ``http://world:8000``).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

from agents.api_client import ApiClient
from agents.base import BaseAgent
from world.action_log import ActionLog
from world.api import BASELINES_DIR, create_app
from world.sim import World


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
    return cls  # type: ignore[no-any-return]


def _make_inprocess_client(
    *,
    runs_root: Path | None = None,
) -> tuple[ApiClient, World, ActionLog]:
    """Build an in-process API client + world + log.  No uvicorn, no socket."""
    from fastapi.testclient import TestClient

    # open-source-arena slice 03: pin the world's recorder + the action
    # log to the SAME run folder so `metadata.json`, `states.jsonl`,
    # `final.json`, and `actions.jsonl` live side-by-side. Replay (-r
    # cmd_replay) passes a sibling temp dir so the original run is
    # preserved verbatim.
    root = str(runs_root) if runs_root is not None else "runs"
    world = World(runs_root=root)
    run_id = world.recorder.run_id if world.recorder is not None else None
    log = ActionLog(root=root, run_id=run_id)
    app = create_app(world=world, action_log=log)
    return ApiClient(transport=TestClient(app)), world, log


def _score_breakdown(final_state: dict[str, Any], seed: int) -> dict[str, Any] | None:
    """Recompute the /score breakdown from a captured final_state dict.

    Mirrors world.scoring.score so we don't need a live World handle in
    the HTTP path.  Returns None if no baseline file exists for the
    seed (matches the /score 404 contract).
    """
    baseline_path = BASELINES_DIR / f"seed_{seed}.json"
    if not baseline_path.exists():
        return None
    payload = json.loads(baseline_path.read_text())
    p_ref = float(payload["p_ref"])
    t_ref = float(payload["t_ref"])

    starting_cash = float(final_state["config"]["starting_cash"])
    P = float(final_state["population"])
    T = float(final_state["treasury"]) - starting_cash
    total_kwh = float(final_state.get("cumulative_total_served_kwh", 0.0))
    renewable_kwh = float(final_state.get("cumulative_renewable_served_kwh", 0.0))
    R = renewable_kwh / max(total_kwh, 1.0)

    p_term = 0.5 * min(P / max(p_ref, 1.0), 3.0)
    t_term = 0.4 * 0.5 * (1.0 + math.tanh(T / max(t_ref, 1.0)))
    r_term = 0.1 * R
    return {
        "P": P,
        "P_ref": p_ref,
        "p_term": p_term,
        "T": T,
        "T_ref": t_ref,
        "t_term": t_term,
        "R": R,
        "r_term": r_term,
        "score": p_term + t_term + r_term,
    }


def _dispatch(api: ApiClient, endpoint: str, params: dict[str, Any]) -> Any:
    """Re-issue a logged action against the fresh ApiClient."""
    if endpoint == "/reset":
        return api.reset(seed=params.get("seed"))
    if endpoint == "/step":
        return api.step(days=int(params.get("days", 1)))
    if endpoint == "/build":
        return api.build(params["tile_type"], int(params["x"]), int(params["y"]))
    if endpoint == "/demolish":
        return api.demolish(int(params["x"]), int(params["y"]))
    if endpoint == "/survey":
        return api.survey(int(params["x"]), int(params["y"]), int(params.get("size", 8)))
    if endpoint == "/drill":
        return api.drill(
            int(params["x"]),
            int(params["y"]),
            int(params["target_z"]),
            str(params.get("well_type", "production")),
        )
    if endpoint == "/control/well":
        return api.control_well(str(params["well_id"]), float(params["rate_bbl_day"]))
    if endpoint == "/control/refinery":
        return api.control_refinery(str(params["refinery_id"]), float(params["rate_bbl_day"]))
    raise ValueError(f"unknown endpoint in action log: {endpoint!r}")


# --- Commands ---------------------------------------------------------------


def cmd_eval(module_name: str, seed: int, api_url: str | None) -> int:
    AgentCls = _load_agent_class(module_name)
    world: World | None = None
    if api_url:
        api = ApiClient(base_url=api_url)
        # When running against a live world we don't own the action log
        # directory; final_state lands next to evaluate.py's cwd under a
        # synthetic run_id so --replay still has somewhere to look.
        run_dir = Path("runs") / f"live-{seed}"
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        api, world, _log = _make_inprocess_client()
        # play_game starts with /reset, which reallocates the recorder's
        # run folder. We resolve `run_dir` AFTER play_game so the path
        # reflects the post-reset folder rather than the stale
        # construction-time one.
        run_dir = _log.dir  # tentative; reassigned below after play_game

    agent = AgentCls(api, seed=seed)
    final_state = agent.play_game()

    if world is not None and world.recorder is not None:
        run_dir = world.recorder.dir

    (run_dir / "final_state.json").write_text(
        json.dumps(final_state, sort_keys=True, default=str) + "\n"
    )

    breakdown = _score_breakdown(final_state, seed)
    line = {
        "agent": module_name,
        "seed": seed,
        "run_id": run_dir.name,
        "score": breakdown,
    }
    print(json.dumps(line))
    return 0


def cmd_replay(run_dir: Path) -> int:
    actions_path = run_dir / "actions.jsonl"
    final_state_path = run_dir / "final_state.json"
    if not actions_path.exists():
        print(f"missing {actions_path}", file=sys.stderr)
        return 1
    if not final_state_path.exists():
        print(f"missing {final_state_path}", file=sys.stderr)
        return 1

    expected = json.loads(final_state_path.read_text())

    # Replay into a sibling temp dir so we don't clobber the original log.
    replay_root = run_dir.parent / f"_replay-{run_dir.name}"
    api, _world, _log = _make_inprocess_client(runs_root=replay_root)

    with actions_path.open() as fh:
        for raw in fh:
            entry = json.loads(raw)
            endpoint = entry["endpoint"]
            params = entry.get("params", {}) or {}
            # Original was rejected (e.g. invalid /step) — the replay
            # rejection is the deterministic mirror; state is unchanged.
            with contextlib.suppress(RuntimeError):
                _dispatch(api, endpoint, params)

    actual = json.loads(json.dumps(api.state(), sort_keys=True, default=str))
    expected_norm = json.loads(json.dumps(expected, sort_keys=True, default=str))
    if actual != expected_norm:
        print("replay drift: state does not match recorded final_state.json", file=sys.stderr)
        return 1
    print(json.dumps({"replay": "ok", "run_id": run_dir.name}))
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
    parser.add_argument("--replay", type=Path, help="Path to runs/{run_id} to replay.")
    args = parser.parse_args(argv)

    if args.replay is not None:
        return cmd_replay(args.replay)

    if not args.agent:
        parser.error("either --agent or --replay is required")
    return cmd_eval(args.agent, int(args.seed), args.api_url)


if __name__ == "__main__":
    raise SystemExit(main())

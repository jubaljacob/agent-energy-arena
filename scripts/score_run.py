#!/usr/bin/env python
"""Score a recorded run from its `states.jsonl`.

Accepts either a run directory containing `states.jsonl` (the shape
`Recorder` writes under `runs/<run_id>/`) or the `states.jsonl` path
directly. Prints the same payload `GET /score` returns: headline
score in [0, 100], `n_days`, and the per-axis breakdown.

Examples:

    python scripts/score_run.py runs/1779088449-73a954c5
    python scripts/score_run.py scenarios/longest_3789_days/states.jsonl
    python scripts/score_run.py runs/foo --starting-cash 250000

Starting cash defaults to `Config.starting_cash` (the same value the
HTTP `/score` reads off `world.config.starting_cash`). Pass
`--starting-cash` to override when scoring a run played under a
non-default config.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from world.config import load_config  # noqa: E402
from world.scoring import compute_score  # noqa: E402


def _resolve_states_path(arg: str) -> Path:
    p = Path(arg)
    if p.is_dir():
        return p / "states.jsonl"
    return p


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "run",
        help="Run directory (containing states.jsonl) or path to states.jsonl.",
    )
    parser.add_argument(
        "--starting-cash",
        type=float,
        default=None,
        help="Override starting cash. Defaults to Config.starting_cash.",
    )
    args = parser.parse_args(argv)

    states_path = _resolve_states_path(args.run)
    if not states_path.exists():
        print(f"not found: {states_path}", file=sys.stderr)
        return 1

    starting_cash = (
        float(args.starting_cash)
        if args.starting_cash is not None
        else float(load_config().starting_cash)
    )

    snapshots = _load_snapshots(states_path)
    result = compute_score(snapshots, starting_cash)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

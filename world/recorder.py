"""Per-run recorder. Writes metadata, per-day state log, and final snapshot.

The recorder owns a run folder under `runs/` (peer of `action_log.py`'s
`actions.jsonl`). It is allocated by `World` at construction time and is
finalized + replaced on `World.reset` — no run is destroyed by a reset.

Three artifacts per run folder:
  * `metadata.json` — seed, scenario dotted path, session marker,
    started-at timestamp, run id. Written once at construction.
  * `states.jsonl` — one line per simulated day. `record_step(world, day)`
    appends an entry with the end-of-day `state_dict()` and the
    per-day `today_summary_so_far`. Scenario-driven weather overrides
    and `scenario_trace` entries are visible through the embedded
    state.
  * `final.json` — written exactly once by `finalize(world)`. Repeated
    finalize calls after the first are no-ops.

The recorder is purely additive — `world.action_log.ActionLog`
continues to own `actions.jsonl` inside the same folder.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from world.sim import World


class Recorder:
    def __init__(
        self,
        root: str | os.PathLike[str] = "runs",
        run_id: str | None = None,
        *,
        seed: int,
        scenario_name: str | None,
        session: str,
    ) -> None:
        self.root = Path(root)
        self.run_id = run_id or _new_run_id()
        self.dir = self.root / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.metadata_path = self.dir / "metadata.json"
        self.states_path = self.dir / "states.jsonl"
        self.final_path = self.dir / "final.json"
        self._finalized = False
        self._write_metadata(seed=seed, scenario_name=scenario_name, session=session)

    def _write_metadata(self, *, seed: int, scenario_name: str | None, session: str) -> None:
        payload = {
            "run_id": self.run_id,
            "seed": int(seed),
            "scenario": scenario_name,
            "session": session,
            "started_at": time.time(),
        }
        self.metadata_path.write_text(json.dumps(payload) + "\n")

    def record_step(self, world: World, day: int) -> None:
        """Append one line to states.jsonl after a successful simulated day.

        `day` is the just-completed day; the embedded `state` snapshot is
        the world's end-of-day view via `state_dict()`. The per-day
        summary mirrors `state.today_summary_so_far` — same fields the
        UI's step response and the daily P&L surface.
        """
        entry = {
            "day": int(day),
            "state": world.state_dict(),
            "summary": dict(world.state.today_summary_so_far),
        }
        with self.states_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=_json_default) + "\n")

    def finalize(self, world: World) -> None:
        """Write final.json exactly once. Repeated calls are no-ops."""
        if self._finalized:
            return
        self._finalized = True
        payload = {
            "run_id": self.run_id,
            "final_state": world.state_dict(),
            "ended_at": time.time(),
        }
        self.final_path.write_text(json.dumps(payload, default=_json_default) + "\n")


def _new_run_id() -> str:
    return f"{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)

"""System prompt + tool schemas for the LLM ReAct agent.

Three things live here, all named as extension points so participants
can hot-swap any of them in `submit/agent.py`:

  SYSTEM_PROMPT   — mechanic primer + scoring objective + output format.
  ACTION_TOOLS    — exactly the 7 tools from PRD §"Reference agents":
                    build, demolish, survey, drill, set_well_rate,
                    set_refinery_rate, step. No `skip` (step with no
                    other actions is the equivalent). No `set_plant_rate`
                    (`/control/plant` was dropped in v1).
  TILE_TYPES      — the build-catalog vocabulary the model is allowed
                    to pass to `build.tile_type`.
"""

from __future__ import annotations

from typing import Any

TILE_TYPES: list[str] = [
    "road",
    "house",
    "commercial",
    "industrial",
    "solar_farm",
    "wind_turbine",
    "gas_peaker",
    "coal_plant",
    "refinery",
    "pipeline",
]


SYSTEM_PROMPT: str = """\
You manage the energy, infrastructure, and economy of a small city over
a 10-year horizon. Each turn you observe a compressed state summary and
emit tool calls that mutate the world. Your goal is to maximize the
final score:

  score = 0.5·min(P/P_ref, 3.0) + 0.4·0.5·(1+tanh(T/T_ref)) + 0.1·R

where P is final population, T = treasury − starting_cash, and R is the
lifetime renewable-served-kWh fraction (excluding curtailment).

Key mechanics:
- Build civilian tiles (house/commercial/industrial/refinery) only on
  squares orthogonally adjacent to a road (or the town hall).
- Power plants and wells need no road. Renewables (solar/wind) are zero
  at the evening peak — only gas/coal cover the dispatchable margin.
- Population grows when jobs ≥ pop, capacity > pop, happiness ≥ 0.5.
  Coal plants nearby drop happiness (chebyshev radius 3).
- Surveys reveal a column of voxels; cost scales as 15_000·(size/8)².
  Resurvey allowed; each draw is independent noise. Drill when an
  estimated voxel has oil ≥ 5000 bbl AND perm ≥ 200 mD.
- Injection wells act as demand-response: shed during brownout/blackout,
  ramp 2× during curtailment.
- Events (heatwave / fuel-price-shock / demand-surprise / plant-failure
  / regulatory-tightening) fire daily. Drop step size to 1 during a
  crisis.
- Carbon price starts at $25/ton, jumps 1.5× on each regulatory event
  (cap 3). Demolish coal once it exceeds ~$80/ton.

Output format:
- Emit one or more tool calls per turn.
- The LAST tool call MUST be `step` with a `days` parameter in [1, 7].
  If you omit it, the harness will auto-advance days=7.
- No prose. Tool calls only.
"""


# ---------- Action tools schema --------------------------------------------


def _build_action_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "build",
            "description": (
                "Place a tile at (x, y). Civilian tiles must be adjacent to a road. "
                "Returns ok=false with error='no_road_adjacency' / 'occupied' / "
                "'insufficient_funds' on rejection."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tile_type": {"type": "string", "enum": TILE_TYPES},
                    "x": {"type": "integer", "minimum": 0},
                    "y": {"type": "integer", "minimum": 0},
                },
                "required": ["tile_type", "x", "y"],
            },
        },
        {
            "name": "demolish",
            "description": "Remove the tile at (x, y). Refunds 25% of CAPEX.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "minimum": 0},
                    "y": {"type": "integer", "minimum": 0},
                },
                "required": ["x", "y"],
            },
        },
        {
            "name": "survey",
            "description": (
                "Reveal a size×size column of subsurface voxels centered at (x, y). "
                "Cost = 15_000·(size/8)². Size in [4, 16]."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "minimum": 0},
                    "y": {"type": "integer", "minimum": 0},
                    "size": {"type": "integer", "minimum": 4, "maximum": 16},
                },
                "required": ["x", "y"],
            },
        },
        {
            "name": "drill",
            "description": (
                "Drill a well at (x, y) targeting voxel z. well_type ∈ {production, injection}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "minimum": 0},
                    "y": {"type": "integer", "minimum": 0},
                    "target_z": {"type": "integer", "minimum": 0},
                    "well_type": {"type": "string", "enum": ["production", "injection"]},
                },
                "required": ["x", "y", "target_z"],
            },
        },
        {
            "name": "set_well_rate",
            "description": (
                "Set a well's setpoint rate in bbl/day. Production rate is clamped "
                "to the well's pool maximum; injection rate is the baseline before "
                "DR adjustments."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "well_id": {"type": "string"},
                    "rate_bbl_day": {"type": "number", "minimum": 0},
                },
                "required": ["well_id", "rate_bbl_day"],
            },
        },
        {
            "name": "set_refinery_rate",
            "description": "Set a refinery's throughput in bbl/day of crude input.",
            "parameters": {
                "type": "object",
                "properties": {
                    "refinery_id": {"type": "string"},
                    "rate_bbl_day": {"type": "number", "minimum": 0},
                },
                "required": ["refinery_id", "rate_bbl_day"],
            },
        },
        {
            "name": "step",
            "description": (
                "Advance the simulation by `days` days. Must be the LAST tool call "
                "of the turn. days in [1, 7]."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "minimum": 1, "maximum": 7},
                },
                "required": ["days"],
            },
        },
    ]


ACTION_TOOLS: list[dict[str, Any]] = _build_action_tools()

"""System prompt + tool schemas for the LLM ReAct agent.

Three things live here, all named as extension points so participants
can hot-swap any of them in `submit/agent.py`:

  SYSTEM_PROMPT   — short API-and-invariants briefing + output format.
                    Deliberately terse: it tells the agent what the
                    tools do and where to look (RULES.md, /state,
                    /catalog, /forecast), then gets out of the way.
                    Strategy and tuning are the participant's job.
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
    "park",
    "solar_farm",
    "wind_turbine",
    "gas_peaker",
    "coal_plant",
    "battery",
    "refinery",
    "pipeline",
]


SYSTEM_PROMPT: str = """\
You manage a city-energy simulation over a 10-year horizon (3650 days
by default). Each turn you observe a compressed state summary and emit
tool calls that mutate the world. Maximise the final score, which
weighs treasury, population, happiness, renewable share, and solvency
over the full per-day trace — no single metric dominates.

World shape:
- 32x32 surface grid. 16-voxel deep subsurface (z=0 top, z=15 bottom).
  24 hours/day. Step cadence is 1-7 days per /step; you choose via the
  `step` tool.
- Starting treasury $500,000, population 100. A town hall at the centre
  counts as a road and provides housing + jobs at no cost.

How the tools relate:
- `build` / `demolish` mutate the surface. Civilian tiles (house,
  commercial, industrial, refinery) require road adjacency; plants and
  wells don't. Coal, gas peakers, and wind turbines impose a one-cell
  no-build halo on neighbours (roads and batteries are exempt).
- `survey` reveals a size x size column of subsurface voxels. Cost
  grows quadratically with size; default size is the cheapest.
- `drill` places production or injection wells targeting a specific
  voxel depth. Two wells at the same (x, y) are only legal if their
  target_z differs by at least 3 voxels (stacked completion).
- `set_well_rate` / `set_refinery_rate` set per-tile setpoints in
  bbl/day. They persist across days until changed.
- `step` advances the clock and ends the turn.

Mutating tools return `{ok, error?, treasury_after, result}`. Read the
`error` field on rejections — keys like `tile_occupied`, `out_of_bounds`,
`no_road_adjacency`, `insufficient_funds`, `completion_overlap`,
`spacing_violation` name exactly what the world refused and why.

The complete mechanics — pricing, dispatch order, pipeline routing,
reservoir pressure support, events, scoring formulas — live in
RULES.md and the live API surface (`/state`, `/catalog`, `/forecast`,
`/score`). Discover and exploit them.

Output format:
- Emit one or more tool calls per turn.
- The LAST tool call MUST be `step` with `days` in [1, 7]. If you omit
  it, the harness auto-advances days=7.
- No prose. Tool calls only.
"""


# ---------- Action tools schema --------------------------------------------


def _build_action_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "build",
            "description": (
                "Place a tile at (x, y). Civilian tiles must be adjacent to a road. "
                "Coal, gas, and wind impose a one-cell no-build halo (roads and "
                "batteries are admitted inside it). Returns ok=false with "
                "error='unknown_tile_type' / 'out_of_bounds' / 'tile_occupied' / "
                "'no_road_adjacency' / 'spacing_violation' / 'insufficient_funds' "
                "on rejection."
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
            "description": (
                "Remove the tile at (x, y). Refunds 25% of CAPEX. "
                "Demolishing a road is rejected if it would orphan any "
                "road-requiring tile (house / commercial / industrial / "
                "refinery) from the town-hall road network — the response "
                "is ok=false, error='would_disconnect', and result.stranded "
                "lists the {x, y, type} of every tile that would be cut off. "
                "Other rejections: 'out_of_bounds' / 'no_tile' / "
                "'cannot_demolish_townhall'."
            ),
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
                "Cost = 15_000·(size/4)². Default size 4 ($15k, cheapest). "
                "Size in [4, 16]."
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
                "Drill a well at (x, y) targeting voxel z. well_type ∈ {production, injection}. "
                "Capex = base·(1 + (target_z/world_depth)²). For injectors to support a producer, "
                "drill in the same reservoir_id at 3D Chebyshev distance > 1 from the producer."
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

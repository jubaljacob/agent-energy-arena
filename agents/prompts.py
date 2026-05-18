"""System prompt + tool schemas for the LLM ReAct agent.

Three things live here, all named as extension points so participants
can hot-swap any of them in `submit/agent.py`:

  SYSTEM_PROMPT   — mechanic primer + scoring objective + output format,
                    appended with the canonical `RULES.md` (read live from
                    disk at import time so the LLM sees the same mechanics
                    document maintainers edit).
  ACTION_TOOLS    — exactly the 7 tools from PRD §"Reference agents":
                    build, demolish, survey, drill, set_well_rate,
                    set_refinery_rate, step. No `skip` (step with no
                    other actions is the equivalent). No `set_plant_rate`
                    (`/control/plant` was dropped in v1).
  TILE_TYPES      — the build-catalog vocabulary the model is allowed
                    to pass to `build.tile_type`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Path to the canonical mechanics doc. `prompts.py` lives at
# `<repo>/agents/prompts.py`; RULES.md is at the repo root.
_RULES_MD_PATH: Path = Path(__file__).resolve().parents[1] / "RULES.md"

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


_PROMPT_HEAD: str = """\
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
- Population grows when jobs ≥ pop, capacity > pop, happiness ≥ 0.3.
  Growth multiplier = max(0, (happiness - 0.3) / 1.2): h=1.0 → 58%,
  h=1.5 → 100%. Place parks within chebyshev-2 of houses to lift
  happiness (+0.10 per nearby park, capped 0.30/house, averaged).
  Industrial + refinery within chebyshev-2 of houses cost -0.03 each
  (halved to -0.015 by a park within radius-2 of both). Coal plants
  nearby drop happiness (chebyshev radius 3).

Subsurface & oilfield (oilfield-v2):
- HC voxels are tagged with `reservoir_id` (1-indexed). All voxels
  sharing the same id form a single 26-connected reservoir blob;
  treat the id as the "pool" name for routing pressure-support.
- Surveys reveal a size×size column of voxels. Cost = 15_000·(size/4)²
  → size 4 = $15k (cheapest, default), size 8 = $60k, size 16 = $240k.
  Resurvey allowed; each draw is independent noise. Prefer many size-4
  sweeps to one big column. Drill when an estimated voxel has
  oil ≥ 5000 bbl AND perm ≥ 200 mD.
- Drill capex is quadratic in depth:
    capex = base · (1 + (target_z / world_depth)²)
  At z=0 you pay `base`; deeper targets cost more. `world_depth` is
  `config.world_d`; base capex per well_type is in /catalog.
- Per-voxel oil capacity is small (~4k–17k bbl, mean ~8.5k); a 36-voxel
  reservoir holds ~300k bbl. Tall reservoirs deplete within the game
  horizon, so stacking completions on the same surface tile is a real
  lever for engaged-rollup growth.
- Stacked completions: two wells may share (x, y) as long as their
  target_z values differ by ≥ 3 (their 3×3×3 drainage cubes can't
  overlap). Drilling a second producer at the same (x, y) with
  |Δz| < 3 returns ok=false with error='completion_overlap'.
- Pressure support is RATE-based, not cumulative. Each producer's
  `pressure_boost` is recomputed daily from YESTERDAY's flows:
    boost = min(0.5, Σ qualifying_injector_yesterday_rate
                       / max(producer_yesterday_rate, 1))
  An injector "qualifies" iff it shares the producer's `reservoir_id`
  AND its 3D Chebyshev distance from the producer's (x, y, target_z)
  is STRICTLY > 1 (adjacent injectors are rejected — breakthrough).
  Cap is 0.5. To benefit a producer, drill the injector in the same
  reservoir at Chebyshev distance ≥ 2.
- Injection wells double as demand-response: shed during brownout/
  blackout, ramp 2× during curtailment.

Pipelines & crude routing:
- Pipeline tiles are placed via `build`. Producers route crude to
  refineries ONLY through orthogonally-adjacent (4-connected) chains
  of pipeline tiles. Tile-adjacent producers/refineries also count as
  network endpoints (no pipeline tile required between a well and a
  refinery sitting next to each other).
- Routing is PER-NETWORK: each connected pipeline component routes
  its own producers' crude to its own refineries (descending setpoint,
  workforce-capped). Crude does NOT cross between disjoint networks.
- Orphan economics:
    * Producer with no refinery on its network → 100% raw sale at
      $40/bbl (no refining margin, no carbon credit).
    * Refinery with no producer on its network → zero throughput.
  Lay pipeline BEFORE expecting refined revenue.

Events & macro:
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


def _load_rules_md() -> str:
    """Read RULES.md from the repo root so the LLM sees the canonical
    mechanics doc the project maintains. Returns empty string if the
    file isn't reachable (e.g., the agents package was installed standalone
    without the surrounding repo) — in that case the curated `_PROMPT_HEAD`
    still ships the scoring objective + output-format contract.
    """
    try:
        return _RULES_MD_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


_RULES_MD: str = _load_rules_md()


SYSTEM_PROMPT: str = _PROMPT_HEAD + (
    "\n\n# Canonical mechanics (RULES.md)\n\n"
    "The block below is the live `RULES.md` from the repo. Every\n"
    "formula, threshold, and error key in it is the source of truth\n"
    "for what the world will accept and how it will respond. Use it\n"
    "when picking actions — when this section and the primer above\n"
    "disagree, this section wins.\n\n" + _RULES_MD
    if _RULES_MD
    else ""
)


# ---------- Action tools schema --------------------------------------------


def _build_action_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "build",
            "description": (
                "Place a tile at (x, y). Civilian tiles must be adjacent to a road. "
                "Coal, gas, and wind impose a one-cell no-build halo (roads and "
                "batteries are admitted inside it). Returns ok=false with "
                "error='no_road_adjacency' / 'spacing_violation' / 'occupied' / "
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

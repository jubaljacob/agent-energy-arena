"""Build catalog for civilian and (later) energy tiles.

Costs and per-tile attributes from §4.12 of the design brief. Slice 02 lights
up only the civilian tiles plus the immutable town hall; energy plants and
wells land in slices 05 and 07 respectively.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TileSpec:
    tile_type: str
    capex: float
    opex_per_day: float
    requires_road: bool
    description: str
    housing_capacity: int = 0
    jobs: int = 0
    # Peak electric demand in kW for this tile type. Commercial scales by an
    # hourly factor (full 8-20h, 20% otherwise); industrial draws this value
    # continuously. See world.power.total_demand_kw.
    demand_kw: float = 0.0
    buildable: bool = True  # False = not placeable via /build (town_hall, wells)


TILE_CATALOG: dict[str, TileSpec] = {
    "road": TileSpec(
        tile_type="road",
        capex=500,
        opex_per_day=0,
        requires_road=False,
        description="Enables connectivity for civilian tiles.",
    ),
    "house": TileSpec(
        tile_type="house",
        capex=3_000,
        opex_per_day=20,
        requires_road=True,
        description="+8 housing capacity. Requires road adjacency.",
        housing_capacity=8,
    ),
    "commercial": TileSpec(
        tile_type="commercial",
        capex=8_000,
        opex_per_day=50,
        requires_road=True,
        description="+12 jobs. 50 kW peak demand (8-20h, 20% otherwise). Requires road adjacency.",
        jobs=12,
        demand_kw=50,
    ),
    "industrial": TileSpec(
        tile_type="industrial",
        capex=20_000,
        opex_per_day=200,
        requires_road=True,
        description="+30 jobs. 300 kW continuous demand. Requires road adjacency.",
        jobs=30,
        demand_kw=300,
    ),
    "park": TileSpec(
        tile_type="park",
        capex=5_000,
        opex_per_day=30,
        requires_road=False,
        description="Boosts happiness; no road requirement.",
    ),
    "pipeline": TileSpec(
        tile_type="pipeline",
        capex=2_000,
        opex_per_day=5,
        requires_road=False,
        description="Aesthetic / connectivity tile (v1: no transport cost).",
    ),
    "town_hall": TileSpec(
        tile_type="town_hall",
        capex=0,
        opex_per_day=0,
        requires_road=False,
        description="Civic center; counts as road for adjacency. Immutable.",
        housing_capacity=100,
        jobs=30,
        buildable=False,
    ),
}


def build_catalog() -> dict[str, Any]:
    tiles = []
    for spec in TILE_CATALOG.values():
        tiles.append(
            {
                "tile_type": spec.tile_type,
                "capex": spec.capex,
                "opex_per_day": spec.opex_per_day,
                "requires_road": spec.requires_road,
                "description": spec.description,
                "housing_capacity": spec.housing_capacity,
                "jobs": spec.jobs,
                "demand_kw": spec.demand_kw,
                "buildable": spec.buildable,
            }
        )
    return {"tiles": tiles, "wells": []}


def is_buildable(tile_type: str) -> bool:
    spec = TILE_CATALOG.get(tile_type)
    return bool(spec and spec.buildable)

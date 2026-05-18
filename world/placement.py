"""Build-time spacing matrix for halo'd power facilities.

Coal, gas, and wind generators impose a one-cell no-build halo on the
8-neighborhood. Roads and batteries are admitted inside the halo so plants
can still be serviced and storage co-located. Solar and battery candidates
impose no halo of their own; their placement is rejected only by other
rules. town_hall counts as a road (see `world.grid.ROAD_TYPES`), so it is
admitted as well.

Validation runs at build time only — existing tiles violating the rule at
release are grandfathered (the rule is consulted by `World.build` and
nowhere else).
"""

from __future__ import annotations

from collections.abc import Iterable

from world.state import Tile

# Halo'd candidate types. Adding a tile_type here turns on the
# 8-neighborhood spacing check for that candidate.
HALO_TYPES: frozenset[str] = frozenset({"coal_plant", "gas_peaker", "wind_turbine"})

# Neighbor tile types that are admitted inside any halo. Roads thread
# logistics; batteries co-locate with plants for self-firming. town_hall is
# road-network-equivalent (see `world.grid.ROAD_TYPES`).
HALO_ADMITTED_NEIGHBORS: frozenset[str] = frozenset({"road", "battery", "town_hall"})

# Fixed scan order for the 8-neighborhood. Returning the first non-admitted
# neighbor in this order makes the rejection deterministic when multiple
# tiles violate the halo.
_NEIGHBOR_OFFSETS: tuple[tuple[int, int], ...] = (
    (-1, -1),
    (0, -1),
    (1, -1),
    (-1, 0),
    (1, 0),
    (-1, 1),
    (0, 1),
    (1, 1),
)


def validate(
    candidate_type: str,
    candidate_coords: tuple[int, int],
    tiles: Iterable[Tile],
) -> Tile | None:
    """Return the offending neighbor for a halo'd candidate, else None.

    A non-halo'd candidate returns None unconditionally. A halo'd candidate
    scans the 8-neighborhood in a fixed (dx, dy) order and returns the
    first existing tile that is not in HALO_ADMITTED_NEIGHBORS. If no tile
    violates the halo, returns None.
    """
    if candidate_type not in HALO_TYPES:
        return None
    cx, cy = candidate_coords
    by_pos: dict[tuple[int, int], Tile] = {(t.x, t.y): t for t in tiles}
    for dx, dy in _NEIGHBOR_OFFSETS:
        neighbor = by_pos.get((cx + dx, cy + dy))
        if neighbor is None:
            continue
        if neighbor.type in HALO_ADMITTED_NEIGHBORS:
            continue
        return neighbor
    return None

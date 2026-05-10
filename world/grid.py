"""Surface-grid helpers: bounds checks and road-network adjacency.

The road network is the connected component (4-connected) of road and
town-hall tiles that contains the town hall. A new civilian tile that
requires road adjacency must have at least one orthogonal neighbor inside
this network — i.e. an island road in a corner cannot anchor a house.
"""

from __future__ import annotations

from collections.abc import Iterable

from world.state import Tile

# Tile types that participate in the road network for adjacency purposes.
ROAD_TYPES: frozenset[str] = frozenset({"road", "town_hall"})


def in_bounds(x: int, y: int, w: int, h: int) -> bool:
    return 0 <= x < w and 0 <= y < h


def road_connected_set(tiles: Iterable[Tile], world_w: int, world_h: int) -> set[tuple[int, int]]:
    """4-connected flood-fill of road/town_hall tiles starting from town hall.

    Returns the set of (x, y) coordinates reachable. Empty if no town hall
    exists (which should not happen post-reset, but the function stays
    defensive).
    """
    by_pos: dict[tuple[int, int], Tile] = {(t.x, t.y): t for t in tiles}
    start: tuple[int, int] | None = None
    for pos, t in by_pos.items():
        if t.type == "town_hall":
            start = pos
            break
    if start is None:
        return set()

    seen: set[tuple[int, int]] = {start}
    stack: list[tuple[int, int]] = [start]
    while stack:
        x, y = stack.pop()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if not in_bounds(nx, ny, world_w, world_h):
                continue
            if (nx, ny) in seen:
                continue
            tile = by_pos.get((nx, ny))
            if tile is None or tile.type not in ROAD_TYPES:
                continue
            seen.add((nx, ny))
            stack.append((nx, ny))
    return seen


def has_road_adjacency(x: int, y: int, tiles: Iterable[Tile], world_w: int, world_h: int) -> bool:
    """True iff (x, y) has an orthogonal neighbor inside the town-hall road network."""
    network = road_connected_set(tiles, world_w, world_h)
    if not network:
        return False
    return any((x + dx, y + dy) in network for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))

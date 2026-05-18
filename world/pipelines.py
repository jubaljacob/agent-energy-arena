"""Pipeline graph helpers: 4-connected components and per-network routing units.

A *pipeline tile* is a `Tile` with `type == "pipeline"`. Two pipeline tiles
belong to the same component iff they are orthogonally adjacent (Manhattan
distance 1); diagonals do not connect. A well or refinery belongs to a
component iff one of its four orthogonal neighbours is a pipeline tile in
that component; otherwise it is an *orphan* with respect to crude routing.

This module is intentionally pure — it has no `World` dependency, does not
mutate its inputs, and is testable without a sim instance. Sim integration
(per-network `route_crude` aggregation and orphan accounting) lives in
`world/sim.py`.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from world.state import Tile, Well

_ORTHO: tuple[tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))


def pipeline_components(
    tiles: Iterable[Tile], world_w: int, world_h: int
) -> list[set[tuple[int, int]]]:
    """Return 4-connected components of pipeline tiles as sets of `(x, y)`.

    Components are returned in deterministic order: the lowest-(y, x) cell
    of each component seeds it, and components are ordered by their seed.
    """
    pipes: set[tuple[int, int]] = {(t.x, t.y) for t in tiles if t.type == "pipeline"}
    seen: set[tuple[int, int]] = set()
    components: list[set[tuple[int, int]]] = []
    for start in sorted(pipes, key=lambda p: (p[1], p[0])):
        if start in seen:
            continue
        comp: set[tuple[int, int]] = {start}
        seen.add(start)
        queue: deque[tuple[int, int]] = deque([start])
        while queue:
            x, y = queue.popleft()
            for dx, dy in _ORTHO:
                nx, ny = x + dx, y + dy
                if not (0 <= nx < world_w and 0 <= ny < world_h):
                    continue
                if (nx, ny) in seen:
                    continue
                if (nx, ny) not in pipes:
                    continue
                seen.add((nx, ny))
                comp.add((nx, ny))
                queue.append((nx, ny))
        components.append(comp)
    return components


def routing_units(
    tiles: Iterable[Tile], wells: Iterable[Well]
) -> tuple[
    list[tuple[list[Well], list[Tile]]],
    list[Well],
    list[Tile],
]:
    """Group wells and refineries by 4-connected pipeline component.

    Returns ``(networks, orphan_wells, orphan_refineries)`` where each
    network is ``(wells_in_network, refineries_in_network)``. A well or
    refinery is assigned to a component iff one of its orthogonal
    neighbours is a pipeline tile in that component. Anything with no
    pipeline neighbour goes to the orphan list. Components that end up
    with neither a well nor a refinery are dropped from `networks`.

    A well with pipeline neighbours in multiple components is assigned to
    the first one found (stable by component index).
    """
    tiles_list = list(tiles)
    wells_list = list(wells)

    # The bounds passed to pipeline_components only filter out-of-range
    # neighbours; since pipeline coordinates come from the input tiles
    # themselves, any bound larger than the maximum tile coordinate is
    # safe. Derive one from the inputs so callers don't have to plumb
    # world_w / world_h through.
    max_xy = 0
    for t in tiles_list:
        if t.x > max_xy:
            max_xy = t.x
        if t.y > max_xy:
            max_xy = t.y
    for wl in wells_list:
        if wl.x > max_xy:
            max_xy = wl.x
        if wl.y > max_xy:
            max_xy = wl.y
    bound = max_xy + 2

    components = pipeline_components(tiles_list, bound, bound)
    pos_to_comp: dict[tuple[int, int], int] = {}
    for idx, comp in enumerate(components):
        for pos in comp:
            pos_to_comp[pos] = idx

    refineries: list[Tile] = [t for t in tiles_list if t.type == "refinery"]

    network_wells: list[list[Well]] = [[] for _ in components]
    network_refs: list[list[Tile]] = [[] for _ in components]
    orphan_wells: list[Well] = []
    orphan_refineries: list[Tile] = []

    for wl in wells_list:
        comp_idx = _first_neighbour_component(wl.x, wl.y, pos_to_comp)
        if comp_idx is None:
            orphan_wells.append(wl)
        else:
            network_wells[comp_idx].append(wl)

    for ref in refineries:
        comp_idx = _first_neighbour_component(ref.x, ref.y, pos_to_comp)
        if comp_idx is None:
            orphan_refineries.append(ref)
        else:
            network_refs[comp_idx].append(ref)

    networks: list[tuple[list[Well], list[Tile]]] = [
        (network_wells[i], network_refs[i])
        for i in range(len(components))
        if network_wells[i] or network_refs[i]
    ]
    return networks, orphan_wells, orphan_refineries


def _first_neighbour_component(
    x: int, y: int, pos_to_comp: dict[tuple[int, int], int]
) -> int | None:
    for dx, dy in _ORTHO:
        idx = pos_to_comp.get((x + dx, y + dy))
        if idx is not None:
            return idx
    return None


def _neighbour_components(x: int, y: int, pos_to_comp: dict[tuple[int, int], int]) -> set[int]:
    found: set[int] = set()
    for dx, dy in _ORTHO:
        idx = pos_to_comp.get((x + dx, y + dy))
        if idx is not None:
            found.add(idx)
    return found


def peaker_supply(peaker_tile: Tile, tiles: Iterable[Tile]) -> bool:
    """True iff the gas peaker shares a 4-connected pipeline network with at
    least one operational refinery.

    The peaker is "on a network" iff one of its four orthogonal neighbours is
    a pipeline tile in that component (mirrors the well/refinery rule in
    `routing_units`). Diagonal adjacency does not connect. Non-operational
    refineries do not count as supply — destroying a refinery makes every
    peaker on its network unsupplied on the next call.
    """
    tiles_list = list(tiles)

    # Same bound derivation as `routing_units`: the bound only filters
    # out-of-range pipeline neighbours, so any value strictly larger than
    # the max input coord is safe.
    max_xy = 0
    for t in tiles_list:
        if t.x > max_xy:
            max_xy = t.x
        if t.y > max_xy:
            max_xy = t.y
    if peaker_tile.x > max_xy:
        max_xy = peaker_tile.x
    if peaker_tile.y > max_xy:
        max_xy = peaker_tile.y
    bound = max_xy + 2

    components = pipeline_components(tiles_list, bound, bound)
    pos_to_comp: dict[tuple[int, int], int] = {}
    for idx, comp in enumerate(components):
        for pos in comp:
            pos_to_comp[pos] = idx

    peaker_comps = _neighbour_components(peaker_tile.x, peaker_tile.y, pos_to_comp)
    if not peaker_comps:
        return False

    for t in tiles_list:
        if t.type != "refinery" or not t.operational:
            continue
        ref_comps = _neighbour_components(t.x, t.y, pos_to_comp)
        if ref_comps & peaker_comps:
            return True
    return False

"""Workforce allocator (PRD: World v2 — Workforce & Per-Facility Staffing).

Pure functions, no RNG, no I/O. Owns the entire labor calculus behind a small
surface:

- ``efficiency(tile_or_well)`` — staffing ratio in [0, 1] (1.0 for passive tiles).
- ``employed(state)`` — total ``staffed_jobs`` across tiles + wells.
- ``unemployed(state)`` — ``max(0, population - employed(state))``.
- ``producers(state)`` — tiles+wells with ``spec.jobs > 0``, oldest-first.
- ``hire_to_fill(state)`` — fills vacancies oldest-first until the unemployed
  pool is empty.
- ``drain_n(state, n)`` — drains ``n`` people: unemployed first, then
  newest-first fire of staffed producers (decrements both ``staffed_jobs`` and
  ``state.population``).

Ordering is deterministic ``(creation_day, id_string)`` ascending — no RNG draws.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Union

from world.catalog import TILE_CATALOG

if TYPE_CHECKING:
    from world.state import Tile, Well, WorldState

Producer = Union["Tile", "Well"]


def _spec_jobs(item: Producer) -> int:
    """Catalog-declared job count for a tile or well."""
    from world.state import Tile

    if isinstance(item, Tile):
        spec = TILE_CATALOG.get(item.type)
        return spec.jobs if spec is not None else 0
    spec_type = "oil_well" if item.type == "production" else "injection_well"
    spec = TILE_CATALOG.get(spec_type)
    return spec.jobs if spec is not None else 0


def _creation_day(item: Producer) -> int:
    from world.state import Tile

    if isinstance(item, Tile):
        return item.built_day
    return item.drilled_day


def efficiency(item: Producer) -> float:
    """Return ``staffed_jobs / spec.jobs`` clamped to [0, 1].

    Passive tiles (``spec.jobs == 0``) always return 1.0 — they have no
    workforce dependency.
    """
    jobs = _spec_jobs(item)
    if jobs <= 0:
        return 1.0
    ratio = item.staffed_jobs / jobs
    if ratio < 0.0:
        return 0.0
    if ratio > 1.0:
        return 1.0
    return ratio


def employed(state: WorldState) -> int:
    total = 0
    for t in state.tiles:
        total += t.staffed_jobs
    for w in state.wells:
        total += w.staffed_jobs
    return total


def unemployed(state: WorldState) -> int:
    return max(0, int(state.population) - employed(state))


def total_jobs(state: WorldState) -> int:
    """Job slots across tiles + wells.

    Tiles carry a build-time snapshot in ``Tile.jobs`` (see ``state.py``); we
    use that so retunes don't retroactively shift live cities. Wells have no
    ``.jobs`` field on the dataclass, so they read from the catalog via
    ``_spec_jobs``. The sum stays consistent with ``employed()``, which is
    why the agent-facing ``jobs_vacant`` field and the growth model in
    ``world.population`` both call this rather than rolling their own sum.
    """
    total = 0
    for t in state.tiles:
        total += t.jobs
    for w in state.wells:
        total += _spec_jobs(w)
    return total


def producers(state: WorldState) -> Iterable[Producer]:
    """Yield staffable tiles and wells sorted by ``(creation_day, id)``."""
    items: list[Producer] = []
    for t in state.tiles:
        if _spec_jobs(t) > 0:
            items.append(t)
    for w in state.wells:
        if _spec_jobs(w) > 0:
            items.append(w)
    items.sort(key=lambda p: (_creation_day(p), p.id))
    return items


def hire_to_fill(state: WorldState) -> None:
    """Fill vacancies oldest-first from the unemployed pool."""
    pool = unemployed(state)
    if pool <= 0:
        return
    for p in producers(state):
        if pool <= 0:
            break
        vacancy = _spec_jobs(p) - p.staffed_jobs
        if vacancy <= 0:
            continue
        hire = min(vacancy, pool)
        p.staffed_jobs += hire
        pool -= hire


def drain_n(state: WorldState, n: int) -> None:
    """Drain ``n`` people from the city.

    First decrement ``state.population`` by ``min(n, unemployed(state))`` — the
    unemployed leave silently. If ``n`` is still positive, fire workers
    newest-first, decrementing both ``staffed_jobs`` and ``population`` one at
    a time until ``n`` is exhausted or every producer is empty.
    """
    if n <= 0:
        return
    unemp = unemployed(state)
    silent = min(n, unemp)
    state.population -= silent
    n -= silent
    if n <= 0:
        return
    fire_order = list(producers(state))
    fire_order.reverse()
    for p in fire_order:
        while n > 0 and p.staffed_jobs > 0:
            p.staffed_jobs -= 1
            state.population -= 1
            n -= 1
        if n <= 0:
            return

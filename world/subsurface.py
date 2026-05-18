"""3D voxel grid for the subsurface, plus seismic survey mechanic.

Implements §3.5 (reservoir generation), §4.10 (seismic survey) of the brief
and the oilfield-v2 quadratic survey-cost override
(`cost = 15_000 × (size/4)²`; size-4 is the cheapest legal column).
The voxel grid is generated at world reset from `sim_rng`; surveys also draw
from `sim_rng` (they happen between `/step` calls so the per-day RNG-budget
contract that anchors step-size invariance is unaffected).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# Per-voxel oil capacity, named "VOXEL_VOLUME_BBL" in §3.5 of the brief and
# explicitly flagged there as a "calibration constant". An earlier slice
# tuned this to 700_000 so seed 42 landed at ~6.7M bbl OOIP, but at
# Q_MAX_WELL_BBL_DAY = 200 a single producer drains <5% of a typical
# 36-voxel reservoir over a 10-year game — the geology is undepletable
# on the game's time horizon, so the depletion signal stays invisible to
# both the player and the LLM agent. The reservoir-scale rescale dropped
# the constant 10× to 70_000; the economy-rebalance pass then dropped it
# another 20% to 56_000 so depletion becomes a credible mid-to-late-game
# pressure within a typical play horizon (seed-42 total OOIP ~622k bbl,
# down from ~777k). Reservoir geometry (blob count, radius range,
# HC_PROBABILITY_BASE) is unchanged — only per-voxel volumetrics shrink.
VOXEL_VOLUME_BBL = 56_000.0

# Survey constants (§4.10 + oilfield-v2 §"Survey rescale": cost is
# 15_000 × (size/4)² so a size-4 column costs $15k and a size-8 column
# costs $60k. Default UI survey size is 4 — the cheapest legal column.
SEISMIC_BASE_COST = 15_000.0
SEISMIC_DEFAULT_SIZE = 4
SEISMIC_MIN_SIZE = 4
SEISMIC_MAX_SIZE = 16
SEISMIC_OIL_SIGMA = 0.25
SEISMIC_PERM_SIGMA = 0.30

# Reservoir generation (§3.5).
N_RESERVOIRS_MIN = 3
N_RESERVOIRS_MAX = 7
BLOB_RADIUS_MIN = 3
BLOB_RADIUS_MAX = 6
HC_PROBABILITY_BASE = 0.6
POROSITY_MIN, POROSITY_MAX = 0.10, 0.30
PERM_LOG_MIN, PERM_LOG_MAX = 10.0, 1000.0
OIL_SAT_MIN, OIL_SAT_MAX = 0.55, 0.80

# Well production (brief §4.5).
Q_MAX_WELL_BBL_DAY: float = 200.0
PERM_NORMALIZATION_MD: float = 500.0  # divides mean(perm) so k_eff is dimensionless
CRUDE_PRICE_USD_PER_BBL: float = 40.0
WELL_SETPOINT_MIN: float = 0.0
WELL_SETPOINT_MAX: float = Q_MAX_WELL_BBL_DAY

# Injection wells (brief §4.5 + PRD DR mechanic).
INJECTION_KWH_PER_BBL: float = 50.0
PRESSURE_BOOST_MAX: float = 0.5

# Production wells (economy-rebalance slice 07). Symmetric to injection's
# kWh/bbl coupling but at a smaller magnitude — producers do less lifting
# work per barrel than injectors do compressing water. Consumed by both
# `world.pricing.well_production_kwh_per_day` (informational popup) and the
# hourly throttling block in `world.sim` that caps actual rate at
# power_kw / PRODUCTION_KWH_PER_BBL when the grid is under-supplied.
PRODUCTION_KWH_PER_BBL: float = 15.0


@dataclass
class Voxel:
    x: int
    y: int
    z: int
    porosity: float
    permeability: float  # mD
    oil_saturation: float
    oil_in_place_bbl: float
    oil_remaining_bbl: float
    # 1-indexed reservoir tag assigned at generation time. Every HC voxel
    # belongs to exactly one reservoir; the BFS percolation generator
    # guarantees that all voxels with the same `reservoir_id` form a single
    # 26-connected component. Non-HC voxels are absent from `grid.voxels`
    # entirely, so the 0 default is only reachable by test-only construction.
    reservoir_id: int = 0
    # Append-only history of survey readings — one entry per survey that
    # included this voxel in its column. Resurveys produce independent noise
    # samples (PRD §"Subsurface").
    estimates: list[dict[str, float]] = field(default_factory=list)


@dataclass
class SubsurfaceGrid:
    width: int
    height: int
    depth: int
    # Sparse: only hydrocarbon-bearing voxels are stored. Empty rock has
    # oil_in_place=0 and permeability=0; surveys over empty rock return
    # estimates of 0 without recording history.
    voxels: dict[tuple[int, int, int], Voxel] = field(default_factory=dict)
    # (x, y) columns that have been surveyed at least once. Used for the
    # "n_explored_columns" aggregate in `/state.reservoirs_revealed`.
    explored_columns: set[tuple[int, int]] = field(default_factory=set)

    def get(self, x: int, y: int, z: int) -> Voxel | None:
        return self.voxels.get((x, y, z))

    def total_oil_in_place(self) -> float:
        return sum(v.oil_in_place_bbl for v in self.voxels.values())


def survey_cost(size: int) -> float:
    """Oilfield-v2 quadratic cost: 15_000 × (size / 4)². A size-4 column
    costs the base $15k; a size-8 column costs $60k; a size-16 column
    costs $240k."""
    return SEISMIC_BASE_COST * (size / SEISMIC_DEFAULT_SIZE) ** 2


def is_size_valid(size: int) -> bool:
    return SEISMIC_MIN_SIZE <= size <= SEISMIC_MAX_SIZE


def drill_capex(base_capex: float, target_z: int, world_d: int) -> float:
    """Quadratic-in-depth drilling cost: `base * (1 + (target_z / world_d)**2)`.

    Applies uniformly to production and injection wells (the caller passes
    the per-well-type base). At `target_z = 0` returns `base`; at deeper z
    the cost grows quadratically so deeper drainage targets cost more. The
    formula string is mirrored in `/catalog.subsurface.drill.{production,
    injection}.cost_formula` so UI / agent clients can replicate it.
    """
    return base_capex * (1.0 + (target_z / world_d) ** 2)


def _neighbors_26(
    x: int, y: int, z: int, width: int, height: int, depth: int
) -> list[tuple[int, int, int]]:
    """26-connected in-bounds neighbors of (x, y, z), in fixed iteration order
    (dx -1..1, dy -1..1, dz -1..1; skipping the origin). Deterministic order
    matters for the BFS frontier so seed-42 reproduces byte-identically."""
    out: list[tuple[int, int, int]] = []
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                if dx == 0 and dy == 0 and dz == 0:
                    continue
                nx, ny, nz = x + dx, y + dy, z + dz
                if 0 <= nx < width and 0 <= ny < height and 0 <= nz < depth:
                    out.append((nx, ny, nz))
    return out


def _make_voxel(rng: np.random.Generator, x: int, y: int, z: int, reservoir_id: int) -> Voxel:
    """Draw porosity, permeability, oil-saturation from §3.5 distributions
    and build the Voxel. Always consumes exactly three RNG draws so the
    per-voxel cost is independent of acceptance bookkeeping."""
    porosity = float(rng.uniform(POROSITY_MIN, POROSITY_MAX))
    perm = float(np.exp(rng.uniform(np.log(PERM_LOG_MIN), np.log(PERM_LOG_MAX))))
    s_o = float(rng.uniform(OIL_SAT_MIN, OIL_SAT_MAX))
    oip = porosity * s_o * VOXEL_VOLUME_BBL
    return Voxel(
        x=x,
        y=y,
        z=z,
        porosity=porosity,
        permeability=perm,
        oil_saturation=s_o,
        oil_in_place_bbl=oip,
        oil_remaining_bbl=oip,
        reservoir_id=reservoir_id,
    )


def generate_subsurface(
    rng: np.random.Generator, width: int, height: int, depth: int
) -> SubsurfaceGrid:
    """Place 3-7 reservoir blobs via BFS percolation (oilfield-v2 PRD §1).

    For each blob:
      * Pick a seed (cx, cy, cz) with cz ∈ [4, depth-2] and radius r ∈ [3, 6].
      * The seed voxel is always accepted (unless already claimed by an
        earlier blob, in which case the entire blob is skipped).
      * Expand via a FIFO frontier seeded with the 26-connected neighbors of
        the seed. A candidate is accepted iff:
          - within Manhattan distance r of the seed,
          - passes p = HC_PROBABILITY_BASE × (1 - dist/r),
          - not already claimed (by this or any previous blob), and
          - has ≥ 1 already-accepted same-blob neighbor in its 3×3×3
            neighborhood (guaranteed-by-construction here because the
            frontier is fed exclusively by accepted neighbors).
      * Accepted voxels are tagged with `reservoir_id = blob_idx + 1`.

    Each blob is a single 26-connected component because the frontier never
    advances past a non-accepted voxel; two blobs that spawn adjacent stay
    distinct because each voxel is claimed by exactly one blob.
    """
    grid = SubsurfaceGrid(width=width, height=height, depth=depth)
    n_blobs = int(rng.integers(N_RESERVOIRS_MIN, N_RESERVOIRS_MAX + 1))

    z_lo = 4
    z_hi = depth - 2  # inclusive
    if z_hi < z_lo:
        return grid  # degenerate world dimensions; no reservoirs

    for blob_idx in range(n_blobs):
        cx = int(rng.integers(0, width))
        cy = int(rng.integers(0, height))
        cz = int(rng.integers(z_lo, z_hi + 1))
        r = int(rng.integers(BLOB_RADIUS_MIN, BLOB_RADIUS_MAX + 1))
        reservoir_id = blob_idx + 1

        if not (0 <= cx < width and 0 <= cy < height and 0 <= cz < depth):
            continue
        if (cx, cy, cz) in grid.voxels:
            # Seed already claimed by an earlier blob. Skip without
            # consuming the property-RNG quota — the blob simply yields no
            # voxels.
            continue

        grid.voxels[(cx, cy, cz)] = _make_voxel(rng, cx, cy, cz, reservoir_id)

        # `attempted` tracks every cell this blob has rolled (whether
        # accepted or rejected) so duplicate frontier entries don't burn
        # extra RNG draws. The seed is implicitly in here — we never re-add
        # the seed to the frontier.
        attempted: set[tuple[int, int, int]] = {(cx, cy, cz)}
        frontier: deque[tuple[int, int, int]] = deque(
            _neighbors_26(cx, cy, cz, width, height, depth)
        )

        while frontier:
            x, y, z = frontier.popleft()
            if (x, y, z) in attempted:
                continue
            attempted.add((x, y, z))
            if (x, y, z) in grid.voxels:
                continue  # claimed by an earlier blob; no RNG draws here
            dist = abs(x - cx) + abs(y - cy) + abs(z - cz)
            if dist > r:
                continue
            p_hc = HC_PROBABILITY_BASE * (1.0 - dist / r) if r > 0 else 0.0
            roll = float(rng.random())
            if roll >= p_hc:
                continue
            grid.voxels[(x, y, z)] = _make_voxel(rng, x, y, z, reservoir_id)
            for nb in _neighbors_26(x, y, z, width, height, depth):
                if nb in attempted:
                    continue
                frontier.append(nb)

    return grid


def voxel_reservoir_id(grid: SubsurfaceGrid, x: int, y: int, z: int) -> int | None:
    """Return the `reservoir_id` of the voxel at (x, y, z), or None if the
    cell is non-HC (no entry in `grid.voxels`)."""
    v = grid.get(x, y, z)
    return None if v is None else v.reservoir_id


def well_reservoir_id(grid: SubsurfaceGrid, x: int, y: int, target_z: int) -> int | None:
    """Return the `reservoir_id` of the voxel a well at (x, y) would target
    at `target_z`. Drilling into rock (non-HC voxel) returns None — the well
    is recorded but has no reservoir affiliation."""
    return voxel_reservoir_id(grid, x, y, target_z)


def _column_bounds(x: int, y: int, size: int, width: int, height: int) -> tuple[int, int, int, int]:
    """Return the in-bounds [x0, x1) × [y0, y1) range for a survey centered
    at (x, y) with side `size`. Clipped to the grid; no padding."""
    half = size // 2
    x0 = max(0, x - half)
    y0 = max(0, y - half)
    x1 = min(width, x - half + size)
    y1 = min(height, y - half + size)
    return x0, y0, x1, y1


def survey(
    grid: SubsurfaceGrid,
    rng: np.random.Generator,
    x: int,
    y: int,
    size: int,
    survey_day: int,
) -> list[dict[str, Any]]:
    """Reveal a `size × size × depth` column. Returns per-voxel estimates
    with §4.10 noise; appends an entry to each HC voxel's history."""
    x0, y0, x1, y1 = _column_bounds(x, y, size, grid.width, grid.height)
    out: list[dict[str, Any]] = []
    for vy in range(y0, y1):
        for vx in range(x0, x1):
            grid.explored_columns.add((vx, vy))
            for vz in range(grid.depth):
                v = grid.get(vx, vy, vz)
                if v is not None:
                    oil_noise = float(rng.normal(0.0, SEISMIC_OIL_SIGMA))
                    perm_noise = float(rng.normal(0.0, SEISMIC_PERM_SIGMA))
                    oil_est = max(0.0, v.oil_in_place_bbl * (1.0 + oil_noise))
                    perm_est = max(0.0, v.permeability * (1.0 + perm_noise))
                    v.estimates.append(
                        {
                            "survey_day": float(survey_day),
                            "oil_estimate_bbl": oil_est,
                            "perm_estimate_md": perm_est,
                        }
                    )
                else:
                    oil_est = 0.0
                    perm_est = 0.0
                out.append(
                    {
                        "x": vx,
                        "y": vy,
                        "z": vz,
                        "oil_estimate_bbl": oil_est,
                        "perm_estimate_md": perm_est,
                    }
                )
    return out


def _latest_estimate(v: Voxel) -> tuple[float, float, int]:
    """Return (oil_est, perm_est, survey_day) for a voxel's most recent
    survey reading. Caller must check `v.estimates` is non-empty."""
    last = v.estimates[-1]
    return (
        float(last["oil_estimate_bbl"]),
        float(last["perm_estimate_md"]),
        int(last["survey_day"]),
    )


def revealed_voxels(
    grid: SubsurfaceGrid, *, min_oil: float = 0.0, top_k: int | None = None
) -> list[dict[str, Any]]:
    """Voxels with at least one survey reading, ranked by latest
    oil_estimate × perm_estimate descending, optionally filtered by min_oil
    on the latest oil_estimate."""
    rows: list[dict[str, Any]] = []
    for v in grid.voxels.values():
        if not v.estimates:
            continue
        oil_est, perm_est, day = _latest_estimate(v)
        if oil_est < min_oil:
            continue
        rows.append(
            {
                "x": v.x,
                "y": v.y,
                "z": v.z,
                "reservoir_id": v.reservoir_id,
                "oil_estimate_bbl": oil_est,
                "perm_estimate_md": perm_est,
                "survey_day": day,
                "n_surveys": len(v.estimates),
            }
        )
    rows.sort(key=lambda r: r["oil_estimate_bbl"] * r["perm_estimate_md"], reverse=True)
    if top_k is not None:
        rows = rows[: max(0, top_k)]
    return rows


def voxels_in_3x3x3(grid: SubsurfaceGrid, x: int, y: int, target_z: int) -> tuple[list[Voxel], int]:
    """Return (hc_voxels, n_positions) for the 3×3×3 pool centered on
    (x, y, target_z), clipped to grid bounds (no padding).

    `n_positions` is the count of in-grid cells in the pool — including
    non-HC ones, which contribute oil_in_place=0 and permeability=0
    implicitly. The mean-of-perm in §4.5 divides by `n_positions`, not by
    the number of HC voxels, so non-HC cells dilute k_eff as the brief
    requires.
    """
    pool: list[Voxel] = []
    n_positions = 0
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                vx, vy, vz = x + dx, y + dy, target_z + dz
                if not (0 <= vx < grid.width and 0 <= vy < grid.height and 0 <= vz < grid.depth):
                    continue
                n_positions += 1
                v = grid.get(vx, vy, vz)
                if v is not None:
                    pool.append(v)
    return pool, n_positions


def pools_intersect(ax: int, ay: int, az: int, bx: int, by: int, bz: int) -> bool:
    """Two 3×3×3 pools intersect iff their centers differ by at most 2 on each axis."""
    return abs(ax - bx) <= 2 and abs(ay - by) <= 2 and abs(az - bz) <= 2


def drill_collision(
    wells: list[Any],
    tiles: list[Any],
    x: int,
    y: int,
    target_z: int,
) -> str | None:
    """Predicate for whether a new drill at (x, y, target_z) is blocked.

    Returns the error key, or None if the new completion is legal:
      - "tile_occupied" when a non-well build (road / refinery / pipeline …)
        occupies the surface tile at (x, y).
      - "completion_overlap" when an existing well shares (x, y) and its
        target_z is within 2 of the new target_z (so the two 3×3×3
        drainage cubes would overlap on the z-axis). The legal stacked-
        completion rule is |Δtarget_z| ≥ 3.
      - None otherwise.

    Tile-occupied is checked first so a road-blocked surface reports the
    tile error rather than a stacked-completion error when both would apply.
    Duck-typed on the `.x` / `.y` (and `.target_z` for wells) attributes.
    """
    for t in tiles:
        if t.x == x and t.y == y:
            return "tile_occupied"
    for w in wells:
        if w.x == x and w.y == y and abs(w.target_z - target_z) < 3:
            return "completion_overlap"
    return None


def well_production_bbl_day(
    grid: SubsurfaceGrid,
    x: int,
    y: int,
    target_z: int,
    setpoint_rate_bbl_day: float,
    *,
    qualifying_inj_rate_bbl_day: float = 0.0,
    producer_yesterday_rate_bbl_day: float = 0.0,
    efficiency: float = 1.0,
) -> float:
    """Run the brief §4.5 production formula for one day. Mutates
    `oil_remaining_bbl` on the pool's HC voxels by perm × remaining
    weights, and returns `q_actual` (bbl produced today).

    `qualifying_inj_rate_bbl_day` is the sum of `yesterday_rate_bbl_day`
    across injection wells that (a) share the producer's `reservoir_id`
    and (b) sit at Chebyshev distance > 1 from the producer's target
    voxel (avoiding the breakthrough gate). Caller (sim.py) computes
    this. `producer_yesterday_rate_bbl_day` is this producer's own
    `yesterday_rate_bbl_day` snapshot.

    Pressure term (oilfield-v2 §"Rate-based pressure"):
        pressure_boost = min(0.5, qualifying_inj_rate
                                  / max(producer_yesterday_rate, 1.0))
    On the day a well is drilled both yesterday rates are 0 so
    pressure_boost = 0 that day.

    `efficiency` is the staffing ratio in [0, 1] from
    ``workforce.efficiency(well)``; it scales the effective max
    production cap (``Q_MAX_WELL_BBL_DAY × efficiency``). The
    player-facing setpoint is **not** clamped by efficiency — only the
    realised throughput is. Idle wells (``efficiency=0``) produce 0
    bbl/day regardless of setpoint or reservoir.
    """
    pool, n_positions = voxels_in_3x3x3(grid, x, y, target_z)
    if n_positions == 0:
        return 0.0
    V_init = sum(v.oil_in_place_bbl for v in pool)
    if V_init <= 0.0:
        return 0.0
    V_remain = sum(v.oil_remaining_bbl for v in pool)
    # Invariant: a fully depleted pool produces nothing, even if a qualifying
    # injector would otherwise lift `effective_fraction` via `pressure_boost`.
    # Without this guard the drain loop short-circuits on `W = 0` but
    # `q_actual` still flows into `cumulative_produced_bbl` — the well prints
    # oil from a dead reservoir.
    if V_remain <= 0.0:
        return 0.0
    fraction = V_remain / V_init
    k_eff = sum(v.permeability for v in pool) / n_positions / PERM_NORMALIZATION_MD
    pressure_boost = min(
        PRESSURE_BOOST_MAX,
        qualifying_inj_rate_bbl_day / max(producer_yesterday_rate_bbl_day, 1.0),
    )
    effective_fraction = min(1.0, fraction + pressure_boost)
    effective_q_max = Q_MAX_WELL_BBL_DAY * efficiency
    q_potential = effective_q_max * k_eff * effective_fraction
    q_actual = max(0.0, min(float(setpoint_rate_bbl_day), q_potential))

    weights = [v.permeability * v.oil_remaining_bbl for v in pool]
    W = sum(weights)
    if W > 0.0 and q_actual > 0.0:
        for v, w in zip(pool, weights, strict=False):
            v.oil_remaining_bbl = max(0.0, v.oil_remaining_bbl - q_actual * w / W)
    return q_actual


def reservoirs_voxel_summary(grid: SubsurfaceGrid, *, top_k: int = 10) -> dict[str, Any]:
    """Bounded view for `/state.reservoirs_revealed` — top-K plus aggregates.

    Renamed from `reservoirs_summary` to free that name for the new
    per-reservoir rollup added in wells-reservoir-rollup #01; this helper
    still ships the per-voxel `top_k` strip plus the world-wide
    n_revealed / n_explored aggregates that the UI consumes.
    """
    revealed = [v for v in grid.voxels.values() if v.estimates]
    total_oil = 0.0
    for v in revealed:
        oil_est, _perm, _day = _latest_estimate(v)
        total_oil += oil_est
    return {
        "top_k": revealed_voxels(grid, top_k=top_k),
        "n_revealed_voxels": len(revealed),
        "total_estimated_oil_remaining_bbl": total_oil,
        "n_explored_columns": len(grid.explored_columns),
    }


def injector_supports(injector: Any, wells: list[Any]) -> list[str]:
    """Producer ids that an injection well currently qualifies to support.

    Owns the same-reservoir + Chebyshev > 1 gate used by the rate-based
    pressure_boost in `world.sim._advance_one_day`. A producer qualifies
    iff:
      * it shares `injector.reservoir_id` (both non-None), AND
      * its `(x, y, target_z)` sits at 3D Chebyshev distance strictly
        greater than 1 from the injector's `(x, y, target_z)` (the
        breakthrough gate).

    Injectors with `reservoir_id is None` (drilled into rock) always
    return `[]` — no reservoir to share. Non-injection wells passed in
    also return `[]`; the field exists on producer dicts only for type
    symmetry. Result is sorted by ascending producer-id string.
    """
    rid = getattr(injector, "reservoir_id", None)
    if rid is None:
        return []
    if getattr(injector, "type", None) != "injection":
        return []
    ix = int(injector.x)
    iy = int(injector.y)
    iz = int(injector.target_z)
    out: list[str] = []
    for w in wells:
        if getattr(w, "type", None) != "production":
            continue
        if getattr(w, "reservoir_id", None) != rid:
            continue
        cheb = max(
            abs(int(w.x) - ix),
            abs(int(w.y) - iy),
            abs(int(w.target_z) - iz),
        )
        if cheb <= 1:
            continue
        out.append(str(w.id))
    out.sort()
    return out


def engaged_voxels(grid: SubsurfaceGrid, wells: list[Any]) -> set[tuple[int, int, int]]:
    """Voxel positions reached by at least one well's 3×3×3 drainage cube.

    Geometric, reservoir-agnostic union: a well with `reservoir_id=None`
    (drilled into rock) still contributes its cube. Cube cells are clipped
    to grid bounds (no padding). The result is a set, so overlapping
    wells contribute via set union — never double-counted.
    """
    out: set[tuple[int, int, int]] = set()
    for w in wells:
        wx = int(w.x)
        wy = int(w.y)
        wz = int(w.target_z)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    vx, vy, vz = wx + dx, wy + dy, wz + dz
                    if 0 <= vx < grid.width and 0 <= vy < grid.height and 0 <= vz < grid.depth:
                        out.add((vx, vy, vz))
    return out


def engaged_summary(grid: SubsurfaceGrid, wells: list[Any]) -> dict[int, dict[str, Any]]:
    """Per-reservoir rollup of the engaged set's HC content.

    Walks `engaged_voxels(grid, wells)` in ascending (x, y, z) order
    (determinism: float-sum order is stable across runs, regardless of
    Python set iteration), groups by `reservoir_id`, and emits
    `{engaged_voxel_count, engaged_bbl, engaged_remaining_bbl}` per
    reservoir. Non-HC cells (no entry in `grid.voxels`) are skipped —
    they have no reservoir affiliation.
    """
    by_rid: dict[int, dict[str, Any]] = {}
    for key in sorted(engaged_voxels(grid, wells)):
        v = grid.voxels.get(key)
        if v is None:
            continue
        rid = int(v.reservoir_id)
        slot = by_rid.setdefault(
            rid,
            {
                "engaged_voxel_count": 0,
                "engaged_bbl": 0.0,
                "engaged_remaining_bbl": 0.0,
            },
        )
        slot["engaged_voxel_count"] += 1
        slot["engaged_bbl"] += float(v.oil_in_place_bbl)
        slot["engaged_remaining_bbl"] += float(v.oil_remaining_bbl)
    return by_rid


def reservoirs_summary(grid: SubsurfaceGrid, wells: list[Any]) -> list[dict[str, Any]]:
    """Per-reservoir rollup for the Wells-tab grouping + LLM RESERVOIRS block.

    Returns one entry per `reservoir_id` that has at least one revealed
    voxel. Reservoirs with zero revealed voxels are omitted entirely (no
    information leak). Entries are sorted by ascending `reservoir_id`.

    Each entry carries:
      * `reservoir_id`
      * `estimated_bbl` — Σ latest `oil_estimate_bbl` over revealed
        voxels of this reservoir (resurveying grows it).
      * `remaining_bbl` — `estimated_bbl − cumulative_produced_bbl`.
        Allowed to go negative (no clamp); the UI displays the raw
        signed value so players see when a reservoir has been
        over-pulled relative to the noisy estimate.
      * `n_revealed_voxels` — count of voxels in this reservoir with
        ≥1 survey entry.
      * `cumulative_produced_bbl` — Σ over production wells with
        matching `reservoir_id` (null-reservoir wells contribute to
        NO reservoir).
      * `cumulative_injected_bbl` — Σ over injection wells with
        matching `reservoir_id`.
      * `producer_ids` / `injector_ids` — ascending-sorted lists of
        well-id strings in this reservoir.
      * `engaged_voxel_count` / `engaged_bbl` / `engaged_remaining_bbl`
        — `engaged_summary` rollup for this reservoir, or explicit zeros
        when the reservoir is revealed but unwelled (the zero is the
        "drill here" affordance, so we don't omit it).
    """
    revealed_by_id: dict[int, list[Voxel]] = {}
    for v in grid.voxels.values():
        if not v.estimates:
            continue
        revealed_by_id.setdefault(v.reservoir_id, []).append(v)

    producers_by_id: dict[int, list[Any]] = {}
    injectors_by_id: dict[int, list[Any]] = {}
    for w in wells:
        rid = getattr(w, "reservoir_id", None)
        if rid is None:
            continue
        if getattr(w, "type", None) == "production":
            producers_by_id.setdefault(int(rid), []).append(w)
        elif getattr(w, "type", None) == "injection":
            injectors_by_id.setdefault(int(rid), []).append(w)

    engaged = engaged_summary(grid, wells)

    out: list[dict[str, Any]] = []
    for rid in sorted(revealed_by_id.keys()):
        voxels = revealed_by_id[rid]
        estimated = 0.0
        for v in voxels:
            oil_est, _perm, _day = _latest_estimate(v)
            estimated += oil_est
        prods = producers_by_id.get(rid, [])
        injs = injectors_by_id.get(rid, [])
        cum_produced = sum(float(w.cumulative_produced_bbl) for w in prods)
        cum_injected = sum(float(w.cumulative_injected_bbl) for w in injs)
        e = engaged.get(
            rid,
            {"engaged_voxel_count": 0, "engaged_bbl": 0.0, "engaged_remaining_bbl": 0.0},
        )
        out.append(
            {
                "reservoir_id": rid,
                "estimated_bbl": estimated,
                "remaining_bbl": estimated - cum_produced,
                "n_revealed_voxels": len(voxels),
                "cumulative_produced_bbl": cum_produced,
                "cumulative_injected_bbl": cum_injected,
                "producer_ids": sorted(str(w.id) for w in prods),
                "injector_ids": sorted(str(w.id) for w in injs),
                "engaged_voxel_count": int(e["engaged_voxel_count"]),
                "engaged_bbl": float(e["engaged_bbl"]),
                "engaged_remaining_bbl": float(e["engaged_remaining_bbl"]),
            }
        )
    return out

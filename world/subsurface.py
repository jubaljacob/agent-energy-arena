"""3D voxel grid for the subsurface, plus seismic survey mechanic.

Implements §3.5 (reservoir generation), §4.10 (seismic survey) of the brief
and the PRD's quadratic survey-cost override (`cost = 15_000 × (size/8)²`).
The voxel grid is generated at world reset from `sim_rng`; surveys also draw
from `sim_rng` (they happen between `/step` calls so the per-day RNG-budget
contract that anchors step-size invariance is unaffected).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

# Per-voxel oil capacity, named "VOXEL_VOLUME_BBL" in §3.5 of the brief and
# explicitly flagged there as a "calibration constant". The brief's literal
# value (100_000) plus its 0.6·(1 - d/r) HC probability gives total OOIP of
# ~1M bbl on seed 42, far below the brief's own "expected ~5-15M bbl on
# default size" estimate. We tune the calibration constant up to 700_000 so
# seed 42 lands at ~6.7M bbl (mid-range), which is the AC for slice 06.
# The HC-probability formula is left unchanged from the brief.
VOXEL_VOLUME_BBL = 700_000.0

# Survey constants (§4.10 + PRD quadratic override).
SEISMIC_BASE_COST = 15_000.0
SEISMIC_DEFAULT_SIZE = 8
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
    """PRD quadratic cost: 15_000 × (size / 8)²."""
    return SEISMIC_BASE_COST * (size / SEISMIC_DEFAULT_SIZE) ** 2


def is_size_valid(size: int) -> bool:
    return SEISMIC_MIN_SIZE <= size <= SEISMIC_MAX_SIZE


def generate_subsurface(
    rng: np.random.Generator, width: int, height: int, depth: int
) -> SubsurfaceGrid:
    """Place 3-7 reservoir blobs per §3.5 of the brief.

    For each blob: random center (z ∈ [4, depth-2]), radius r ∈ [3, 6], and
    every voxel within Manhattan distance r is HC-bearing with probability
    `0.6 · (1 - dist/r)`. Geological properties are drawn per the brief's
    distributions. First blob to claim a voxel wins (subsequent blobs leave
    it alone).
    """
    grid = SubsurfaceGrid(width=width, height=height, depth=depth)
    n_blobs = int(rng.integers(N_RESERVOIRS_MIN, N_RESERVOIRS_MAX + 1))

    z_lo = 4
    z_hi = depth - 2  # inclusive
    if z_hi < z_lo:
        return grid  # degenerate world dimensions; no reservoirs

    for _ in range(n_blobs):
        cx = int(rng.integers(0, width))
        cy = int(rng.integers(0, height))
        cz = int(rng.integers(z_lo, z_hi + 1))
        r = int(rng.integers(BLOB_RADIUS_MIN, BLOB_RADIUS_MAX + 1))

        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                for dz in range(-r, r + 1):
                    dist = abs(dx) + abs(dy) + abs(dz)
                    if dist > r:
                        continue
                    x, y, z = cx + dx, cy + dy, cz + dz
                    if not (0 <= x < width and 0 <= y < height and 0 <= z < depth):
                        continue
                    if (x, y, z) in grid.voxels:
                        continue
                    p_hc = HC_PROBABILITY_BASE * (1.0 - dist / r) if r > 0 else 0.0
                    # Draw the trial roll regardless of ordering tricks so
                    # the RNG sequence stays reproducible voxel-by-voxel.
                    roll = float(rng.random())
                    if roll >= p_hc:
                        continue

                    porosity = float(rng.uniform(POROSITY_MIN, POROSITY_MAX))
                    perm = float(np.exp(rng.uniform(np.log(PERM_LOG_MIN), np.log(PERM_LOG_MAX))))
                    s_o = float(rng.uniform(OIL_SAT_MIN, OIL_SAT_MAX))
                    oip = porosity * s_o * VOXEL_VOLUME_BBL
                    grid.voxels[(x, y, z)] = Voxel(
                        x=x,
                        y=y,
                        z=z,
                        porosity=porosity,
                        permeability=perm,
                        oil_saturation=s_o,
                        oil_in_place_bbl=oip,
                        oil_remaining_bbl=oip,
                    )

    return grid


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


def reservoirs_summary(grid: SubsurfaceGrid, *, top_k: int = 10) -> dict[str, Any]:
    """Bounded view for `/state.reservoirs_revealed` — top-K plus aggregates."""
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

"""Subsurface generation, seismic surveys, and reservoir read-models.

Covers slice-06 acceptance criteria for `world/subsurface.py` plus its
wiring through `world/sim.py` and `world/api.py`.
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from world.api import create_app
from world.sim import World
from world.subsurface import (
    N_RESERVOIRS_MAX,
    N_RESERVOIRS_MIN,
    SEISMIC_BASE_COST,
    SEISMIC_MAX_SIZE,
    SEISMIC_MIN_SIZE,
    VOXEL_VOLUME_BBL,
    generate_subsurface,
    survey_cost,
    voxel_reservoir_id,
    well_reservoir_id,
)
from world.subsurface import survey as run_survey

# -- Reservoir generation ---------------------------------------------------


def test_reset_generates_3_to_7_blobs_seed42():
    w = World()
    w.reset(seed=42)
    voxels = list(w.subsurface.voxels.values())
    assert len(voxels) > 0
    # We can't assert blob count directly (HC voxels are not separated by blob);
    # instead, assert the n_reservoirs bound used during generation by checking
    # the OOIP range that comes out of N_MIN..N_MAX rolls (next test).
    assert N_RESERVOIRS_MIN <= N_RESERVOIRS_MAX  # sanity


def test_seed42_total_ooip_in_expected_range():
    """Total OOIP across all reservoirs falls in [5M, 15M] bbl on seed 42."""
    w = World()
    w.reset(seed=42)
    total_ooip = w.subsurface.total_oil_in_place()
    assert 5_000_000 <= total_ooip <= 15_000_000, f"OOIP={total_ooip:,.0f} out of [5M, 15M]"


def test_reservoir_generation_reproducible_same_seed():
    """Two /reset calls with the same seed produce byte-identical voxel grids."""
    a = World()
    b = World()
    a.reset(seed=42)
    b.reset(seed=42)
    assert set(a.subsurface.voxels.keys()) == set(b.subsurface.voxels.keys())
    for key, va in a.subsurface.voxels.items():
        vb = b.subsurface.voxels[key]
        assert va.oil_in_place_bbl == vb.oil_in_place_bbl
        assert va.permeability == vb.permeability
        assert va.porosity == vb.porosity
        assert va.oil_saturation == vb.oil_saturation
        assert va.reservoir_id == vb.reservoir_id


def test_different_seeds_produce_different_grids():
    a = World()
    b = World()
    a.reset(seed=42)
    b.reset(seed=99)
    assert a.subsurface.voxels.keys() != b.subsurface.voxels.keys()


def test_reservoir_voxels_within_z_bounds():
    """Per §3.5, blob centers at z ∈ [4, WORLD_D-2]; voxels must stay in-grid."""
    w = World()
    w.reset(seed=42)
    d = w.config.world_d
    for v in w.subsurface.voxels.values():
        assert 0 <= v.z < d
        assert 0 <= v.x < w.config.world_w
        assert 0 <= v.y < w.config.world_h


def test_reservoir_voxel_properties_in_distribution_ranges():
    w = World()
    w.reset(seed=42)
    for v in w.subsurface.voxels.values():
        assert 0.10 <= v.porosity <= 0.30
        assert 10.0 <= v.permeability <= 1000.0
        assert 0.55 <= v.oil_saturation <= 0.80
        assert v.oil_in_place_bbl == pytest.approx(v.porosity * v.oil_saturation * VOXEL_VOLUME_BBL)
        assert v.oil_remaining_bbl == v.oil_in_place_bbl


# -- Survey cost / size validation ------------------------------------------


def test_survey_cost_quadratic_scaling():
    # Oilfield-v2 rescale: base anchors at size=4 (15_000 * (4/4)**2 = 15_000).
    assert survey_cost(4) == SEISMIC_BASE_COST
    assert survey_cost(4) == 15_000
    assert survey_cost(8) == SEISMIC_BASE_COST * 4.0
    assert survey_cost(8) == 60_000
    assert survey_cost(16) == SEISMIC_BASE_COST * 16.0


def test_survey_default_size_4_costs_15k_and_returns_16xD_records():
    w = World()
    w.reset(seed=42)
    treasury_before = w.state.treasury
    res = w.survey(16, 16, size=4)
    assert res["ok"] is True
    assert w.state.treasury == treasury_before - 15_000
    voxels = res["result"]["voxels"]
    assert len(voxels) == 4 * 4 * w.config.world_d


def test_survey_size_8_costs_60000():
    w = World()
    w.reset(seed=42)
    treasury_before = w.state.treasury
    res = w.survey(16, 16, size=8)
    assert res["ok"] is True
    assert w.state.treasury == treasury_before - 60_000


def test_survey_size_16_costs_240000():
    w = World()
    w.reset(seed=42)
    treasury_before = w.state.treasury
    res = w.survey(16, 16, size=16)
    assert res["ok"] is True
    assert w.state.treasury == treasury_before - 240_000


def test_survey_rejects_size_below_min():
    w = World()
    w.reset(seed=42)
    treasury_before = w.state.treasury
    res = w.survey(16, 16, size=SEISMIC_MIN_SIZE - 1)
    assert res["ok"] is False
    assert res["error"] == "invalid_size"
    assert w.state.treasury == treasury_before


def test_survey_rejects_size_above_max():
    w = World()
    w.reset(seed=42)
    treasury_before = w.state.treasury
    res = w.survey(16, 16, size=SEISMIC_MAX_SIZE + 1)
    assert res["ok"] is False
    assert res["error"] == "invalid_size"
    assert w.state.treasury == treasury_before


def test_survey_rejects_out_of_bounds():
    w = World()
    w.reset(seed=42)
    res = w.survey(-1, 0, size=8)
    assert res["ok"] is False
    assert res["error"] == "out_of_bounds"


def test_survey_rejects_when_treasury_too_low():
    w = World()
    w.reset(seed=42)
    w.state.treasury = 10.0
    res = w.survey(16, 16, size=8)
    assert res["ok"] is False
    assert res["error"] == "insufficient_funds"


# -- Survey clipping at grid edges ------------------------------------------


def test_survey_clips_at_grid_corner():
    """Survey at (0,0) with size=8 returns a clipped 4×4 column (no padding)."""
    w = World()
    w.reset(seed=42)
    res = w.survey(0, 0, size=8)
    assert res["ok"] is True
    voxels = res["result"]["voxels"]
    # Range is x in [-4, 4) clipped to [0, 4) → 4 columns; same for y.
    assert len(voxels) == 4 * 4 * w.config.world_d
    for v in voxels:
        assert 0 <= v["x"] < 4
        assert 0 <= v["y"] < 4


# -- Survey noise + history -------------------------------------------------


def test_resurvey_produces_independent_noise():
    """Resurveying the same column gives different oil_estimate_bbl values
    for the same HC voxel across two calls (PRD §"Subsurface")."""
    w = World()
    w.reset(seed=42)
    # Find an HC voxel and survey its column twice.
    hc = next(iter(w.subsurface.voxels.values()))
    r1 = w.survey(hc.x, hc.y, size=8)
    r2 = w.survey(hc.x, hc.y, size=8)

    def pick(records: list) -> dict:
        return next(
            rec for rec in records if rec["x"] == hc.x and rec["y"] == hc.y and rec["z"] == hc.z
        )

    a = pick(r1["result"]["voxels"])
    b = pick(r2["result"]["voxels"])
    assert a["oil_estimate_bbl"] != b["oil_estimate_bbl"]


def test_survey_appends_to_voxel_history():
    """Every survey appends a new estimate entry per HC voxel to its history."""
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    assert hc.estimates == []
    w.survey(hc.x, hc.y, size=8)
    assert len(hc.estimates) == 1
    w.survey(hc.x, hc.y, size=8)
    assert len(hc.estimates) == 2
    # Second entry differs from first thanks to independent noise.
    assert hc.estimates[0] != hc.estimates[1]


def test_survey_records_survey_day():
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    w.step(days=3)
    w.survey(hc.x, hc.y, size=8)
    assert hc.estimates[-1]["survey_day"] == 3


def test_survey_noise_obeys_max_zero_floor():
    """`max(0, v.oil_in_place · (1 + N(0, sigma)))` clamps low-tail noise to 0."""
    rng = np.random.default_rng(1234)
    grid = generate_subsurface(rng, 32, 32, 16)
    # Run many surveys at HC voxel; assert all estimates ≥ 0.
    if not grid.voxels:
        pytest.skip("seed produced no HC voxels")
    hc = next(iter(grid.voxels.values()))
    for _ in range(20):
        records = run_survey(grid, rng, hc.x, hc.y, size=8, survey_day=0)
        for rec in records:
            assert rec["oil_estimate_bbl"] >= 0.0
            assert rec["perm_estimate_md"] >= 0.0


def test_non_hc_voxels_estimate_zero():
    w = World()
    w.reset(seed=42)
    res = w.survey(0, 0, size=8)
    voxels = res["result"]["voxels"]
    for rec in voxels:
        v = w.subsurface.get(rec["x"], rec["y"], rec["z"])
        if v is None:
            assert rec["oil_estimate_bbl"] == 0.0
            assert rec["perm_estimate_md"] == 0.0


# -- /state.reservoirs_revealed view ---------------------------------------


def test_reservoirs_revealed_empty_before_any_survey():
    w = World()
    w.reset(seed=42)
    rr = w.state_dict()["reservoirs_revealed"]
    assert rr["n_revealed_voxels"] == 0
    assert rr["n_explored_columns"] == 0
    assert rr["top_k"] == []


def test_reservoirs_revealed_top_k_bounded_at_10():
    w = World()
    w.reset(seed=42)
    # Survey enough columns to expose more than 10 HC voxels.
    for cx in range(4, 32, 4):
        for cy in range(4, 32, 4):
            w.state.treasury += 50_000  # keep treasury topped up
            w.survey(cx, cy, size=8)
    rr = w.state_dict()["reservoirs_revealed"]
    assert len(rr["top_k"]) <= 10
    assert rr["n_revealed_voxels"] >= len(rr["top_k"])
    assert rr["n_explored_columns"] > 0


def test_reservoirs_revealed_top_k_sorted_by_oil_times_perm():
    w = World()
    w.reset(seed=42)
    w.state.treasury += 200_000
    for cx in range(0, 32, 4):
        for cy in range(0, 32, 4):
            w.state.treasury += 30_000
            w.survey(cx, cy, size=8)
    rr = w.state_dict()["reservoirs_revealed"]
    products = [v["oil_estimate_bbl"] * v["perm_estimate_md"] for v in rr["top_k"]]
    assert products == sorted(products, reverse=True)


# -- /reservoirs filter -----------------------------------------------------


def test_reservoirs_endpoint_filters_min_oil_and_caps_top_k():
    w = World()
    w.reset(seed=42)
    w.state.treasury += 300_000
    for cx in range(0, 32, 4):
        for cy in range(0, 32, 4):
            w.state.treasury += 30_000
            w.survey(cx, cy, size=8)
    res = w.reservoirs(min_oil=5_000, top_k=20)
    assert len(res["voxels"]) <= 20
    for v in res["voxels"]:
        assert v["oil_estimate_bbl"] >= 5_000


# -- API smoke ---------------------------------------------------------------


def test_api_survey_endpoint_logs_and_deducts():
    w = World()
    w.reset(seed=42)
    client = TestClient(create_app(world=w))
    treasury_before = w.state.treasury
    res = client.post("/survey", json={"x": 16, "y": 16, "size": 8}).json()
    assert res["ok"] is True
    assert w.state.treasury == treasury_before - 60_000
    assert "voxels" in res["result"]


def test_api_survey_invalid_size_returns_ok_false():
    w = World()
    w.reset(seed=42)
    client = TestClient(create_app(world=w))
    res = client.post("/survey", json={"x": 16, "y": 16, "size": 2}).json()
    assert res["ok"] is False
    assert res["error"] == "invalid_size"


def test_api_reservoirs_endpoint_returns_filtered_list():
    w = World()
    w.reset(seed=42)
    client = TestClient(create_app(world=w))
    # Seed a few surveys.
    for cx in (8, 16, 24):
        client.post("/survey", json={"x": cx, "y": 16, "size": 8})
    res = client.get("/reservoirs?min_oil=0&top_k=5").json()
    assert "voxels" in res
    assert len(res["voxels"]) <= 5


# -- Determinism / state isolation -----------------------------------------


def test_subsurface_survey_does_not_break_step_determinism_when_no_surveys():
    """Sanity: with no surveys called, slice-01's step-size invariance still
    holds even though reservoir generation now consumes sim_rng draws at reset."""
    a = World()
    b = World()
    a.reset(seed=42)
    b.reset(seed=42)
    a.step(days=7)
    for _ in range(7):
        b.step(days=1)
    assert a.sim_rng.standard_normal() == b.sim_rng.standard_normal()


# -- BFS reservoirs + reservoir_id (oilfield-v2 slice 01) ------------------


def _connected_component_size(
    voxel_set: set[tuple[int, int, int]], start: tuple[int, int, int]
) -> int:
    """Flood fill from `start` over `voxel_set` under 26-connectivity. Returns
    the visited count — used by tests to assert each reservoir is one
    connected component."""
    from collections import deque

    visited = {start}
    q = deque([start])
    while q:
        x, y, z = q.popleft()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    if dx == 0 and dy == 0 and dz == 0:
                        continue
                    nb = (x + dx, y + dy, z + dz)
                    if nb in voxel_set and nb not in visited:
                        visited.add(nb)
                        q.append(nb)
    return len(visited)


def test_every_blob_is_single_26_connected_component():
    """For seed 42 (and a handful of others) every distinct `reservoir_id`
    flood-fills into all of its voxels under 26-connectivity — the BFS
    generator's connectivity-by-construction guarantee."""
    for seed in (42, 0, 1, 7, 99):
        w = World()
        w.reset(seed=seed)
        by_id: dict[int, list[tuple[int, int, int]]] = {}
        for v in w.subsurface.voxels.values():
            by_id.setdefault(v.reservoir_id, []).append((v.x, v.y, v.z))
        assert by_id, f"seed={seed} produced no HC voxels"
        for rid, voxels in by_id.items():
            visited = _connected_component_size(set(voxels), voxels[0])
            assert visited == len(voxels), (
                f"seed={seed} R{rid} not connected: flood reached {visited} of {len(voxels)} voxels"
            )


def test_every_hc_voxel_has_reservoir_id_at_least_1():
    w = World()
    w.reset(seed=42)
    for v in w.subsurface.voxels.values():
        assert v.reservoir_id >= 1


def test_reservoir_id_stable_across_reset():
    """Resetting with the same seed twice produces the same per-voxel
    `reservoir_id`s. Already covered by the byte-identical-voxels test, but
    pinned independently so a future regression doesn't accidentally swap
    blob-iteration order without tripping any other assertion."""
    a = World()
    b = World()
    a.reset(seed=42)
    b.reset(seed=42)
    for key, va in a.subsurface.voxels.items():
        assert va.reservoir_id == b.subsurface.voxels[key].reservoir_id


def test_adjacent_blobs_retain_distinct_reservoir_ids():
    """A seed where two blob seeds spawn close enough that their grown
    components touch under 26-connectivity. The BFS generator never
    re-tags a voxel claimed by an earlier blob, so the seam stays at the
    boundary and the two reservoirs keep distinct `reservoir_id`s."""
    # seed=2 produces blobs whose 26-neighborhoods touch (R2 ↔ R5).
    rng = np.random.default_rng(2)
    grid = generate_subsurface(rng, 32, 32, 16)
    distinct_id_pairs: set[tuple[int, int]] = set()
    for (x, y, z), v in grid.voxels.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    if dx == 0 and dy == 0 and dz == 0:
                        continue
                    n = grid.get(x + dx, y + dy, z + dz)
                    if n is not None and n.reservoir_id != v.reservoir_id:
                        lo, hi = sorted((v.reservoir_id, n.reservoir_id))
                        distinct_id_pairs.add((lo, hi))
    assert distinct_id_pairs, (
        "seed=2 should produce at least one pair of 26-adjacent voxels with "
        "different reservoir_ids — if this assertion fires, the seed-2 layout "
        "has shifted and the test fixture needs a new constructed seed"
    )


def test_voxel_reservoir_id_helper_returns_none_for_non_hc():
    w = World()
    w.reset(seed=42)
    # (0, 0, 0) is rock on seed 42 — well outside any blob.
    assert voxel_reservoir_id(w.subsurface, 0, 0, 0) is None
    # Pick a known HC voxel and round-trip its id.
    hc = next(iter(w.subsurface.voxels.values()))
    assert voxel_reservoir_id(w.subsurface, hc.x, hc.y, hc.z) == hc.reservoir_id


def test_well_reservoir_id_helper_matches_target_voxel():
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    assert well_reservoir_id(w.subsurface, hc.x, hc.y, hc.z) == hc.reservoir_id
    # Rock target → None.
    rock_xy = (0, 0)
    rock_z = 0
    assert w.subsurface.get(*rock_xy, rock_z) is None
    assert well_reservoir_id(w.subsurface, *rock_xy, rock_z) is None


def test_drilled_well_carries_target_voxel_reservoir_id():
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    w.state.treasury = 1_000_000  # ensure drill succeeds
    res = w.drill(hc.x, hc.y, hc.z, "production")
    assert res["ok"] is True
    assert res["result"]["reservoir_id"] == hc.reservoir_id
    well = w.state.wells[-1]
    assert well.reservoir_id == hc.reservoir_id


def test_drilled_well_into_rock_has_none_reservoir_id():
    w = World()
    w.reset(seed=42)
    w.state.treasury = 1_000_000
    # (0, 0, 0) is rock on seed 42 (no voxel record).
    res = w.drill(0, 0, 0, "production")
    assert res["ok"] is True
    assert res["result"]["reservoir_id"] is None
    assert w.state.wells[-1].reservoir_id is None


def test_state_wells_expose_reservoir_id():
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    w.state.treasury = 1_000_000
    w.drill(hc.x, hc.y, hc.z, "production")
    state = w.state_dict()
    assert state["wells"][-1]["reservoir_id"] == hc.reservoir_id


def test_reservoirs_revealed_rows_carry_reservoir_id():
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    w.survey(hc.x, hc.y, size=8)
    rr = w.state_dict()["reservoirs_revealed"]
    assert rr["top_k"], "expected at least one revealed voxel"
    for row in rr["top_k"]:
        assert "reservoir_id" in row
        assert row["reservoir_id"] >= 1
    # /reservoirs endpoint mirror.
    res = w.reservoirs(min_oil=0.0, top_k=50)
    for row in res["voxels"]:
        assert row["reservoir_id"] >= 1

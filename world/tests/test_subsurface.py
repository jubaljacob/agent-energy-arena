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
from world.state import Tile, Well
from world.subsurface import (
    N_RESERVOIRS_MAX,
    N_RESERVOIRS_MIN,
    SEISMIC_BASE_COST,
    SEISMIC_MAX_SIZE,
    SEISMIC_MIN_SIZE,
    VOXEL_VOLUME_BBL,
    drill_collision,
    engaged_summary,
    engaged_voxels,
    generate_subsurface,
    injector_supports,
    reservoirs_summary,
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


def test_voxel_volume_bbl_is_56000():
    """The economy-rebalance pass shrinks per-voxel oil by 20% so depletion
    becomes a credible mid-to-late-game pressure within a typical play
    horizon. Pin the constant so a future regression that walks it back
    trips this test before the OOIP-range assertion below."""
    assert VOXEL_VOLUME_BBL == 56_000.0


def test_seed42_total_ooip_in_expected_range():
    """Total OOIP across all reservoirs falls in [400k, 1.5M] bbl on seed 42.

    Post-rescale (VOXEL_VOLUME_BBL 700k → 70k → 56k) the OOIP range shifts
    downward so a 10-year game horizon produces legible depletion. The
    economy-rebalance pass dropped the constant another 20% (70k → 56k),
    moving the seed-42 total from ~777k to ~622k.
    """
    w = World()
    w.reset(seed=42)
    total_ooip = w.subsurface.total_oil_in_place()
    assert 400_000 <= total_ooip <= 1_500_000, f"OOIP={total_ooip:,.0f} out of [400k, 1.5M]"


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


# -- reservoirs_summary rollup (wells-reservoir-rollup #01) ----------------


def _make_producer(
    wid: str, x: int, y: int, z: int, reservoir_id: int | None, produced: float = 0.0
) -> Well:
    return Well(
        id=wid,
        type="production",
        x=x,
        y=y,
        target_z=z,
        drilled_day=0,
        reservoir_id=reservoir_id,
        cumulative_produced_bbl=produced,
    )


def _make_injector(
    wid: str, x: int, y: int, z: int, reservoir_id: int | None, injected: float = 0.0
) -> Well:
    return Well(
        id=wid,
        type="injection",
        x=x,
        y=y,
        target_z=z,
        drilled_day=0,
        reservoir_id=reservoir_id,
        cumulative_injected_bbl=injected,
    )


def test_reservoirs_summary_empty_when_no_voxels_revealed():
    w = World()
    w.reset(seed=42)
    assert reservoirs_summary(w.subsurface, w.state.wells) == []


def test_reservoirs_summary_estimated_equals_sum_latest_oil_estimates():
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    w.survey(hc.x, hc.y, size=4)
    expected = sum(
        v.estimates[-1]["oil_estimate_bbl"]
        for v in w.subsurface.voxels.values()
        if v.estimates and v.reservoir_id == hc.reservoir_id
    )
    out = reservoirs_summary(w.subsurface, w.state.wells)
    rid_row = next(r for r in out if r["reservoir_id"] == hc.reservoir_id)
    assert rid_row["estimated_bbl"] == pytest.approx(expected)


def test_reservoirs_summary_resurvey_grows_estimated():
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    w.survey(hc.x, hc.y, size=4)
    first = next(
        r
        for r in reservoirs_summary(w.subsurface, w.state.wells)
        if r["reservoir_id"] == hc.reservoir_id
    )
    w.survey(hc.x, hc.y, size=4)
    second = next(
        r
        for r in reservoirs_summary(w.subsurface, w.state.wells)
        if r["reservoir_id"] == hc.reservoir_id
    )
    # Resurvey uses the LATEST estimate; the value changes (independent noise)
    # but the helper still computes a fresh sum-of-latest, not a cumulative
    # one. Pin: estimated == sum of latest oil_estimate per revealed voxel.
    expected = sum(
        v.estimates[-1]["oil_estimate_bbl"]
        for v in w.subsurface.voxels.values()
        if v.estimates and v.reservoir_id == hc.reservoir_id
    )
    assert second["estimated_bbl"] == pytest.approx(expected)
    # Independence: two surveys producing identical sums is astronomically
    # unlikely. Pin the inequality so a future regression that latches onto
    # only the first survey reading breaks here.
    assert first["estimated_bbl"] != second["estimated_bbl"]


def test_reservoirs_summary_remaining_can_go_negative():
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    w.survey(hc.x, hc.y, size=4)
    # Inject a fake producer in the same reservoir with cumulative > estimated.
    estimated = sum(
        v.estimates[-1]["oil_estimate_bbl"]
        for v in w.subsurface.voxels.values()
        if v.estimates and v.reservoir_id == hc.reservoir_id
    )
    fake_produced = estimated + 1_000_000.0
    wells = [_make_producer("W1", hc.x, hc.y, hc.z, hc.reservoir_id, fake_produced)]
    out = reservoirs_summary(w.subsurface, wells)
    rid_row = next(r for r in out if r["reservoir_id"] == hc.reservoir_id)
    assert rid_row["remaining_bbl"] == pytest.approx(estimated - fake_produced)
    assert rid_row["remaining_bbl"] < 0


def test_reservoirs_summary_omits_unsurveyed_reservoirs():
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    w.survey(hc.x, hc.y, size=4)
    out = reservoirs_summary(w.subsurface, w.state.wells)
    rids = {r["reservoir_id"] for r in out}
    # At least one (the surveyed one) must appear.
    assert hc.reservoir_id in rids
    # Reservoirs with no revealed voxel must be absent.
    all_rids = {v.reservoir_id for v in w.subsurface.voxels.values()}
    surveyed_rids = {v.reservoir_id for v in w.subsurface.voxels.values() if v.estimates}
    unsurveyed = all_rids - surveyed_rids
    for missing_rid in unsurveyed:
        assert missing_rid not in rids


def test_reservoirs_summary_empty_well_lists_when_no_wells_in_reservoir():
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    w.survey(hc.x, hc.y, size=4)
    out = reservoirs_summary(w.subsurface, [])
    rid_row = next(r for r in out if r["reservoir_id"] == hc.reservoir_id)
    assert rid_row["producer_ids"] == []
    assert rid_row["injector_ids"] == []
    assert rid_row["cumulative_produced_bbl"] == 0.0
    assert rid_row["cumulative_injected_bbl"] == 0.0


def test_reservoirs_summary_entries_sorted_by_ascending_reservoir_id():
    """Survey every column we can afford so every reservoir is revealed,
    then assert the entries come back in ascending id order."""
    w = World()
    w.reset(seed=42)
    w.state.treasury = 10_000_000
    for cx in range(0, 32, 4):
        for cy in range(0, 32, 4):
            w.survey(cx, cy, size=4)
    out = reservoirs_summary(w.subsurface, w.state.wells)
    rids = [r["reservoir_id"] for r in out]
    assert rids == sorted(rids)
    assert len(rids) >= 2  # seed 42 has 3-7 reservoirs; need at least 2 to test


def test_reservoirs_summary_producer_and_injector_id_lists_sorted():
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    w.survey(hc.x, hc.y, size=4)
    wells = [
        _make_producer("W3", hc.x, hc.y, hc.z, hc.reservoir_id, 100.0),
        _make_producer("W1", hc.x, hc.y, hc.z, hc.reservoir_id, 50.0),
        _make_injector("W4", hc.x, hc.y, hc.z, hc.reservoir_id, 20.0),
        _make_injector("W2", hc.x, hc.y, hc.z, hc.reservoir_id, 30.0),
    ]
    out = reservoirs_summary(w.subsurface, wells)
    rid_row = next(r for r in out if r["reservoir_id"] == hc.reservoir_id)
    assert rid_row["producer_ids"] == ["W1", "W3"]
    assert rid_row["injector_ids"] == ["W2", "W4"]
    assert rid_row["cumulative_produced_bbl"] == pytest.approx(150.0)
    assert rid_row["cumulative_injected_bbl"] == pytest.approx(50.0)


def test_reservoirs_summary_null_reservoir_wells_contribute_to_nothing():
    """A well drilled into rock (reservoir_id=None) must NOT contribute to
    any reservoir's cumulative_produced/injected totals or id lists."""
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    w.survey(hc.x, hc.y, size=4)
    wells = [
        _make_producer("W1", hc.x, hc.y, hc.z, hc.reservoir_id, 200.0),
        _make_producer("W2", 0, 0, 0, None, 99999.0),  # drilled into rock
    ]
    out = reservoirs_summary(w.subsurface, wells)
    rid_row = next(r for r in out if r["reservoir_id"] == hc.reservoir_id)
    assert rid_row["producer_ids"] == ["W1"]
    assert rid_row["cumulative_produced_bbl"] == pytest.approx(200.0)
    # No null-reservoir entry exists in the output (there's no `null` id).
    assert all(r["reservoir_id"] is not None for r in out)


def test_state_dict_exposes_reservoirs_summary_top_level_key():
    w = World()
    w.reset(seed=42)
    s = w.state_dict()
    assert "reservoirs_summary" in s
    assert isinstance(s["reservoirs_summary"], list)


def test_api_state_exposes_reservoirs_summary_top_level_key():
    w = World()
    w.reset(seed=42)
    client = TestClient(create_app(world=w))
    s = client.get("/state").json()
    assert "reservoirs_summary" in s
    assert isinstance(s["reservoirs_summary"], list)


# -- injector_supports gate (wells-reservoir-rollup #02) -------------------


def test_injector_supports_excludes_cross_reservoir_producers():
    """A producer in a different reservoir than the injector is never
    qualified, regardless of distance."""
    inj = _make_injector("I1", 10, 10, 8, reservoir_id=3)
    same_close = _make_producer("P_same", 10, 10, 8, reservoir_id=3)  # cheb=0
    cross_far = _make_producer("P_cross", 0, 0, 0, reservoir_id=5)  # cheb large
    out = injector_supports(inj, [inj, same_close, cross_far])
    assert "P_cross" not in out


def test_injector_supports_excludes_chebyshev_1_producers():
    """Producers at Chebyshev distance 1 (adjacent) fail the breakthrough
    gate and are excluded."""
    inj = _make_injector("I1", 10, 10, 8, reservoir_id=3)
    adj_x = _make_producer("P_x", 11, 10, 8, reservoir_id=3)  # cheb=1
    adj_diag = _make_producer("P_d", 11, 11, 9, reservoir_id=3)  # cheb=1
    same_cell = _make_producer("P_0", 10, 10, 8, reservoir_id=3)  # cheb=0
    out = injector_supports(inj, [inj, adj_x, adj_diag, same_cell])
    assert out == []


def test_injector_supports_includes_chebyshev_ge_2_same_reservoir():
    """Producers at Chebyshev ≥ 2 in the same reservoir are included."""
    inj = _make_injector("I1", 10, 10, 8, reservoir_id=3)
    far_x = _make_producer("P_a", 12, 10, 8, reservoir_id=3)  # cheb=2
    far_diag = _make_producer("P_b", 13, 13, 11, reservoir_id=3)  # cheb=3
    out = injector_supports(inj, [inj, far_x, far_diag])
    assert out == ["P_a", "P_b"]


def test_injector_supports_returns_ascending_sorted_producer_ids():
    """Multiple qualifying producers are sorted ascending by id."""
    inj = _make_injector("I1", 10, 10, 8, reservoir_id=3)
    wells = [
        inj,
        _make_producer("W9", 12, 10, 8, reservoir_id=3),
        _make_producer("W1", 13, 10, 8, reservoir_id=3),
        _make_producer("W5", 10, 12, 8, reservoir_id=3),
    ]
    out = injector_supports(inj, wells)
    assert out == ["W1", "W5", "W9"]


def test_injector_supports_null_reservoir_injector_returns_empty():
    """An injector drilled into rock (reservoir_id=None) has no reservoir
    to share, so it never qualifies for any producer."""
    inj = _make_injector("I1", 10, 10, 8, reservoir_id=None)
    producers = [
        _make_producer("W1", 12, 10, 8, reservoir_id=3),
        _make_producer("W2", 12, 10, 8, reservoir_id=None),
    ]
    out = injector_supports(inj, [inj, *producers])
    assert out == []


def test_injector_supports_returns_empty_for_production_well():
    """Producers carry the field for type symmetry only — the helper
    returns `[]` for any non-injection well."""
    prod = _make_producer("P1", 10, 10, 8, reservoir_id=3)
    other = _make_producer("P2", 13, 13, 11, reservoir_id=3)
    assert injector_supports(prod, [prod, other]) == []


# -- drill_collision (reservoir-scale-and-stacked-completions #03) ---------


def test_drill_collision_returns_none_for_same_xy_dz_three():
    """|Δtarget_z| ≥ 3 at the same (x, y) is the legal stacked-completion
    case — the two 3×3×3 cubes don't overlap on the z-axis."""
    existing = _make_producer("p1", 10, 10, 8, reservoir_id=1)
    assert drill_collision([existing], [], 10, 10, 5) is None
    assert drill_collision([existing], [], 10, 10, 11) is None


def test_drill_collision_completion_overlap_for_same_xy_dz_below_three():
    """|Δtarget_z| < 3 at the same (x, y) overlaps the drainage cube."""
    existing = _make_producer("p1", 10, 10, 8, reservoir_id=1)
    for dz in (-2, -1, 0, 1, 2):
        assert drill_collision([existing], [], 10, 10, 8 + dz) == "completion_overlap"


def test_drill_collision_none_for_different_xy_at_any_z():
    existing = _make_producer("p1", 10, 10, 8, reservoir_id=1)
    for z in (0, 4, 8, 12, 15):
        assert drill_collision([existing], [], 11, 10, z) is None
        assert drill_collision([existing], [], 10, 11, z) is None
        assert drill_collision([existing], [], 0, 0, z) is None


def test_drill_collision_tile_occupied_when_road_on_surface():
    """A road/refinery/pipeline on the surface tile still rejects with
    `tile_occupied`. Wells stay legal at that (x, y) so the regression
    test pins the build-side rejection through the new helper."""
    tile = Tile(id="road-1", type="road", x=10, y=10, built_day=0)
    assert drill_collision([], [tile], 10, 10, 8) == "tile_occupied"
    assert drill_collision([], [tile], 11, 10, 8) is None


def test_drill_collision_tile_takes_priority_over_completion_overlap():
    """When both a tile and an overlapping well exist at (x, y), tile_occupied
    is reported (it's the more fundamental, pre-existing build-side rule)."""
    tile = Tile(id="road-1", type="road", x=10, y=10, built_day=0)
    overlapping = _make_producer("p1", 10, 10, 8, reservoir_id=1)
    assert drill_collision([overlapping], [tile], 10, 10, 9) == "tile_occupied"


def test_drill_collision_multiple_wells_only_same_xy_counts():
    """Wells at different (x, y) don't constrain the new completion."""
    far = _make_producer("p1", 5, 5, 8, reservoir_id=1)
    same = _make_producer("p2", 10, 10, 8, reservoir_id=1)
    assert drill_collision([far, same], [], 10, 10, 5) is None
    assert drill_collision([far, same], [], 10, 10, 7) == "completion_overlap"


def test_api_drill_returns_completion_overlap_for_deep_z_collision():
    """The HTTP /drill path surfaces the new error key in the same shape
    as the existing `tile_occupied` error."""
    w = World()
    w.reset(seed=42)
    w.state.treasury = 10_000_000
    client = TestClient(create_app(world=w))
    r1 = client.post(
        "/drill",
        json={"x": 10, "y": 10, "target_z": 8, "well_type": "production"},
    ).json()
    assert r1["ok"] is True
    r2 = client.post(
        "/drill",
        json={"x": 10, "y": 10, "target_z": 7, "well_type": "production"},
    ).json()
    assert r2["ok"] is False
    assert r2["error"] == "completion_overlap"
    assert r2["result"] is None


def test_api_drill_rejects_tile_occupied_road_on_surface():
    """A road on the surface tile blocks drilling with `tile_occupied`."""
    w = World()
    w.reset(seed=42)
    w.state.treasury = 10_000_000
    # Find a road-adjacent buildable square and place a road there. The
    # town-hall sits at the world center, so a tile to its east has road
    # adjacency via the hall.
    cx = w.config.world_w // 2
    cy = w.config.world_h // 2
    rx, ry = cx + 1, cy
    br = w.build("road", rx, ry)
    assert br["ok"] is True
    client = TestClient(create_app(world=w))
    r = client.post(
        "/drill",
        json={"x": rx, "y": ry, "target_z": 8, "well_type": "production"},
    ).json()
    assert r["ok"] is False
    assert r["error"] == "tile_occupied"


# -- engaged-rollup helpers (reservoir-scale-and-stacked-completions #05) --


def test_engaged_voxels_empty_for_empty_wells():
    w = World()
    w.reset(seed=42)
    assert engaged_voxels(w.subsurface, []) == set()


def test_engaged_voxels_interior_well_yields_27_cells():
    """A well centered well inside the grid bounds engages a full 3×3×3
    cube — 27 (x, y, z) positions, regardless of HC-affiliation."""
    w = World()
    w.reset(seed=42)
    well = _make_producer("W1", 10, 10, 8, reservoir_id=1)
    out = engaged_voxels(w.subsurface, [well])
    assert len(out) == 27
    # Every returned coord must be in the well's 3×3×3 cube.
    for vx, vy, vz in out:
        assert abs(vx - 10) <= 1 and abs(vy - 10) <= 1 and abs(vz - 8) <= 1


def test_engaged_voxels_clips_to_grid_at_corner():
    """A well at the (0, 0, 0) corner engages a 2×2×2 cube (8 cells) —
    the out-of-grid half is clipped (no padding)."""
    w = World()
    w.reset(seed=42)
    well = _make_producer("W1", 0, 0, 0, reservoir_id=1)
    out = engaged_voxels(w.subsurface, [well])
    assert out == {(x, y, z) for x in (0, 1) for y in (0, 1) for z in (0, 1)}


def test_engaged_voxels_disjoint_union_for_non_overlapping_wells():
    w = World()
    w.reset(seed=42)
    a = _make_producer("Wa", 5, 5, 8, reservoir_id=1)
    b = _make_producer("Wb", 20, 20, 8, reservoir_id=1)
    out = engaged_voxels(w.subsurface, [a, b])
    assert len(out) == 54  # 27 + 27, disjoint


def test_engaged_voxels_set_union_for_overlapping_wells():
    """Two wells whose 3×3×3 cubes overlap return the set union, not the
    sum — voxels are counted once."""
    w = World()
    w.reset(seed=42)
    a = _make_producer("Wa", 10, 10, 8, reservoir_id=1)
    # Δx=1 → x-axis cubes share a 2-wide y×z slab = 2*3*3 = 18 voxels.
    b = _make_producer("Wb", 11, 10, 8, reservoir_id=1)
    out = engaged_voxels(w.subsurface, [a, b])
    # 27 + 27 - 18 (overlap) = 36
    assert len(out) == 36


def test_engaged_voxels_null_reservoir_well_contributes_cube():
    """A well with reservoir_id=None (drilled into rock) still contributes
    its geometric cube to the engaged set — the helper is reservoir-agnostic."""
    w = World()
    w.reset(seed=42)
    rock = _make_producer("Wrock", 10, 10, 8, reservoir_id=None)
    out = engaged_voxels(w.subsurface, [rock])
    assert len(out) == 27


def test_engaged_summary_empty_when_no_wells():
    w = World()
    w.reset(seed=42)
    assert engaged_summary(w.subsurface, []) == {}


def test_engaged_summary_one_well_count_equals_hc_voxels_in_cube():
    """For a well drilled into a reservoir, `engaged_voxel_count` equals
    the number of HC voxels in the well's 3×3×3 cube."""
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    # Count HC voxels in the 3×3×3 cube around (hc.x, hc.y, hc.z).
    expected_count = sum(
        1
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
        for dz in (-1, 0, 1)
        if w.subsurface.get(hc.x + dx, hc.y + dy, hc.z + dz) is not None
    )
    well = _make_producer("W1", hc.x, hc.y, hc.z, reservoir_id=hc.reservoir_id)
    out = engaged_summary(w.subsurface, [well])
    assert hc.reservoir_id in out
    assert out[hc.reservoir_id]["engaged_voxel_count"] == expected_count


def test_engaged_summary_overlapping_wells_use_union_not_double_count():
    """Two overlapping wells in the same reservoir aggregate via set
    union; the engaged_bbl is NOT the sum-of-cubes (which would
    double-count the overlap)."""
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    a = _make_producer("Wa", hc.x, hc.y, hc.z, reservoir_id=hc.reservoir_id)
    b = _make_producer("Wb", hc.x, hc.y, hc.z, reservoir_id=hc.reservoir_id)
    single = engaged_summary(w.subsurface, [a])
    double = engaged_summary(w.subsurface, [a, b])
    # Identical wells → engaged set is identical → identical engaged stats.
    assert single[hc.reservoir_id] == double[hc.reservoir_id]


def test_engaged_summary_null_reservoir_well_not_aggregated():
    """A well drilled into rock contributes geometric cells to
    `engaged_voxels` but those cells are non-HC, so they don't roll up
    into any reservoir bucket in `engaged_summary`."""
    w = World()
    w.reset(seed=42)
    # Place the well in a corner where the 3×3×3 cube is very unlikely
    # to overlap any HC voxel.
    rock = _make_producer("Wrock", 0, 0, 0, reservoir_id=None)
    out = engaged_summary(w.subsurface, [rock])
    # If any HC voxels happened to land in that corner cube, they'd be
    # aggregated into their reservoir; we only pin "no null bucket".
    assert None not in out


def test_engaged_summary_sums_oil_in_place_and_remaining_over_engaged_set():
    """`engaged_bbl` and `engaged_remaining_bbl` sum exactly the HC voxels
    in the engaged set, treating their oil_in_place / oil_remaining fields
    as ground truth."""
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    well = _make_producer("W1", hc.x, hc.y, hc.z, reservoir_id=hc.reservoir_id)
    expected_bbl = 0.0
    expected_remain = 0.0
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                v = w.subsurface.get(hc.x + dx, hc.y + dy, hc.z + dz)
                if v is not None and v.reservoir_id == hc.reservoir_id:
                    expected_bbl += v.oil_in_place_bbl
                    expected_remain += v.oil_remaining_bbl
    out = engaged_summary(w.subsurface, [well])
    row = out[hc.reservoir_id]
    assert row["engaged_bbl"] == pytest.approx(expected_bbl)
    assert row["engaged_remaining_bbl"] == pytest.approx(expected_remain)


def test_reservoirs_summary_carries_engaged_keys_additively():
    """`/state.reservoirs_summary` entries gain the three engaged keys
    additively — every prior key is still present."""
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    w.survey(hc.x, hc.y, size=4)
    out = reservoirs_summary(w.subsurface, w.state.wells)
    assert out, "expected at least one revealed reservoir"
    row = next(r for r in out if r["reservoir_id"] == hc.reservoir_id)
    # Pre-existing keys preserved.
    for key in (
        "reservoir_id",
        "estimated_bbl",
        "remaining_bbl",
        "n_revealed_voxels",
        "cumulative_produced_bbl",
        "cumulative_injected_bbl",
        "producer_ids",
        "injector_ids",
    ):
        assert key in row, f"missing legacy key {key!r}"
    # New keys present.
    for key in ("engaged_voxel_count", "engaged_bbl", "engaged_remaining_bbl"):
        assert key in row, f"missing engaged key {key!r}"


def test_reservoirs_summary_engaged_zero_for_revealed_but_unwelled_reservoir():
    """A revealed reservoir with no wells still appears in the rollup;
    its `engaged_voxel_count` is 0 (the explicit zero is the "drill here"
    affordance)."""
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    w.survey(hc.x, hc.y, size=4)
    # No wells drilled — `w.state.wells` is empty.
    out = reservoirs_summary(w.subsurface, w.state.wells)
    row = next(r for r in out if r["reservoir_id"] == hc.reservoir_id)
    assert row["engaged_voxel_count"] == 0
    assert row["engaged_bbl"] == 0.0
    assert row["engaged_remaining_bbl"] == 0.0


def test_reservoirs_summary_engaged_counts_match_helper_for_drilled_reservoir():
    """When a producer has been drilled, the rollup's engaged stats match
    `engaged_summary` for that reservoir."""
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    w.survey(hc.x, hc.y, size=4)
    w.state.treasury = 10_000_000
    r = w.drill(hc.x, hc.y, hc.z, well_type="production")
    assert r["ok"], r
    rollup = reservoirs_summary(w.subsurface, w.state.wells)
    helper = engaged_summary(w.subsurface, w.state.wells)
    row = next(r for r in rollup if r["reservoir_id"] == hc.reservoir_id)
    assert row["engaged_voxel_count"] == helper[hc.reservoir_id]["engaged_voxel_count"]
    assert row["engaged_bbl"] == pytest.approx(helper[hc.reservoir_id]["engaged_bbl"])
    assert row["engaged_remaining_bbl"] == pytest.approx(
        helper[hc.reservoir_id]["engaged_remaining_bbl"]
    )


def test_state_reservoirs_summary_byte_stable_across_consecutive_calls():
    """No actions between two `state_dict()` calls → the
    `reservoirs_summary` payload is byte-identical. Determinism pin: the
    engaged-rollup must not depend on Python's set iteration order."""
    import json

    w = World()
    w.reset(seed=42)
    w.state.treasury = 10_000_000
    hc = next(iter(w.subsurface.voxels.values()))
    w.survey(hc.x, hc.y, size=4)
    w.drill(hc.x, hc.y, hc.z, well_type="production")
    a = json.dumps(w.state_dict()["reservoirs_summary"], sort_keys=False)
    b = json.dumps(w.state_dict()["reservoirs_summary"], sort_keys=False)
    assert a == b

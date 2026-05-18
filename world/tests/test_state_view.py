"""Wire-format projectors that turn ``Tile``/``Well`` into the dicts
``World.build()``, ``World.drill()`` and ``World.state_dict()`` return.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from world.api import create_app
from world.sim import World


def test_well_view_injection_carries_supports_producer_ids():
    """`well_view` populates `supports_producer_ids` on injection wells
    from the same-reservoir + Chebyshev > 1 gate."""
    w = World()
    w.reset(seed=42)
    # Find a producer voxel and an injector voxel that share a reservoir id
    # AND sit at Chebyshev > 1 from each other (so the gate qualifies).
    voxels = list(w.subsurface.voxels.values())
    pairs = (
        (a, b)
        for a in voxels
        for b in voxels
        if a is not b
        and a.reservoir_id == b.reservoir_id
        and max(abs(a.x - b.x), abs(a.y - b.y), abs(a.z - b.z)) > 1
        and (a.x, a.y) != (b.x, b.y)  # /drill rejects same (x, y)
    )
    prod_vox, inj_vox = next(pairs)
    r = w.drill(prod_vox.x, prod_vox.y, prod_vox.z, well_type="production")
    assert r["ok"], r
    prod_id = r["result"]["id"]
    r = w.drill(inj_vox.x, inj_vox.y, inj_vox.z, well_type="injection")
    assert r["ok"], r
    inj_dict = r["result"]
    assert inj_dict["type"] == "injection"
    assert inj_dict["supports_producer_ids"] == [prod_id]


def test_well_view_production_carries_empty_supports_list():
    """Production wells carry `supports_producer_ids = []` for type symmetry."""
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    r = w.drill(hc.x, hc.y, hc.z, well_type="production")
    assert r["ok"], r
    assert r["result"]["supports_producer_ids"] == []


def test_state_wells_supports_producer_ids_present_on_injection():
    """The `/state` payload's injection-well dicts carry the new field."""
    w = World()
    w.reset(seed=42)
    hc = next(iter(w.subsurface.voxels.values()))
    r = w.drill(hc.x, hc.y, hc.z, well_type="injection")
    assert r["ok"], r
    client = TestClient(create_app(world=w))
    s = client.get("/state").json()
    inj_dicts = [ww for ww in s["wells"] if ww["type"] == "injection"]
    assert inj_dicts, "expected at least one injection well in /state"
    for iw in inj_dicts:
        assert "supports_producer_ids" in iw
        assert isinstance(iw["supports_producer_ids"], list)

"""Pure-module tests for the pipeline graph helpers (oilfield-v2 slice 07).

The module under test is `world.pipelines` — graph reasoning over `Tile`
and `Well` lists with no `World` dependency. Sim integration lands in
slice 08; this slice only pins the pure-function contract.
"""

from __future__ import annotations

from world.pipelines import peaker_supply, pipeline_components, routing_units
from world.state import Tile, Well


def _pipe(id_: str, x: int, y: int) -> Tile:
    return Tile(id=id_, type="pipeline", x=x, y=y, built_day=0)


def _refinery(id_: str, x: int, y: int, operational: bool = True) -> Tile:
    return Tile(id=id_, type="refinery", x=x, y=y, built_day=0, operational=operational)


def _peaker(id_: str, x: int, y: int) -> Tile:
    return Tile(id=id_, type="gas_peaker", x=x, y=y, built_day=0)


def _well(id_: str, x: int, y: int, well_type: str = "production") -> Well:
    return Well(id=id_, type=well_type, x=x, y=y, target_z=8, drilled_day=0)


# -- pipeline_components -----------------------------------------------------


def test_two_orthogonal_pipelines_are_one_component():
    tiles = [_pipe("p1", 5, 5), _pipe("p2", 6, 5)]
    comps = pipeline_components(tiles, 32, 32)
    assert len(comps) == 1
    assert comps[0] == {(5, 5), (6, 5)}


def test_two_diagonal_pipelines_are_two_components():
    tiles = [_pipe("p1", 5, 5), _pipe("p2", 6, 6)]
    comps = pipeline_components(tiles, 32, 32)
    assert len(comps) == 2
    assert {frozenset(c) for c in comps} == {
        frozenset({(5, 5)}),
        frozenset({(6, 6)}),
    }


def test_l_shaped_pipeline_path_is_one_component():
    tiles = [
        _pipe("p1", 5, 5),
        _pipe("p2", 6, 5),
        _pipe("p3", 6, 6),
        _pipe("p4", 6, 7),
    ]
    comps = pipeline_components(tiles, 32, 32)
    assert len(comps) == 1
    assert comps[0] == {(5, 5), (6, 5), (6, 6), (6, 7)}


def test_removing_bridge_splits_component_on_next_call():
    bridge = _pipe("bridge", 6, 5)
    tiles = [_pipe("p_left", 5, 5), bridge, _pipe("p_right", 7, 5)]
    comps_before = pipeline_components(tiles, 32, 32)
    assert len(comps_before) == 1

    tiles_after = [t for t in tiles if t.id != "bridge"]
    comps_after = pipeline_components(tiles_after, 32, 32)
    assert len(comps_after) == 2
    assert {frozenset(c) for c in comps_after} == {
        frozenset({(5, 5)}),
        frozenset({(7, 5)}),
    }


def test_non_pipeline_tiles_are_ignored():
    tiles = [
        _pipe("p1", 5, 5),
        _refinery("r1", 6, 5),  # would bridge if it counted
        _pipe("p2", 7, 5),
    ]
    comps = pipeline_components(tiles, 32, 32)
    assert len(comps) == 2


# -- routing_units -----------------------------------------------------------


def test_one_network_with_shipping_well_and_receiving_refinery():
    tiles = [
        _pipe("p1", 5, 5),
        _pipe("p2", 6, 5),
        _refinery("ref1", 7, 5),
    ]
    wells = [_well("w1", 4, 5)]  # adjacent to p1
    networks, orphan_w, orphan_r = routing_units(tiles, wells)

    assert len(networks) == 1
    net_wells, net_refs = networks[0]
    assert [w.id for w in net_wells] == ["w1"]
    assert [r.id for r in net_refs] == ["ref1"]
    assert orphan_w == []
    assert orphan_r == []


def test_well_with_no_pipeline_neighbor_is_orphan():
    tiles = [_pipe("p1", 5, 5), _refinery("ref1", 6, 5)]
    wells = [_well("w1", 20, 20)]
    networks, orphan_w, orphan_r = routing_units(tiles, wells)

    assert [w.id for w in orphan_w] == ["w1"]
    # The network exists (refinery is adjacent to p1) and contains no wells.
    assert len(networks) == 1
    net_wells, net_refs = networks[0]
    assert net_wells == []
    assert [r.id for r in net_refs] == ["ref1"]


def test_refinery_with_no_pipeline_neighbor_is_orphan():
    tiles = [_pipe("p1", 5, 5), _refinery("ref1", 20, 20)]
    wells = [_well("w1", 4, 5)]
    networks, orphan_w, orphan_r = routing_units(tiles, wells)

    assert [r.id for r in orphan_r] == ["ref1"]
    assert len(networks) == 1
    net_wells, net_refs = networks[0]
    assert [w.id for w in net_wells] == ["w1"]
    assert net_refs == []


def test_two_disjoint_networks_route_independently():
    tiles = [
        # network A
        _pipe("pa1", 5, 5),
        _pipe("pa2", 6, 5),
        _refinery("refA", 7, 5),
        # network B (no adjacency to A)
        _pipe("pb1", 20, 20),
        _pipe("pb2", 21, 20),
        _refinery("refB", 22, 20),
    ]
    wells = [
        _well("wA", 4, 5),
        _well("wB", 19, 20),
    ]
    networks, orphan_w, orphan_r = routing_units(tiles, wells)

    assert len(networks) == 2
    assert orphan_w == []
    assert orphan_r == []

    # Find each network by its refinery id.
    by_ref = {net_refs[0].id: (net_wells, net_refs) for net_wells, net_refs in networks}
    a_wells, a_refs = by_ref["refA"]
    b_wells, b_refs = by_ref["refB"]
    assert [w.id for w in a_wells] == ["wA"]
    assert [w.id for w in b_wells] == ["wB"]
    assert [r.id for r in a_refs] == ["refA"]
    assert [r.id for r in b_refs] == ["refB"]


def test_removing_bridging_pipeline_splits_network_into_two():
    bridge = _pipe("bridge", 6, 5)
    tiles = [
        _pipe("p_left", 5, 5),
        bridge,
        _pipe("p_right", 7, 5),
        _refinery("ref_right", 8, 5),
    ]
    wells = [_well("w_left", 4, 5)]

    nets_before, orphan_w_before, _ = routing_units(tiles, wells)
    assert len(nets_before) == 1
    net_wells, net_refs = nets_before[0]
    assert [w.id for w in net_wells] == ["w_left"]
    assert [r.id for r in net_refs] == ["ref_right"]
    assert orphan_w_before == []

    tiles_after = [t for t in tiles if t.id != "bridge"]
    nets_after, orphan_w_after, _ = routing_units(tiles_after, wells)

    # Two components now: {(5,5)} with the well, {(7,5)} with the refinery.
    assert len(nets_after) == 2
    # Each network now has either a well or a refinery but not both.
    well_ids_per_net = [{w.id for w in nw} for nw, _ in nets_after]
    ref_ids_per_net = [{r.id for r in nr} for _, nr in nets_after]
    assert {"w_left"} in well_ids_per_net
    assert {"ref_right"} in ref_ids_per_net
    # And the well/refinery never appear in the same network.
    for nw, nr in nets_after:
        well_set = {w.id for w in nw}
        ref_set = {r.id for r in nr}
        assert not (well_set and ref_set)


def test_pure_no_mutation_of_inputs():
    tiles = [_pipe("p1", 5, 5), _refinery("ref1", 6, 5)]
    wells = [_well("w1", 4, 5)]
    tiles_snapshot = list(tiles)
    wells_snapshot = list(wells)

    routing_units(tiles, wells)
    pipeline_components(tiles, 32, 32)

    assert tiles == tiles_snapshot
    assert wells == wells_snapshot


# -- peaker_supply ----------------------------------------------------------


def test_peaker_supply_true_when_sharing_network_with_operational_refinery():
    """Peaker adjacent to a pipeline whose component reaches an operational refinery."""
    peaker = _peaker("gp1", 4, 5)
    tiles = [
        peaker,
        _pipe("p1", 5, 5),
        _pipe("p2", 6, 5),
        _refinery("ref1", 7, 5),
    ]
    assert peaker_supply(peaker, tiles) is True


def test_peaker_supply_false_when_connected_refinery_is_not_operational():
    """Only operational refineries count as supply."""
    peaker = _peaker("gp1", 4, 5)
    tiles = [
        peaker,
        _pipe("p1", 5, 5),
        _pipe("p2", 6, 5),
        _refinery("ref1", 7, 5, operational=False),
    ]
    assert peaker_supply(peaker, tiles) is False


def test_peaker_supply_false_when_pipeline_network_isolated_from_refineries():
    """Peaker shares a network that has no refinery at all."""
    peaker = _peaker("gp1", 4, 5)
    tiles = [
        peaker,
        # Peaker's network: no refinery touches it.
        _pipe("p1", 5, 5),
        _pipe("p2", 6, 5),
        # Disjoint network with an operational refinery — should not help.
        _pipe("q1", 20, 20),
        _refinery("ref_far", 21, 20),
    ]
    assert peaker_supply(peaker, tiles) is False


def test_peaker_supply_false_when_peaker_has_no_pipeline_adjacency():
    """No 4-neighbour pipeline tile → not on any network → unsupplied."""
    peaker = _peaker("gp1", 10, 10)
    tiles = [
        peaker,
        _pipe("p1", 5, 5),
        _refinery("ref1", 6, 5),
    ]
    assert peaker_supply(peaker, tiles) is False


def test_peaker_supply_diagonal_adjacency_is_not_enough():
    """Pipeline graph is 4-connected; a diagonal pipeline does not connect."""
    peaker = _peaker("gp1", 4, 5)
    tiles = [
        peaker,
        # Pipeline diagonally adjacent only.
        _pipe("p1", 5, 6),
        _refinery("ref1", 6, 6),
    ]
    assert peaker_supply(peaker, tiles) is False


def test_peaker_supply_picks_up_refinery_reached_via_long_path():
    """Reachability through the component, not just direct adjacency."""
    peaker = _peaker("gp1", 4, 5)
    tiles = [
        peaker,
        _pipe("p1", 5, 5),
        _pipe("p2", 6, 5),
        _pipe("p3", 6, 6),
        _pipe("p4", 6, 7),
        _refinery("ref1", 7, 7),
    ]
    assert peaker_supply(peaker, tiles) is True


def test_peaker_supply_pure_no_mutation():
    peaker = _peaker("gp1", 4, 5)
    tiles = [peaker, _pipe("p1", 5, 5), _refinery("ref1", 6, 5)]
    snapshot = list(tiles)
    peaker_supply(peaker, tiles)
    assert tiles == snapshot

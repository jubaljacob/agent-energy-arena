## Language

**World**:
The deterministic simulator. Owns the day loop, RNG streams, and the
authoritative mutable game state. The `World` class also exposes the methods
the API surface calls (`build`, `drill`, `step`, `state_dict`, …).
_Avoid_: Game, Simulation, Engine

**Tile**:
A surface-grid object on the 2D city plane (house, road, plant, refinery,
industrial, commercial, town hall, battery, pipeline). Lives in
`state.tiles`. Has a `type`, `(x, y)`, and operational state.
_Avoid_: Building, Cell, Square

**Well**:
A subsurface object completed in a voxel `(x, y, target_z)`. Either a
**production** well (lifts crude) or an **injection** well (pumps water to
boost reservoir pressure). Lives in `state.wells`.
_Avoid_: Borehole, Drill site

**state_view**:
The external dict shape `World` returns to API consumers (UI, agent
clients, tests) for a single `Tile` or `Well`. Distinct from the domain
object: dicts carry the same identity plus derived popup fields (estimated
revenue, CO2, fuel/carbon cost, net). Produced by `tile_view` / `well_view`
in `world/state_view.py`. Used inside `World.build`, `World.drill`, and
`World.state_dict`.
_Avoid_: Serialized tile, Tile DTO, Wire format

## Relationships

- A **World** contains many **Tiles** and many **Wells**.
- `World.state_dict()` returns a snapshot that includes one **state_view**
  per **Tile** and per **Well**.
- **state_view** dicts compose values from `world/pricing.py` (per-facility
  economics) — the popup row and the city-wide aggregator share those
  helpers as a single source of truth.

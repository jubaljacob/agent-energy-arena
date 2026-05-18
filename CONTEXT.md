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

**hourly_tick**:
One hour of simulated World time. A day is `ticks_per_day` (=24) hourly
ticks. Each tick determines this hour's demand (civilian load + injection /
production well power draw + refinery process load), runs plant `dispatch`
against that demand with one-hour-lagged DR, applies battery
charge/discharge, computes the bus-level balance state, and yields the
`prev_outputs`/`prev_balance` carried into the next tick. The tick is the
unit shared between `World.step` (advances and mutates) and
`world.preview.preview_next_day` (read-only projection of the next 24
ticks).
_Avoid_: Step (reserved for `World.step`, which advances ≥ 1 day), Update,
Frame

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
- A **World**'s day is `ticks_per_day` **hourly_tick**s; `World.step`
  advances them and mutates state, while `preview_next_day` simulates the
  next 24 ticks without mutation.
- `World.state_dict()` returns a snapshot that includes one **state_view**
  per **Tile** and per **Well**.
- **state_view** dicts compose values from `world/pricing.py` (per-facility
  economics) — the popup row and the city-wide aggregator share those
  helpers as a single source of truth.

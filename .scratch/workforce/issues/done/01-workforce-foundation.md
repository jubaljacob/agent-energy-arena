---
Status: needs-triage
---

# 01 — Workforce foundation: module, state field, catalog, allocator hooks, API surface

## Parent

[PRD: World v2 — Workforce & Per-Facility Staffing](../PRD.md)

## What to build

The **wiring tracer bullet** for the workforce mechanic. Introduces the `world/workforce.py` module, a `staffed_jobs` field on `Tile` and `Well`, the catalog `jobs` entries for plants and wells, the allocator hooks on the build/drill/demolish/reset paths, and the `/state` and `/catalog` API surface that exposes labor to clients.

**Out of scope for this slice**:

- Population integration in `world/population.py` (growth-branch auto-hire and decline-branch drain) — that lives in slice 02.
- Efficiency scaling on producer outputs — coal dispatch, refinery routing, well production, demand-side, CO2 — all stay at 100% regardless of `staffed_jobs` in this slice. Each producer family gets its own slice (04–07).
- Forecast snapshot verification — slice 03.
- UI — slice 08.

After this slice merges, a player who builds a coal plant will see `staffed_jobs=8/8` on the new tile in `/state`, the city's `unemployed` counter in `/state` will drop by 8, and demolishing the plant will return the 8 workers to the unemployed pool. But the coal plant still produces at its full catalog capacity, and once a day passes and `update_population` runs, the new arrivals will not yet be auto-hired (that's slice 02).

### Implementation details

**`world/workforce.py`** — new module, pure functions only, no I/O, no RNG.

- `efficiency(item) -> float` — given a `Tile` or `Well` with `spec.jobs > 0`, returns `staffed_jobs / spec.jobs` clamped to `[0, 1]`. Returns `1.0` for passive tiles (`spec.jobs == 0`).
- `employed(state) -> int` — sum of `staffed_jobs` across all tiles and wells.
- `unemployed(state) -> int` — `max(0, state.population - employed(state))`. The `max(0, ...)` clamp tolerates test injections that set `population` below the sum of injected `staffed_jobs`.
- `producers(state) -> Iterable[Tile | Well]` — yields all tiles and wells with `spec.jobs > 0`, sorted by `(creation_day, id_string)` ascending. `creation_day` is `tile.built_day` for tiles and `well.drilled_day` for wells. The id-string tiebreak ensures determinism when two facilities share a day (e.g., the town hall on day 0 and any same-day reset injection).
- `hire_to_fill(state) -> None` — walks `producers(state)` oldest-first. For each, fills `min(spec.jobs - staffed_jobs, available_unemployed)` into the facility's `staffed_jobs`. Stops when `unemployed(state) == 0`. Pure mutation of `staffed_jobs` fields; does **not** touch `state.population`.
- `drain_n(state, n) -> None` — drains `n` people total from the city. First decrements `state.population` by `min(n, unemployed(state))` (silent unemployed departures). If `n` is still positive, walks `producers(state)` **newest-first** (reverse of `producers`), decrementing both `staffed_jobs` and `state.population` by one each iteration until `n` is exhausted or all facilities are empty. Used by slice 02; included here so the module surface is complete.

The catalog-spec lookup for `spec.jobs` should reuse `TILE_CATALOG[tile.type].jobs` for tiles and the well-spec analogue for wells (the existing well types live in `WELL_TYPES` in `world/catalog.py`).

**`world/state.py`**:

- Add `staffed_jobs: int = 0` to `Tile`.
- Add `staffed_jobs: int = 0` to `Well`.

**`world/catalog.py`** — add `jobs` to the energy/well specs (existing town_hall=30, commercial=12, industrial=30, refinery=25 stay unchanged):

- `coal_plant`: `jobs=8`
- `gas_peaker`: `jobs=4`
- `solar_farm`: `jobs=2`
- `wind_turbine`: `jobs=2`
- `oil_well`: `jobs=3`
- `injection_well`: `jobs=2`

Passive tiles (`road, house, park, pipeline`) stay at the dataclass default `jobs=0`.

**`world/sim.py`** hook points:

- `reset()` — after `_place_town_hall()` appends the town hall to `state.tiles`, call `workforce.hire_to_fill(state)`. With `starting_pop=100` and `town_hall.jobs=30`, the town hall ends up at `staffed_jobs=30` and `unemployed(state) == 70`.
- `build()` — after the new tile is appended to `state.tiles` and its `capex_paid` / `opex_per_day` snapshot is set, call `workforce.hire_to_fill(state)`. New `Tile` should be constructed with `staffed_jobs=0` (default); the allocator pass fills it.
- `drill()` — after the new well is appended to `state.wells`, call `workforce.hire_to_fill(state)`. New `Well` constructed with `staffed_jobs=0`.
- `demolish()` — before the tile is removed, capture nothing special: dropping the tile from `state.tiles` removes its `staffed_jobs` entry from the `employed` sum, so those workers are implicitly returned to the unemployed pool. After the removal, call `workforce.hire_to_fill(state)` to backfill any under-staffed older facilities (e.g., demolishing a young industrial frees 30 workers; an older under-staffed refinery should hire first).

The town hall is **not demolishable** (existing `if tile.type == "town_hall"` guard in `world/sim.py:256` stands), so the demolish hook never touches it.

**`/state` surface** (in `world/api.py`):

- Top-level dict adds `"employed": int` and `"unemployed": int` keys, computed via `workforce.employed(state)` and `workforce.unemployed(state)`.
- Each entry in `state["tiles"]` adds `"staffed_jobs": int`.
- Each entry in `state["wells"]` adds `"staffed_jobs": int`.

**`/catalog` surface**:

- The existing serialiser at `world/catalog.py:171` already emits `"jobs": spec.jobs`, so the new plant/well job counts flow through automatically. Verify in a test.

**Test-helper migration** (the hybrid strategy from the PRD):

- The injection helpers in `world/tests/` (e.g. `_inject_tile`, `_inject_well` — names vary per test file) default `staffed_jobs = spec.jobs` so existing tests that inject a producer tile and step the world continue to pass. Tests that need a partially-staffed tile pass `staffed_jobs=X` explicitly.
- The existing test at `world/tests/test_economy.py:622` (`test_industrial_pays_flat_co2_even_when_no_grid`) is **not** rewritten in this slice. It currently asserts an industrial tile emits 2 t/day flat CO2 even with `pop=0`. Under helper auto-staff, the injected tile gets `staffed_jobs=30`, and since this slice does **not** scale flat CO2 by efficiency, the test still passes. The rewrite belongs in slice 05.

### Tests to add in this slice

- **`test_workforce.py` (new, unit)** — direct tests on the module:
  - `efficiency` boundary cases: passive tile returns 1.0; `jobs>0, staffed=0` returns 0.0; `jobs>0, staffed=jobs` returns 1.0; partial returns the ratio.
  - `producers` ordering: build a town hall on day 0, a coal plant on day 2, an oil well on day 1, an injection well on day 2 with an id lexicographically after the coal plant; assert the yielded order is town_hall, oil_well, coal_plant, injection_well.
  - `producers` excludes passive tiles: build a road and a house alongside an industrial; only industrial appears.
  - `hire_to_fill` oldest-first: with pop=10 and producers needing [town_hall=30, refinery=25] in age order, town_hall ends at `staffed_jobs=10` and refinery stays at 0.
  - `hire_to_fill` fills until pool empty: pop=40, producers [town_hall=30 (older), industrial=30]: town_hall=30, industrial=10.
  - `drain_n` unemployed-first: pop=100 with town_hall staffed 30/30 (so unemployed=70), `drain_n(50)` reduces population to 50 with town_hall still 30/30.
  - `drain_n` falls through to newest-first fire when unemployed is exhausted: pop=30 with town_hall=30/30 (unemployed=0), build a younger industrial staffed 0 — `drain_n(5)` would naturally need to fire town_hall (the only producer with staff); after the call town_hall is 25/30 and pop=25.
  - `drain_n` fires newest first: pop=60, town_hall (older) 30/30 and industrial (younger) 30/30, unemployed=0. `drain_n(10)` reduces industrial to 20/30 and pop to 50; town_hall untouched.
- **`test_build_api.py` / new `test_workforce_integration.py`**:
  - Fresh `world.reset(seed=...)`: assert `state["employed"] == 30` (town hall), `state["unemployed"] == 70`, town_hall tile has `staffed_jobs=30`.
  - Build a coal plant with available labor: post-build, the new tile in `/state` has `staffed_jobs=8`, `unemployed` dropped by 8.
  - Build a coal plant with insufficient labor (drain pop first via a setup helper so `unemployed=5`): post-build, the new tile has `staffed_jobs=5`, `unemployed=0`.
  - Build a coal plant with zero unemployed: post-build, the new tile has `staffed_jobs=0`. (Efficiency scaling lands in slice 04, so this slice does **not** assert that the plant produces zero kW — only that it is unstaffed.)
  - Drill an oil well with available labor: well in `/state` has `staffed_jobs=3`.
  - Demolish a staffed industrial tile while an older refinery is under-staffed: after demolish, the refinery's `staffed_jobs` has increased by `min(demolished_workers, refinery_vacancies)`, and any leftover workers appear in `unemployed`.
- **`test_catalog.py`** — assert `/catalog` exposes the new `jobs` values: coal=8, gas=4, solar=2, wind=2, oil=3, inj=2.

### Determinism

- The workforce module consumes no RNG. The skeleton's per-day `sim_rng.standard_normal()` budget contract (anchored in `test_determinism.py`) is untouched.
- Producer ordering is `(creation_day, id_string)` ascending, so two facilities built on the same day have a stable id-string tiebreak.

## Acceptance criteria

- [ ] `world/workforce.py` exists and exports `efficiency`, `employed`, `unemployed`, `producers`, `hire_to_fill`, `drain_n` with the semantics above. No RNG, no I/O.
- [ ] `Tile.staffed_jobs: int = 0` and `Well.staffed_jobs: int = 0` added to `world/state.py`.
- [ ] `coal_plant.jobs=8`, `gas_peaker.jobs=4`, `solar_farm.jobs=2`, `wind_turbine.jobs=2`, `oil_well.jobs=3`, `injection_well.jobs=2` set in `world/catalog.py`. Existing job counts unchanged. Passive tiles still `jobs=0`.
- [ ] `world/sim.py` calls `workforce.hire_to_fill(state)` after `_place_town_hall` (in `reset`), after the new tile is appended in `build`, after the new well is appended in `drill`, and after the tile is removed in `demolish`.
- [ ] `/state` includes `employed` and `unemployed` top-level keys plus `staffed_jobs` on every tile and well entry.
- [ ] `/catalog` reports `jobs` for `coal_plant`, `gas_peaker`, `solar_farm`, `wind_turbine`, `oil_well`, `injection_well`.
- [ ] Day-zero `reset` leaves the town hall at `staffed_jobs=30`, `unemployed=70`, `employed=30`.
- [ ] Test helpers in `world/tests/` default `staffed_jobs = spec.jobs` for producer injections.
- [ ] New `test_workforce.py` unit tests pass; new integration tests for build/drill/demolish staffing pass.
- [ ] `make check` is green.

## Blocked by

None — can start immediately.

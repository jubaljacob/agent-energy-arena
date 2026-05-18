# `World` owns its wire-format dicts; `api.py` stays a thin pass-through

The per-element wire-format projectors (`tile_view`, `well_view` in
`world/state_view.py`) are co-located with the simulator rather than pushed
into the FastAPI layer. `World.build()`, `World.drill()`, and
`World.state_dict()` return these dicts directly, and `api.py` is a one-line
pass-through (`return app.state.world.state_dict()`).

This is deliberate. `World` methods are called by **non-HTTP consumers**
too — tests, agent `ApiClient` callers, the in-process `UiAgentApiClient`
— all of which read the same dict shape. Pushing the projectors up to
`api.py` would force every `World` mutator to change its return contract
(or duplicate the shaping at every call site), for a marginal purity win.

Co-locating the projectors in `world/state_view.py` (rather than inlining
them at the top of `world/sim.py` as they were originally) keeps the wire
format one grep target without moving the responsibility to the wrong
layer.

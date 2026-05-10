---
Status: needs-triage
---

# 01 — Server skeleton + determinism foundation

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## What to build

The minimum viable simulation server that an empty world can be built on top of. After this slice, `docker compose up` brings up a FastAPI server that answers `/state`, `/step`, `/reset`, `/seed`, and `/catalog` for an empty world, and a static UI at `localhost:8000` renders the empty surface grid with a day counter.

The slice is responsible for getting the **determinism foundation right from day one** — there is no easy refactor for this later. Two RNG streams (`sim_rng` and `forecast_rng`) are seeded from a master seed via independent `numpy.random.SeedSequence` children. The simulation RNG advances per simulated day, not per `/step` call. Every action submitted to a mutating endpoint is logged to `runs/{run_id}/actions.jsonl` regardless of success.

The `/step` endpoint accepts a `days` parameter in the range `[1, 7]`, default `7`. It always advances the full requested number of days — never early-terminates. Internal hourly ticks (`TICKS_PER_DAY = 24`) advance per simulated day even though the world has no dynamics yet; the loop is in place.

`MANUAL_GAME_DAYS = 365` and `GAME_DAYS = 3650` are both env-var configured; the active value depends on whether the world was created via the manual or agent path. The session type is exposed in `/state.config`.

## Acceptance criteria

- [ ] `docker compose up` brings up the server and UI under 60 seconds on a developer laptop.
- [ ] `GET /state` on a fresh world returns a JSON payload conforming to the brief's §5.3 schema with empty `tiles`, empty `wells`, day = 0, treasury = `STARTING_CASH`, population = `STARTING_POP`.
- [ ] `POST /step { "days": 7 }` advances the world by 7 days; day counter in `/state` increments.
- [ ] `POST /step { "days": 1 }` repeated 7 times produces byte-identical world state to a single `POST /step { "days": 7 }`. Verified by a test in `world/tests/test_determinism.py`.
- [ ] `POST /reset { "seed": 42 }` restores day = 0 and re-seeds both RNG streams.
- [ ] Calling `GET /forecast` does not perturb the simulation RNG state (forecast uses `forecast_rng`, never advances `sim_rng`). Verified by test.
- [ ] All mutating endpoint calls (including failures) append a JSON line to `runs/{run_id}/actions.jsonl` with timestamp, endpoint, params, ok/error.
- [ ] `GET /catalog` returns the build catalog (§4.12) as machine-readable JSON. The catalog is empty in this slice but the endpoint is wired.
- [ ] `GET /seed` returns the active seed.
- [ ] The UI renders an empty 32×32 surface grid, a top-bar day counter, treasury, population, and a non-functional "Next Day" button (functional in slice 16).
- [ ] `MANUAL_GAME_DAYS` env var defaults to 365; `GAME_DAYS` defaults to 3650; both are exposed in `/state.config`.
- [ ] `world/tests/test_api_smoke.py` boots the server and walks through reset → step → state → reset.

## Blocked by

None — can start immediately.

# 04: API — scenario attach/inspect + run folder id + reset-with-scenario

Status: ready-for-agent

## Parent

`.scratch/open-source-arena/PRD.md`

## What to build

Extend the FastAPI surface in `world/api.py` with three new endpoints and one optional reset field:

- `POST /scenario` — body `{ "dotted_path": "scenarios.grid_stress" }`. Resolves via `load_scenario`, attaches the scenario to the world mid-game. Returns the resolved scenario name. Errors are 400 with the message from the loader.
- `GET /scenario` — returns the currently-attached scenario dotted path (or `null` for `NullScenario`).
- `GET /run` — returns the current run folder identifier (and path) so the UI can display it.
- `POST /reset` body grows one optional field `scenario` (dotted path). When present, the new world starts with that scenario attached.

All four call sites write to the action log so an action-log replay reproduces the exact sequence including mid-game scenario attaches.

## Acceptance criteria

- [ ] `POST /scenario`, `GET /scenario`, `GET /run` endpoints exist and behave as described.
- [ ] `POST /reset` accepts the optional `scenario` field.
- [ ] Invalid dotted paths return a 400 with the loader's error message.
- [ ] The action log captures scenario-attach calls and the scenario chosen at reset.
- [ ] An action-log-driven replay reproduces a session that includes a mid-game scenario attach byte-identically.
- [ ] API smoke tests cover the four call sites (happy path + invalid dotted path).
- [ ] `make check` passes.

## Blocked by

- 02 (`scenario protocol + day-loop hook`)

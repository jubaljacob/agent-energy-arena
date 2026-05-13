# 03: Recorder — per-game state log, metadata, final snapshot

Status: ready-for-agent

## Parent

`.scratch/open-source-arena/PRD.md`

## What to build

Add `world/recorder.py` as a peer of `action_log.py`. Each world session — agent-driven or UI-interactive — writes a self-contained run folder with three artifacts:

- `metadata.json` — seed, scenario name (dotted path), session marker (`agent` vs `ui`), started-at timestamp, run identifier.
- `states.jsonl` — one line per simulated day, containing the full end-of-day world state and a per-day summary. Scenario effects (weather overrides, scenario_trace entries) are visible here.
- `final.json` — written exactly once on finalize, regardless of how many times finalize is called.

The recorder exposes two methods called from `world/sim.py`:
- `record_step(world, day)` — called from the daily step after a successful tick.
- `finalize(world)` — called on reset (closing the in-progress run) or when the session ends.

The recorder owns run folder allocation and naming under `runs/`. On reset, the in-progress run is finalized and a fresh run id is allocated for the next game; no run is destroyed by a reset. Add `runs/` to `.gitignore` if not already present.

The existing `ActionLog` is unchanged and continues to own its own JSON-lines file inside the same run folder. The recorder is additive.

## Acceptance criteria

- [ ] `world/recorder.py` exists with `record_step` and `finalize` entry points and run-folder ownership.
- [ ] Every world session — agent-driven and UI-interactive — writes a run folder under `runs/`.
- [ ] `runs/` is gitignored.
- [ ] `metadata.json` contains seed, scenario dotted path, session marker, started-at timestamp, run id.
- [ ] `states.jsonl` has exactly N lines after N successful steps.
- [ ] `final.json` is written exactly once per run; repeated `finalize` calls are idempotent.
- [ ] Reset finalizes the current run and allocates a fresh run id; the prior run folder is preserved.
- [ ] Action log continues to be written into the same run folder, unchanged.
- [ ] Unit tests cover schema after one step, after many steps, metadata field presence, and `finalize` idempotency.
- [ ] `make check` passes.

## Blocked by

None — can start immediately.

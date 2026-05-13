# 06: `evaluate.py` — `--scenario` flag + replay reads scenario from metadata

Status: ready-for-agent

## Parent

`.scratch/open-source-arena/PRD.md`

## What to build

Extend the existing single-game evaluation CLI in `evaluate.py`:

- A new `--scenario <dotted_path>` flag on the play subcommand. When present, the world is reset with that scenario attached; absent, `NullScenario` is used.
- The replay subcommand reads the scenario dotted path from the run folder's `metadata.json` and attaches it before re-simulating from the action log. This keeps action-log replay byte-identical for runs that involved a scenario.

## Acceptance criteria

- [ ] `evaluate.py --scenario scenarios.grid_stress ...` runs the scripted agent on the grid-stress scenario and produces a run folder identifying the scenario in metadata.
- [ ] The replay subcommand reads `metadata.json.scenario` and attaches it, producing a byte-identical replay.
- [ ] Omitting `--scenario` is equivalent to running with `NullScenario` (existing default behavior preserved).
- [ ] `make check` passes.

## Blocked by

- 02 (`scenario protocol + day-loop hook`)
- 03 (`recorder`)

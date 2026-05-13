# 09: UI replay mode — file picker, JSON-lines parse, scrub, step back

Status: ready-for-agent

## Parent

`.scratch/open-source-arena/PRD.md`

## What to build

Add a "load run" file picker to the UI that reads a run folder via the browser File API and renders the recorded game without server contact. Parsing happens client-side from `states.jsonl` and `metadata.json`. The existing live-play render path is unchanged; replay is a separate render mode toggled by whether a run is loaded.

Replay mode supports:
- Day-by-day scrubbing along a timeline.
- Step forward to the next day; step back to the previous day for before/after comparisons.
- Display of the metadata identifying scenario, seed, session marker, and run id.

The UI does NOT need new server endpoints for replay; everything is file-based in the browser.

## Acceptance criteria

- [ ] A file picker in the UI accepts a run folder and switches the UI to replay mode.
- [ ] In replay mode, the UI renders end-of-day world state from `states.jsonl` without contacting the server.
- [ ] The user can step forward and step back through days.
- [ ] Metadata (scenario, seed, session marker, run id) is visible while in replay mode.
- [ ] Live mode is unchanged when no run is loaded; the existing Next-Day, Reset, build, drill, and survey controls still work.
- [ ] A zipped run folder shared between machines opens and renders the same game on the receiving side without a server.
- [ ] Manual play verification of the new flow is documented in the PR description (no frontend test runner exists; see PRD).
- [ ] `make check` passes.

## Blocked by

- 03 (`recorder`)

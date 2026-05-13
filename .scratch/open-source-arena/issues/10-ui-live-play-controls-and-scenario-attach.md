# 10: UI live play/fast-play/pause + mid-game scenario attach control

Status: ready-for-agent

## Parent

`.scratch/open-source-arena/PRD.md`

## What to build

Add play, fast-play, and pause controls to the UI that drive an auto-advance timer:
- Play: 500 ms per day, calls the existing step endpoint each tick (live mode) or advances the in-memory index (replay mode).
- Fast-play: 250 ms per day.
- Pause: stops the timer; current day stays on screen for inspection.
- Keyboard shortcuts mirror the buttons.

Add a UI control to attach a stress scenario mid-game by dotted path. The control calls the `POST /scenario` endpoint from issue 04 and displays the currently-attached scenario name via `GET /scenario`. Existing Next-Day, Reset, build, drill, and survey controls remain unchanged.

## Acceptance criteria

- [ ] Play, fast-play, and pause buttons appear in the UI with documented keyboard shortcuts.
- [ ] In live mode, play advances the world via the step endpoint at 500 ms (fast-play 250 ms).
- [ ] In replay mode, play advances the in-memory index at the same intervals.
- [ ] Pause halts the timer; the displayed day does not change while paused.
- [ ] A dotted-path input attaches a scenario mid-game via `POST /scenario`; the currently-attached scenario is shown in the UI.
- [ ] Existing controls (Next Day, Reset, build, drill, survey) behave unchanged.
- [ ] Manual play verification is documented in the PR description.
- [ ] `make check` passes.

## Blocked by

- 04 (`API scenario attach + run folder id`)

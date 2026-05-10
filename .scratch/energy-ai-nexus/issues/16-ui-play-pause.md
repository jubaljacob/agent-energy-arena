---
Status: needs-triage
---

# 16 — UI play/pause + speed controls + action ticker

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## What to build

The "Next Day" button from slice 01 is replaced with a play/pause control matching the PRD's UX spec:

- **Play (▶) / Pause (⏸) button** in the top bar. Clicking play begins auto-step; clicking pause stops it.
- **Spacebar** toggles play/pause from the keyboard.
- **Period (`.`) key** single-steps one day. Only works while paused.
- **Speed selector** with options 0.5x, 1x, 2x, 4x days/sec. The active speed determines the auto-step interval (e.g., 1x = 1000ms between `/step { "days": 1 }` calls).
- **Action ticker (bottom bar)** lists the actions queued during the current paused turn. The ticker is a UI affordance only; each `/build`, `/demolish`, etc. POST commits server-side immediately. The ticker is a client-side log of "actions taken since last day-advance."

When auto-step is running, the user can click any tile or use any UI control and the action commits in real time without pausing first. (For human ergonomics, the UI may auto-pause when the user enters build-mode, then resume after the action commits — this is an implementer choice.)

## Acceptance criteria

- [ ] Top bar shows a ▶ button when paused; clicking starts auto-step at the active speed.
- [ ] When playing, the ▶ button changes to ⏸; clicking pauses.
- [ ] Spacebar toggles play/pause regardless of focus (except when typing in a text field).
- [ ] Period key single-steps one day; ignored while playing.
- [ ] Speed selector exposes 0.5x, 1x, 2x, 4x; switching speed during playback updates the interval immediately.
- [ ] During auto-step, `/step { "days": 1 }` is called at the configured interval; the UI re-fetches `/state` after each step.
- [ ] Bottom-bar action ticker lists actions submitted during the current paused turn (since last `/step`).
- [ ] Action ticker clears when `/step` is called.
- [ ] The brief's auto-step rate (1 day/sec via Shift+Space) is preserved as 1x speed.
- [ ] No tests required for this slice (UI-only); manual verification in browser.

## Blocked by

- 01 — Server skeleton + determinism foundation

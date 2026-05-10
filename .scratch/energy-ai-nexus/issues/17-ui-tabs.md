---
Status: needs-triage
---

# 17 — UI finance tab + history tab + polish

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## What to build

The right-rail UI tabs are completed:

- **Finance tab**: a line chart of treasury over the last 7 weeks (or the entire game, agent-toggle), plus today's P&L breakdown showing tax, power, oil, OPEX, fuel, carbon, blackout penalty. Each line item is labeled with the value from `/state.today_summary_so_far`.
- **History tab**: a scrollable log of past daily summaries, one row per day with the key numbers (treasury delta, pop change, blackout hours, renewable share, events_active). Latest day at top; user can scroll back arbitrary distance using `/history?days=N`.
- **Polish**: the subsurface tab (slice 06), wells tab (slice 07), events tab (slice 11) are reviewed for visual consistency; any gaps from earlier slices are filled.

The "Year N of M (manual session / agent session)" indicator from the PRD is added to the top bar so a player knows whether they're in a 365-day tutorial run or a 3650-day agent-style run.

## Acceptance criteria

- [ ] Finance tab renders a treasury-over-time line chart using `/history` data.
- [ ] Finance tab daily P&L breakdown shows: tax_revenue, power_revenue, oil_revenue, opex, fuel_cost, carbon_cost, blackout_penalty.
- [ ] History tab shows past daily summaries in reverse-chronological order with key fields (treasury delta, pop change, blackout hours, renewable share, events_active).
- [ ] History tab supports scrolling back arbitrary days; lazy-loads more via `/history?days=N`.
- [ ] Top bar shows session indicator: "Year N of 1 (manual session)" or "Year N of 10 (agent session)" depending on `MANUAL_GAME_DAYS` vs `GAME_DAYS`.
- [ ] All right-rail tabs (subsurface, power, finance, wells, events, history) render without layout regressions.
- [ ] No tests required for this slice (UI-only); manual verification in browser.

## Blocked by

- 13 — Scoring + baselines + `/score` endpoint

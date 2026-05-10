---
Status: needs-triage
---

# 14 — Scripted agent + baseline regression test

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## What to build

`agents/base.py` exposes the `Agent` Protocol and the `BaseAgent` helper from the brief's §7.1. `agents/api_client.py` is a thin `requests` wrapper providing methods for every endpoint.

`agents/scripted.py` implements the **competent 5-phase strategy** from the PRD:

| Phase | Weeks | Goal |
|---|---|---|
| Bootstrap | 1–4 | Halt pop bleed, baseline grid: 1 road, 2 commercial, 2 houses, 4 solar, 1 gas peaker, survey center column |
| Buildout | 5–26 | Reach pop ~500, oil first revenue: commercial+industrial pairs, plants on demand, drill on promising surveys |
| Diversify | 27–104 | Pop ~1500, refinery online, first injection well next to a solar farm (DR demo), survey new column every 8 weeks |
| Mature | 105–260 | Pop ~3000, full grid; replace gas peakers with renewables as carbon price rises; re-explore when reservoir local fraction < 0.4 |
| Late | 261–521 | Maintain, pivot to renewables; demolish coal once carbon price > $80/ton; stop building housing once pop within 90% of capacity |

Plus an **always-on Crisis Response policy**: when `events_active` is non-empty in the latest summary, scripted's next `/step` call uses `days=1` instead of the default `days=7`. On heatwave, build an emergency gas peaker; demolish after the event ends. On blackout, emergency gas peaker.

Strict, deterministic priority ordering of build decisions (10-point list per PRD): starvation triage → blackout response → reserve-margin → capacity → carbon-driven coal demolition → reservoir re-exploration → drilling → refinery → DR-injection siting → skip.

A driver script `python -m agents.scripted --seed 42` runs the agent end-to-end on seed 42 and writes the resulting `(P, T)` to `baselines/seed_42.json` as `{"seed": 42, "p_ref": <P>, "t_ref": <T>}`.

## Acceptance criteria

- [ ] `agents/base.py` exposes `Agent` Protocol and `BaseAgent` class per the brief.
- [ ] `agents/api_client.py` exposes a method per endpoint, all returning parsed JSON or raising on HTTP errors.
- [ ] `agents/scripted.py` implements the 5-phase strategy with the priority-ordered decision list.
- [ ] Scripted uses variable step size: `days=1` when `events_active` non-empty in latest summary, otherwise `days=7`.
- [ ] On heatwave: builds an emergency gas peaker; demolishes after `ends_day`.
- [ ] On blackout: builds an emergency gas peaker.
- [ ] `python -m agents.scripted --seed 42 --output baselines/seed_42.json` runs the scripted agent for the full 3650-day game and writes the baseline file.
- [ ] Scripted completes a 3650-day game without crashing or hanging (verified by `tests/test_scripted_smoke.py`).
- [ ] Two scripted runs on the same seed produce byte-identical world state at every checkpoint (regression test in `tests/test_determinism.py`).
- [ ] **Baseline regression test**: `tests/test_scripted_baseline.py` runs the scripted agent on seed 42 and asserts the final score is within 5% of the committed `baselines/seed_42.json`.
- [ ] Scripted agent code is approximately 200–300 lines and readable in one sitting; comments cite the PRD and brief sections of the mechanics being applied.

## Blocked by

- 13 — Scoring + baselines + `/score` endpoint

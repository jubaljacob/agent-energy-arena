---
Status: needs-triage
---

# 08 — Injection wells + demand-response

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## What to build

`POST /drill { "well_type": "injection", ... }` creates an injection well at $30,000 CAPEX. `POST /control/well` accepts injection wells; setpoint defines a baseline `bbl/day` rate.

The **demand-response mechanic** from the PRD is implemented. Each hour, an injection well's actual power draw and bbl injected depend on the **previous hour's grid balance state**:

- During brownout or blackout: injection well power = 0 kW (sheds load).
- During curtailment: injection well power = `min(2 × baseline_kw, Q_MAX_WELL_BBL_DAY × INJECTION_KWH_PER_BBL / 24)` (ramps up to absorb surplus).
- During balanced state: injection well runs at baseline.

This breaks the otherwise-circular dependency between injection power (a load) and balance state. Initial hour 0 of a fresh world treats the previous balance state as "balanced".

`cumulative_injected_bbl` reflects the actually-delivered injection (sum of hourly contributions), not setpoint × days. The production formula's `effective_fraction = min(1.0, fraction + pressure_boost)` now uses `inj_total = sum of cumulative_injected_bbl` from injection wells whose 3×3×3 pools intersect the production well's pool, with `pressure_boost = min(0.5, inj_total / V_init)`.

## Acceptance criteria

- [ ] `POST /drill { "well_type": "injection", ... }` deducts $30,000 and creates an injection well.
- [ ] `POST /control/well` on an injection well sets the baseline setpoint in [0, 200] bbl/day.
- [ ] During balanced grid hours, injection well power equals `setpoint × INJECTION_KWH_PER_BBL / 24` (= 50 kWh/bbl × setpoint / 24).
- [ ] During brownout or blackout hours, injection well power is 0 kW; no bbl injected that hour.
- [ ] During curtailment hours, injection well power ramps to up to 2× baseline (capped at `Q_MAX_WELL_BBL_DAY` equivalent power); the additional injection is recorded in `cumulative_injected_bbl`.
- [ ] The previous hour's balance state drives the current hour's injection behavior (1-hour lag).
- [ ] At fresh-world hour 0, previous balance state is treated as "balanced".
- [ ] `cumulative_injected_bbl` reflects DR-adjusted actual injection, not naive `setpoint × hours`.
- [ ] When injection wells exist and intersect a production well's 3×3×3 pool, the production formula uses `effective_fraction = min(1.0, fraction + pressure_boost)`. Production capacity rises accordingly.
- [ ] Daily summary's `injection_kw` accurately accumulates the DR-adjusted hourly power.
- [ ] Tests in `world/tests/test_injection.py` cover: shed-during-shortage, ramp-during-curtailment, baseline-during-balanced, prev-hour lag, pressure_boost integration with production well capacity.
- [ ] Integration test: solar-overbuild + injection-well + production-well cluster shows production capacity stays high in late game compared to no-injection control.

## Blocked by

- 05 — Plants + dispatch + balance state + power revenue
- 07 — Production wells + crude revenue

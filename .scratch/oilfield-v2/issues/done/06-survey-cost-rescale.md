# 06 — Survey cost `(size/4)²`

Status: needs-triage

## Parent

`.scratch/oilfield-v2/PRD.md`

## What to build

Rescale survey cost to `15_000 * (size/4)**2` so a size-4 column costs $15k (same as today's default click) and a size-8 column costs $60k. Default UI survey size drops from 8 to 4 so the cheapest option is the path of least friction. Catalog exposes the new formula and default.

## Acceptance criteria

- [ ] `world/subsurface.py:survey_cost(size)` returns `SEISMIC_BASE_COST * (size / 4) ** 2` (base $15k unchanged).
- [ ] `SEISMIC_DEFAULT_SIZE = 4`.
- [ ] `/catalog.subsurface.survey` exposes `cost_formula: "base * (size/4)**2"` and `default_size: 4`. `base_cost` already present stays.
- [ ] `world/ui/index.html` survey size input: `value="4"`, min/max unchanged (4–16 still valid).
- [ ] UI cost preview reads the new formula and updates live as the size input changes.
- [ ] Existing or new test asserts `survey_cost(4) == 15_000` and `survey_cost(8) == 60_000`.
- [ ] `make check` passes (any test pinning the old size-8 default cost is updated).

## Blocked by

None - can start immediately.

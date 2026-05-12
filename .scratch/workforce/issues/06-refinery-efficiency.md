---
Status: needs-triage
---

# 06 — Refinery scales with efficiency

## Parent

[PRD: World v2 — Workforce & Per-Facility Staffing](../PRD.md)

## What to build

A refinery's max throughput cap and process electric load both scale with staffing efficiency. A half-staffed refinery processes at most half of `REFINERY_MAX_BBL_DAY` per day and draws half the process kW it would at full staff. An idle refinery has zero throughput and zero process load.

### Implementation details

**`world/economy.py`** — `REFINERY_MAX_BBL_DAY = 500.0` at line 33 stays as the catalog constant. The per-refinery effective cap becomes a function of staffing:

- **`refine_one(setpoint_rate_bbl_day, available_crude_bbl)`** at `world/economy.py:48` currently caps at `REFINERY_MAX_BBL_DAY`. Add a third argument or thread the refinery tile so the cap becomes `REFINERY_MAX_BBL_DAY × efficiency(refinery)`. Prefer threading the tile (cleaner) rather than recomputing from scratch.
- **`route_crude(refineries, total_crude_bbl)`** at `world/economy.py:63` distributes crude across refineries up to each refinery's `REFINERY_MAX_BBL_DAY` cap. Per-refinery cap becomes `REFINERY_MAX_BBL_DAY × efficiency(refinery)`. The distribution policy (fairshare? sequential? — preserve whatever the existing implementation does) is unchanged; only the cap input changes.
- A 0%-staffed refinery has cap=0 and receives no crude.
- The `setpoint_rate_bbl_day` clamp (`REFINERY_SETPOINT_MAX` at line 38) is a UI/API affordance — the player can still set the setpoint to 500. The *actual* throughput is capped at `min(setpoint, effective_max, available_crude)` per refinery. **Do not** clamp the setpoint itself by efficiency — that would surprise the player when staffing changes.

**`world/economy.py:84` — `refinery_process_kw(throughput_bbl_day)`**:

The process kW is `throughput_bbl_day × kWh_per_bbl / 24`. Already linear in throughput, so scaling is automatic once the throughput respects the efficiency-capped cap. No change to `refinery_process_kw` itself.

But the refinery's appearance in `_process_loads_kw` (in `world/power.py`) reads `t.current_throughput_bbl_day` — that's yesterday's actual throughput, already efficiency-scaled because `route_crude` was efficiency-aware. So process load scales correctly through this path.

Verify: an idle refinery has `current_throughput_bbl_day=0` (since `route_crude` allocates 0 crude to it), which yields `refinery_process_kw=0`. An idle refinery draws zero process electric load.

**`world/economy.py` — `daily_emissions_t(world)`**:

Refinery CO2 is `0.3 t/bbl × refined_bbl_today`. Already linear in throughput; no change needed. An idle refinery emits 0 t/day from refining (PRD story 19 generalises to refinery via the uniform rule).

### Tests to add in this slice

`world/tests/test_economy.py`:

- **Half-staffed refinery routes half crude**: inject one refinery `staffed_jobs=12` (jobs=25, efficiency=0.48). `route_crude([refinery], total_crude=1000)`: refinery gets `min(1000, 500 × 0.48) = 240 bbl` allocated.
- **Idle refinery routes zero crude**: refinery `staffed_jobs=0`. `route_crude([refinery], total_crude=1000)`: refinery gets 0; if there are no other refineries, 1000 bbl of crude remains unrouted (sells as crude or sits in inventory — match whatever the existing v1 behaviour is).
- **Two-refinery routing favors higher-effective-cap**: refinery A `staffed_jobs=25` (full, cap=500), refinery B `staffed_jobs=12` (half, cap=240). With 600 bbl available, the distribution respects A's cap of 500 and B's cap of 240. (If the routing is fairshare, both saturate where they can; if sequential by id, A fills first to 500 then B gets 100. Preserve the existing routing policy and assert its specific behaviour.)
- **Idle refinery draws zero process load**: refinery `staffed_jobs=0`, `current_throughput_bbl_day=0`. `refinery_process_kw(0) = 0`. Through `_process_loads_kw`, `total_demand_kw` has no contribution from this refinery.
- **Half-staffed refinery process load tracks throughput**: refinery `staffed_jobs=12`, yesterday's throughput pinned at 240 bbl/day. `refinery_process_kw(240) = 240 × 200 / 24 = 2000 kW`. Half of what a full refinery at 500 bbl/day would draw (4167 kW).
- **Idle refinery emits zero CO2**: refinery with `staffed_jobs=0` after running a full day with available crude. `daily_emissions_t(world)` reflects 0 t from refining. (PRD story 19 — verifying idle = zero footprint generalises.)
- **Setpoint not auto-clamped by efficiency**: refinery with `setpoint_rate_bbl_day=500`, mutate `staffed_jobs` to 12. Assert `setpoint_rate_bbl_day` remains 500 in `/state`. Only the *actual* `current_throughput_bbl_day` reflects the cap.

## Acceptance criteria

- [ ] `route_crude` per-refinery cap becomes `REFINERY_MAX_BBL_DAY × workforce.efficiency(refinery)`.
- [ ] `refine_one` (or its replacement) caps throughput at the efficiency-scaled max.
- [ ] Refinery process electric load through `_process_loads_kw` reflects the efficiency-scaled throughput.
- [ ] Refinery CO2 emissions track refined throughput, so an idle refinery emits 0 from refining.
- [ ] The player-facing `setpoint_rate_bbl_day` is **not** clamped by efficiency (only the actual throughput is).
- [ ] New tests cover idle / half-staffed / fully-staffed routing and process load.
- [ ] `make check` is green.

## Blocked by

- 01 — Workforce foundation (provides `workforce.efficiency` and `staffed_jobs` field)

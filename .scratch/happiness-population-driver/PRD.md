# PRD — Happiness as a Continuous Population Driver

Status: needs-triage

## Problem Statement

Happiness in the current model is a tripwire, not a driver. The four-branch cascade in `world/population.py` only makes happiness "matter" in two narrow regimes: it can *slow growth* (when the grow gate is open, happiness ≥ 0.3) or *cause decline* (when happiness drops below 0.3). Everywhere in between — happiness 0.3 to 1.0, with adequate jobs and housing — happiness is **invisible**. A city sitting at happiness 0.6 with 161 jobs and 252 capacity looks identical to one at happiness 1.0: both fall into the grow branch with sub-1/day growth that gets truncated to zero by the `int(pop)` cast on every tick.

By the time happiness hits 0.3, the city is already at the bottom of a death spiral the player had no warning signs about. Outages, coal proximity, and industrial noise can accumulate over many in-game weeks without registering as a population effect, then suddenly trip the `pop *= 0.99` branch and decline catastrophically. The mechanic punishes already-broken cities and gives no signal to actively-degrading ones.

A separate but related defect: even when the grow branch fires, fractional growth (< 1 person/day) is silently lost because `state.population` is stored as `int` and re-truncated every tick. A 157-person city with growth headroom of 4 jobs and happiness 0.8 computes a daily delta of ~0.78 — every day, for the rest of the game — and stays at exactly 157.

## Solution

Happiness becomes a continuous **signed velocity** around a neutral point of 1.0. Every tick:

- Above neutral (happiness > 1.0, achieved via parks): population grows.
- At neutral (happiness = 1.0, vanilla city): population is stable.
- Below neutral (happiness < 1.0, any penalty): population *actively emigrates*, with the rate proportional to the deficit.

The four-branch `if/elif/elif/elif` cascade in `update_population` collapses into one velocity formula plus two structural clamps (housing exodus and jobs floor, kept gradual as today). Population is stored as a float so fractional dynamics accumulate correctly; API and UI consumers continue to see integers via a serializer cast.

The scripted reference agent is updated in the same change to build parks during its Bootstrap phase, so the baseline run on seed 42 produces happiness > 1.0 and meaningful growth. `baselines/seed_42.json` is regenerated. Without this, every agent's `P_ref` would be artificially deflated and score ratios would be meaningless for one PR cycle.

## User Stories

1. As a human player, I want to see population react to *any* drop in happiness, so that I learn the consequences of my decisions before the city is doomed.
2. As a human player, I want parks to drive growth, so that there is a meaningful "happiness lever" to pull when I have spare cash.
3. As a human player, I want a single coal plant near my houses to register as a slow, visible population drag, so that the carbon/happiness tradeoff is felt during placement decisions rather than discovered after the fact.
4. As a human player, I want industrial tiles to make nearby housing undesirable, so that zoning matters and I have a reason to keep noise sources away from clusters.
5. As a human player, I want a 5-hour blackout to noticeably bleed population, so that grid reliability feels like a continuous priority not a binary one.
6. As a human player, I want a happy city to grow at a rate I can plan around, so that residential expansion has a predictable payoff curve.
7. As a human player, I want happiness above neutral (parks built, no penalties) to be visibly rewarded with population growth, so that the "city builder" loop has a clean positive feedback signal.
8. As a human player, I want a misclick that drops me below housing capacity to give me ~10 days to react rather than instantly erasing 50 residents, so that the game remains forgiving of input mistakes.
9. As an AI agent author, I want population dynamics to be continuous and predictable from observed state, so that planning over a 7-day step horizon produces accurate projections.
10. As an AI agent author, I want the same `pop_delta = b · pop · (happiness − 1.0)` formula to apply at every population scale, so that policies I tune at pop=100 still hold at pop=2000.
11. As an AI agent author, I want fractional population changes to accumulate across days, so that long-horizon planning doesn't waste budget on actions whose effects get rounded away.
12. As an AI agent author, I want the integer population reported on the API wire, so that my existing state-summarization and parsing logic continues to work unchanged.
13. As an AI agent author, I want the scripted-agent baseline (P_ref, T_ref) to remain a meaningful "competent floor" under the new rules, so that my score ratios are interpretable.
14. As an AI agent author, I want clear documentation of the new formula in the prompt or brief, so that LLM-driven agents have the mechanics necessary to make park/industrial siting decisions.
15. As a judge/organizer, I want determinism preserved (same seed + same actions → same final state), so that scoring and replay remain reproducible.
16. As a judge/organizer, I want the dev-seed baseline file regenerated in the same PR that changes the mechanics, so that the committed reference is always consistent with the shipped rules.
17. As a maintainer, I want the velocity formula and structural clamps separated into pure helper functions, so that I can unit-test the math without standing up a full World fixture.
18. As a maintainer, I want a regression test for the fractional-accumulation behavior, so that the truncation bug we just fixed cannot silently re-appear.
19. As a maintainer, I want the workforce module to keep operating on integer worker counts, so that the existing job-staffing logic stays untouched even after population becomes a float.
20. As a maintainer, I want the four-branch cascade gone from the code, so that the population logic reads as one formula plus two clamps and is comprehensible in a single pass.
21. As a maintainer, I want the scripted agent to ship with a tested park-placement rule, so that the baseline doesn't depend on emergent agent behavior that could regress silently.

## Implementation Decisions

**Core dynamics — `world.population`**

- Replace the four-branch `if/elif/elif/elif` cascade with a single signed-velocity formula: `delta = b · pop · (happiness − h_neutral)` with `b = 0.012` (kept identical to today's `base_growth_rate`) and `h_neutral = 1.0`.
- Upward velocity is capped by structural headroom: `delta = min(delta, max(0, capacity − pop), max(0, jobs − pop))` so growth cannot exceed available housing or jobs. Downward velocity is *not* clamped by structural state — an unhappy city sheds people even if jobs and housing are abundant.
- After the velocity step, the existing **gradual structural clamps** still fire: housing exodus stays as `pop = max(capacity, pop − 5)` and jobs floor stays as `pop = max(jobs/0.7, pop · 0.99)`. They become emergency backstops, not the primary mechanism.
- Happiness is still clipped to `[0, 1.5]`. The asymmetry (max growth pressure +0.5; max emigration pressure −1.0) is intentional: cities die faster than they grow, which rewards proactive happiness defense over reactive recovery.
- The previous `happiness < 0.3 → pop *= 0.99` decline branch is deleted (the velocity formula now covers that regime continuously).

**Module structure**

- Extract `happiness_velocity(pop: float, happiness: float, capacity: int, jobs: int) -> float` as a pure function. Inputs are numbers, output is the signed daily delta after upward clamping. No world reference, no mutation.
- Extract `apply_structural_clamps(pop: float, capacity: int, jobs: int) -> float` as a pure function. Inputs are numbers, output is the post-clamp population. Encapsulates the housing exodus and jobs floor logic.
- `update_population(world)` becomes a thin orchestrator: compute happiness (existing logic, unchanged), call `happiness_velocity`, call `apply_structural_clamps`, persist to state, run workforce churn on integer transitions, accrue daily tax.

**Population storage — `world.state`**

- `WorldState.population` changes type from `int` to `float`.
- The float represents the continuous quantity; the integer count of "residents who can take jobs" is derived as `int(state.population)` at the boundary.

**API and UI — `world.api`**

- The `/state` serializer casts to `int(state.population)` on the way out so existing consumers (UI, agents, tests) see integers on the wire without modification.
- Daily summary payloads and `/state/summary` (if/where applicable) cast similarly.
- No new endpoints. No new fields.

**Workforce coupling — `world.workforce`**

- Workforce continues to operate on integer worker counts (it assigns specific workers to specific tiles; this is fundamentally discrete).
- `drain_n` and `hire_to_fill` are invoked from `update_population` based on the integer transition `delta_int = int(pop_after) − int(pop_before)`. Fractional changes that don't cross an integer boundary do not trigger workforce hooks; they accumulate on `state.population` until the integer part shifts.

**Scripted-agent change — `agents.scripted`**

- The Bootstrap phase gains a park-placement rule: whenever the agent has just placed a cluster of houses, place one park on a road-adjacent tile within Chebyshev radius 2 of the cluster. The rule uses the existing tile-search helpers and respects the existing treasury threshold.
- Targets: every house cluster ends up with at least one park inside its Chebyshev-2 noise window so that the post-Bootstrap happiness floor is ≥ 1.0 (with capacity for parks-driven growth).
- The rule is deterministic and runs in the same decision-priority slot as other Bootstrap building actions.

**Baseline regeneration — `baselines/seed_42.json`**

- Regenerate by running `python -m agents.scripted --seed 42 --output baselines/seed_42.json` after the scripted-agent change lands.
- The regenerated file is committed in the same PR as the mechanics change.
- Both `P_ref` and `T_ref` will shift; this is expected. Score ratios for prior agent runs are not preserved.

**Determinism and replay**

- No new RNG draws. The change is purely deterministic arithmetic on existing state.
- Action-log replay (`tests/test_determinism.py`) continues to pass without modification.

**Configuration**

- The constants `b = 0.012` and `h_neutral = 1.0` may be promoted to `world/config.py` if a quick playtest reveals they need to be env-tunable. Default-only is acceptable for v1 of this change.

## Testing Decisions

**Testing philosophy.** Each test asserts an *externally observable* property — a number that changed, a sequence of state transitions, a published API response — never an implementation detail like the internal branch taken or the order of helper calls. Where a pure helper exists (`happiness_velocity`, `apply_structural_clamps`), tests target that helper directly with numeric inputs and outputs; no World fixture, no API client, no RNG. Where dynamics integrate over multiple ticks, tests use the full `update_population` against a minimal `WorldState` fixture.

**Tests for `happiness_velocity` (pure unit tests)**

- At `happiness = 1.0` exactly, the function returns `0.0` regardless of population (the neutral fixed-point).
- At `happiness > 1.0` with adequate jobs and capacity, returns a positive delta proportional to `pop · (h − 1)`.
- At `happiness < 1.0`, returns a negative delta with magnitude proportional to `pop · (1 − h)`.
- At `happiness = 0.0`, returns the maximum-magnitude negative delta: `−0.012 · pop`.
- At `happiness = 1.5`, returns the maximum-magnitude positive delta after clamping: `+0.012 · pop · 0.5`.
- Upward delta clamps to `min(headroom_jobs, headroom_capacity)` when those bind; downward delta does not clamp on jobs/capacity.
- Asymmetry: max emigration magnitude is double max growth magnitude at the cap edges.

**Tests for `apply_structural_clamps` (pure unit tests)**

- When `pop ≤ capacity` and `jobs ≥ 0.7 · pop`, the function is a no-op (returns input unchanged).
- When `pop > capacity`, housing exodus applies: result is `max(capacity, pop − 5)`. A small overrun (pop = capacity + 1) drains by 1; a large overrun (pop = capacity + 100) drains by 5; the bound is `capacity` exactly when it would be crossed in the next tick.
- When `jobs < 0.7 · pop`, the jobs floor applies: result is `max(jobs / 0.7, pop · 0.99)`. A mild deficit yields gradual 1% decay; a severe deficit snaps to the `jobs/0.7` floor.
- Both clamps interact correctly when both conditions hold simultaneously.

**Tests for `update_population` (integration)**

- A happy city (`happiness = 1.2`) with abundant jobs and capacity grows monotonically over 30 ticks.
- A neutral city (`happiness = 1.0`) with abundant jobs and capacity holds its population (within fractional drift) over 30 ticks.
- An unhappy city (`happiness = 0.7`) with abundant jobs and capacity bleeds population over 30 ticks; the trajectory matches the closed-form `pop · (1 + 0.012 · (h − 1))^N` to within float tolerance.
- A city at small population with sub-integer daily growth accumulates fractional residents across days and crosses integer boundaries on schedule (the regression test for the truncation bug).
- Workforce hooks fire when and only when `int(state.population)` transitions across an integer boundary.
- The daily tax accrual reflects the post-update integer population.

**Tests for scripted-agent baseline**

- Smoke test: `ScriptedAgent` on seed 42 reaches population ≥ N after a full 10-year run, where N is chosen as a regression floor (e.g. `0.8 · committed_P_ref` so minor calibration shifts don't break CI).
- The baseline run terminates deterministically: two consecutive runs with seed 42 produce byte-identical `baselines/seed_42.json` content.

**Prior art**

- `world/tests/test_population.py` already covers the existing happiness components (park benefit, noise penalty, coal proximity, blackout penalty). Those tests target the *happiness number* itself and remain valid — only the tests asserting on the old gate (the `happiness ≥ 0.5 → grow / happiness < 0.5 → decline` cutoffs documented in upgrade-brief §3.3) need to be retired.
- `world/tests/test_determinism.py` provides the pattern for "two runs produce byte-identical output."
- `agents/tests/` provides scripted-agent smoke-test patterns.

## Out of Scope

- LLM-agent prompt updates (`agents/prompts.py`): the LLM ReAct agent will observe the new dynamics through normal state summaries and adapt without prompt surgery. A follow-up PR can add explicit happiness-mechanics text once playtesting reveals what guidance is actually needed.
- UI display rework: integer population on the wire means the existing UI continues to work. Visualizing the "happiness velocity" as a trend indicator on the dashboard is a separate enhancement.
- Re-tuning the existing happiness penalty coefficients (noise = −0.03 per source, blackout = −0.05/hour, coal = −0.05 max, etc.). Under the new model these coefficients produce stronger effects than they did under the old gate — that is on-design for this PRD (the entire point was making happiness bite). Calibration tuning is a planned follow-up after playtesting against the new baseline.
- Promoting `b` and `h_neutral` to env vars in `world/config.py`. Default-only is acceptable for v1; promote if and when playtesting demands a tunable knob.
- Multi-seed baseline regeneration. Only the dev seed (42) is committed to the repo; the eval seed is computed by organizers at scoring time and is unaffected by this change beyond running against the new code.
- Changing the asymmetry of the happiness cap (extending to `[0, 2.0]` or tightening to `[0.5, 1.5]`). The existing `[0, 1.5]` clip is preserved.

## Further Notes

**Why h_neutral = 1.0 rather than a softer value like 0.7.** A softer neutral keeps the game forgiving but defeats the design goal: the entire point of this change is to make happiness a *dominant* lever, not an incremental tax. With `h_neutral = 1.0`, a vanilla city (no parks, no penalties) sits exactly at zero growth. Any happiness drop produces immediate emigration. Any happiness boost (parks) produces growth. The choice is decisive, not gradual — and it means parks become a strategic priority rather than a cosmetic one. The scripted agent change in this same PRD ensures the baseline remains a competent floor under the new rules.

**Why symmetric `b` rather than asymmetric grow/shrink rates.** Asymmetric coefficients are tempting (preserve old growth velocity, soften emigration) but introduce two calibration knobs that will drift over time. One number is easier to reason about, easier to test, and aligns the formula with the brief's stated constant.

**Why the structural clamps stay gradual.** Instant snaps on housing exodus and jobs floor stack with the new happiness pressure and turn single misclicks into multi-week recoveries. Gradual clamps (the current behavior) give the agent — human or AI — a ~10-day window to react. Same equilibrium, more forgiving dynamics.

**Calibration risk.** Under the new model, the existing happiness penalty coefficients produce stronger effects than they did under the old gate. Specifically, max noise (−0.3) produces ~−0.36%/day emigration ≈ −73%/year. A house adjacent to two industrial tiles with no park buffer is now in a slow death spiral with no other input changes. This is *on-design* for the PRD but flagged for the calibration follow-up: if playtesting shows the noise penalty is too punishing in practice, the per-source `0.03` coefficient is the first knob to revisit. The blackout per-hour and coal-proximity coefficients are similar candidates.

**Baseline shift.** The committed `baselines/seed_42.json` values will change in this PR. Any cached comparison data referencing the prior P_ref or T_ref values is invalidated. Downstream tests that assert on specific baseline numbers must be updated alongside the regeneration.

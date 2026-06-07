"""Grid Master — a compounding, workforce-aware clean-city agent.

Scoring (see ``world/scoring.py``) blends six terms into a [0, 100]
headline: treasury level/trend/trough (0.30), population (0.30, saturates
at 400 residents), happiness (0.10), renewable share (0.20), solvency
(0.10), and an additive longevity term (0.15, maxed at 730 days). The two
dominant levers are **population** and **treasury**.

The single most important mechanic this agent is built around is the
**workforce**. Every job-providing tile only functions in proportion to
its staffing ratio: an unstaffed solar farm generates 0 kW, an unstaffed
commercial tile earns $0. Workers come from the population, and the world
fills vacancies *oldest-tile-first*. So population is both the score
objective and the scarce input that powers the whole economy, and *build
order is staffing priority*.

Strategy
--------
Grow a dense, clean residential district east of the town hall (clear of
the starter coal plant's Chebyshev-5 happiness halo):

* **Population** grows only while happiness > 1.0 and there is housing and
  jobs headroom. Happiness is bought with **parks** (worker-free, +0.10
  per park within cheb-2 of a residence, capped +0.30) placed densely
  enough to clear the coal-share penalty and push the growth velocity.
* **Treasury** is driven by **commercial** tiles — carbon-free, cheap, and
  un-deduplicated (every commercial within a 5×5 of housing earns the full
  per-resident amount), so a ring of commercial around a dense housing
  core is a clean money pump that *also* supplies the jobs population
  growth needs. Built early so the workforce staffs them first.
* **Power** keeps the free starter coal plant for bulletproof night
  reliability, and layers on a solar fleet (scaled to the workforce) plus
  worker-free batteries that time-shift renewable energy into the evening
  — firming the peak and keeping the renewable share above the 0.5 target.
* **Solvency** is guaranteed by a cash floor: no discretionary build may
  pull the treasury below a cushion sized to operating expense.

Deterministic throughout (no wall-clock, no RNG).
"""

from __future__ import annotations

import argparse
import json
from collections import deque
from pathlib import Path
from typing import Any

from agents.api_client import ApiClient
from agents.base import BaseAgent

# --- Catalog mirror (stable defaults; see world/catalog.py, GET /catalog) ---
CAPEX: dict[str, float] = {
    "road": 500,
    "house": 3_000,
    "commercial": 8_000,
    "industrial": 20_000,
    "park": 5_000,
    "solar_farm": 25_000,
    "wind_turbine": 40_000,
    "gas_peaker": 80_000,
    "coal_plant": 200_000,
    "battery": 60_000,
    "refinery": 150_000,
    "pipeline": 2_000,
}
OPEX: dict[str, float] = {
    "road": 0,
    "house": 20,
    "commercial": 50,
    "industrial": 200,
    "park": 30,
    "solar_farm": 50,
    "wind_turbine": 80,
    "gas_peaker": 150,
    "coal_plant": 400,
    "battery": 40,
    "refinery": 300,
    "pipeline": 5,
}
JOBS: dict[str, int] = {
    "commercial": 12,
    "industrial": 30,
    "solar_farm": 2,
    "wind_turbine": 2,
    "gas_peaker": 4,
    "coal_plant": 30,
    "refinery": 25,
    "town_hall": 30,
}
HOUSING: dict[str, int] = {"house": 8, "town_hall": 100}

COAL_CAPACITY_KW: float = 1500.0
BATTERY_POWER_KW: float = 200.0

# --- Strategy knobs ---------------------------------------------------------
POP_TARGET: int = 400  # u_pop saturates here
CITY_POP_CAP: int = 470  # stop expanding capacity/jobs past here

# Keep capacity and jobs a little above population so the growth velocity is
# never clamped, but tight enough that occupancy (→ commercial revenue) and
# staffing ratios stay high.
CAP_HEADROOM: int = 24
JOBS_HEADROOM: int = 16

# Parks: target this many parks within cheb-2 of every residence (≈+0.20
# happiness). In a compact city a handful of parks blanket the core, so cap
# the count — over-parking is pure opex drain that starves the battery fleet.
PARK_COVER_TARGET: int = 2
MAX_PARKS: int = 12

# Power fleet, scaled to the workforce so plants do not sit idle:
#   target_solar  = SOLAR_BASE + pop // SOLAR_PER_POP
#   target_battery= BATT_BASE  + pop // BATT_PER_POP
# Grid sizing. The binding constraint is NOT nameplate capacity — coal has
# plenty — but (a) the morning ramp (commercial load jumps 5× at 08:00, far
# faster than coal's 10%/hr ramp) and (b) keeping batteries charged. The fix
# is to build enough solar that midday output *exceeds* demand, creating the
# renewable surplus that charges the (worker-free) batteries that then cover
# the morning/evening ramp. So solar is sized to peak demand, not population.
SOLAR_BASE: int = 4
SOLAR_PEAK_EFFECTIVE_KW: float = 110.0  # ~midday output per farm (sun×cloud)
SOLAR_OVERBUILD: float = 1.35  # midday solar target = this × daytime peak
BATT_BASE: int = 2
BATT_PER_COMMERCIAL: float = 0.5  # batteries firm the commercial morning ramp
MAX_SOLAR: int = 24
MAX_BATTERY: int = 18

# Reliability: a single coal plant is a single point of failure — a
# `plant_failure` event (3-7 days) blacks out the whole city, and the
# resulting blackout penalty ($4k/hr) is the single biggest score killer.
# So N-1 firm redundancy (a second coal plant, 1500 kW — enough to carry the
# whole city alone if the first fails) is treated as essential infrastructure:
# once the city is past SECOND_COAL_POP we hold cash to fund it before
# growing further.
SECOND_COAL_POP: int = 150
SECOND_COAL_RESERVE: float = 235_000.0

# Morning-ramp model. The whole-city load jumps when commercial flips to
# full at 08:00, faster than coal can ramp (10%/hr). What carries that step
# is: coal's must-run floor + one hour of ramp, the (low) morning sun, and —
# critically — charged batteries. Growth is gated on this headroom so new
# load never outruns the grid, and batteries are sized to close the gap.
COAL_MORNING_KW: float = 525.0  # must-run (375) + one hour of ramp (150)
SOLAR_MORNING_KW: float = 45.0  # ~40% sun per farm at 08:00
MORNING_RESERVE: float = 1.12

# Cash management is tiered so load-supporting grid spend has priority over
# load-adding growth spend (otherwise growth drains the seed cash and the
# grid can never be funded, which strands the city in a morning-ramp
# brownout). Growth is only funded out of comfortable surplus, which paces
# the city to the revenue it can actually sustain.
GRID_FLOOR_BASE: float = 15_000.0
GRID_FLOOR_OPEX_DAYS: float = 6.0
GROWTH_FLOOR_BASE: float = 30_000.0
GROWTH_FLOOR_OPEX_DAYS: float = 18.0
# When cash is tight AND the last day ran an operating deficit, build only
# revenue (commercial); pause every discretionary build.
DEFENSIVE_CASH_MULT: float = 1.6

# Residential district: rectangle east of the town hall (x >= cx-2 keeps it
# clear of the starter coal's cheb-5 happiness halo at (cx-8, cy)).
DISTRICT_X0_OFFSET: int = -2
DISTRICT_X1: int = 25
DISTRICT_Y0: int = 10
DISTRICT_Y1: int = 22

# Oil. The treasury engine. Orphan producers (no pipeline / refinery) sell
# crude raw at $40/bbl, and a production well only draws 15 kWh/bbl (vs a
# refinery's 200), sheddable on brownout — so a couple of producers add
# ~$16k/day with almost no grid or renewable-share cost, and NO refinery
# (whose continuous 200 kWh/bbl load would wreck the renewable share). We
# survey to find a reservoir, drill producers on the richest voxels, and add
# injection wells once production starts declining (pressure support).
OIL_START_CASH: float = 220_000.0  # oil is a surplus-funded booster, not core
SURVEY_SIZE: int = 4  # cheapest legal column ($15k)
SURVEY_INTERVAL_DAYS: int = 2  # hunt fast until a reservoir is found
DRILL_MIN_CASH: float = 130_000.0  # only drill a producer with this much cash
# A well's rate scales with the MEAN permeability of its 3×3×3 drainage pool
# (k_eff = mean_perm/500), so drilling the reservoir *interior* (whole pool
# rich) beats a high-estimate edge voxel whose pool is mostly rock. We score
# candidate voxels by the oil×perm of their revealed neighborhood and require
# a minimum pool richness before committing a drill.
POOL_OIL_MIN: float = 20_000.0  # min summed oil over the revealed pool
# Until the first producer is online, hold the seed cash for the oil program
# instead of spending it on city growth (oil is by far the highest-ROI early
# investment, and it funds the whole city thereafter).
OIL_RESERVE: float = 185_000.0
# If the seed sweep finds no reservoir after this many surveys, give up on oil
# and fall back to the clean-commercial city (release the cash reserve).
OIL_ABANDON_SURVEYS: int = 14
OIL_MIN_BBL: float = 3_500.0  # drill thresholds on the revealed voxel
PERM_MIN_MD: float = 140.0
MAX_PRODUCERS: int = 4
WELL_RATE_BBL_DAY: float = 200.0
INJECTION_RATE_BBL_DAY: float = 190.0
DRILL_RESERVE: float = 70_000.0  # keep this much cash after a drill


class GridMasterAgent(BaseAgent):
    """Deterministic compounding clean-city agent."""

    def __init__(self, api: ApiClient, *, seed: int | None = None) -> None:
        super().__init__(api, seed=seed)
        self._cash: float = 0.0
        self._tiles: list[dict[str, Any]] = []
        self._occupied: set[tuple[int, int]] = set()
        self._skeleton: list[tuple[int, int]] = []
        self._w: int = 32
        self._h: int = 32
        self._pop_now: int = 0
        self._current_day: int = 0
        self._oil_online: bool = False
        self._coal_count: int = 1
        self._last_survey_day: int = -SURVEY_INTERVAL_DAYS
        self._survey_idx: int = 0
        self._surveyed: set[tuple[int, int]] = set()

    # -- Cadence ----------------------------------------------------------

    def next_step_days(self, state: dict[str, Any]) -> int:
        if state.get("active_events"):
            return 1
        if int(state["population"]) < POP_TARGET:
            return 1
        stressed = any(
            s in ("brownout", "blackout")
            for s in (state.get("last_day_balance_state_by_hour") or [])
        )
        return 1 if stressed else 7

    # -- Per-turn decisions ----------------------------------------------

    def act(self, state: dict[str, Any]) -> None:
        cfg = state["config"]
        self._w, self._h = int(cfg["world_w"]), int(cfg["world_h"])
        cx, cy = self._w // 2, self._h // 2
        self._cash = float(state["treasury"])
        self._tiles = [dict(t) for t in state["tiles"]]
        self._occupied = {(t["x"], t["y"]) for t in self._tiles}
        self._occupied |= {(w["x"], w["y"]) for w in state["wells"]}
        if not self._skeleton:
            self._skeleton = self._build_skeleton_plan(cx, cy)

        day = int(state["day"])
        self._current_day = day
        pop = int(state["population"])
        self._pop_now = pop
        self._oil_online = any(w["type"] == "production" for w in state["wells"])
        self._coal_count = sum(1 for t in self._tiles if t["type"] == "coal_plant")
        events = {e["type"] for e in (state.get("active_events") or [])}
        hourly = state.get("last_day_balance_state_by_hour") or []
        stressed = any(s in ("brownout", "blackout") for s in hourly)

        if day == 0:
            self._bootstrap(pop)
            return

        # Defensive mode: tight cash AND an operating deficit last day → only
        # add revenue, let the treasury recover. Guards the solvency term.
        defensive = (
            self._cash < DEFENSIVE_CASH_MULT * self._growth_floor() and self._op_net(state) < 0
        )

        # 1. Grid first — build solar/battery toward the demand-driven target
        #    so generation stays ahead of load. A blackout torches happiness,
        #    treasury and solvency simultaneously.
        self._ensure_grid(pop, stressed, events)

        # 1b. Oil — the treasury engine. Survey, drill producers (sell crude
        #     raw), add injection support as wells decline. Funds growth and
        #     reliability. Gated on cash so a drill never risks solvency.
        self._ensure_oil(state, day)

        # 2. Growth, gated on (a) morning-ramp headroom and (b) battery
        #    resilience — only grow the city once the battery fleet could carry
        #    the evening peak through a multi-day coal failure (the dominant
        #    crash risk). This ties city size to reliability investment, which
        #    ties it to sustainable cash, keeping the city solvent as it grows.
        counts = self._counts()
        resilient = counts["battery"] >= self._resilience_batteries(pop, counts)
        can_grow = pop < CITY_POP_CAP and self._morning_headroom(pop) > 0 and resilient
        if can_grow:
            self._ensure_jobs(pop)

        if not defensive:
            # 3. Parks: happiness → growth velocity (and a score term).
            self._ensure_parks()
            # 4. Housing: keep capacity ahead of population (and dense, so
            #    commercial tiles read plenty of housing in their radius).
            if can_grow:
                self._ensure_housing(pop)

    # =====================================================================
    # Bootstrap
    # =====================================================================

    def _bootstrap(self, pop: int) -> None:
        # Lay the full road skeleton up front (roads are cheap at $500 and a
        # complete, connected comb is what lets the city densify without the
        # network fragmenting). Then stand up a small profitable core.
        self._extend_skeleton(limit=len(self._skeleton))
        for _ in range(4):  # a few commercial near the town hall (revenue + jobs)
            cell = self._best_commercial_cell(self._network())
            if cell is None or not self._affordable("commercial", growth=True):
                break
            self._place("commercial", *cell)
        for _ in range(2):  # minimal housing
            cell = self._house_cell(self._network())
            if cell is None or not self._affordable("house", growth=True):
                break
            self._place("house", *cell)
        for _ in range(2):  # at least one park to clear the no-parks penalty
            if not self._affordable("park", growth=True) or not self._ensure_parks(one=True):
                break
        for _ in range(2):  # a little solar for baseline renewable share
            if not self._affordable("solar_farm") or not self._build_plant("solar_farm"):
                break

    # =====================================================================
    # Subsystems
    # =====================================================================

    def _daytime_peak_kw(self, pop: int, counts: dict[str, int], events: set[str]) -> float:
        res_factor = 1.5 if "heatwave" in events else 1.2
        return pop * 0.333 * res_factor + counts["commercial"] * 50.0 + counts["industrial"] * 300.0

    def _morning_demand_kw(self, pop: int, counts: dict[str, int]) -> float:
        # 08:00 step: residential factor ~1.10 plus commercial flips to full.
        return pop * 0.333 * 1.10 + counts["commercial"] * 50.0 + counts["industrial"] * 300.0

    def _morning_supply_kw(self, counts: dict[str, int]) -> float:
        # Batteries only carry the morning ramp if there is enough solar to
        # have charged them the day before (charging needs midday solar >
        # demand). If solar is undersized, the battery SoC is ~0 at 08:00, so
        # we don't credit it. This keeps the growth gate honest.
        peak = self._daytime_peak_kw_current(counts)
        solar_midday = counts["solar_farm"] * SOLAR_PEAK_EFFECTIVE_KW
        battery_effective = counts["battery"] if solar_midday > peak else 0
        return (
            counts["coal_plant"] * COAL_MORNING_KW
            + counts["solar_farm"] * SOLAR_MORNING_KW
            + battery_effective * BATTERY_POWER_KW
        )

    def _daytime_peak_kw_current(self, counts: dict[str, int]) -> float:
        # Current daytime peak (no event multiplier) for the charging check.
        return (
            self._pop_now * 0.333 * 1.5 + counts["commercial"] * 50.0 + counts["industrial"] * 300.0
        )

    def _morning_headroom(self, pop: int) -> float:
        """kW of morning-ramp headroom above the next commercial increment."""
        counts = self._counts()
        demand = self._morning_demand_kw(pop, counts) + 50.0  # + one more commercial
        return self._morning_supply_kw(counts) - MORNING_RESERVE * demand

    def _target_solar(self, pop: int, counts: dict[str, int], events: set[str]) -> int:
        # Enough that midday solar exceeds daytime demand → surplus to charge
        # the batteries (and lift the renewable share).
        peak = self._daytime_peak_kw(pop, counts, events)
        need = int(SOLAR_OVERBUILD * peak / SOLAR_PEAK_EFFECTIVE_KW) + 1
        return min(MAX_SOLAR, max(SOLAR_BASE, need))

    def _ensure_grid(self, pop: int, stressed: bool, events: set[str]) -> None:
        counts = self._counts()

        # N-1 firm redundancy: a second coal once the city is large enough to
        # justify the must-run floor, or if a coal failure is in progress.
        plant_down = "plant_failure" in events
        want_second_coal = counts["coal_plant"] < 2 and (
            pop >= SECOND_COAL_POP or (stressed and plant_down)
        )
        if want_second_coal and self._affordable("coal_plant"):
            self._build_west_coal()
            return

        # Solar to the charging target (so batteries can fill), built first so
        # there is surplus energy to store.
        target_solar = self._target_solar(pop, counts, events)
        if (
            counts["solar_farm"] < target_solar
            and self._affordable("solar_farm")
            and self._build_plant("solar_farm")
        ):
            return
        # Batteries are the backbone of reliability: worker-free, immune to
        # plant_failure, they cover the morning ramp AND let the city ride a
        # multi-day coal failure (the single biggest crash risk) as a brownout
        # instead of a blackout. Size them to the evening peak so that, with
        # coal down, solar (day) + batteries (evening/night) still carry load.
        target_batt = self._target_battery(pop, counts)
        if counts["battery"] < target_batt and self._affordable("battery"):
            self._build_plant("battery")

    def _resilience_batteries(self, pop: int, counts: dict[str, int]) -> int:
        """Batteries needed to carry the evening peak through a coal failure
        (each delivers 200 kW). This is the reliability bar the city must clear
        before it is allowed to grow further."""
        evening_peak = pop * 0.366 + counts["commercial"] * 50.0 + counts["industrial"] * 300.0
        # With coal down, solar covers the day; batteries must cover the
        # evening shoulder. Size to the evening peak (coal-down), capped.
        return min(MAX_BATTERY, max(BATT_BASE, int(evening_peak / 200.0)))

    def _target_battery(self, pop: int, counts: dict[str, int]) -> int:
        # Build a margin above the resilience bar so growth is not blocked the
        # instant the city ticks up.
        return min(MAX_BATTERY, self._resilience_batteries(pop, counts) + 1)

    # =====================================================================
    # Oil (raw-crude producers; the treasury engine)
    # =====================================================================

    def _ensure_oil(self, state: dict[str, Any], day: int) -> None:
        if self._cash < OIL_START_CASH:
            return
        wells = state["wells"]
        producers = [w for w in wells if w["type"] == "production"]
        injectors = [w for w in wells if w["type"] == "injection"]

        # Make sure every producing well is actually selling at full rate.
        for w in producers:
            if w.get("setpoint_rate_bbl_day", 0) < WELL_RATE_BBL_DAY - 1:
                self.api.control_well(w["id"], WELL_RATE_BBL_DAY)

        if len(producers) < MAX_PRODUCERS:
            voxels = self.api.reservoirs(top_k=100).get("voxels", [])
            target = self._best_drill_voxel(voxels)
            # Drill a producer on the richest revealed voxel (needs a healthy
            # cash balance — the drill is a big lump).
            if target is not None and self._cash >= DRILL_MIN_CASH:
                x, y, z = target
                r = self.api.drill(x, y, z, "production")
                if r.get("ok"):
                    self._cash = float(r["treasury_after"])
                    self._occupied.add((x, y))
                    self.api.control_well(r["result"]["id"], WELL_RATE_BBL_DAY)
                    return
            # No drillable voxel yet → keep surveying (cheap) until we find one.
            if target is None and day - self._last_survey_day >= SURVEY_INTERVAL_DAYS:
                self._survey_next(voxels)
                return

        # Injection support: once we have producers and some have begun to
        # decline (current rate well below setpoint), add an injector in the
        # same reservoir to prop up pressure. Injection load is sheddable.
        declining = any(
            w.get("current_rate_bbl_day", 0) < 0.85 * w.get("setpoint_rate_bbl_day", 1)
            for w in producers
        )
        if (
            producers
            and declining
            and len(injectors) < len(producers)
            and self._cash - 50_000 >= DRILL_RESERVE
        ):
            self._drill_injection(producers, injectors)

    def _survey_next(self, voxels: list[dict[str, Any]]) -> None:
        """Adaptive survey: coarse sweep to find a reservoir, then densify
        around the richest hit to map its interior before drilling."""
        cx, cy = self._w // 2, self._h // 2
        coarse = [
            (cx, cy),
            (cx - 10, cy - 10),
            (cx + 10, cy - 10),
            (cx - 10, cy + 10),
            (cx + 10, cy + 10),
            (cx - 10, cy),
            (cx + 10, cy),
            (cx, cy - 10),
            (cx, cy + 10),
            (cx - 6, cy - 6),
            (cx + 6, cy - 6),
            (cx - 6, cy + 6),
            (cx + 6, cy + 6),
        ]
        anchors: list[tuple[int, int]] = []
        if voxels:
            # Densify around the richest revealed voxel to fill its drainage
            # neighbourhood, so the pool-quality drill score is well-informed.
            rich = max(voxels, key=lambda v: v["oil_estimate_bbl"] * v["perm_estimate_md"])
            rx, ry = int(rich["x"]), int(rich["y"])
            anchors += [(rx + dx, ry + dy) for dx in (-4, 0, 4) for dy in (-4, 0, 4)]
        anchors += coarse
        for ax, ay in anchors:
            sx = max(2, min(self._w - 3, ax))
            sy = max(2, min(self._h - 3, ay))
            if (sx, sy) in self._surveyed:
                continue
            r = self.api.survey(sx, sy, SURVEY_SIZE)
            if r.get("ok"):
                self._cash = float(r["treasury_after"])
                self._surveyed.add((sx, sy))
                self._survey_idx += 1
                self._last_survey_day = self._current_day
            return

    def _best_drill_voxel(self, voxels: list[dict[str, Any]]) -> tuple[int, int, int] | None:
        """Pick the voxel whose 3×3×3 drainage pool (from revealed neighbours)
        is richest — high summed oil AND high mean perm — since the well rate
        scales with the *pool's* mean permeability, not the single voxel's."""
        best: tuple[int, int, int] | None = None
        best_score = 0.0
        for v in voxels:
            x, y, z = int(v["x"]), int(v["y"]), int(v["z"])
            if (x, y) in self._occupied or not (0 <= x < self._w and 0 <= y < self._h):
                continue
            pool = [
                u
                for u in voxels
                if abs(u["x"] - x) <= 1 and abs(u["y"] - y) <= 1 and abs(u["z"] - z) <= 1
            ]
            pool_oil = sum(float(u["oil_estimate_bbl"]) for u in pool)
            pool_perm = sum(float(u["perm_estimate_md"]) for u in pool) / max(1, len(pool))
            if pool_oil < POOL_OIL_MIN or pool_perm < PERM_MIN_MD:
                continue
            score = pool_oil * pool_perm
            if score > best_score:
                best_score, best = score, (x, y, z)
        return best

    def _drill_injection(
        self, producers: list[dict[str, Any]], injectors: list[dict[str, Any]]
    ) -> None:
        voxels = self.api.reservoirs(top_k=100).get("voxels", [])
        for prod in producers:
            rid = prod.get("reservoir_id")
            if rid is None:
                continue
            for v in voxels:
                if v.get("reservoir_id") != rid:
                    continue
                x, y, z = int(v["x"]), int(v["y"]), int(v["z"])
                if (x, y) in self._occupied or not (0 <= x < self._w and 0 <= y < self._h):
                    continue
                # Chebyshev > 1 from the producer's completion (adjacent
                # injectors are rejected as breakthrough).
                if max(abs(x - prod["x"]), abs(y - prod["y"]), abs(z - prod["target_z"])) <= 1:
                    continue
                r = self.api.drill(x, y, z, "injection")
                if r.get("ok"):
                    self._cash = float(r["treasury_after"])
                    self._occupied.add((x, y))
                    self.api.control_well(r["result"]["id"], INJECTION_RATE_BBL_DAY)
                    return

    def _ensure_jobs(self, pop: int) -> None:
        if self._total(JOBS) >= pop + JOBS_HEADROOM or not self._affordable(
            "commercial", growth=True
        ):
            return
        cell = self._best_commercial_cell(self._network())
        if cell is None:
            self._extend_skeleton(limit=3)
            return
        self._place("commercial", *cell)

    def _ensure_housing(self, pop: int) -> None:
        if self._total(HOUSING) >= pop + CAP_HEADROOM or not self._affordable("house", growth=True):
            return
        cell = self._house_cell(self._network())
        if cell is None:
            self._extend_skeleton(limit=3)
            return
        self._place("house", *cell)

    def _ensure_parks(self, one: bool = False) -> bool:
        """Keep PARK_COVER_TARGET parks within cheb-2 of every residence.

        Builds the single park covering the most under-covered residences.
        Returns True if it built a park (used by the bootstrap loop)."""
        residences = [t for t in self._tiles if t["type"] in ("house", "town_hall")]
        parks = [t for t in self._tiles if t["type"] == "park"]
        if len(parks) >= MAX_PARKS:
            return False

        def coverage(rx: int, ry: int) -> int:
            return sum(1 for p in parks if max(abs(rx - p["x"]), abs(ry - p["y"])) <= 2)

        under = [r for r in residences if coverage(r["x"], r["y"]) < PARK_COVER_TARGET]
        if not under or not self._affordable("park", growth=True):
            return False
        best, best_score = None, 0
        for x, y in self._field_candidates():
            score = sum(1 for r in under if max(abs(r["x"] - x), abs(r["y"] - y)) <= 2)
            if score > best_score:
                best_score, best = score, (x, y)
        if best is None:
            return False
        return self._place("park", *best)

    # =====================================================================
    # Placement primitives
    # =====================================================================

    def _place(self, tile_type: str, x: int, y: int) -> bool:
        r = self.api.build(tile_type, x, y)
        if r.get("ok"):
            self._cash = float(r["treasury_after"])
            self._tiles.append(
                {
                    "type": tile_type,
                    "x": x,
                    "y": y,
                    "housing_capacity": HOUSING.get(tile_type, 0),
                    "jobs": JOBS.get(tile_type, 0),
                    "operational": True,
                }
            )
            self._occupied.add((x, y))
            return True
        return False

    def _build_plant(self, tile_type: str) -> bool:
        for x, y in self._open_field_cells():
            if (x, y) in self._occupied:
                continue
            if self._place(tile_type, x, y):
                return True
        return False

    def _daily_opex(self) -> float:
        return sum(OPEX.get(t["type"], 0) for t in self._tiles)

    def _grid_floor(self) -> float:
        base = GRID_FLOOR_BASE + GRID_FLOOR_OPEX_DAYS * self._daily_opex()
        return max(OIL_RESERVE, base) if self._reserving_for_oil() else base

    def _growth_floor(self) -> float:
        base = GROWTH_FLOOR_BASE + GROWTH_FLOOR_OPEX_DAYS * self._daily_opex()
        if self._saving_for_coal2():
            base = max(base, SECOND_COAL_RESERVE)
        if self._reserving_for_oil():
            base = max(base, OIL_RESERVE)
        return base

    def _reserving_for_oil(self) -> bool:
        # Reliability and a growing city come first; oil is an opportunistic
        # treasury booster funded from genuine surplus, never the seed cash.
        return False

    def _saving_for_coal2(self) -> bool:
        # Once the city is large enough that a coal failure would be
        # catastrophic, hold cash to fund the redundant second coal plant
        # before spending on further growth.
        return self._pop_now >= SECOND_COAL_POP and self._coal_count < 2

    def _affordable(self, tile_type: str, *, growth: bool = False) -> bool:
        floor = self._growth_floor() if growth else self._grid_floor()
        return self._cash - CAPEX[tile_type] >= floor

    def _op_net(self, state: dict[str, Any]) -> float:
        t = state.get("today") or {}
        rev = (
            float(t.get("tax_revenue", 0))
            + float(t.get("power_revenue", 0))
            + float(t.get("oil_revenue", 0))
            + float(t.get("commercial_revenue", 0))
            + float(t.get("industrial_revenue", 0))
        )
        cost = (
            float(t.get("opex", 0))
            + float(t.get("fuel_cost", 0))
            + float(t.get("carbon_cost", 0))
            + float(t.get("outage_penalty", 0))
        )
        return rev - cost

    def _renewable_share(self, state: dict[str, Any]) -> float:
        total = float(state.get("cumulative_total_served_kwh", 0.0))
        if total <= 0.0:
            return 1.0
        return float(state.get("cumulative_renewable_served_kwh", 0.0)) / total

    # =====================================================================
    # Geometry
    # =====================================================================

    def _counts(self) -> dict[str, int]:
        c = {
            k: 0
            for k in (
                "house",
                "commercial",
                "industrial",
                "park",
                "solar_farm",
                "wind_turbine",
                "battery",
                "coal_plant",
                "gas_peaker",
                "refinery",
            )
        }
        for t in self._tiles:
            if t["type"] in c:
                c[t["type"]] += 1
        return c

    def _total(self, table: dict[str, Any]) -> int:
        return sum(int(table.get(t["type"], 0)) for t in self._tiles)

    def _network(self) -> set[tuple[int, int]]:
        by_pos = {(t["x"], t["y"]): t for t in self._tiles}
        start = next((p for p, t in by_pos.items() if t["type"] == "town_hall"), None)
        if start is None:
            return set()
        seen = {start}
        q: deque[tuple[int, int]] = deque([start])
        while q:
            x, y = q.popleft()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                p = (x + dx, y + dy)
                if p in seen:
                    continue
                t = by_pos.get(p)
                if t is None or t["type"] not in ("road", "town_hall"):
                    continue
                seen.add(p)
                q.append(p)
        return seen

    def _road_adjacent(self, x: int, y: int, network: set[tuple[int, int]]) -> bool:
        return any((x + dx, y + dy) in network for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))

    def _district_cells(self) -> list[tuple[int, int]]:
        # Chebyshev-distance ordering fills a dense *square* core around the
        # town hall (Manhattan ordering sprawls into a thin strip along the
        # road spine, which starves commercial tiles of nearby housing).
        cx, cy = self._w // 2, self._h // 2
        x0 = cx + DISTRICT_X0_OFFSET
        cells = [
            (x, y)
            for x in range(x0, min(DISTRICT_X1 + 1, self._w))
            for y in range(DISTRICT_Y0, min(DISTRICT_Y1 + 1, self._h))
        ]
        cells.sort(key=lambda p: (max(abs(p[0] - cx), abs(p[1] - cy)), p[0], p[1]))
        return cells

    def _civ_candidates(self, network: set[tuple[int, int]]) -> list[tuple[int, int]]:
        skel = self._skeleton_set()
        return [
            (x, y)
            for (x, y) in self._district_cells()
            if (x, y) not in self._occupied
            and (x, y) not in skel  # never build on a planned road
            and self._road_adjacent(x, y, network)
        ]

    def _field_candidates(self) -> list[tuple[int, int]]:
        """Empty district cells for parks (no road needed). Excludes planned
        road cells so parks never fragment the road network."""
        skel = self._skeleton_set()
        return [
            (x, y)
            for (x, y) in self._district_cells()
            if (x, y) not in self._occupied and (x, y) not in skel
        ]

    def _housing_in_cheb2(self, x: int, y: int) -> int:
        return sum(
            int(t.get("housing_capacity", 0))
            for t in self._tiles
            if int(t.get("housing_capacity", 0)) > 0 and max(abs(t["x"] - x), abs(t["y"] - y)) <= 2
        )

    def _best_commercial_cell(self, network: set[tuple[int, int]]) -> tuple[int, int] | None:
        best, best_score = None, -1
        for x, y in self._civ_candidates(network):
            score = self._housing_in_cheb2(x, y)
            if score > best_score:
                best_score, best = score, (x, y)
        return best

    def _house_cell(self, network: set[tuple[int, int]]) -> tuple[int, int] | None:
        cands = self._civ_candidates(network)
        return cands[0] if cands else None

    def _open_field_cells(self) -> list[tuple[int, int]]:
        cx, cy = self._w // 2, self._h // 2
        cells = []
        for x in range(self._w):
            for y in range(self._h):
                if (x, y) in self._occupied:
                    continue
                in_district = (
                    cx + DISTRICT_X0_OFFSET <= x <= DISTRICT_X1 and DISTRICT_Y0 <= y <= DISTRICT_Y1
                )
                if in_district:
                    continue
                cells.append((x, y))
        cells.sort(key=lambda p: (-(abs(p[0] - cx) + abs(p[1] - cy)), p[0], p[1]))
        return cells

    # -- Road skeleton ----------------------------------------------------

    def _build_skeleton_plan(self, cx: int, cy: int) -> list[tuple[int, int]]:
        """A fully-connected road comb: a horizontal spine along the town-hall
        row spanning the district, plus vertical teeth every 3 columns. Every
        tooth crosses the spine at (c, cy), so the whole skeleton is one
        connected component anchored at the town hall, and every civilian cell
        is orthogonally adjacent to a tooth or the spine."""
        x0 = cx + DISTRICT_X0_OFFSET
        plan: list[tuple[int, int]] = []
        # Spine (town-hall row), built first and from the centre outward so
        # each cell is laid adjacent to an already-connected road.
        for x in sorted(range(x0, DISTRICT_X1 + 1), key=lambda v: abs(v - cx)):
            if x != cx:
                plan.append((x, cy))
        # Vertical teeth every 3 columns, each crossing the spine.
        for c in range(x0, DISTRICT_X1 + 1, 3):
            for y in sorted(range(DISTRICT_Y0, DISTRICT_Y1 + 1), key=lambda v: abs(v - cy)):
                if y != cy:
                    plan.append((c, y))
        return plan

    def _skeleton_set(self) -> set[tuple[int, int]]:
        return set(self._skeleton)

    def _extend_skeleton(self, limit: int) -> None:
        built = 0
        for x, y in self._skeleton:
            if built >= limit:
                return
            if (x, y) in self._occupied or not (0 <= x < self._w and 0 <= y < self._h):
                continue
            if not self._affordable("road"):
                return
            if self._place("road", x, y):
                built += 1

    def _build_west_coal(self) -> None:
        """Place the redundant second coal plant on the far west, fed by a
        short road stub off the starter road chain and clear of the
        residential district's happiness halo."""
        cx, cy = self._w // 2, self._h // 2
        for sx, sy in ((cx - 7, cy - 1), (cx - 7, cy - 2), (cx - 7, cy - 3)):
            if (sx, sy) not in self._occupied and self._affordable("road"):
                self._place("road", sx, sy)
        target = (cx - 8, cy - 2)
        if target not in self._occupied and self._affordable("coal_plant"):
            self._place("coal_plant", *target)


# evaluate.py loader / Agent Play attach both look for a top-level `Agent`.
Agent = GridMasterAgent


# --- CLI driver -------------------------------------------------------------


def _make_inprocess_client() -> ApiClient:
    from fastapi.testclient import TestClient

    from world.api import create_app

    return ApiClient(transport=TestClient(create_app()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Grid Master agent.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--api-url", type=str, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    api = ApiClient(base_url=args.api_url) if args.api_url else _make_inprocess_client()
    final = GridMasterAgent(api, seed=args.seed).play_game()
    payload = {
        "seed": args.seed,
        "p_ref": float(final["population"]),
        "t_ref": float(final["treasury"]) - float(final["config"]["starting_cash"]),
    }
    print(json.dumps(payload, indent=2))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

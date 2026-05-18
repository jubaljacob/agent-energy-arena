"""Scripted reference agent: 5-phase competent baseline.

Implements the strategy in PRD §"Reference agents" / brief §7.2:

  Bootstrap   weeks  1- 4 — halt pop bleed, baseline grid + first survey
  Buildout    weeks  5-26 — scale residential/commercial, drill on hits
  Diversify   weeks 27-104 — refinery online, first injection well
  Mature      weeks 105-260 — replace coal as carbon price rises
  Late        weeks 261-521 — maintain, demolish coal once $80/ton

Plus an always-on Crisis Response that drops step size to `days=1` while
events are active and emergency-builds a gas peaker on heatwave/blackout.

All decisions follow strict deterministic priority ordering (see
`_decide`): starvation triage → blackout response → reserve-margin →
capacity → carbon-driven coal demolition → reservoir re-exploration →
drilling → refinery → DR-injection siting → skip.

Run end-to-end via `python -m agents.scripted.agent --seed 42 --output
baselines/seed_42.json` to write the baseline file consumed by
`/score`.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from agents.api_client import ApiClient
from agents.base import BaseAgent

# --- Strategy thresholds ----------------------------------------------------

BATTERY_CAPEX: float = 60_000.0  # catalog["battery"].capex
MAX_BATTERIES: int = 4  # PRD §"Scripted agent battery rule"
RESERVE_MARGIN_TARGET: float = 0.20  # build a plant when supply/demand < 1.20
HOUSING_HEADROOM: int = 30  # leave this much capacity above pop
JOBS_HEADROOM: int = 20  # commercial/industrial when jobs < pop + JOBS_HEADROOM
SURVEY_INTERVAL_DAYS: int = 56  # 8 weeks
MIN_TREASURY_BUILD: float = 50_000.0
COAL_DEMOLISH_CARBON_USD: float = 80.0
DRILL_OIL_THRESHOLD_BBL: float = 5_000.0
DRILL_PERM_THRESHOLD_MD: float = 200.0
WELL_RATE_BBL_DAY: float = 160.0
# oilfield-v2 §"Rate-based pressure": pressure_boost = qualifying_inj_rate /
# producer_yesterday_rate, capped at 0.5. Setting injector setpoint == producer
# setpoint gets us up to the 0.5 cap once flows steady out.
INJECTION_RATE_BBL_DAY: float = 160.0
REFINERY_RATE_BBL_DAY: float = 400.0
# oilfield-v2 slice 06: cheapest legal survey column is size 4 ($15k).
SURVEY_SIZE: int = 4


class ScriptedAgent(BaseAgent):
    """Rule-based 10-year agent. ~200 lines including comments."""

    def __init__(self, api: ApiClient, *, seed: int | None = None) -> None:
        super().__init__(api, seed=seed)
        self._last_survey_day: int = -SURVEY_INTERVAL_DAYS
        self._survey_seq: int = 0  # which column to survey next
        self._heatwave_peaker_id: str | None = None  # demolish when event ends

    # -- Cadence ----------------------------------------------------------

    def next_step_days(self, state: dict[str, Any]) -> int:
        # Crisis response: anything happening this turn warrants daily ticks.
        return 1 if state.get("active_events") else 7

    # -- Per-turn decisions ----------------------------------------------

    def act(self, state: dict[str, Any]) -> None:
        treasury = float(state["treasury"])
        day = int(state["day"])
        pop = int(state["population"])
        cfg = state["config"]
        w, h = int(cfg["world_w"]), int(cfg["world_h"])
        cx, cy = w // 2, h // 2

        tiles = state["tiles"]
        wells = state["wells"]
        events = state.get("active_events") or []
        carbon_price = float(cfg.get("carbon_price", 25.0))

        # Phase boundaries from PRD §"Reference agents" (week-anchored).
        if day < 28:
            phase = "bootstrap"
        elif day < 26 * 7:
            phase = "buildout"
        elif day < 104 * 7:
            phase = "diversify"
        elif day < 260 * 7:
            phase = "mature"
        else:
            phase = "late"

        capacity = sum(int(t.get("housing_capacity", 0)) for t in tiles)
        jobs = sum(int(t.get("jobs", 0)) for t in tiles)
        n_commercial = sum(1 for t in tiles if t["type"] == "commercial")
        n_industrial = sum(1 for t in tiles if t["type"] == "industrial")
        n_solar = sum(1 for t in tiles if t["type"] == "solar_farm")
        n_wind = sum(1 for t in tiles if t["type"] == "wind_turbine")
        n_battery = sum(1 for t in tiles if t["type"] == "battery")
        n_gas = sum(1 for t in tiles if t["type"] == "gas_peaker")
        n_coal = sum(1 for t in tiles if t["type"] == "coal_plant")
        n_refinery = sum(1 for t in tiles if t["type"] == "refinery")
        n_prod_wells = sum(1 for w_ in wells if w_["type"] == "production")
        n_inj_wells = sum(1 for w_ in wells if w_["type"] == "injection")
        active_event_types = {e["type"] for e in events}

        # Dispatchable capacity (the only thing that helps at night) — gas
        # + coal nameplate. Renewables don't count toward reserve at peak.
        dispatchable_kw = n_gas * 500.0 + n_coal * 1500.0
        # Expected peak demand (evening hour, factor 1.5 + commercial full +
        # industrial continuous). Used as a build-out gating predicate so
        # the agent never lets demand outrun reliable supply.
        expected_peak_kw = pop * 0.333 * 1.5 + n_commercial * 50.0 + n_industrial * 300.0

        occupied = {(t["x"], t["y"]) for t in tiles}
        occupied |= {(w_["x"], w_["y"]) for w_ in wells}

        # ----- Bootstrap: minimum viable city -----------------------------
        # Roads in a + cross around town hall, then 4 houses + 2 commercial,
        # 4 solar + 1 coal_plant. Coal carries the night-peak baseload
        # because issue 09 makes gas peakers useless before a refinery
        # exists. Ensures pop has jobs/housing/electricity before week-4
        # phase rollover.
        if phase == "bootstrap":
            self._lay_initial_roads(treasury, cx, cy, w, h, occupied)
            return

        # ----- Crisis response (highest priority) -------------------------
        # Heatwave → emergency gas peaker (track id to demolish at expiry).
        if "heatwave" in active_event_types and self._heatwave_peaker_id is None:
            new_id = self._build_gas_peaker_with_supply(treasury, cx, cy, w, h, occupied, tiles)
            if new_id is not None:
                self._heatwave_peaker_id = new_id
                return
        elif self._heatwave_peaker_id is not None and "heatwave" not in active_event_types:
            t = next((t for t in tiles if t["id"] == self._heatwave_peaker_id), None)
            if t is not None:
                self.api.demolish(t["x"], t["y"])
            self._heatwave_peaker_id = None

        # Blackout → emergency gas peaker.
        balance = state.get("power_now", {}).get("balance_state", "balanced")
        if balance == "blackout":
            self._build_gas_peaker_with_supply(treasury, cx, cy, w, h, occupied, tiles)
            return

        # ----- Reserve-margin: dispatchable capacity must cover peak ------
        # Renewables don't count — they're zero at hour 22 (the evening peak).
        # If dispatchable < 1.3× expected peak, build a fossil plant; coal in
        # mature phases for cheap baseload, gas otherwise for ramp coverage.
        hourly_states = state.get("last_day_balance_state_by_hour") or []
        had_stress = any(s_ in ("brownout", "blackout") for s_ in hourly_states)
        need_dispatch = dispatchable_kw < 1.3 * expected_peak_kw or had_stress
        if need_dispatch and treasury >= MIN_TREASURY_BUILD:
            # Coal-first if heavy load; gas otherwise (cheaper, ramps fast).
            want_coal = expected_peak_kw > 1000.0 and treasury >= 200_000
            if (
                want_coal
                and phase != "late"
                # Coal requires road adjacency (economy-rebalance #05); route
                # through the civilian / road-aware builder instead of the
                # perimeter spiral used by renewables.
                and self._build_civilian("coal_plant", treasury, cx, cy, w, h, occupied, tiles)
            ):
                return
            if treasury >= 80_000 and self._build_gas_peaker_with_supply(
                treasury, cx, cy, w, h, occupied, tiles
            ):
                return
        # Renewable topup: occasional wind for the r_term, gated on a healthy
        # cash reserve so OPEX doesn't runaway. Only fires once dispatch is
        # already comfortable.
        if (
            phase in ("mature", "late")
            and dispatchable_kw >= 1.3 * expected_peak_kw
            and (n_solar + 1) < (n_commercial + n_industrial * 2)
            and treasury >= 500_000
            and self._build_plant("wind_turbine", treasury, cx, cy, w, h, occupied)
        ):
            return

        # ----- Battery buildout (balance-upgrade-p0 slice 02) -------------
        # Once any renewable plant exists, scale storage with the renewable
        # fleet. Sizing rule (PRD §"Scripted agent battery rule"):
        # `target = min(MAX_BATTERIES, (n_solar + n_wind) // 2)`. Built once
        # treasury can afford the full capex; one per turn keeps the cash
        # discipline consistent with the rest of the agent's single-action
        # branches. Power-margin is already comfortable by the time we land
        # here — the reserve-margin branch above returns early when dispatch
        # is short, so a battery build never starves the grid.
        n_renewable = n_solar + n_wind
        target_batteries = min(MAX_BATTERIES, n_renewable // 2)
        if (
            phase != "bootstrap"
            and n_renewable >= 1
            and n_battery < target_batteries
            and treasury >= BATTERY_CAPEX
            and self._build_plant("battery", treasury, cx, cy, w, h, occupied)
        ):
            return

        # ----- Carbon-driven coal demolition (mature/late only) ------------
        if phase in ("mature", "late") and carbon_price >= COAL_DEMOLISH_CARBON_USD and n_coal > 0:
            for t in tiles:
                if t["type"] == "coal_plant":
                    self.api.demolish(t["x"], t["y"])
                    return

        # ----- Reservoir re-exploration: forced periodic survey -----------
        # Per PRD priority list, surveys come after carbon demolition and
        # before drilling; gating it after capacity branches would never let
        # it fire (capacity always has work). So elevate it: any 56 days
        # without a survey + treasury cushion = run a survey before housing.
        if (
            phase != "bootstrap"
            and day - self._last_survey_day >= SURVEY_INTERVAL_DAYS
            and treasury >= 20_000
        ):
            sx, sy = self._next_survey_anchor(cx, cy, w, h)
            r = self.api.survey(sx, sy, SURVEY_SIZE)
            if r.get("ok"):
                self._last_survey_day = day
                self._survey_seq += 1
                return

        # ----- Drilling on the best revealed voxel -------------------------
        candidates = state.get("reservoirs_revealed", {}).get("top_k", [])
        if treasury >= 50_000 and candidates and n_prod_wells < 6:
            chosen = self._pick_drill_target(candidates, occupied, w, h)
            if chosen is not None:
                cx_, cy_, cz_ = chosen
                r = self.api.drill(cx_, cy_, cz_, "production")
                if r.get("ok"):
                    well_id = r["result"]["id"]
                    self.api.control_well(well_id, WELL_RATE_BBL_DAY)
                    occupied.add((cx_, cy_))
                    # Lay pipeline producer→nearest existing refinery so the
                    # well isn't an orphan (raw $40/bbl). If no refinery yet,
                    # the refinery-build branch below lays pipelines to all
                    # producers when it fires.
                    self._lay_pipeline_to_refinery(cx_, cy_, tiles, occupied, w, h)
                    return

        # ----- Stacked completion (reservoir-scale-and-stacked-completions
        # #07): drill a second producer at the same (x, y) of an existing
        # producer at a target_z ≥ 3 voxels away. The relaxed §4.12 rule
        # (slice 03) makes this legal as long as the 3×3×3 drainage cubes
        # don't overlap. Fires at most once per existing producer; we detect
        # "already stacked" by checking whether any other well shares the
        # producer's (x, y), so three-deep stacks stay out of scope. The
        # n_prod_wells fresh-drill cap is NOT applied here — the per-producer
        # "at most one stack" rule is the bound that matters for this branch.
        #
        # The state-level `reservoirs_revealed.top_k` ships only the top 10
        # voxels by oil×perm; the deeper z's that make a stack legal are
        # typically lower-ranked and fall outside that window. Query the
        # richer `/reservoirs?top_k=100` endpoint so the helper sees the
        # full per-column z-profile under each producer.
        if treasury >= 50_000:
            stacked_candidates = self.api.reservoirs(top_k=100).get("voxels", [])
            stacked = self._pick_stacked_drill_target(wells, stacked_candidates, w, h)
            if stacked is not None:
                sx, sy, sz = stacked
                r = self.api.drill(sx, sy, sz, "production")
                if r.get("ok"):
                    well_id = r["result"]["id"]
                    self.api.control_well(well_id, WELL_RATE_BBL_DAY)
                    # (sx, sy) is already in `occupied` from the existing
                    # producer at the same surface tile; no need to re-add.
                    self._lay_pipeline_to_refinery(sx, sy, tiles, occupied, w, h)
                    return

        # ----- Capacity: housing first, then commercial/industrial ---------
        # Stop building housing in late phase once pop within 90% of capacity.
        # Civilian builds are gated on power: never grow demand past dispatch.
        power_ok = dispatchable_kw >= 1.2 * expected_peak_kw
        nearing_cap = phase == "late" and pop > 0.9 * capacity
        want_civilian = power_ok and treasury >= 3_000

        # Want a buffer between pop and {capacity, jobs} so int(pop) growth
        # always has room. Without it, pop saturates at jobs == pop and stops.
        cap_short = capacity <= pop + HOUSING_HEADROOM
        jobs_short = jobs <= pop + JOBS_HEADROOM

        if (
            want_civilian
            and cap_short
            and not nearing_cap
            and self._build_civilian("house", treasury, cx, cy, w, h, occupied, tiles)
        ):
            return

        if want_civilian and jobs_short and treasury >= 8_000:
            # Prefer commercial early (cheap, +12 jobs), industrial whenever
            # power is comfortable (+30 jobs, 300 kW continuous, +revenue).
            want_industrial = (
                phase != "bootstrap"
                and treasury >= 20_000
                and dispatchable_kw >= 1.2 * (expected_peak_kw + 300.0)
            )
            kind = "industrial" if want_industrial else "commercial"
            if self._build_civilian(kind, treasury, cx, cy, w, h, occupied, tiles):
                return

        # Fallback: extend the road network so future civilian builds find a
        # slot. Fires when both housing + jobs branches found no road-adj
        # space. One road per turn.
        if (
            want_civilian
            and (cap_short or jobs_short)
            and treasury >= 1_000
            and self._extend_roads(treasury, cx, cy, w, h, occupied, tiles)
        ):
            return

        # Refresh production-well setpoints (idempotent-ish; clamp on world).
        candidates = state.get("reservoirs_revealed", {}).get("top_k", [])
        for w_ in wells:
            if w_["type"] == "production" and w_.get("setpoint_rate_bbl_day", 0) < 1.0:
                self.api.control_well(w_["id"], WELL_RATE_BBL_DAY)

        # ----- Refinery: once 2+ producing wells ---------------------------
        want_refinery = (
            phase in ("diversify", "mature", "late")
            and n_prod_wells >= 2
            and n_refinery == 0
            and treasury >= 150_000
        )
        if want_refinery and self._build_civilian(
            "refinery", treasury, cx, cy, w, h, occupied, tiles
        ):
            # Set throughput. Refinery id is the last-built tile id of type refinery.
            latest = self.api.state()
            refinery_xy: tuple[int, int] | None = None
            for t in latest["tiles"]:
                if t["type"] == "refinery":
                    self.api.control_refinery(t["id"], REFINERY_RATE_BBL_DAY)
                    refinery_xy = (int(t["x"]), int(t["y"]))
                    break
            # Refinery just landed: lay pipeline from each existing producer
            # to it so prior orphans (issue 10 AC: "queues the path as part
            # of the refinery-build flow") get connected. Also connect every
            # standing gas peaker — they don't dispatch without a pipeline
            # path to an operational refinery (issue 09).
            if refinery_xy is not None:
                latest_occupied = {(t["x"], t["y"]) for t in latest["tiles"]}
                latest_occupied |= {(w_["x"], w_["y"]) for w_ in latest["wells"]}
                for w_ in latest["wells"]:
                    if w_["type"] != "production":
                        continue
                    self._lay_pipeline_to_refinery(
                        int(w_["x"]),
                        int(w_["y"]),
                        latest["tiles"],
                        latest_occupied,
                        w,
                        h,
                        refinery_xy=refinery_xy,
                    )
                for t in latest["tiles"]:
                    if t["type"] != "gas_peaker":
                        continue
                    self._lay_pipeline_to_refinery(
                        int(t["x"]),
                        int(t["y"]),
                        latest["tiles"],
                        latest_occupied,
                        w,
                        h,
                        refinery_xy=refinery_xy,
                    )
            return

        # ----- DR-injection well in same reservoir as a producer ----------
        # oilfield-v2 §"Rate-based pressure": injector qualifies for the
        # producer's pressure_boost iff same `reservoir_id` AND Chebyshev
        # distance > 1. We site at distance ≥ 2 (just past the breakthrough
        # gate) so the boost lands on day 1.
        if (
            phase in ("diversify", "mature", "late")
            and n_inj_wells == 0
            and n_prod_wells >= 1
            and treasury >= 30_000
        ):
            self._drill_injection_same_reservoir(wells, candidates, occupied, w, h)

    # -- Helpers ----------------------------------------------------------

    def _lay_initial_roads(
        self,
        treasury: float,
        cx: int,
        cy: int,
        w: int,
        h: int,
        occupied: set[tuple[int, int]],
    ) -> None:
        """Bootstrap (PRD weeks 1-4): minimum viable city.

        Brief baseline: 1 road / 2 commercial / 2 houses / 4 solar / 1 coal
        plant. We ship a + cross of roads (cheap, anchors more growth) +
        the prescribed civilian/plant counts, all in one turn. Bootstrap
        is the only multi-action turn — every later phase is single-action
        for cash discipline."""
        # Plants off the road cross axes so they don't collide with the
        # bootstrap road extension below. Gas peakers are intentionally
        # absent: issue 09 makes a peaker only dispatch when sharing a
        # pipeline network with an operational refinery, and there is no
        # refinery this early. Coal carries the night-peak baseload until
        # the diversify phase builds out the oil loop.
        plan: list[tuple[str, int, int]] = [
            ("solar_farm", cx + 8, cy - 1),
            ("solar_farm", cx + 8, cy),
            ("solar_farm", cx + 8, cy + 1),
            ("solar_farm", cx - 8, cy + 2),
        ]
        # Long road cross (radius 6 along each axis = 24 roads, $12k). Anchors
        # ~36 unique road-adjacent civilian slots, well above the 16-or-so a
        # short cross would yield. Buildout phase fills these in incrementally
        # then `_extend_roads` keeps growing the network outward.
        for d in range(1, 7):
            for dx, dy in ((d, 0), (-d, 0), (0, d), (0, -d)):
                plan.append(("road", cx + dx, cy + dy))
        # Coal must run AFTER the road cross — needs road adjacency
        # (economy-rebalance #05). (cx+5, cy) is a road from the loop above;
        # (cx+5, cy+1) is the coal slot.
        plan.append(("coal_plant", cx + 5, cy + 1))
        # 6 commercial up-front: 2 from PRD baseline + 4 to defeat the
        # int(pop) growth-truncation trap. With pop=100 and only 2
        # commercial, jobs<0.7·pop triggers immediate decline.
        # Each house is paired with a park on a road-adjacent square within
        # Chebyshev radius 2 (happiness-population-driver #02) — the same
        # window `world.population.update_population` uses for the spatial
        # park benefit. The pair drives happiness above the neutral 1.0
        # anchor so the velocity model unlocks real growth.
        plan += [
            ("commercial", cx + 1, cy + 1),
            ("commercial", cx - 1, cy - 1),
            ("commercial", cx + 1, cy - 1),
            ("commercial", cx - 1, cy + 1),
            ("commercial", cx + 2, cy + 1),
            ("commercial", cx - 2, cy - 1),
            ("house", cx + 2, cy - 1),
            ("park", cx + 3, cy - 1),
            ("house", cx - 2, cy + 1),
            ("park", cx - 3, cy + 1),
        ]

        for tile_type, x, y in plan:
            if (x, y) in occupied or not _in_bounds(x, y, w, h):
                continue
            r = self.api.build(tile_type, x, y)
            if r.get("ok"):
                occupied.add((x, y))
            elif r.get("error") == "insufficient_funds":
                return

    def _build_plant(
        self,
        tile_type: str,
        treasury: float,
        cx: int,
        cy: int,
        w: int,
        h: int,
        occupied: set[tuple[int, int]],
    ) -> str | None:
        """Place a power plant on the perimeter (no road needed). Returns tile id on success."""
        for x, y in _perimeter_spiral(cx, cy, w, h):
            if (x, y) in occupied:
                continue
            r = self.api.build(tile_type, x, y)
            if r.get("ok"):
                return str(r["result"]["id"])
            if r.get("error") == "insufficient_funds":
                return None
        return None

    def _build_gas_peaker_with_supply(
        self,
        treasury: float,
        cx: int,
        cy: int,
        w: int,
        h: int,
        occupied: set[tuple[int, int]],
        tiles: list[dict[str, Any]],
    ) -> str | None:
        """Build a gas peaker on the perimeter, then lay a pipeline to the
        nearest refinery so the peaker actually dispatches (issue 09 — gas
        peakers require a 4-connected pipeline path to an operational
        refinery). No-op pipeline pass if no refinery exists yet; the
        refinery-build branch retroactively connects existing peakers."""
        for x, y in _perimeter_spiral(cx, cy, w, h):
            if (x, y) in occupied:
                continue
            r = self.api.build("gas_peaker", x, y)
            if r.get("ok"):
                tile_id = str(r["result"]["id"])
                occupied.add((x, y))
                self._lay_pipeline_to_refinery(x, y, tiles, occupied, w, h)
                return tile_id
            if r.get("error") == "insufficient_funds":
                return None
        return None

    def _extend_roads(
        self,
        treasury: float,
        cx: int,
        cy: int,
        w: int,
        h: int,
        occupied: set[tuple[int, int]],
        tiles: list[dict[str, Any]],
    ) -> bool:
        """Place a road that extends the existing network. Anchors a future
        civilian build slot. One road per turn — keeps progress visible."""
        road_set = {(t["x"], t["y"]) for t in tiles if t["type"] in ("road", "town_hall")}
        for x, y in _spiral(cx, cy, w, h):
            if (x, y) in occupied or (x, y) in road_set:
                continue
            # Adjacent to an existing road → extends the network.
            if any((x + dx, y + dy) in road_set for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                r = self.api.build("road", x, y)
                if r.get("ok"):
                    return True
                if r.get("error") == "insufficient_funds":
                    return False
        return False

    def _build_civilian(
        self,
        tile_type: str,
        treasury: float,
        cx: int,
        cy: int,
        w: int,
        h: int,
        occupied: set[tuple[int, int]],
        tiles: list[dict[str, Any]],
    ) -> bool:
        """Place a road-adjacent tile near the existing road cluster."""
        # Candidate positions: empty squares orthogonally adjacent to a road or town hall.
        road_set = {(t["x"], t["y"]) for t in tiles if t["type"] in ("road", "town_hall")}
        for x, y in _spiral(cx, cy, w, h):
            if (x, y) in occupied:
                continue
            if any((x + dx, y + dy) in road_set for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                r = self.api.build(tile_type, x, y)
                if r.get("ok"):
                    return True
                if r.get("error") == "insufficient_funds":
                    return False
                if r.get("error") == "no_road_adjacency":
                    continue
        return False

    def _next_survey_anchor(self, cx: int, cy: int, w: int, h: int) -> tuple[int, int]:
        """Sweep the map systematically with 8-tile-stride anchors.

        Reservoirs are placed near the periphery on most seeds, so the
        sweep starts at center then jumps to the four corners + four
        edge-midpoints before tightening. Each anchor is clamped 2 tiles
        inside the boundary so the size-4 survey column (centered at the
        anchor; spans [x-2, x+2)) stays in-grid."""
        offsets = [
            (0, 0),  # center first
            (-12, -12),
            (12, -12),
            (-12, 12),
            (12, 12),  # four corners
            (-12, 0),
            (12, 0),
            (0, -12),
            (0, 12),  # edge midpoints
            (-6, -6),
            (6, -6),
            (-6, 6),
            (6, 6),  # inner ring
            (-12, -6),
            (12, -6),
            (-12, 6),
            (12, 6),
            (-6, -12),
            (6, -12),
            (-6, 12),
            (6, 12),
        ]
        idx = self._survey_seq % len(offsets)
        dx, dy = offsets[idx]
        return max(2, min(w - 3, cx + dx)), max(2, min(h - 3, cy + dy))

    def _pick_drill_target(
        self,
        candidates: list[dict[str, Any]],
        occupied: set[tuple[int, int]],
        w: int,
        h: int,
    ) -> tuple[int, int, int] | None:
        """Pick the best (x, y, z) where (x, y) is unoccupied and oil×perm passes thresholds."""
        for v in candidates:
            oil = float(v["oil_estimate_bbl"])
            perm = float(v["perm_estimate_md"])
            if oil < DRILL_OIL_THRESHOLD_BBL or perm < DRILL_PERM_THRESHOLD_MD:
                continue
            x, y, z = int(v["x"]), int(v["y"]), int(v["z"])
            if (x, y) in occupied or not _in_bounds(x, y, w, h):
                continue
            return x, y, z
        return None

    def _pick_stacked_drill_target(
        self,
        wells: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        w: int,
        h: int,
    ) -> tuple[int, int, int] | None:
        """Pick (x, y, z) for a second producer stacked on an existing one.

        Per reservoir-scale-and-stacked-completions slice 07 AC:
        - Same (x, y) as an existing producer whose `reservoir_id` is non-null
        - Candidate voxel in that same reservoir with |Δz| ≥ 3 vs the
          existing completion's `target_z` (the relaxed §4.12 drill rule)
        - Fire at most once per existing producer — detected by checking
          whether any other well already shares the producer's (x, y); if
          so, that producer is treated as already stacked.

        Iterate producers in id (creation) order, then candidates in their
        existing top_k ranking, so the result is deterministic across runs.
        """
        producers = [w_ for w_ in wells if w_["type"] == "production"]
        if not producers:
            return None
        # Count wells per surface tile so we can skip producers that already
        # carry a stacked completion (three-deep stacks remain out of scope).
        wells_at_xy: dict[tuple[int, int], int] = {}
        for w_ in wells:
            wells_at_xy[(int(w_["x"]), int(w_["y"]))] = (
                wells_at_xy.get((int(w_["x"]), int(w_["y"])), 0) + 1
            )
        for prod in producers:
            px, py = int(prod["x"]), int(prod["y"])
            if wells_at_xy.get((px, py), 0) > 1:
                continue
            prod_rid = prod.get("reservoir_id")
            if prod_rid is None:
                continue
            prod_z = int(prod["target_z"])
            # Candidate must be at the producer's (px, py) column — the
            # whole point of stacking is to reach voxels under the same
            # surface tile. A same-rid voxel at a different (x, y) would
            # need a fresh drill anyway, and drilling at (px, py, vz)
            # where the actual voxel at that coordinate is rock would
            # leave the new well with reservoir_id=None.
            for v in candidates:
                if int(v["x"]) != px or int(v["y"]) != py:
                    continue
                if v.get("reservoir_id") != prod_rid:
                    continue
                vz = int(v["z"])
                if abs(vz - prod_z) < 3:
                    continue
                # Looser thresholds than fresh drills: the surface tile and
                # pipeline are sunk costs, so even a stranded low-perm voxel
                # is worth draining if it has bbl. We keep the oil floor
                # (no point drilling at a dry voxel) but drop the perm gate
                # — the 3×3×3 drainage cube aggregates perm across 27
                # cells, so a single low-perm target voxel is still
                # commercial when surrounded by higher-perm rock.
                oil = float(v["oil_estimate_bbl"])
                if oil < DRILL_OIL_THRESHOLD_BBL:
                    continue
                if not _in_bounds(px, py, w, h):
                    continue
                return px, py, vz
        return None

    def _lay_pipeline_to_refinery(
        self,
        wx: int,
        wy: int,
        tiles: list[dict[str, Any]],
        occupied: set[tuple[int, int]],
        w: int,
        h: int,
        *,
        refinery_xy: tuple[int, int] | None = None,
    ) -> None:
        """Build a 4-connected pipeline path from the well at (wx, wy) to the
        nearest refinery. Path is L-shaped: walk x then y (or y then x —
        whichever has the most existing pipeline overlap, deterministic
        tiebreak: x-first). Endpoints are skipped (well + refinery occupy
        them); pipelines must only be orthogonally adjacent, not on top of,
        the producer/refinery — and `routing_units` only requires one
        pipeline neighbour per facility for membership.

        No-op if no refinery exists, or if `treasury` runs out mid-build
        (insufficient_funds is the API-level signal; we stop on first
        rejection of that kind)."""
        if refinery_xy is None:
            refineries = [t for t in tiles if t["type"] == "refinery"]
            if not refineries:
                return
            refineries.sort(key=lambda t: abs(int(t["x"]) - wx) + abs(int(t["y"]) - wy))
            refinery_xy = (int(refineries[0]["x"]), int(refineries[0]["y"]))
        rx, ry = refinery_xy

        path = _l_path((wx, wy), (rx, ry))
        for px, py in path:
            if (px, py) in occupied:
                continue
            if not _in_bounds(px, py, w, h):
                continue
            r = self.api.build("pipeline", px, py)
            if r.get("ok"):
                occupied.add((px, py))
            elif r.get("error") == "insufficient_funds":
                return

    def _drill_injection_same_reservoir(
        self,
        wells: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        occupied: set[tuple[int, int]],
        w: int,
        h: int,
    ) -> None:
        """Place an injection well in the same `reservoir_id` as an existing
        producer, at Chebyshev distance ≥ 2 from that producer's target.

        Per AC: read `reservoir_id` from `/state.wells` (do not recompute
        connectivity client-side); read candidate voxels' `reservoir_id`
        from `/state.reservoirs_revealed.top_k` (the surveyor exposes the
        per-voxel tag there). Setpoint matches WELL_RATE_BBL_DAY so the
        producer's `pressure_boost` is achievable up to its 0.5 cap."""
        producers = [w_ for w_ in wells if w_["type"] == "production"]
        if not producers:
            return
        # Iterate producers in id order (creation order) so the placement is
        # deterministic across runs.
        for prod in producers:
            prod_rid = prod.get("reservoir_id")
            if prod_rid is None:
                continue
            for v in candidates:
                x, y, z = int(v["x"]), int(v["y"]), int(v["z"])
                vrid = v.get("reservoir_id")
                if vrid is None or vrid != prod_rid:
                    continue
                if (x, y) in occupied or not _in_bounds(x, y, w, h):
                    continue
                cheb = max(
                    abs(x - int(prod["x"])),
                    abs(y - int(prod["y"])),
                    abs(z - int(prod["target_z"])),
                )
                if cheb < 2:
                    continue  # breakthrough gate (sim treats <=1 as breakthrough)
                r = self.api.drill(x, y, z, "injection")
                if r.get("ok"):
                    self.api.control_well(r["result"]["id"], INJECTION_RATE_BBL_DAY)
                    return


# --- Geometry helpers -------------------------------------------------------


def _in_bounds(x: int, y: int, w: int, h: int) -> bool:
    return 0 <= x < w and 0 <= y < h


def _l_path(a: tuple[int, int], b: tuple[int, int]) -> list[tuple[int, int]]:
    """L-shaped 4-connected walk from `a` exclusive to `b` exclusive.

    Walk x-first (along ay) then y-first (along bx); output the cells
    strictly between the two endpoints (so neither `a` nor `b` is in the
    list — they're occupied by the well + refinery). Path is contiguous
    4-connected, includes the corner at (bx, ay) when the corner is not
    `a` and not `b`."""
    ax, ay = a
    bx, by = b
    out: list[tuple[int, int]] = []
    if ax != bx:
        sx = 1 if bx > ax else -1
        x = ax + sx
        while True:
            if (x, ay) == b:
                break
            out.append((x, ay))
            if x == bx:
                break
            x += sx
    if ay != by:
        sy = 1 if by > ay else -1
        y = ay + sy
        while True:
            if (bx, y) == b:
                break
            if (bx, y) != a:
                out.append((bx, y))
            if y == by:
                break
            y += sy
    return out


def _spiral(cx: int, cy: int, w: int, h: int) -> list[tuple[int, int]]:
    """Expanding chebyshev rings around (cx, cy), in deterministic order."""
    out: list[tuple[int, int]] = [(cx, cy)]
    for r in range(1, max(w, h)):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                x, y = cx + dx, cy + dy
                if _in_bounds(x, y, w, h):
                    out.append((x, y))
    return out


def _perimeter_spiral(cx: int, cy: int, w: int, h: int) -> list[tuple[int, int]]:
    """Far-from-center first: expanding chebyshev rings starting at radius 4."""
    out: list[tuple[int, int]] = []
    for r in range(4, max(w, h)):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                x, y = cx + dx, cy + dy
                if _in_bounds(x, y, w, h):
                    out.append((x, y))
    return out


# --- CLI driver -------------------------------------------------------------


def _make_inprocess_client() -> ApiClient:
    """Build an ApiClient backed by an in-process FastAPI TestClient. Lets the
    CLI run a full game without booting uvicorn or requiring a network port."""
    from fastapi.testclient import TestClient

    from world.api import create_app

    app = create_app()
    return ApiClient(transport=TestClient(app))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the scripted reference agent.")
    parser.add_argument("--seed", type=int, default=42, help="World seed (default: 42)")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="If given, write {seed, p_ref, t_ref} JSON to this path.",
    )
    parser.add_argument(
        "--api-url",
        type=str,
        default=None,
        help="Connect to a live world at this URL (otherwise run in-process).",
    )
    args = parser.parse_args(argv)

    api = ApiClient(base_url=args.api_url) if args.api_url else _make_inprocess_client()
    agent = ScriptedAgent(api, seed=args.seed)
    final = agent.play_game()

    p_ref = float(final["population"])
    starting_cash = float(final["config"]["starting_cash"])
    t_ref = float(final["treasury"]) - starting_cash

    payload = {"seed": args.seed, "p_ref": p_ref, "t_ref": t_ref}
    print(json.dumps(payload, indent=2))

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2) + "\n")

    # Sanity: score must be a real number; surface any NaNs as failure.
    if not (math.isfinite(p_ref) and math.isfinite(t_ref)):
        return 1
    return 0


# Agent Play attach contract: the handler prefers a top-level `Agent`
# symbol that is a BaseAgent subclass (`world.api.post_agent_attach`).
Agent = ScriptedAgent


if __name__ == "__main__":
    raise SystemExit(main())

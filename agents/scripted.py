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

Run end-to-end via `python -m agents.scripted --seed 42 --output
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
        n_gas = sum(1 for t in tiles if t["type"] == "gas_peaker")
        n_coal = sum(1 for t in tiles if t["type"] == "coal_plant")
        n_refinery = sum(1 for t in tiles if t["type"] == "refinery")
        n_prod_wells = sum(1 for w_ in wells if w_["type"] == "production")
        n_inj_wells = sum(1 for w_ in wells if w_["type"] == "injection")
        active_event_types = {e["type"] for e in events}

        # Dispatchable capacity (the only thing that helps at night) — gas
        # + coal nameplate. Renewables don't count toward reserve at peak.
        dispatchable_kw = n_gas * 500.0 + n_coal * 800.0
        # Expected peak demand (evening hour, factor 1.5 + commercial full +
        # industrial continuous). Used as a build-out gating predicate so
        # the agent never lets demand outrun reliable supply.
        expected_peak_kw = pop * 0.333 * 1.5 + n_commercial * 50.0 + n_industrial * 300.0

        occupied = {(t["x"], t["y"]) for t in tiles}
        occupied |= {(w_["x"], w_["y"]) for w_ in wells}

        # ----- Bootstrap: minimum viable city -----------------------------
        # Roads in a + cross around town hall, then 4 houses + 2 commercial,
        # 4 solar + 1 gas peaker. Ensures pop has jobs/housing/electricity
        # before week-4 phase rollover.
        if phase == "bootstrap":
            self._lay_initial_roads(treasury, cx, cy, w, h, occupied)
            return

        # ----- Crisis response (highest priority) -------------------------
        # Heatwave → emergency gas peaker (track id to demolish at expiry).
        if "heatwave" in active_event_types and self._heatwave_peaker_id is None:
            new_id = self._build_plant("gas_peaker", treasury, cx, cy, w, h, occupied)
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
            self._build_plant("gas_peaker", treasury, cx, cy, w, h, occupied)
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
                and self._build_plant("coal_plant", treasury, cx, cy, w, h, occupied)
            ):
                return
            if treasury >= 80_000 and self._build_plant(
                "gas_peaker", treasury, cx, cy, w, h, occupied
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
            # of the refinery-build flow") get connected.
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

        Brief baseline: 1 road / 2 commercial / 2 houses / 4 solar / 1 gas
        peaker. We ship a + cross of roads (cheap, anchors more growth) +
        the prescribed civilian/plant counts, all in one turn. Bootstrap
        is the only multi-action turn — every later phase is single-action
        for cash discipline."""
        # Plants off the road cross axes so they don't collide with the
        # bootstrap road extension below.
        plan: list[tuple[str, int, int]] = [
            ("gas_peaker", cx - 8, cy),
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
        # 6 commercial up-front: 2 from PRD baseline + 4 to defeat the
        # int(pop) growth-truncation trap. With pop=100 and only 2
        # commercial, jobs<0.7·pop triggers immediate decline.
        plan += [
            ("commercial", cx + 1, cy + 1),
            ("commercial", cx - 1, cy - 1),
            ("commercial", cx + 1, cy - 1),
            ("commercial", cx - 1, cy + 1),
            ("commercial", cx + 2, cy + 1),
            ("commercial", cx - 2, cy - 1),
            ("house", cx + 2, cy - 1),
            ("house", cx - 2, cy + 1),
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


if __name__ == "__main__":
    raise SystemExit(main())

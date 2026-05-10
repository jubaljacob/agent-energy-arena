"""FastAPI surface for the simulation.

Endpoints wired through slice 02:
  /state, /step, /reset, /seed, /catalog, /forecast, /build, /demolish.
All mutating calls (success or failure) are appended to
runs/{run_id}/actions.jsonl.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from world.action_log import ActionLog
from world.catalog import build_catalog
from world.sim import World


class ResetBody(BaseModel):
    seed: int | None = None


class StepBody(BaseModel):
    days: int = Field(default=7, ge=1, le=7)


class BuildBody(BaseModel):
    tile_type: str
    x: int
    y: int


class DemolishBody(BaseModel):
    x: int
    y: int


class SurveyBody(BaseModel):
    x: int
    y: int
    size: int = Field(default=8)


class DrillBody(BaseModel):
    x: int
    y: int
    target_z: int
    well_type: str = Field(default="production")


class WellControlBody(BaseModel):
    well_id: str
    rate_bbl_day: float


class RefineryControlBody(BaseModel):
    refinery_id: str
    rate_bbl_day: float


def create_app(world: World | None = None, action_log: ActionLog | None = None) -> FastAPI:
    app = FastAPI(title="Energy-AI Nexus", version="0.1.0")

    app.state.world = world or World()
    app.state.action_log = action_log or ActionLog()

    @app.get("/seed")
    def get_seed() -> dict[str, int]:
        return {"seed": app.state.world.state.seed}

    @app.get("/catalog")
    def get_catalog() -> dict[str, Any]:
        return build_catalog()

    @app.get("/state")
    def get_state() -> dict[str, Any]:
        return app.state.world.state_dict()

    @app.get("/events")
    def get_events() -> dict[str, Any]:
        s = app.state.world.state
        return {
            "active": list(s.active_events),
            "historical": list(s.historical_events),
            "regulatory_tightenings_applied": s.regulatory_tightenings_applied,
        }

    @app.get("/forecast")
    def get_forecast(hours: int = 24) -> dict[str, Any]:
        if hours < 1 or hours > 168:
            raise HTTPException(status_code=400, detail="hours must be in [1, 168]")
        return app.state.world.forecast(hours=hours)

    @app.post("/reset")
    def post_reset(body: ResetBody) -> dict[str, Any]:
        params = body.model_dump()
        try:
            app.state.world.reset(seed=body.seed)
            result = {
                "ok": True,
                "treasury_after": app.state.world.state.treasury,
                "result": {"seed": app.state.world.state.seed, "day": 0},
            }
            app.state.action_log.append("/reset", params, ok=True, result=result["result"])
            return result
        except Exception as exc:  # pragma: no cover - defensive
            app.state.action_log.append("/reset", params, ok=False, error=str(exc))
            raise

    @app.post("/step")
    def post_step(body: StepBody) -> dict[str, Any]:
        params = body.model_dump()
        try:
            summary = app.state.world.step(days=body.days)
            result = {
                "ok": True,
                "day_completed": summary.day_completed,
                "summary": summary.summary,
                "treasury_after": summary.treasury_after,
            }
            app.state.action_log.append(
                "/step", params, ok=True, result={"day_completed": summary.day_completed}
            )
            return result
        except ValueError as exc:
            app.state.action_log.append("/step", params, ok=False, error=str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/build")
    def post_build(body: BuildBody) -> dict[str, Any]:
        params = body.model_dump()
        result = app.state.world.build(body.tile_type, body.x, body.y)
        app.state.action_log.append(
            "/build",
            params,
            ok=result["ok"],
            error=result.get("error"),
            result=result.get("result"),
        )
        return result

    @app.post("/survey")
    def post_survey(body: SurveyBody) -> dict[str, Any]:
        params = body.model_dump()
        result = app.state.world.survey(body.x, body.y, body.size)
        # Strip the bulky voxel array out of the action log entry; agents that
        # care can re-read /reservoirs. Keep cost/size/x/y for forensics.
        log_result: Any = None
        if result.get("result"):
            log_result = {k: v for k, v in result["result"].items() if k != "voxels"}
            log_result["n_voxels"] = len(result["result"].get("voxels", []))
        app.state.action_log.append(
            "/survey",
            params,
            ok=result["ok"],
            error=result.get("error"),
            result=log_result,
        )
        return result

    @app.get("/reservoirs")
    def get_reservoirs(min_oil: float = 0.0, top_k: int = 100) -> dict[str, Any]:
        if top_k < 1 or top_k > 4096:
            raise HTTPException(status_code=400, detail="top_k must be in [1, 4096]")
        return app.state.world.reservoirs(min_oil=min_oil, top_k=top_k)

    @app.post("/drill")
    def post_drill(body: DrillBody) -> dict[str, Any]:
        params = body.model_dump()
        result = app.state.world.drill(body.x, body.y, body.target_z, body.well_type)
        app.state.action_log.append(
            "/drill",
            params,
            ok=result["ok"],
            error=result.get("error"),
            result=result.get("result"),
        )
        return result

    @app.post("/control/well")
    def post_control_well(body: WellControlBody) -> dict[str, Any]:
        params = body.model_dump()
        result = app.state.world.control_well(body.well_id, body.rate_bbl_day)
        app.state.action_log.append(
            "/control/well",
            params,
            ok=result["ok"],
            error=result.get("error"),
            result=result.get("result"),
        )
        return result

    @app.post("/control/refinery")
    def post_control_refinery(body: RefineryControlBody) -> dict[str, Any]:
        params = body.model_dump()
        result = app.state.world.control_refinery(body.refinery_id, body.rate_bbl_day)
        app.state.action_log.append(
            "/control/refinery",
            params,
            ok=result["ok"],
            error=result.get("error"),
            result=result.get("result"),
        )
        return result

    @app.post("/demolish")
    def post_demolish(body: DemolishBody) -> dict[str, Any]:
        params = body.model_dump()
        result = app.state.world.demolish(body.x, body.y)
        app.state.action_log.append(
            "/demolish",
            params,
            ok=result["ok"],
            error=result.get("error"),
            result=result.get("result"),
        )
        return result

    # Static UI -----------------------------------------------------------
    ui_dir = Path(__file__).parent / "ui"
    if ui_dir.exists():
        app.mount("/ui", StaticFiles(directory=ui_dir), name="ui")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(ui_dir / "index.html")

    return app


app = create_app()

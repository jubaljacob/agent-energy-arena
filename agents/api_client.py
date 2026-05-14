"""Thin HTTP wrapper around the world FastAPI surface.

One method per endpoint; each returns parsed JSON. Errors raise the
underlying transport's HTTP exception so failures show up loudly.

Two transports are supported:

  - `base_url`: live HTTP via `httpx.Client`. Use this when the world is
    running in a separate process (Docker, dev server).
  - `transport`: any object exposing `requests`-compatible `.get()` /
    `.post()` (FastAPI's `TestClient` is the canonical example). Lets
    tests + the scripted-agent CLI run in-process without a TCP socket.

Mutating endpoints return the world's `{ok, error?, treasury_after,
result}` envelope unchanged. Read endpoints return their payload
directly.
"""

from __future__ import annotations

from typing import Any, Protocol


class _Transport(Protocol):
    def get(self, url: str, *, params: dict[str, Any] | None = ...) -> Any: ...
    def post(self, url: str, *, json: dict[str, Any] | None = ...) -> Any: ...


class ApiClient:
    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        *,
        transport: _Transport | None = None,
    ) -> None:
        if transport is not None:
            self._client: _Transport = transport
        else:
            import httpx

            self._client = httpx.Client(base_url=base_url, timeout=120.0)
        self._catalog_cache: dict[str, Any] | None = None

    # -- Read endpoints ---------------------------------------------------

    def state(self) -> dict[str, Any]:
        return self._get("/state")

    def seed(self) -> dict[str, Any]:
        return self._get("/seed")

    def catalog(self) -> dict[str, Any]:
        if self._catalog_cache is None:
            self._catalog_cache = self._get("/catalog")
        return self._catalog_cache

    def survey_cost_preview(self, size: int) -> float:
        """Quadratic survey cost derived from the cached /catalog response.

        Mirrors `world.subsurface.survey_cost(size)` so UI hover and agent
        planning agree on the cost without re-reading the brief. Issues at
        most one GET /catalog (cached for the lifetime of the client).
        """
        survey = self.catalog()["subsurface"]["survey"]
        base_cost = float(survey["base_cost"])
        base_size = float(survey["base_size"])
        return base_cost * (size / base_size) ** 2

    def events(self) -> dict[str, Any]:
        return self._get("/events")

    def score(self) -> dict[str, Any]:
        return self._get("/score")

    def forecast(self, hours: int = 24) -> list[dict[str, Any]]:
        return self._get("/forecast", params={"hours": hours})

    def reservoirs(self, *, min_oil: float = 0.0, top_k: int = 100) -> dict[str, Any]:
        return self._get("/reservoirs", params={"min_oil": min_oil, "top_k": top_k})

    # -- Mutating endpoints -----------------------------------------------

    def reset(self, seed: int | None = None, *, scenario: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"seed": seed}
        if scenario is not None:
            body["scenario"] = scenario
        return self._post("/reset", body)

    def attach_scenario(self, dotted_path: str) -> dict[str, Any]:
        return self._post("/scenario", {"dotted_path": dotted_path})

    def scenario(self) -> dict[str, Any]:
        return self._get("/scenario")

    def run(self) -> dict[str, Any]:
        return self._get("/run")

    def step(self, days: int = 7) -> dict[str, Any]:
        return self._post("/step", {"days": days})

    def build(self, tile_type: str, x: int, y: int) -> dict[str, Any]:
        return self._post("/build", {"tile_type": tile_type, "x": x, "y": y})

    def demolish(self, x: int, y: int) -> dict[str, Any]:
        return self._post("/demolish", {"x": x, "y": y})

    def survey(self, x: int, y: int, size: int = 8) -> dict[str, Any]:
        return self._post("/survey", {"x": x, "y": y, "size": size})

    def drill(self, x: int, y: int, target_z: int, well_type: str = "production") -> dict[str, Any]:
        return self._post(
            "/drill",
            {"x": x, "y": y, "target_z": target_z, "well_type": well_type},
        )

    def control_well(self, well_id: str, rate_bbl_day: float) -> dict[str, Any]:
        return self._post("/control/well", {"well_id": well_id, "rate_bbl_day": rate_bbl_day})

    def control_refinery(self, refinery_id: str, rate_bbl_day: float) -> dict[str, Any]:
        return self._post(
            "/control/refinery",
            {"refinery_id": refinery_id, "rate_bbl_day": rate_bbl_day},
        )

    # -- Internals --------------------------------------------------------

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        r = self._client.get(path, params=params or {})
        return _parse(r)

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        r = self._client.post(path, json=body)
        return _parse(r)


class UiAgentApiClient(ApiClient):
    """ApiClient variant handed to agents attached via the UI's Agent Play
    mode. The human owns the clock and the scenario; the agent owns world
    mutations. Slice #4 fills in the clock-violation overrides (`step`,
    `reset`, `attach_scenario` will raise client-side); this slice ships
    the type only so the wiring is in place.
    """


def _parse(response: Any) -> Any:
    """Raise for non-2xx, then return parsed JSON. Works for httpx.Response
    and FastAPI TestClient responses (both expose status_code + .json()).
    """
    status = getattr(response, "status_code", 200)
    if status >= 400:
        text = getattr(response, "text", "")
        raise RuntimeError(f"HTTP {status}: {text}")
    return response.json()

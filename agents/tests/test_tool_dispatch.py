"""Tests for `agents.tool_dispatch.dispatch_tool_call`.

The dispatcher is the single source of truth for `ToolCall → ApiClient`
mutator routing. Tests pin three guarantees:

1. Every supported tool name lands on the right `ApiClient` method with
   the right kwargs.
2. Unknown tool names return `None` (LLM hallucination is silently
   skipped by callers).
3. The world's response envelope passes through `dispatch_tool_call`
   unmodified.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from agents.api_client import ApiClient
from agents.llm import ToolCall
from agents.tool_dispatch import dispatch_tool_call


class _RecordingApi:
    """Captures the last method + kwargs and returns a sentinel envelope.

    Stubbed in place of `ApiClient` so tests verify routing + passthrough
    without needing a live world. `cast(ApiClient, ...)` keeps mypy happy
    at the call site — only the mutator surface is exercised here.
    """

    def __init__(self, envelope: dict[str, Any]) -> None:
        self.envelope = envelope
        self.last: tuple[str, dict[str, Any]] | None = None

    def build(self, *, tile_type: str, x: int, y: int) -> dict[str, Any]:
        self.last = ("build", {"tile_type": tile_type, "x": x, "y": y})
        return self.envelope

    def demolish(self, *, x: int, y: int) -> dict[str, Any]:
        self.last = ("demolish", {"x": x, "y": y})
        return self.envelope

    def survey(self, *, x: int, y: int, size: int = 8) -> dict[str, Any]:
        self.last = ("survey", {"x": x, "y": y, "size": size})
        return self.envelope

    def drill(
        self, *, x: int, y: int, target_z: int, well_type: str = "production"
    ) -> dict[str, Any]:
        self.last = (
            "drill",
            {"x": x, "y": y, "target_z": target_z, "well_type": well_type},
        )
        return self.envelope

    def control_well(self, *, well_id: str, rate_bbl_day: float) -> dict[str, Any]:
        self.last = ("control_well", {"well_id": well_id, "rate_bbl_day": rate_bbl_day})
        return self.envelope

    def control_refinery(self, *, refinery_id: str, rate_bbl_day: float) -> dict[str, Any]:
        self.last = (
            "control_refinery",
            {"refinery_id": refinery_id, "rate_bbl_day": rate_bbl_day},
        )
        return self.envelope


@pytest.fixture
def envelope() -> dict[str, Any]:
    return {"ok": True, "treasury_after": 1000.0, "result": {"detail": "sentinel"}}


@pytest.fixture
def api(envelope: dict[str, Any]) -> _RecordingApi:
    return _RecordingApi(envelope)


# ---------- Routing: one test per mutator -----------------------------------


def test_build_routes_to_api_build(api: _RecordingApi, envelope: dict[str, Any]) -> None:
    out = dispatch_tool_call(
        cast(ApiClient, api),
        ToolCall("build", {"tile_type": "road", "x": 3, "y": 4}),
    )
    assert api.last == ("build", {"tile_type": "road", "x": 3, "y": 4})
    assert out is envelope


def test_demolish_routes_to_api_demolish(api: _RecordingApi, envelope: dict[str, Any]) -> None:
    out = dispatch_tool_call(
        cast(ApiClient, api),
        ToolCall("demolish", {"x": 5, "y": 6}),
    )
    assert api.last == ("demolish", {"x": 5, "y": 6})
    assert out is envelope


def test_survey_routes_with_default_size(api: _RecordingApi) -> None:
    """`size` is optional in the tool schema; the dispatcher must apply
    the same default (8) the `ApiClient.survey` method uses."""
    dispatch_tool_call(cast(ApiClient, api), ToolCall("survey", {"x": 1, "y": 2}))
    assert api.last == ("survey", {"x": 1, "y": 2, "size": 8})


def test_survey_routes_with_explicit_size(api: _RecordingApi) -> None:
    dispatch_tool_call(
        cast(ApiClient, api),
        ToolCall("survey", {"x": 1, "y": 2, "size": 12}),
    )
    assert api.last == ("survey", {"x": 1, "y": 2, "size": 12})


def test_drill_routes_with_default_well_type(api: _RecordingApi) -> None:
    dispatch_tool_call(
        cast(ApiClient, api),
        ToolCall("drill", {"x": 1, "y": 2, "target_z": 1200}),
    )
    assert api.last == (
        "drill",
        {"x": 1, "y": 2, "target_z": 1200, "well_type": "production"},
    )


def test_drill_routes_with_explicit_well_type(api: _RecordingApi) -> None:
    dispatch_tool_call(
        cast(ApiClient, api),
        ToolCall(
            "drill",
            {"x": 1, "y": 2, "target_z": 1200, "well_type": "injection"},
        ),
    )
    assert api.last == (
        "drill",
        {"x": 1, "y": 2, "target_z": 1200, "well_type": "injection"},
    )


def test_set_well_rate_routes_to_control_well(api: _RecordingApi) -> None:
    """LLM-facing tool name (`set_well_rate`) maps onto the ApiClient
    method name (`control_well`)."""
    dispatch_tool_call(
        cast(ApiClient, api),
        ToolCall("set_well_rate", {"well_id": "w-1", "rate_bbl_day": 100.0}),
    )
    assert api.last == ("control_well", {"well_id": "w-1", "rate_bbl_day": 100.0})


def test_set_refinery_rate_routes_to_control_refinery(api: _RecordingApi) -> None:
    dispatch_tool_call(
        cast(ApiClient, api),
        ToolCall("set_refinery_rate", {"refinery_id": "r-1", "rate_bbl_day": 250.0}),
    )
    assert api.last == (
        "control_refinery",
        {"refinery_id": "r-1", "rate_bbl_day": 250.0},
    )


# ---------- Unknowns + passthrough ------------------------------------------


@pytest.mark.parametrize("name", ["step", "scoreboard", "", "BUILD"])
def test_unknown_tool_name_returns_none(api: _RecordingApi, name: str) -> None:
    """Hallucinated names — including `step` (clock advancement is the
    caller's job) and case-mismatched aliases — return None without
    touching the api."""
    assert dispatch_tool_call(cast(ApiClient, api), ToolCall(name, {})) is None
    assert api.last is None


def test_envelope_passes_through_unmodified(api: _RecordingApi, envelope: dict[str, Any]) -> None:
    """The dispatcher returns the exact dict the ApiClient method
    returned — no copy, no filtering."""
    out = dispatch_tool_call(
        cast(ApiClient, api),
        ToolCall("build", {"tile_type": "road", "x": 0, "y": 0}),
    )
    assert out is envelope

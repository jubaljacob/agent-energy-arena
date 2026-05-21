"""Shared tool-call → `ApiClient` mutator routing.

`dispatch_tool_call` is the single source of truth for translating a
`ToolCall` emitted by an LLM into the matching `ApiClient` mutator call.
Both the attach-mode runtime (`agents.attach_runtime`) and the LLM
reference agents call into here so the routing table only lives in one
place.

Unknown tool names return `None` rather than raising: LLMs hallucinate
tool names, and the caller is expected to skip the call silently. The
caller still owns argument-shape errors (`KeyError`, `TypeError`,
`ValueError`) and world-side rejections (`RuntimeError` from the
`ApiClient` 4xx envelope) — `dispatch_tool_call` does not catch them.
"""

from __future__ import annotations

from typing import Any

from agents.api_client import ApiClient
from agents.llm import ToolCall


def dispatch_tool_call(api: ApiClient, call: ToolCall) -> dict[str, Any] | None:
    """Route a `ToolCall` to the matching `ApiClient` mutator.

    Returns the world's response envelope (`{ok, error?, treasury_after,
    result}`) on a known tool name, or `None` when the name does not
    match any mutator (the model hallucinated). Does not route `step` —
    clock advancement is owned by the caller in both attach-mode and
    CLI-mode loops.
    """
    a = call.arguments
    name = call.name
    if name == "build":
        return api.build(tile_type=str(a["tile_type"]), x=int(a["x"]), y=int(a["y"]))
    if name == "demolish":
        return api.demolish(x=int(a["x"]), y=int(a["y"]))
    if name == "survey":
        return api.survey(x=int(a["x"]), y=int(a["y"]), size=int(a.get("size", 8)))
    if name == "drill":
        return api.drill(
            x=int(a["x"]),
            y=int(a["y"]),
            target_z=int(a["target_z"]),
            well_type=str(a.get("well_type", "production")),
        )
    if name == "set_well_rate":
        return api.control_well(
            well_id=str(a["well_id"]),
            rate_bbl_day=float(a["rate_bbl_day"]),
        )
    if name == "set_refinery_rate":
        return api.control_refinery(
            refinery_id=str(a["refinery_id"]),
            rate_bbl_day=float(a["rate_bbl_day"]),
        )
    return None

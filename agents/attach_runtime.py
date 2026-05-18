"""Attach-mode runtime for the LLM reference agents.

The reference LLM agents (`LLMReactAgent`, `LangGraphAgent`) drive
themselves via `play_game()` in CLI mode, but Agent Play (UI) calls
`act(state)` per `/step` and lets the human own the clock. Without an
explicit override both agents inherit the no-op `BaseAgent.act` — so
attached they contribute nothing. `drive_one_turn` is what each
agent's `act()` delegates to: one LLM call per attach-mode `/step`
that summarises the world, asks the model, and dispatches every
non-`step` tool call it emits. The `step` tool is silently dropped —
the surrounding `/step` handler is the only place that may advance
the clock in attach mode.

`drive_one_turn` uses only `api.build`, `api.demolish`, `api.survey`,
`api.drill`, `api.control_well`, `api.control_refinery`, and the read
methods on `ApiClient` — every mutator that `UiAgentApiClient` still
permits during attach. It never calls `api.step` / `api.reset` /
`api.attach_scenario`, which the attach client guards against.
"""

from __future__ import annotations

from typing import Any

from agents.llm import LLMClient, ToolCall, Usage
from agents.state_summary import summarize_state


def drive_one_turn(
    api: Any,
    state: dict[str, Any],
    llm: LLMClient,
    *,
    system_prompt: str,
    action_tools: list[dict[str, Any]],
    max_tokens: int = 2048,
    forecast_hours: int = 24,
) -> Usage:
    """One LLM call's worth of mutations against the attached world.

    Mirrors `LLMReactAgent.decide` minus the `step` tool: the human
    owns the clock in attach mode, so a `step` from the model is
    silently discarded (and would raise from `UiAgentApiClient.step`
    even if we tried to honour it). Every other tool call is
    dispatched in order; malformed args or world-side rejections
    (`tile_occupied`, `insufficient_funds`, …) are swallowed so a bad
    suggestion from the model doesn't crash the turn.

    Returns the LLM's `Usage` so callers can maintain a cumulative
    token counter (and fire whatever budget warning they like).
    """
    forecast = _safe_forecast(api, forecast_hours)
    user_msg = summarize_state(state, forecast)
    response = llm.chat(
        system=system_prompt,
        user=user_msg,
        tools=action_tools,
        max_tokens=max_tokens,
    )
    for call in response.tool_calls:
        if call.name == "step":
            # Human owns the clock in attach mode. Ignore and continue —
            # any tool calls AFTER `step` still represent valid intent
            # from the model and we'd rather dispatch than drop them.
            continue
        try:
            _dispatch_one(api, call)
        except (RuntimeError, KeyError, TypeError, ValueError):
            # RuntimeError covers the world's 4xx envelopes via ApiClient's
            # raise-on-error parsing; KeyError/TypeError/ValueError cover
            # malformed model arguments (missing field, wrong shape).
            continue
    return response.usage


def _safe_forecast(api: Any, hours: int) -> list[dict[str, Any]] | None:
    try:
        return api.forecast(hours=hours)
    except (RuntimeError, AttributeError):
        return None


def _dispatch_one(api: Any, call: ToolCall) -> dict[str, Any] | None:
    """Single point of truth for tool-call → ApiClient method routing in
    attach mode. The CLI-mode agents keep their own copies inline so this
    helper can evolve independently as new tools land."""
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
    # Unknown tool name — model hallucinated; silently skip.
    return None

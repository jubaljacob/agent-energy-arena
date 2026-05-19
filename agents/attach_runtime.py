"""Attach-mode runtime for the LLM reference agents.

The reference LLM agents (`LLMReactAgent`, `LangGraphAgent`) drive
themselves via `play_game()` in CLI mode, but Agent Play (UI) calls
`act(state)` per `/step` and lets the human own the clock. Without an
explicit override both agents inherit the no-op `BaseAgent.act` — so
attached they contribute nothing. `drive_one_turn` is what each
agent's `act()` delegates to: one LLM call per attach-mode `/step`
that summarises the world, asks the model, and dispatches every
non-`step` tool call it emits.

`drive_one_turn` never calls `api.step` itself — `UiAgentApiClient`
forbids that during attach so the action log's per-day slicing stays
intact. Instead it captures the `days` arg from the model's `step`
tool call and returns it: the surrounding `/step` handler uses that
to skip the next N-1 `act()` invocations while the human's play
timer keeps ticking. A model that emits no `step` returns `None`,
meaning "wake me every step" — the existing behavior.

`drive_one_turn` uses only `api.build`, `api.demolish`, `api.survey`,
`api.drill`, `api.control_well`, `api.control_refinery`, and the read
methods on `ApiClient` — every mutator that `UiAgentApiClient` still
permits during attach.
"""

from __future__ import annotations

import sys
from typing import Any, TextIO

from agents.llm import LLMClient, ToolCall, Usage
from agents.state_summary import summarize_state

_DEFAULT_STEP_DAYS_FALLBACK: int = 7


def drive_one_turn(
    api: Any,
    state: dict[str, Any],
    llm: LLMClient,
    *,
    system_prompt: str,
    action_tools: list[dict[str, Any]],
    max_tokens: int = 2048,
    forecast_hours: int = 24,
    log_stream: TextIO | None = None,
) -> tuple[Usage, int | None]:
    """One LLM call's worth of mutations against the attached world.

    Mirrors `LLMReactAgent.decide` minus the direct `step` dispatch:
    the human owns the clock in attach mode, so a `step` call from
    the model is read for its `days` argument and otherwise dropped
    (calling `api.step` would raise from `UiAgentApiClient.step`).
    Every other tool call is dispatched in order; malformed args or
    world-side rejections (`tile_occupied`, `insufficient_funds`, …)
    are swallowed so a bad suggestion from the model doesn't crash
    the turn.

    Returns `(usage, skip_days)`:
      - `usage` is the LLM's token accounting so callers can
        maintain a cumulative counter.
      - `skip_days` is the clamped (1..7) `days` from the first
        `step` tool call the model emitted, or `None` when the
        model emitted no `step`. The `/step` handler interprets
        `None` as "act every step" and an int `N` as "act now,
        skip the next N-1 steps."
    """
    forecast = _safe_forecast(api, forecast_hours)
    user_msg = summarize_state(state, forecast)
    response = llm.chat(
        system=system_prompt,
        user=user_msg,
        tools=action_tools,
        max_tokens=max_tokens,
    )
    skip_days: int | None = None
    dispatched: list[tuple[str, bool]] = []  # (one-line summary, ok)
    for call in response.tool_calls:
        if call.name == "step":
            # Capture the first step's days as the requested skip
            # interval; drop the tool call itself (the surrounding
            # /step handler owns clock advancement). Subsequent
            # non-step tool calls still dispatch — see module
            # docstring for the rationale.
            if skip_days is None:
                skip_days = _clamp_days(call.arguments.get("days"))
            continue
        ok = True
        try:
            _dispatch_one(api, call)
        except (RuntimeError, KeyError, TypeError, ValueError):
            # RuntimeError covers the world's 4xx envelopes via ApiClient's
            # raise-on-error parsing; KeyError/TypeError/ValueError cover
            # malformed model arguments (missing field, wrong shape).
            ok = False
        dispatched.append((_summarise_call(call), ok))
    _log_turn(
        log_stream if log_stream is not None else sys.stderr,
        state=state,
        dispatched=dispatched,
        skip_days=skip_days,
    )
    return response.usage, skip_days


def _summarise_call(call: ToolCall) -> str:
    """One-line, log-friendly rendering of a tool call. Keeps the
    args terse so a fast UI play with many tool calls/turn doesn't
    drown the terminal."""
    a = call.arguments
    name = call.name
    try:
        if name == "build":
            return f"build({a['tile_type']}, {a['x']},{a['y']})"
        if name == "demolish":
            return f"demolish({a['x']},{a['y']})"
        if name == "survey":
            return f"survey({a['x']},{a['y']}, size={a.get('size', 8)})"
        if name == "drill":
            return f"drill({a['x']},{a['y']},z={a['target_z']}, {a.get('well_type', 'production')})"
        if name == "set_well_rate":
            return f"set_well_rate({a['well_id']}, {a['rate_bbl_day']})"
        if name == "set_refinery_rate":
            return f"set_refinery_rate({a['refinery_id']}, {a['rate_bbl_day']})"
    except (KeyError, TypeError):
        pass
    return f"{name}({a!r})"


def _log_turn(
    stream: TextIO,
    *,
    state: dict[str, Any],
    dispatched: list[tuple[str, bool]],
    skip_days: int | None,
) -> None:
    """Single-line per-turn summary. Format:

    [agent day=12] build(road, 5,8); drill(7,9,z=1200,production) (rejected); skip=3
    """
    day = state.get("day", "?")
    if dispatched:
        parts = [s if ok else f"{s} (rejected)" for s, ok in dispatched]
        actions = "; ".join(parts)
    else:
        actions = "(no actions)"
    skip = "every step" if skip_days is None else f"{skip_days}d"
    print(f"[agent day={day}] {actions}; skip={skip}", file=stream, flush=True)


def _clamp_days(raw: Any) -> int:
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_STEP_DAYS_FALLBACK
    return max(1, min(7, days))


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

"""End-to-end tests for the LLM ReAct agent using `MockLLM`.

The real LLM-credential AC (5 min wall-clock, ≤1M tokens, score >115%
of scripted) is HITL — see issue 15. AFK coverage focuses on:

- ACTION_TOOLS schema shape (the 7-tool contract).
- summarize_state output stays within a soft token budget.
- Tool calls dispatch through ApiClient to the real world.
- Step-fallback fires when the LLM omits `step`.
- Cumulative token counter + 800K stderr warning.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agents.api_client import ApiClient
from agents.llm import LLMResponse, MockLLM, ToolCall, Usage
from agents.llm_react import LLMReactAgent
from agents.prompts import ACTION_TOOLS, SYSTEM_PROMPT, TILE_TYPES
from agents.state_summary import summarize_state
from world.api import create_app
from world.sim import World


def _client(world: World | None = None) -> tuple[ApiClient, World]:
    w = world or World()
    return ApiClient(transport=TestClient(create_app(world=w))), w


def _resp(tool_calls: list[ToolCall], *, in_tok: int = 100, out_tok: int = 20) -> LLMResponse:
    return LLMResponse(tool_calls=tool_calls, text="", usage=Usage(in_tok, out_tok))


# ---------- ACTION_TOOLS schema --------------------------------------------


def test_action_tools_has_exactly_the_seven_prd_tools() -> None:
    names = [t["name"] for t in ACTION_TOOLS]
    assert names == [
        "build",
        "demolish",
        "survey",
        "drill",
        "set_well_rate",
        "set_refinery_rate",
        "step",
    ]


def test_action_tools_have_object_parameters_with_required_lists() -> None:
    for tool in ACTION_TOOLS:
        params = tool["parameters"]
        assert params["type"] == "object"
        assert "properties" in params
        assert isinstance(params.get("required", []), list)


def test_build_tool_enumerates_tile_types() -> None:
    build = next(t for t in ACTION_TOOLS if t["name"] == "build")
    assert build["parameters"]["properties"]["tile_type"]["enum"] == TILE_TYPES


def test_step_tool_constrains_days_to_one_through_seven() -> None:
    step = next(t for t in ACTION_TOOLS if t["name"] == "step")
    days = step["parameters"]["properties"]["days"]
    assert days["minimum"] == 1 and days["maximum"] == 7


def test_action_tools_does_not_include_skip_or_set_plant_rate() -> None:
    """PRD explicitly drops both — `skip` is just `step` with no other
    actions, and `/control/plant` was dropped in v1."""
    names = {t["name"] for t in ACTION_TOOLS}
    assert "skip" not in names
    assert "set_plant_rate" not in names


def test_system_prompt_describes_step_final_requirement() -> None:
    """If the prompt doesn't mention that step must be last, the model
    won't reliably emit it. This is the contract that gates auto-step."""
    assert "step" in SYSTEM_PROMPT.lower()
    assert "last" in SYSTEM_PROMPT.lower()


# ---------- state_summary ---------------------------------------------------


def test_summarize_state_includes_top_lines_for_fresh_world() -> None:
    _, world = _client()
    s = world.state_dict()
    summary = summarize_state(s, forecast=None)
    assert "DAY" in summary
    assert "treasury" in summary
    assert "carbon_price" in summary


def test_summarize_state_token_budget_stays_under_target() -> None:
    """Soft target: ~1000 tokens. Use a 4-chars-per-token rough proxy and
    cap at 6000 chars (≈ 1500 tokens) to leave headroom for the system
    prompt and tool schemas. Fresh world should be well under this."""
    _, world = _client()
    # Play a few weeks so reservoirs, wells, and history are populated.
    api = ApiClient(transport=TestClient(create_app(world=world)))
    api.reset(seed=42)
    # /step caps at 7 days per request; two calls land us at day 14.
    api.step(days=7)
    api.step(days=7)
    s = api.state()
    fc = api.forecast(hours=24)
    summary = summarize_state(s, forecast=fc)
    assert len(summary) <= 6000, f"summary too long: {len(summary)} chars"


def test_summarize_state_renders_forecast_block_when_provided() -> None:
    api, _ = _client()
    api.reset(seed=42)
    forecast = api.forecast(hours=8)
    summary = summarize_state(api.state(), forecast=forecast)
    assert "FORECAST" in summary
    # Each forecast hour gets its own line.
    assert summary.count("h solar=") >= 1


# ---------- Dispatch -------------------------------------------------------


def test_dispatch_step_advances_the_world() -> None:
    api, world = _client()
    mock = MockLLM(responses=[_resp([ToolCall("step", {"days": 1})])])
    agent = LLMReactAgent(api, seed=42, llm=mock)
    agent.api.reset(seed=42)
    state = agent.api.state()
    days_before = state["day"]
    agent.decide(state, forecast=None, game_days=10)
    assert agent.api.state()["day"] == days_before + 1


def test_dispatch_build_call_creates_tile() -> None:
    api, world = _client()
    api.reset(seed=42)
    # Town hall is at center; build a road one tile north of it so it has
    # connectivity. State exposes town_hall position via tiles list.
    s = api.state()
    th = next(t for t in s["tiles"] if t["type"] == "town_hall")
    x, y = th["x"], th["y"] + 1
    mock = MockLLM(
        responses=[
            _resp(
                [
                    ToolCall("build", {"tile_type": "road", "x": x, "y": y}),
                    ToolCall("step", {"days": 1}),
                ]
            )
        ]
    )
    agent = LLMReactAgent(api, seed=42, llm=mock)
    agent.decide(api.state(), forecast=None, game_days=10)
    tiles_after = api.state()["tiles"]
    assert any(t["type"] == "road" and t["x"] == x and t["y"] == y for t in tiles_after)


def test_dispatch_swallows_malformed_tool_arguments() -> None:
    """A bad argument from the LLM (missing required field, wrong type)
    must not crash the agent — just skip the call and move on."""
    api, _ = _client()
    api.reset(seed=42)
    bad = ToolCall("build", {"tile_type": "road"})  # missing x/y
    mock = MockLLM(responses=[_resp([bad, ToolCall("step", {"days": 1})])])
    agent = LLMReactAgent(api, seed=42, llm=mock)
    # Should not raise — the malformed call is silently dropped, step still fires.
    agent.decide(api.state(), forecast=None, game_days=10)
    assert api.state()["day"] == 1


def test_dispatch_unknown_tool_name_is_silently_ignored() -> None:
    api, _ = _client()
    api.reset(seed=42)
    mock = MockLLM(
        responses=[_resp([ToolCall("hallucinate", {"x": 1}), ToolCall("step", {"days": 1})])]
    )
    agent = LLMReactAgent(api, seed=42, llm=mock)
    agent.decide(api.state(), forecast=None, game_days=10)
    assert api.state()["day"] == 1


def test_dispatch_clamps_step_days_to_seven() -> None:
    """If the LLM passes days=99, we clamp to 7. The world's StepBody
    pydantic schema would reject anything >7 with a 422."""
    api, _ = _client()
    api.reset(seed=42)
    mock = MockLLM(responses=[_resp([ToolCall("step", {"days": 99})])])
    agent = LLMReactAgent(api, seed=42, llm=mock)
    agent.decide(api.state(), forecast=None, game_days=100)
    assert api.state()["day"] == 7


def test_dispatch_clamps_step_days_to_remaining_game_days() -> None:
    """End-of-game: never overshoot. If 3 days are left, days=7 ⇒ 3."""
    api, _ = _client()
    api.reset(seed=42)
    api.step(days=7)  # day = 7
    mock = MockLLM(responses=[_resp([ToolCall("step", {"days": 7})])])
    agent = LLMReactAgent(api, seed=42, llm=mock)
    agent.decide(api.state(), forecast=None, game_days=10)  # 3 days remaining
    assert api.state()["day"] == 10


# ---------- Step fallback --------------------------------------------------


def test_play_game_emits_fallback_step_when_llm_omits_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LLM is supposed to emit `step` as the final tool call, but
    if it doesn't (e.g., returned an empty tool_calls list), the harness
    advances days=7 so the world doesn't hang."""
    # Cap the game at a few weeks via env so we don't run a full 10-year sim.
    monkeypatch.setenv("GAME_DAYS", "14")
    monkeypatch.setenv("MANUAL_GAME_DAYS", "14")
    api2, _ = _client(world=World())
    mock = MockLLM(responses=[_resp([])])  # never emits step
    agent = LLMReactAgent(api2, seed=42, llm=mock)
    final = agent.play_game()
    assert final["day"] == 14


# ---------- Token tracking -------------------------------------------------


def test_cumulative_tokens_accumulate_across_turns() -> None:
    api, _ = _client()
    mock = MockLLM(
        responses=[
            _resp([ToolCall("step", {"days": 1})], in_tok=100, out_tok=20),
            _resp([ToolCall("step", {"days": 1})], in_tok=110, out_tok=22),
        ]
    )
    agent = LLMReactAgent(api, seed=42, llm=mock)
    agent.api.reset(seed=42)
    agent.decide(agent.api.state(), forecast=None, game_days=10)
    agent.decide(agent.api.state(), forecast=None, game_days=10)
    assert agent.cumulative_tokens == 100 + 20 + 110 + 22


def test_token_budget_warning_logs_to_stderr_once_over_threshold() -> None:
    """At 800K cumulative tokens the agent emits a single stderr warning.
    Subsequent turns do not re-warn (would be noise)."""
    api, _ = _client()
    api.reset(seed=42)
    big = _resp([ToolCall("step", {"days": 1})], in_tok=600_000, out_tok=210_000)
    later = _resp([ToolCall("step", {"days": 1})], in_tok=10, out_tok=10)
    mock = MockLLM(responses=[big, later, later])
    buf = io.StringIO()
    agent = LLMReactAgent(api, seed=42, llm=mock, stderr=buf)
    state = api.state()
    agent.decide(state, forecast=None, game_days=10)  # crosses threshold
    agent.decide(api.state(), forecast=None, game_days=10)  # still over, no re-warn
    output = buf.getvalue()
    assert "exceeded 80%" in output
    assert output.count("exceeded 80%") == 1


def test_token_budget_no_warning_under_threshold() -> None:
    api, _ = _client()
    api.reset(seed=42)
    mock = MockLLM(responses=[_resp([ToolCall("step", {"days": 1})], in_tok=100, out_tok=20)])
    buf = io.StringIO()
    agent = LLMReactAgent(api, seed=42, llm=mock, stderr=buf)
    agent.decide(api.state(), forecast=None, game_days=10)
    assert buf.getvalue() == ""


# ---------- End-to-end smoke ----------------------------------------------


def test_short_game_runs_to_completion_with_mock_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock LLM emits a build-then-step pattern; the agent runs a short
    14-day game to completion without crashing."""
    monkeypatch.setenv("GAME_DAYS", "14")
    monkeypatch.setenv("MANUAL_GAME_DAYS", "14")
    api = ApiClient(transport=TestClient(create_app(world=World())))
    api.reset(seed=42)
    s = api.state()
    th = next(t for t in s["tiles"] if t["type"] == "town_hall")
    plan: list[Any] = [
        _resp(
            [
                ToolCall("build", {"tile_type": "road", "x": th["x"] + 1, "y": th["y"]}),
                ToolCall("step", {"days": 7}),
            ]
        ),
        _resp([ToolCall("step", {"days": 7})]),
    ]
    mock = MockLLM(responses=plan)
    agent = LLMReactAgent(api, seed=42, llm=mock)
    final = agent.play_game()
    assert final["day"] == 14
    assert agent.turns >= 1


# ---------- LLM construction sanity ----------------------------------------


def test_agent_defers_llm_construction_when_passed_explicitly() -> None:
    """Passing llm= avoids the make_llm_from_env path, so the agent is
    constructible without LLM_API_KEY in the environment."""
    api, _ = _client()
    mock = MockLLM(responses=[_resp([ToolCall("step", {"days": 1})])])
    # Would raise inside make_llm_from_env if it ran. The mock arg short-circuits it.
    agent = LLMReactAgent(api, seed=42, llm=mock)
    assert agent.llm is mock


def test_agent_requires_env_key_when_llm_not_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    api, _ = _client()
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        LLMReactAgent(api, seed=42)

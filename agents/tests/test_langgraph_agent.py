"""Tests for the LangGraph reference agent's 5-node graph + rule critic.

LangGraph is an OPTIONAL dependency (declared under
`[project.optional-dependencies.llm]`). When it isn't installed, the
whole module skips — AFK CI without the extra installed still passes.

Coverage:
- One unit test per critic rule (2 tests) — pure functions, no graph.
- `_route_after_critique` returns the `plan` target on full rejection.
- `_route_after_critique` routes forward to `execute` on partial rejection.
- The 1-retry cap is honored (second full rejection proceeds to execute).
- Rejection reasons appear in the user message on the re-plan pass.
- `_execute` silently skips unknown tool names.
- One MockLLM-driven end-to-end smoke test that reaches game_days.
- The CLI raises when `LLM_API_KEY` is missing (same as ReAct).
"""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph")  # noqa: E402  — skip whole module if missing.

from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from agents.api_client import ApiClient
from agents.langgraph_agent import LangGraphAgent
from agents.langgraph_agent.agent import out_of_bounds, tile_occupied
from agents.llm import LLMResponse, MockLLM, ToolCall, Usage
from world.api import create_app
from world.sim import World


def _make_client(world: World | None = None) -> tuple[ApiClient, World]:
    w = world or World()
    return ApiClient(transport=TestClient(create_app(world=w))), w


def _resp(tool_calls: list[ToolCall], *, in_tok: int = 5, out_tok: int = 2) -> LLMResponse:
    return LLMResponse(tool_calls=tool_calls, text="", usage=Usage(in_tok, out_tok))


def _step_only_mock() -> MockLLM:
    return MockLLM(responses=[_resp([ToolCall("step", {"days": 7})])])


# ---------- Critic rules (pure functions) ---------------------------------


def test_rule_out_of_bounds_rejects_negative_or_oversize_coords() -> None:
    state_view = {"config": {"world_w": 16, "world_h": 16}}
    reason = out_of_bounds(ToolCall("build", {"tile_type": "road", "x": 20, "y": 5}), state_view)
    assert reason is not None and "out_of_bounds" in reason
    # In-bounds is None.
    assert (
        out_of_bounds(ToolCall("build", {"tile_type": "road", "x": 5, "y": 5}), state_view) is None
    )
    # Rule ignores non-coord-bearing tools.
    assert out_of_bounds(ToolCall("set_well_rate", {"well_id": "w-1"}), state_view) is None


def test_rule_tile_occupied_rejects_build_on_existing_tile() -> None:
    state_view = {
        "config": {"world_w": 16, "world_h": 16},
        "tiles": [{"x": 5, "y": 5, "type": "house"}],
    }
    reason = tile_occupied(ToolCall("build", {"tile_type": "road", "x": 5, "y": 5}), state_view)
    assert reason is not None and "tile_occupied" in reason
    assert (
        tile_occupied(ToolCall("build", {"tile_type": "road", "x": 6, "y": 5}), state_view) is None
    )
    # Non-build calls bypass the rule.
    assert tile_occupied(ToolCall("survey", {"x": 5, "y": 5}), state_view) is None


# ---------- Routing -------------------------------------------------------


def test_critique_back_edge_fires_on_full_rejection() -> None:
    api, _ = _make_client()
    agent = LangGraphAgent(api, seed=42, llm=_step_only_mock())
    api.reset(seed=42)
    obs = api.state()
    # Single mutator call that the critic will reject (out_of_bounds).
    out = agent._critique(
        {
            "pending_calls": [ToolCall("build", {"tile_type": "road", "x": 9999, "y": 9999})],
            "obs": obs,
        }
    )
    assert out["survivors"] == []
    assert any("out_of_bounds" in r for r in out["rejections"])
    route = agent._route_after_critique(
        {
            "pending_calls": [ToolCall("build", {"tile_type": "road", "x": 9999, "y": 9999})],
            "survivors": out["survivors"],
            "rejections": out["rejections"],
            "replan_retries": 0,
        }
    )
    assert route == "plan"


def test_critique_routes_forward_to_execute_on_partial_rejection() -> None:
    api, _ = _make_client()
    agent = LangGraphAgent(api, seed=42, llm=_step_only_mock())
    api.reset(seed=42)
    obs = api.state()
    th = next(t for t in obs["tiles"] if t["type"] == "town_hall")
    pending = [
        ToolCall("build", {"tile_type": "road", "x": th["x"] + 1, "y": th["y"]}),  # OK
        ToolCall("build", {"tile_type": "road", "x": 9999, "y": 9999}),  # rejected
    ]
    out = agent._critique({"pending_calls": pending, "obs": obs})
    assert len(out["survivors"]) == 1
    assert out["survivors"][0].arguments["x"] == th["x"] + 1
    assert len(out["rejections"]) == 1
    route = agent._route_after_critique(
        {
            "pending_calls": pending,
            "survivors": out["survivors"],
            "rejections": out["rejections"],
            "replan_retries": 0,
        }
    )
    assert route == "execute"


def test_replan_cap_of_one_is_honored() -> None:
    api, _ = _make_client()
    agent = LangGraphAgent(api, seed=42, llm=_step_only_mock())
    api.reset(seed=42)
    obs = api.state()
    pending = [ToolCall("build", {"tile_type": "road", "x": 9999, "y": 9999})]
    out = agent._critique({"pending_calls": pending, "obs": obs})
    # Already retried once — must route forward to execute even though
    # this critique was a full rejection.
    route = agent._route_after_critique(
        {
            "pending_calls": pending,
            "survivors": out["survivors"],
            "rejections": out["rejections"],
            "replan_retries": 1,
        }
    )
    assert route == "execute"


def test_rejection_reasons_appear_in_replan_user_message() -> None:
    api, _ = _make_client()
    api.reset(seed=42)
    obs = api.state()
    captured: dict[str, str] = {}

    class CapturingMock(MockLLM):
        def chat(
            self,
            *,
            system: str,
            user: str,
            tools: list[dict[str, Any]],
            max_tokens: int = 2048,
        ) -> LLMResponse:
            captured["user"] = user
            return super().chat(system=system, user=user, tools=tools, max_tokens=max_tokens)

    mock = CapturingMock(responses=[_resp([ToolCall("step", {"days": 1})])])
    agent = LangGraphAgent(api, seed=42, llm=mock)
    from agents.langgraph_agent.agent import GraphState

    state: GraphState = {
        "obs": obs,
        "forecast": None,
        "day": 0,
        "game_days": 14,
        "cumulative_tokens": 0,
        "turn": 0,
        "rejections": ["build(road,9999,9999) out_of_bounds (world 16x16)"],
        "replan_retries": 0,
    }
    agent._plan(state)
    assert "out_of_bounds" in captured["user"]
    assert "ALL rejected" in captured["user"]


def test_execute_silently_skips_unknown_tool_names() -> None:
    api, _ = _make_client()
    agent = LangGraphAgent(api, seed=42, llm=_step_only_mock())
    api.reset(seed=42)
    pre_tile_count = len(api.state()["tiles"])
    agent._execute({"survivors": [ToolCall("hallucinate", {"foo": "bar"})]})
    # No crash, no state change.
    assert len(api.state()["tiles"]) == pre_tile_count


# ---------- End-to-end smoke ----------------------------------------------


def test_short_game_runs_to_completion_with_mock_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GAME_DAYS", "14")
    monkeypatch.setenv("MANUAL_GAME_DAYS", "14")
    api = ApiClient(transport=TestClient(create_app(world=World())))
    th_state = World().state_dict()
    th = next(t for t in th_state["tiles"] if t["type"] == "town_hall")
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
    agent = LangGraphAgent(api, seed=42, llm=mock)
    final = agent.play_game()
    assert final["day"] == 14
    assert agent.turns >= 1
    assert agent.cumulative_tokens > 0


_LLM_ENV_VARS = (
    "LLM_API_KEY",
    "LLM_PROVIDER",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "NIM_BASE_URL",
    "NIM_CHAT_TEMPLATE_KWARGS",
    "NVIDIA_API_KEY",
)


def _isolate_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`test_nim_live.py` calls `load_dotenv` at import time, which leaks
    the user's local `.env` into `os.environ`. Tests that expect the
    openai branch (the default) must clear every var that would route
    the factory elsewhere — otherwise the assertion silently fails."""
    for var in _LLM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_agent_requires_llm_when_env_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    api, _ = _make_client()
    _isolate_llm_env(monkeypatch)
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        LangGraphAgent(api, seed=42)


def test_cli_raises_without_llm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """No MockLLM offline fallback — running without a key must raise."""
    from agents.langgraph_agent import agent as agent_module

    _isolate_llm_env(monkeypatch)
    # `main` mutates os.environ; monkeypatch restores it after the test
    # so the GAME_DAYS / MANUAL_GAME_DAYS knobs don't leak into the
    # scripted-agent smoke tests.
    monkeypatch.setenv("GAME_DAYS", "1")
    monkeypatch.setenv("MANUAL_GAME_DAYS", "1")
    # Patch in-process client construction so we don't accidentally hit a live URL.
    with (
        patch.object(agent_module, "_make_inprocess_client", _make_client_for_cli),
        pytest.raises(RuntimeError, match="LLM_API_KEY"),
    ):
        agent_module.main(["--seed", "42", "--days", "1"])


def _make_client_for_cli() -> ApiClient:
    api, _ = _make_client()
    return api

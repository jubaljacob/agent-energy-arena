"""Tests for `agents.attach_runtime` + the LLM agents' attach-mode `act()`.

`drive_one_turn` is the single helper used by `act()` in Agent Play: one
LLM call per `/step`, dispatch every non-`step` tool the model emits,
let the human's `/step` handler advance the clock. Without it both
`LLMReactAgent` and `LangGraphAgent` would inherit `BaseAgent.act`'s
no-op and contribute nothing per `/step`.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agents.api_client import ApiClient, UiAgentApiClient
from agents.attach_runtime import drive_one_turn
from agents.llm import LLMResponse, MockLLM, ToolCall, Usage
from agents.llm_react import LLMReactAgent
from agents.prompts import ACTION_TOOLS, SYSTEM_PROMPT
from world.api import create_app
from world.sim import World


def _resp(calls: list[ToolCall]) -> LLMResponse:
    return LLMResponse(tool_calls=calls, text="", usage=Usage(0, 0))


def _ui_api(world: World) -> UiAgentApiClient:
    """Attach-mode api client: `step`/`reset`/`attach_scenario` raise."""
    return UiAgentApiClient(transport=TestClient(create_app(world=world)))


# ---------- drive_one_turn ------------------------------------------------


def test_drive_one_turn_dispatches_non_step_tool_calls() -> None:
    """The LLM emits `build` + `step`; drive_one_turn must apply the build
    and silently drop the step (human owns the clock in attach mode)."""
    world = World()
    api = ApiClient(transport=TestClient(create_app(world=world)))
    api.reset(seed=42)

    th = next(t for t in api.state()["tiles"] if t["type"] == "town_hall")
    target = (th["x"], th["y"] + 1)
    mock = MockLLM(
        responses=[
            _resp(
                [
                    ToolCall("build", {"tile_type": "road", "x": target[0], "y": target[1]}),
                    ToolCall("step", {"days": 1}),
                ]
            )
        ]
    )

    day_before = api.state()["day"]
    drive_one_turn(
        api,
        api.state(),
        mock,
        system_prompt=SYSTEM_PROMPT,
        action_tools=ACTION_TOOLS,
        max_tokens=128,
    )
    state_after = api.state()
    assert state_after["day"] == day_before  # step was dropped
    assert any(t["type"] == "road" and (t["x"], t["y"]) == target for t in state_after["tiles"])


def test_drive_one_turn_swallows_world_rejections() -> None:
    """A malformed build (out-of-bounds) returns a 4xx envelope; the
    helper must continue rather than crash the turn."""
    world = World()
    api = ApiClient(transport=TestClient(create_app(world=world)))
    api.reset(seed=42)

    mock = MockLLM(
        responses=[_resp([ToolCall("build", {"tile_type": "road", "x": 9999, "y": 9999})])]
    )
    # No raise — confirms the dispatcher handles the world-side error.
    drive_one_turn(
        api,
        api.state(),
        mock,
        system_prompt=SYSTEM_PROMPT,
        action_tools=ACTION_TOOLS,
        max_tokens=128,
    )


# ---------- LLMReactAgent.act under attach --------------------------------


def test_llm_react_act_calls_llm_on_day_zero() -> None:
    """No more deterministic prime — the LLM plays natively from day 0.
    A `build` emitted by the model on the freshly-reset world must
    land in state, and the LLM must actually be consulted (no shortcut)."""
    world = World()
    api = _ui_api(world)
    ApiClient(transport=TestClient(create_app(world=world))).reset(seed=42)

    th = next(t for t in world.state_dict()["tiles"] if t["type"] == "town_hall")
    road_xy = (th["x"] + 1, th["y"])  # orthogonally adjacent to the town hall
    mock = MockLLM(
        responses=[
            _resp(
                [
                    ToolCall("build", {"tile_type": "road", "x": road_xy[0], "y": road_xy[1]}),
                    ToolCall("step", {"days": 7}),  # MUST be dropped
                ]
            )
        ]
    )
    agent = LLMReactAgent(api, seed=42, llm=mock)
    agent.act(world.state_dict())

    assert len(mock.calls) == 1, "LLM was not consulted"
    tiles = world.state_dict()["tiles"]
    assert any(t["type"] == "road" and (t["x"], t["y"]) == road_xy for t in tiles), (
        "LLM-driven build did not land on day 0"
    )
    # The world clock did NOT advance from inside act() — `step` was dropped.
    assert world.state_dict()["day"] == 0


def test_llm_react_act_calls_llm_on_subsequent_days() -> None:
    """Every `/step` triggers a fresh LLM call — the agent should not
    fall silent after day 0."""
    world = World()
    api = _ui_api(world)
    real = ApiClient(transport=TestClient(create_app(world=world)))
    real.reset(seed=42)
    real.step(days=1)  # day=1

    mock = MockLLM(responses=[_resp([])])
    agent = LLMReactAgent(api, seed=42, llm=mock)
    agent.act(world.state_dict())
    assert len(mock.calls) == 1


def test_llm_react_act_accumulates_tokens_from_attach_turns() -> None:
    """Attach-mode `act()` shares the same 1M-token envelope as CLI
    `decide()` calls — the cumulative_tokens counter must include
    attach-turn usage so the 80%-budget warning fires at the right time."""
    world = World()
    api = _ui_api(world)
    ApiClient(transport=TestClient(create_app(world=world))).reset(seed=42)

    mock = MockLLM(responses=[LLMResponse(tool_calls=[], text="", usage=Usage(123, 45))])
    agent = LLMReactAgent(api, seed=42, llm=mock)
    agent.act(world.state_dict())
    assert agent.cumulative_tokens == 123 + 45


def test_llm_react_attached_step_lands_llm_builds_in_state() -> None:
    """End-to-end: attach the agent, fire one /step. The LLM's emitted
    build must be visible in /state — no prime tiles, no hidden state."""
    world = World()
    client = TestClient(create_app(world=world))
    client.post("/reset", json={"seed": 42})

    th = next(t for t in client.get("/state").json()["tiles"] if t["type"] == "town_hall")
    road_xy = (th["x"] + 1, th["y"])
    mock = MockLLM(
        responses=[
            _resp(
                [
                    ToolCall("build", {"tile_type": "road", "x": road_xy[0], "y": road_xy[1]}),
                ]
            )
        ]
    )
    agent = LLMReactAgent(
        UiAgentApiClient(transport=client),
        seed=42,
        llm=mock,
    )
    client.app.state.attached_agent = agent  # type: ignore[attr-defined]

    r = client.post("/step", json={"days": 1})
    assert r.status_code == 200, r.text

    tiles = client.get("/state").json()["tiles"]
    types = [t["type"] for t in tiles]
    # No deterministic prime — the world should only contain the town
    # hall plus whatever the LLM emitted this turn.
    assert types.count("road") == 1
    assert "gas_peaker" not in types
    assert "solar_farm" not in types
    assert any(t["type"] == "road" and (t["x"], t["y"]) == road_xy for t in tiles)

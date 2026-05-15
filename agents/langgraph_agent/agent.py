"""LangGraph reference agent — full-API showcase (issue 19).

Where `agents/llm_react.py` is a minimal single-call ReAct loop, this
agent is a **graph-based example** that explicitly walks through every
endpoint of the world API so participants have a how-to for each.

Optimising score is out of scope. The point is to demonstrate one turn
of a graph that:

  observe → summarise → plan → (dispatch branches) → step → loop

Each dispatch branch is its own node so the reader can see the
"per-tool-call" wiring. The agent reuses `agents.llm.LLMClient` /
`agents.prompts.ACTION_TOOLS` / `agents.state_summary.summarize_state`
from slice 15 unchanged — there is no new LLM stack.

The `langgraph` package is an OPTIONAL dependency declared under
`[project.optional-dependencies.llm]`. Install with `pip install -e
".[llm]"`. Running the agent without it raises a clear error at
construction time.

CLI:
  python -m agents.langgraph_agent.agent --seed 42 --days 30   # short demo
  python -m agents.langgraph_agent.agent --seed 42 --full      # full game

When LLM_API_KEY is unset, the CLI plugs in a `MockLLM` that loops
`step(days=7)` so the offline demo finishes deterministically.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from collections.abc import Hashable
from pathlib import Path
from typing import Any, TypedDict

from agents.api_client import ApiClient
from agents.base import BaseAgent
from agents.llm import LLMClient, LLMResponse, MockLLM, ToolCall, Usage, make_llm_from_env
from agents.prompts import ACTION_TOOLS, SYSTEM_PROMPT
from agents.state_summary import summarize_state

DEFAULT_STEP_DAYS_FALLBACK: int = 7
MAX_TOKENS_PER_TURN: int = 2048
FORECAST_HOURS: int = 24

DISPATCH_TOOLS: tuple[str, ...] = (
    "build",
    "demolish",
    "survey",
    "drill",
    "set_well_rate",
    "set_refinery_rate",
)


class GraphState(TypedDict, total=False):
    """Per-turn state that flows through the LangGraph nodes."""

    day: int
    game_days: int
    obs: dict[str, Any]
    forecast: list[dict[str, Any]] | None
    events: dict[str, Any]
    reservoirs: dict[str, Any]
    summary: str
    pending_calls: list[ToolCall]
    step_days: int
    last_envelope: dict[str, Any] | None
    cumulative_tokens: int
    turn: int


class LangGraphAgent(BaseAgent):
    """Graph-based reference agent. Implements the `Agent` protocol
    (`__init__(api, *, seed=None)` + `play_game() -> dict`).

    Subclasses `BaseAgent` so the Agent Play attach handler accepts it,
    but overrides `play_game()` entirely — the per-turn `act(state)`
    hook is the no-op inherited default. Agent Play attach therefore
    loads the class without errors but the agent contributes nothing
    per `/step`; use the CLI (`python -m agents.langgraph_agent.agent`)
    for a full game run.
    """

    def __init__(
        self,
        api: ApiClient,
        *,
        seed: int | None = None,
        llm: LLMClient | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        action_tools: list[dict[str, Any]] | None = None,
        max_tokens_per_turn: int = MAX_TOKENS_PER_TURN,
    ) -> None:
        self.api = api
        self._seed = seed
        self.llm: LLMClient = llm if llm is not None else make_llm_from_env()
        self.system_prompt: str = system_prompt
        self.action_tools: list[dict[str, Any]] = action_tools or ACTION_TOOLS
        self.max_tokens_per_turn: int = max_tokens_per_turn
        self.cumulative_tokens: int = 0
        self.turns: int = 0
        self.catalog: dict[str, Any] | None = None
        self.final_score: dict[str, Any] | None = None
        self.graph = self._build_graph()

    # -- Graph construction ----------------------------------------------

    def _build_graph(self) -> Any:
        try:
            from langgraph.graph import END, START, StateGraph
        except ImportError as exc:
            raise RuntimeError(
                "langgraph is not installed — install the optional 'llm' extra: "
                'pip install -e ".[llm]"'
            ) from exc

        g = StateGraph(GraphState)
        g.add_node("observe", self._observe)
        g.add_node("summarise", self._summarise)
        g.add_node("plan", self._plan)
        for name in DISPATCH_TOOLS:
            g.add_node(name, self._make_dispatch_node(name))
        g.add_node("step", self._step)

        g.add_edge(START, "observe")
        g.add_edge("observe", "summarise")
        g.add_edge("summarise", "plan")
        # `plan` fans out to one of the dispatch nodes or directly to `step`.
        g.add_conditional_edges("plan", self._route_next, _route_targets())
        # Each dispatch node loops back to `plan`'s router so chained calls
        # (build, build, step) run one per visit.
        for name in DISPATCH_TOOLS:
            g.add_conditional_edges(name, self._route_next, _route_targets())
        # `step` decides whether to continue or end the game.
        g.add_conditional_edges("step", self._loop, {"observe": "observe", "end": END})

        return g.compile()

    # -- Public entry ----------------------------------------------------

    def play_game(self) -> dict[str, Any]:
        """Reset, fetch /catalog once, invoke the graph, fetch /score."""
        self.api.reset(seed=self._seed)
        # /catalog is read once on startup — purely informational; the
        # planner consults it to know which tile_types are legal. We
        # don't pass it through graph state because it doesn't change.
        try:
            self.catalog = self.api.catalog()
        except RuntimeError:
            self.catalog = None

        initial_state = self.api.state()
        game_days = int(
            initial_state["config"].get("active_game_days", initial_state["config"]["game_days"])
        )

        # Recursion limit: LangGraph defaults to 25 super-steps. We need
        # roughly (turns × nodes_per_turn) + slack. nodes_per_turn ≈ 6
        # (observe / summarise / plan / dispatch / step / loop).
        recursion_limit = max(50, (game_days + 7) * 12)

        final: GraphState = self.graph.invoke(
            {
                "day": int(initial_state.get("day", 0)),
                "game_days": game_days,
                "cumulative_tokens": 0,
                "turn": 0,
            },
            config={"recursion_limit": recursion_limit},
        )

        self.cumulative_tokens = int(final.get("cumulative_tokens", 0))
        self.turns = int(final.get("turn", 0))

        # /score is read once at game end. 404 (no baseline file) is
        # expected for non-canonical seeds — surface as None.
        try:
            self.final_score = self.api.score()
        except RuntimeError:
            self.final_score = None

        end_state: dict[str, Any] = final.get("obs") or self.api.state()
        return end_state

    # -- Nodes -----------------------------------------------------------

    def _observe(self, state: GraphState) -> GraphState:
        """Fetch /state + /forecast + /events + /reservoirs in one node."""
        obs = self.api.state()
        forecast = _safe(self.api.forecast, hours=FORECAST_HOURS)
        events = _safe_dict(self.api.events)
        reservoirs = _safe_dict(self.api.reservoirs, top_k=30)
        return {
            "obs": obs,
            "forecast": forecast,
            "events": events,
            "reservoirs": reservoirs,
            "day": int(obs.get("day", state.get("day", 0))),
        }

    def _summarise(self, state: GraphState) -> GraphState:
        """Compress the observations for the LLM. Appends one-line summaries
        of the /events and /reservoirs payloads so participants see that
        those endpoints feed the prompt context."""
        summary = summarize_state(state.get("obs", {}), state.get("forecast"))
        events = state.get("events") or {}
        if events.get("active") or events.get("historical"):
            summary += (
                f"\nendpoint:/events active={len(events.get('active') or [])} "
                f"historical={len(events.get('historical') or [])}"
            )
        reservoirs = state.get("reservoirs") or {}
        top = reservoirs.get("top_k") or []
        if top:
            summary += f"\nendpoint:/reservoirs top_k_revealed={len(top)}"
        return {"summary": summary}

    def _plan(self, state: GraphState) -> GraphState:
        """One LLM call. Splits the tool calls into a `pending_calls` queue
        of mutating actions and a `step_days` value picked off the step call
        (or the fallback default if the model omits it)."""
        response = self.llm.chat(
            system=self.system_prompt,
            user=state.get("summary", ""),
            tools=self.action_tools,
            max_tokens=self.max_tokens_per_turn,
        )

        pending: list[ToolCall] = []
        step_days = DEFAULT_STEP_DAYS_FALLBACK
        saw_step = False
        for call in response.tool_calls:
            if call.name == "step":
                step_days = _clamp_days(call.arguments.get("days", DEFAULT_STEP_DAYS_FALLBACK))
                saw_step = True
                break  # step terminates the turn — ignore anything after it
            if call.name in DISPATCH_TOOLS:
                pending.append(call)
        if not saw_step:
            step_days = DEFAULT_STEP_DAYS_FALLBACK

        remaining = max(1, state.get("game_days", 0) - state.get("day", 0))
        step_days = min(step_days, remaining)

        return {
            "pending_calls": pending,
            "step_days": step_days,
            "cumulative_tokens": int(state.get("cumulative_tokens", 0))
            + response.usage.input_tokens
            + response.usage.output_tokens,
            "turn": int(state.get("turn", 0)) + 1,
        }

    def _route_next(self, state: GraphState) -> str:
        """Conditional edge: which dispatch node consumes the next call
        from `pending_calls`. If the queue is empty, advance to `step`."""
        pending = state.get("pending_calls") or []
        if not pending:
            return "step"
        head = pending[0]
        if head.name in DISPATCH_TOOLS:
            return head.name
        # Unknown / hallucinated tool name — drop it and re-route.
        return "step"

    def _make_dispatch_node(self, tool_name: str) -> Any:
        """Build one dispatch node per tool. Pops the head call off
        `pending_calls`, fires the matching ApiClient method, and writes
        the API envelope back to `last_envelope` for transparency."""

        def node(state: GraphState) -> GraphState:
            pending = list(state.get("pending_calls") or [])
            if not pending:
                return {"pending_calls": []}
            call = pending[0]
            rest = pending[1:]
            envelope: dict[str, Any] | None = None
            try:
                envelope = self._dispatch_one(call)
            except RuntimeError:
                # /build, /survey, etc. may 422 on bad payloads. Skip.
                envelope = None
            except (KeyError, TypeError, ValueError):
                envelope = None
            return {"pending_calls": rest, "last_envelope": envelope}

        node.__name__ = f"dispatch_{tool_name}"
        return node

    def _step(self, state: GraphState) -> GraphState:
        """Advance the world by `state.step_days` and refresh `day`."""
        days = max(1, int(state.get("step_days", DEFAULT_STEP_DAYS_FALLBACK)))
        remaining = max(1, state.get("game_days", 0) - state.get("day", 0))
        days = min(days, remaining)
        with contextlib.suppress(RuntimeError):
            self.api.step(days=days)
        new_state = self.api.state()
        return {"obs": new_state, "day": int(new_state.get("day", 0))}

    def _loop(self, state: GraphState) -> str:
        return "observe" if state.get("day", 0) < state.get("game_days", 0) else "end"

    # -- Dispatch helper -------------------------------------------------

    def _dispatch_one(self, call: ToolCall) -> dict[str, Any]:
        a = call.arguments
        if call.name == "build":
            return self.api.build(tile_type=str(a["tile_type"]), x=int(a["x"]), y=int(a["y"]))
        if call.name == "demolish":
            return self.api.demolish(x=int(a["x"]), y=int(a["y"]))
        if call.name == "survey":
            return self.api.survey(x=int(a["x"]), y=int(a["y"]), size=int(a.get("size", 8)))
        if call.name == "drill":
            return self.api.drill(
                x=int(a["x"]),
                y=int(a["y"]),
                target_z=int(a["target_z"]),
                well_type=str(a.get("well_type", "production")),
            )
        if call.name == "set_well_rate":
            return self.api.control_well(
                well_id=str(a["well_id"]),
                rate_bbl_day=float(a["rate_bbl_day"]),
            )
        if call.name == "set_refinery_rate":
            return self.api.control_refinery(
                refinery_id=str(a["refinery_id"]),
                rate_bbl_day=float(a["rate_bbl_day"]),
            )
        return {}


# ---------- helpers --------------------------------------------------------


def _route_targets() -> dict[Hashable, str]:
    """Conditional-edge target map shared by `plan` and every dispatch
    node. Re-built each call so the literal stays close to the routing
    function for reader clarity."""
    targets: dict[Hashable, str] = {name: name for name in DISPATCH_TOOLS}
    targets["step"] = "step"
    return targets


def _safe(fn: Any, **kwargs: Any) -> Any:
    try:
        return fn(**kwargs)
    except RuntimeError:
        return None


def _safe_dict(fn: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        result = fn(**kwargs)
    except RuntimeError:
        return {}
    return result if isinstance(result, dict) else {}


def _clamp_days(raw: Any) -> int:
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_STEP_DAYS_FALLBACK
    return max(1, min(7, days))


# ---------- CLI driver -----------------------------------------------------


def _make_inprocess_client() -> ApiClient:
    from fastapi.testclient import TestClient

    from world.api import create_app

    return ApiClient(transport=TestClient(create_app()))


def _mock_llm_offline() -> MockLLM:
    """Loop a single step(days=7) response so an offline demo runs to
    completion without crashing. The MockLLM repeats its tail response
    indefinitely (see agents.llm.MockLLM)."""
    return MockLLM(
        responses=[
            LLMResponse(
                tool_calls=[ToolCall("step", {"days": 7})],
                text="",
                usage=Usage(0, 0),
            )
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LangGraph reference agent (full-API tour).")
    parser.add_argument("--seed", type=int, default=42, help="World seed (default 42).")
    parser.add_argument("--days", type=int, default=30, help="Cap game length (default 30).")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run the full configured game length (overrides --days).",
    )
    parser.add_argument("--api-url", type=str, default=None, help="Live world URL (else in-proc).")
    parser.add_argument("--output", type=Path, default=None, help="Write summary JSON here.")
    args = parser.parse_args(argv)

    if not args.full:
        os.environ["GAME_DAYS"] = str(args.days)
        os.environ["MANUAL_GAME_DAYS"] = str(args.days)

    api = ApiClient(base_url=args.api_url) if args.api_url else _make_inprocess_client()

    llm: LLMClient
    if os.environ.get("LLM_API_KEY"):
        llm = make_llm_from_env()
    else:
        print("LLM_API_KEY not set — running offline with MockLLM (step-only).", file=sys.stderr)
        llm = _mock_llm_offline()

    agent = LangGraphAgent(api, seed=args.seed, llm=llm)
    final = agent.play_game()

    payload = {
        "seed": args.seed,
        "day": int(final.get("day", 0)),
        "population": int(final.get("population", 0)),
        "treasury": float(final.get("treasury", 0.0)),
        "turns": agent.turns,
        "cumulative_tokens": agent.cumulative_tokens,
        "score": agent.final_score,
    }
    print(json.dumps(payload, indent=2))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2) + "\n")
    return 0


# Agent Play attach contract: the handler prefers a top-level `Agent`
# symbol that is a BaseAgent subclass (`world.api.post_agent_attach`).
Agent = LangGraphAgent


if __name__ == "__main__":
    raise SystemExit(main())

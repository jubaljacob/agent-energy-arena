"""LLM ReAct reference agent.

Each turn:
  1. Fetch /state + /forecast.
  2. Compress with `summarize_state(obs, forecast)`.
  3. Send SYSTEM_PROMPT + summary to the LLM with `tools=ACTION_TOOLS`.
  4. Dispatch each returned tool call to the matching API endpoint.
  5. The model's final tool call must be `step` — that advances the
     world. If the model omits `step`, the harness emits step(days=7)
     so the world doesn't hang.

Token usage is tracked client-side; when cumulative tokens exceed 80%
of 1M a warning logs to stderr (does not crash).

Four named extension points — participants override any of these in
their `submit/agent.py`:

  - `summarize_state` (agents.state_summary) — state-compression boundary.
  - `system_prompt`  (agents.prompts.SYSTEM_PROMPT) — mechanic primer.
  - `decide`         (this module) — per-turn LLM call + dispatch.
  - `ACTION_TOOLS`   (agents.prompts) — the 7-tool action vocabulary.

Configure the LLM provider via env (LLM_PROVIDER, LLM_API_KEY,
LLM_MODEL, LLM_BASE_URL); see `agents.llm.make_llm_from_env`.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from agents.api_client import ApiClient
from agents.attach_runtime import drive_one_turn
from agents.base import BaseAgent
from agents.llm import LLMClient, ToolCall, make_llm_from_env
from agents.prompts import ACTION_TOOLS, SYSTEM_PROMPT
from agents.state_summary import summarize_state

TOKEN_BUDGET: int = 1_000_000
TOKEN_WARN_THRESHOLD: int = 800_000  # 80% of budget — warn at this point
DEFAULT_STEP_DAYS_FALLBACK: int = 7
MAX_TOKENS_PER_TURN: int = 2048


class LLMReactAgent(BaseAgent):
    """OpenAI-compatible chat-completions ReAct agent.

    Override `decide()` (or any of the four extension points) to tune
    the strategy without touching the world or base classes.
    """

    def __init__(
        self,
        api: ApiClient,
        *,
        seed: int | None = None,
        llm: LLMClient | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        action_tools: list[dict[str, Any]] | None = None,
        forecast_hours: int = 24,
        max_tokens_per_turn: int = MAX_TOKENS_PER_TURN,
        stderr: Any = None,
    ) -> None:
        super().__init__(api, seed=seed)
        self.llm: LLMClient = llm if llm is not None else make_llm_from_env()
        self.system_prompt: str = system_prompt
        self.action_tools: list[dict[str, Any]] = action_tools or ACTION_TOOLS
        self.forecast_hours: int = forecast_hours
        self.max_tokens_per_turn: int = max_tokens_per_turn
        self.cumulative_tokens: int = 0
        self.turns: int = 0
        self._warned_budget: bool = False
        self._stderr = stderr if stderr is not None else sys.stderr

    # -- Attach hook ------------------------------------------------------

    def act(self, state: dict[str, Any]) -> None:
        """Per-`/step` hook used when the agent is attached via Agent Play.

        One LLM call per turn: summarise the world, ask the model,
        dispatch every non-`step` tool call. The surrounding `/step`
        handler advances the clock once this returns. The
        cumulative-token counter and the 80%-of-1M budget warning
        carry over from CLI mode — attach mode shares the same budget
        envelope, just spread across UI turns instead of `decide()`
        calls.
        """
        usage = drive_one_turn(
            self.api,
            state,
            self.llm,
            system_prompt=self.system_prompt,
            action_tools=self.action_tools,
            max_tokens=self.max_tokens_per_turn,
        )
        self._record_usage(usage.total)

    def _record_usage(self, tokens: int) -> None:
        """Shared token-accounting tail used by both `decide` (CLI) and
        `act` (attach). Fires the 80%-of-budget warning at most once."""
        self.cumulative_tokens += tokens
        if self.cumulative_tokens >= TOKEN_WARN_THRESHOLD and not self._warned_budget:
            self._warned_budget = True
            print(
                f"WARNING: cumulative LLM tokens {self.cumulative_tokens:,} "
                f"exceeded 80% of {TOKEN_BUDGET:,} budget",
                file=self._stderr,
            )

    # -- Main loop --------------------------------------------------------

    def play_game(self) -> dict[str, Any]:
        """LLM agent owns its own step cadence — `step` is one of the
        tool calls the model emits, so we override the BaseAgent loop."""
        self.api.reset(seed=self._seed)
        state = self.api.state()
        game_days = int(state["config"].get("active_game_days", state["config"]["game_days"]))
        while state["day"] < game_days:
            forecast = self._safe_forecast()
            stepped_days = self.decide(state, forecast, game_days=game_days)
            # If decide returned 0, the LLM ran out of budget or returned
            # nothing actionable — force a step so the world advances.
            if stepped_days <= 0:
                remaining = game_days - state["day"]
                days = min(DEFAULT_STEP_DAYS_FALLBACK, remaining)
                self.api.step(days=days)
            state = self.api.state()
            self.turns += 1
        return state

    def decide(
        self,
        state: dict[str, Any],
        forecast: list[dict[str, Any]] | None,
        *,
        game_days: int,
    ) -> int:
        """One turn: prompt → tool calls → dispatch → step.

        Returns the number of days actually stepped (0 means caller
        should issue a fallback step).
        """
        user_msg = summarize_state(state, forecast)
        response = self.llm.chat(
            system=self.system_prompt,
            user=user_msg,
            tools=self.action_tools,
            max_tokens=self.max_tokens_per_turn,
        )
        self._record_usage(response.usage.total)

        remaining_days = game_days - int(state["day"])
        return self._dispatch_calls(response.tool_calls, remaining_days)

    # -- Tool dispatch ----------------------------------------------------

    def _dispatch_calls(self, calls: list[ToolCall], remaining_days: int) -> int:
        """Apply each tool call in order. Stops at the first `step` (which
        ends the turn). Returns the days actually advanced, or 0 if no
        step was emitted (the play_game loop will then issue a fallback)."""
        stepped = 0
        for call in calls:
            if call.name == "step":
                days = _clamp_days(call.arguments.get("days", DEFAULT_STEP_DAYS_FALLBACK))
                days = min(days, max(1, remaining_days))
                try:
                    self.api.step(days=days)
                    stepped = days
                except RuntimeError:
                    # /step rejected (e.g., bad days value past validation).
                    # Fall through; play_game emits a fallback.
                    pass
                return stepped
            # Non-step tools: dispatch and continue. Catch the world's
            # rejection envelopes (RuntimeError from the ApiClient parse
            # helper) so a bad arg from the LLM doesn't crash the game.
            try:
                self._dispatch_one(call)
            except RuntimeError:
                continue
            except (KeyError, TypeError, ValueError):
                # Malformed arguments from the model — skip and keep going.
                continue
        return stepped

    def _dispatch_one(self, call: ToolCall) -> None:
        args = call.arguments
        name = call.name
        if name == "build":
            self.api.build(
                tile_type=str(args["tile_type"]),
                x=int(args["x"]),
                y=int(args["y"]),
            )
        elif name == "demolish":
            self.api.demolish(x=int(args["x"]), y=int(args["y"]))
        elif name == "survey":
            self.api.survey(
                x=int(args["x"]),
                y=int(args["y"]),
                size=int(args.get("size", 8)),
            )
        elif name == "drill":
            self.api.drill(
                x=int(args["x"]),
                y=int(args["y"]),
                target_z=int(args["target_z"]),
                well_type=str(args.get("well_type", "production")),
            )
        elif name == "set_well_rate":
            self.api.control_well(
                well_id=str(args["well_id"]),
                rate_bbl_day=float(args["rate_bbl_day"]),
            )
        elif name == "set_refinery_rate":
            self.api.control_refinery(
                refinery_id=str(args["refinery_id"]),
                rate_bbl_day=float(args["rate_bbl_day"]),
            )
        # Unknown tool names: silently skip — the model hallucinated.

    # -- Helpers ----------------------------------------------------------

    def _safe_forecast(self) -> list[dict[str, Any]] | None:
        try:
            return self.api.forecast(hours=self.forecast_hours)
        except RuntimeError:
            return None


def _clamp_days(raw: Any) -> int:
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_STEP_DAYS_FALLBACK
    return max(1, min(7, days))


# ---------- CLI driver ------------------------------------------------------


def _make_inprocess_client() -> ApiClient:
    """Build an ApiClient backed by an in-process FastAPI TestClient."""
    from fastapi.testclient import TestClient

    from world.api import create_app

    app = create_app()
    return ApiClient(transport=TestClient(app))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the LLM ReAct reference agent.")
    parser.add_argument("--seed", type=int, default=42, help="World seed (default: 42)")
    parser.add_argument(
        "--api-url",
        type=str,
        default=None,
        help="Connect to a live world (otherwise run in-process).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="If given, write {seed, p_ref, t_ref, turns, tokens} JSON to this path.",
    )
    args = parser.parse_args(argv)

    api = ApiClient(base_url=args.api_url) if args.api_url else _make_inprocess_client()
    agent = LLMReactAgent(api, seed=args.seed)
    final = agent.play_game()

    p_ref = float(final["population"])
    starting_cash = float(final["config"]["starting_cash"])
    t_ref = float(final["treasury"]) - starting_cash

    payload = {
        "seed": args.seed,
        "p_ref": p_ref,
        "t_ref": t_ref,
        "turns": agent.turns,
        "cumulative_tokens": agent.cumulative_tokens,
    }
    print(json.dumps(payload, indent=2))

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2) + "\n")

    if not (math.isfinite(p_ref) and math.isfinite(t_ref)):
        return 1
    return 0


# Agent Play attach contract: the handler prefers a top-level `Agent`
# symbol that is a BaseAgent subclass (`world.api.post_agent_attach`).
Agent = LLMReactAgent


if __name__ == "__main__":
    raise SystemExit(main())

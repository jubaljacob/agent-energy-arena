"""Default participant submission.

`evaluate.py --agent submit.agent` resolves the symbol `Agent` here and
instantiates it as `Agent(api, seed=...)`.  The shipped default re-
exports `agents.scripted.ScriptedAgent` so a clean clone of the repo
can run `docker compose --profile eval run agent` and reproduce the
committed baseline score on seed 42.

Participants replace this file with their own agent.  The minimal
contract is: a class named `Agent` with `__init__(api, *, seed=None)`
and a `play_game(self) -> dict` method (see `agents/base.py`).

To attach this submission to the running world UI's "Agent Play" mode
(POST /agent/attach with folder="submit"), the `Agent` class must
inherit from `agents.base.BaseAgent` so the world has a definition of
`.act(state)` to call per turn. The headless eval harness only needs
the `Agent` protocol's `play_game()`; the UI per-turn callback is an
additional surface that `BaseAgent` provides.

To use the bundled LLM ReAct reference instead, set the LLM env vars
(see `agents/llm.py` — `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`) and
replace the import below:

    from agents.llm_react import LLMReactAgent as Agent
"""

from __future__ import annotations

from agents.scripted import ScriptedAgent as Agent

__all__ = ["Agent"]

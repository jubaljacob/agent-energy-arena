"""LLM ReAct reference agent.

The agent class lives in `agent.py` so the Agent Play attach handler
(`world.api.post_agent_attach`) can load this folder by path. This
package's namespace re-exports the class so existing call sites
(`from agents.llm_react import LLMReactAgent`) keep working unchanged.
"""

from __future__ import annotations

from agents.llm_react.agent import Agent, LLMReactAgent

__all__ = ["Agent", "LLMReactAgent"]

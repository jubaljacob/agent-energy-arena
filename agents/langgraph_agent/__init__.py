"""LangGraph reference agent (full-API showcase).

The agent class lives in `agent.py` so the Agent Play attach handler
(`world.api.post_agent_attach`) can load this folder by path. This
package's namespace re-exports the class (and `DISPATCH_TOOLS`) so
existing call sites (`from agents.langgraph_agent import LangGraphAgent`)
keep working unchanged.
"""

from __future__ import annotations

from agents.langgraph_agent.agent import DISPATCH_TOOLS, Agent, LangGraphAgent

__all__ = ["Agent", "DISPATCH_TOOLS", "LangGraphAgent"]

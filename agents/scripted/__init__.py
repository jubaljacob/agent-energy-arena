"""Scripted reference agent (5-phase deterministic baseline).

The agent class lives in `agent.py` so the Agent Play attach handler
(`world.api.post_agent_attach`) can load this folder by path. This
package's namespace re-exports the class so existing call sites
(`from agents.scripted import ScriptedAgent`) keep working unchanged.
"""

from __future__ import annotations

from agents.scripted.agent import Agent, ScriptedAgent

__all__ = ["Agent", "ScriptedAgent"]

"""Grid Master agent — a compounding clean-city strategy.

The agent class lives in ``agent.py`` so the Agent Play attach handler
(``world.api.post_agent_attach``) can load this folder by path. The
package namespace re-exports the class so ``from agents.grid_master
import GridMasterAgent`` keeps working.
"""

from __future__ import annotations

from agents.grid_master.agent import Agent, GridMasterAgent

__all__ = ["Agent", "GridMasterAgent"]

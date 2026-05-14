---
Status: ready-for-agent
---

# Hot reload loop for edited code

## Parent

[PRD: Agent Play](../PRD.md)

## What to build

Deliver the "edit code → Detach → Attach → see new behavior" loop the PRD promises, including for sibling helper modules (`helpers.py`, `strategy.py`, etc.) that the agent imports. Also support swapping agents (Detach folder A → Attach folder B) without restarting the server.

The mechanism is in the attach handler, before `sys.path.insert(0, folder)`: walk `sys.modules` and `del` every module whose `__file__` lives under the previously-attached folder. This covers sibling helpers, not just `"agent"`. Then `importlib.invalidate_caches()` + `import_module("agent")`. On detach (or re-attach), remove the previously-inserted `sys.path` entry.

Popping only `sys.modules["agent"]` would re-use stale helpers on re-attach, silently breaking the edit-watch loop — the `__file__` walk is what makes this honest.

## Acceptance criteria

- [ ] Attach handler walks `sys.modules` and removes every module whose `__file__` lives under the previously-attached folder, then `importlib.invalidate_caches()`, then `sys.path.insert(0, folder)`, then `import_module("agent")`.
- [ ] Detach removes the `sys.path` entry the matching attach added.
- [ ] Re-attaching the *same* folder reloads `agent.py` from disk.
- [ ] Re-attaching after editing a sibling helper module (e.g. `helpers.py`) reloads the helper from disk — the new constant is visible to the next `act()` call.
- [ ] Attach folder A, then attach folder B: `sys.path` contains B, not A.
- [ ] Test in `world/tests/test_agent_attach.py`: write `agent.py` + `helpers.py` to `tmp_path` where `agent.py` imports a constant from `helpers.py` and surfaces it via an action. Attach, step, observe. Rewrite `helpers.py` with a different constant. Detach + re-attach. Step. Observe the new behavior. (Pattern: `tmp_path` + TestClient, mirroring `world/tests/test_api_scenario_attach.py`.)
- [ ] Test: attach folder A then attach folder B → `sys.path` reflects B and not A.
- [ ] `make check` passes.

## Blocked by

- #01 — Attach/detach + per-turn callback (tracer bullet)

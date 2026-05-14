---
Status: ready-for-agent
---

# Attach validation matrix + error surfacing

## Parent

[PRD: Agent Play](../PRD.md)

## What to build

Tighten the `POST /agent/attach` failure surface so developers can diagnose attach problems from the UI without grepping server logs, and harden the path-safety check against symlink escapes and absolute paths.

All four phases of agent loading (import, lookup, validate, construct) share one try-except. Failures return 400 with `detail` shaped as `f"failed to load agent from {input!r}: {type(exc).__name__}: {exc}"` (per the PRD). Input containing `.` is rejected before any filesystem access — this catches Python dotted paths (`submit.agent`), `..`, and hidden dirs. Resolved folder must be `is_relative_to(repo_root.resolve())` — one boundary check subsumes absolute paths and symlink escapes.

Frontend toast for 4xx/5xx on attach mirrors the `attachScenario` toast pattern (`world/ui/app.js` around the existing scenario-attach error handler).

## Acceptance criteria

- [ ] Body `{"folder": "submit.agent"}` (contains `.`) → 400 with detail naming the bad-input phase.
- [ ] Body `{"folder": ".."}` or `{"folder": ".hidden"}` → 400 (dot-rejection covers both).
- [ ] Body `{"folder": "/etc"}` (absolute path) → 400.
- [ ] Symlink inside the repo that resolves outside the repo → 400.
- [ ] Folder that does not exist → 400.
- [ ] Folder without `agent.py` → 400.
- [ ] Module that imports a missing dependency → 400 with the ImportError message in the detail.
- [ ] Module with no `Agent` class and no `BaseAgent` subclass → 400.
- [ ] `__init__` raising → 400 with the exception type and message verbatim in the detail.
- [ ] World UI toast appears on 4xx from `POST /agent/attach`, mirroring the `attachScenario` error pattern. Toast contains the server's `detail` string.
- [ ] Tests in `world/tests/test_agent_attach.py` cover each of the above 400 cases. Pattern mirrors `world/tests/test_api_scenario_attach.py`.
- [ ] `make check` passes.

## Blocked by

- #01 — Attach/detach + per-turn callback (tracer bullet)

---
Status: needs-triage
---

# 18 — Docker compose + evaluate.py + Makefile + replay

## Parent

[PRD: Energy–AI Nexus Hackathon v1](../PRD.md)

## What to build

The deployment surface from the brief's §11 comes together. A `Dockerfile` builds the world+agents image. `docker-compose.yml` defines two services per the brief:

- `world`: serves the FastAPI on port 8000 with default env (`WORLD_SEED=42`, `GAME_DAYS=3650`, `MANUAL_GAME_DAYS=365`, `WORLD_W=32`, `WORLD_H=32`, `WORLD_D=16`).
- `agent`: opt-in via `--profile eval`. Mounts `./submit/` read-only and runs `python evaluate.py --agent submit.agent --seed 42`.

`evaluate.py` is the CLI driver:

- `python evaluate.py --agent submit.agent --seed 42` — loads the named agent, plays a full game, prints score breakdown, exits with the score as exit code 0 / failure as 1.
- `python evaluate.py --replay runs/{run_id}` — re-runs the action log against a fresh world; asserts state matches the recorded final state.

A `Makefile` provides:

- `make play` — `docker compose up`
- `make eval` — `docker compose --profile eval run agent`
- `make score` — runs the scripted agent on seed 42 and prints the score line

`README.md` documents the three commands every participant must remember (per brief §11.2).

## Acceptance criteria

- [ ] `Dockerfile` builds successfully and produces an image under 500MB.
- [ ] `docker compose up` brings up the world and UI inside 60 seconds on a developer laptop (the brief's §1.4 success criterion).
- [ ] `docker compose --profile eval run agent` runs `python evaluate.py --agent submit.agent --seed 42` and exits cleanly. With `submit/agent.py` symlinked to `agents/scripted.py`, the run produces a baseline-matching score.
- [ ] `python evaluate.py --agent submit.agent --seed 42` prints a JSON line with the score breakdown.
- [ ] `python evaluate.py --replay runs/<run_id>` re-runs the action log and asserts byte-identical final state.
- [ ] `make play`, `make eval`, `make score` work as documented.
- [ ] `README.md` lists the three quick-start commands and points participants at `submit/agent.py` and `submit/WRITEUP.md`.
- [ ] Tests in `tests/test_replay.py` verify replay roundtrip on a short scripted run (e.g., 30 days).

## Blocked by

- 14 — Scripted agent + baseline regression test

# 11: Foundational docs — MIT, README, RULES, API, CONTRIBUTING + archive old briefs

Status: ready-for-agent

## Parent

`.scratch/open-source-arena/PRD.md`

## What to build

Reorganize root-level documentation around the new primary audience: an external agent author arriving at the repo cold. Ship at the repository root:

- `LICENSE` — MIT.
- `README.md` — explains the project in 60 seconds and points to the other root docs.
- `RULES.md` — all game mechanics, formulas, build catalog entries, balance levers, and scoring. Extracted and rewritten from the existing hackathon and upgrade briefs but targeted at agent authors who have not seen the prior context.
- `API.md` — every endpoint with example request and response shapes and error codes (covers the 16 existing endpoints plus the three new ones from issue 04).
- `CONTRIBUTING.md` — the `make check` loop, the PR-as-submission flow for community agents (`agents/community/<one-file-per-agent>.py`), and pointers to where scenarios live.

Move the existing hackathon and upgrade design briefs under `docs/` into a new `docs/archive/` subdirectory. Internal agent-skill documentation under `docs/agents/` stays where it is.

## Acceptance criteria

- [ ] `LICENSE` (MIT) is committed at the repo root.
- [ ] `README.md` is a fresh, agent-author-facing entry point and links to the other root docs.
- [ ] `RULES.md` describes all game mechanics, formulas, balance, and scoring sufficient for an agent author to build without reading source.
- [ ] `API.md` documents every endpoint with example request/response shapes and error codes.
- [ ] `CONTRIBUTING.md` covers `make check`, the community PR flow, and where to put scenarios.
- [ ] The original hackathon and upgrade briefs are moved to `docs/archive/` (history preserved via `git mv`).
- [ ] `docs/agents/` is unchanged.
- [ ] `make check` passes.

## Blocked by

None — can start immediately. Sections may be lightly updated post-merge as later slices land.

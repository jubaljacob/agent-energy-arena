# Leaderboard

Mean-rank aggregation across the v1 public scenarios (`scenarios.baseline`, `scenarios.grid_stress`, `scenarios.economy_stress`) on seed 42.

See [SCENARIOS.md](SCENARIOS.md) for the scenario taxonomy and [RULES.md](RULES.md#scoring) for the score formula. The mean-rank tie-break order is mean raw score (higher wins), then earliest submission timestamp.

Regenerate with `python -m arena.leaderboard` after `make baselines`. The committed file is byte-identical to the regenerated one given the same `baselines/arena/` contents.

| # | Agent | Mean Rank | Mean Score | Scenarios |
|---|-------|-----------|------------|-----------|
| 1 | agents.scripted | 1.00 | 0.2721 | scenarios.baseline, scenarios.economy_stress, scenarios.grid_stress |

## Submitting an agent

Community agents live under `agents/community/<your_handle>.py`. See [CONTRIBUTING.md](CONTRIBUTING.md) for the PR-as-submission flow. A maintainer regenerates this file on merge.

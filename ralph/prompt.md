# ISSUES

Local issue files from `.scratch/<feature>/issues/*.md` are provided at start of context. Parse them to understand the open issues. See `docs/agents/issue-tracker.md` for the conventions.

You will work on the AFK issues only, not the HITL ones. Classify each open issue this way:

- **AFK-eligible** if the issue file's header carries `Status: ready-for-agent` AND either has no `Verification:` line or has `Verification: AFK ...` (e.g. `AFK code + HITL play test`). In the latter case, your job is the code change plus filling in the issue's documented manual-verification protocol into the commit message / PR description for a later human reviewer — you do NOT need to run the protocol yourself.
- **HITL-only** if the issue file carries `Status: ready-for-human`, or carries `Verification: HITL` with no `AFK ...` qualifier. Skip these.
- If the status is anything else (`needs-triage`, `needs-info`, `wontfix`), skip.

You've also been passed a file containing the last few commits. Review these to understand what work has been done. Do NOT use commit-message classifications of past work as authoritative for the current issue's eligibility — read the issue file itself.

If all AFK-eligible tasks are complete, output <promise>NO MORE TASKS</promise>.

# TASK SELECTION

Pick the next task. Prioritize tasks in this order:

1. Critical bugfixes
2. Development infrastructure

Getting development infrastructure like tests and types and dev scripts ready is an important precursor to building features.

3. Tracer bullets for new features

Tracer bullets are small slices of functionality that go through all layers of the system, allowing you to test and validate your approach early. This helps in identifying potential issues and ensures that the overall architecture is sound before investing significant time in development.

TL;DR - build a tiny, end-to-end slice of the feature first, then expand it out.

4. Polish and quick wins
5. Refactors

**Tiebreaker:** when multiple unblocked issues fall into the same priority tier, always pick the one with the lowest issue number (e.g. `08-…` before `18-…`).

# EXPLORATION

Explore the repo.

# IMPLEMENTATION

Use /tdd to complete the task.

# FEEDBACK LOOPS

This is a Python project. There is no `npm`. Before committing, run the canonical pre-commit gate:

- `make check` (runs lint + format-check + typecheck + test, in that order)

Fix the underlying issue rather than skipping a gate. See `CLAUDE.md`.

# COMMIT

Make a git commit. The commit message must:

1. Include key decisions made
2. Include files changed
3. Blockers or notes for next iteration

# THE ISSUE

If the task is complete, move the issue file to its sibling `done/` directory (e.g. `.scratch/<feature>/issues/done/`).

If the task is not complete, add a note to the issue file with what was done.

# FINAL RULES

ONLY WORK ON A SINGLE TASK.

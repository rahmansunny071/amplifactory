---
title: "Merge gates"
description: "The twelve conditions that must all hold on a PR’s current commit before the factory merges it."
---

# Merge gates

The orchestrator merges only when **every** gate holds on the PR's **current** commit. A reviewer's summary is
evidence, not proof. If a gate fails and the fix isn't mechanical, the factory [flags and continues](../concepts/flag-and-continue.md).

| # | Gate | How it's checked |
|---|---|---|
| 1 | **CI green** | A run-level watch: every required check passes, none pending |
| 2 | **Up to date with `main`** | `mergeStateStatus` isn't `BEHIND` or `DIRTY`; if behind, update the branch and re-check gate 1 |
| 3 | **No open blockers** | The review returned `READY`, or every blocker was fixed and verified with its reproduction |
| 4 | **Deviations disclosed and minor** | The PR's "Deviations" section plus the reviewer's undisclosed deviations |
| 5 | **No hard-rule violations** | The orchestrator's own read of the diff against `CLAUDE.md` |
| 6 | **No workflow changes** | Nothing under `.github/`; those go to you, in their own PR |
| 7 | **Dependency changes exactly as planned** | `pyproject.toml` matches the plan; `uv.lock` only follows |
| 8 | **`CLAUDE.md` and `.claude/` unchanged** | `gh pr diff --name-only` |
| 9 | **Closes its issue** | `Closes #N` in the PR body |
| 10 | **Handoff present** | An up-to-date `## Handoff` section |
| 11 | **All review threads resolved** | The GraphQL unresolved-thread query returns 0; also enforced by the ruleset |
| 12 | **Not unsure** | The orchestrator skims the diff; if anything still bothers it, it asks instead of merging |

Merges use **merge commits**, so release tags stay on `main`, and happen **one at a time**.

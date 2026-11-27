---
title: "Merge gates"
description: "The thirteen conditions that must all hold on a PR’s current commit before the factory merges it."
---

# Merge gates

The orchestrator merges only when **every** gate holds on the PR's **current** commit. If a gate fails and the fix
isn't mechanical, the factory [flags and continues](../concepts/flag-and-continue.md).

| # | Gate | How it's checked |
|---|---|---|
| 1 | **CI green** | Watches the run for the PR's head commit (not the newest run on the branch): every required check passes, none pending. With `FACTORY_RUNNER=local`, a pool that's down or paused is offered a start or resume first; if the pool runs in container mode and Docker doesn't answer, you're asked to start Docker Desktop. With `FACTORY_CI=off`, the orchestrator runs the `CLAUDE.md` checks on the PR's head and records them as a `factory/local-checks` commit status ([CI variables](ci-variables.md#the-factorys-ci-gate)) |
| 2 | **Up to date with `main`** | `mergeStateStatus` isn't `BEHIND` or `DIRTY`; if behind, update the branch and re-check gate 1. On `DIRTY` GitHub runs no CI until the conflict is resolved: merge `main` into the branch and run `git diff --cached --check` for leftover conflict markers before committing. `BLOCKED` alone means the merge needs your bypass or approval |
| 3 | **No open blockers** | The review returned `READY`, or every blocker was fixed and verified with its reproduction |
| 4 | **Deviations disclosed and minor** | The PR's "Deviations" section plus the reviewer's undisclosed deviations |
| 5 | **No hard-rule violations** | The orchestrator's own read of the diff against `CLAUDE.md` |
| 6 | **No workflow changes** | Nothing under `.github/`; those go to you, in their own PR |
| 7 | **Dependency changes exactly as planned** | `pyproject.toml` matches the plan; `uv.lock` only follows |
| 8 | **`CLAUDE.md` and `.claude/` unchanged** | `gh pr diff --name-only` |
| 9 | **Closes its issue** | `Closes #N` in the PR body |
| 10 | **Handoff present** | An up-to-date `## Handoff` section |
| 11 | **All review threads resolved** | The GraphQL unresolved-thread query returns 0; also enforced by the ruleset |
| 12 | **Docs ship with the code** | The Handoff has a **Docs** line naming the docs updated, or "none needed" with a reason that holds up (required when code changes but docs don't) |
| 13 | **Not unsure** | If anything still bothers the orchestrator after skimming the diff, it asks instead of merging |

Gate 1 starts from the head commit: `gh pr view <PR> --json headRefOid,mergeStateStatus`, then `gh run list --commit <sha> --workflow ci.yml` (the full 40-character sha) and `gh run watch <run-id> --exit-status`. The verdict holds for that sha only: the head is read again just before merging, and if it changed, gate 1 starts over.

Merges use **merge commits**, so release tags stay on `main`, and happen **one at a time**.

## The merge command

| `mergeStateStatus` | Command |
|---|---|
| `CLEAN` | `gh pr merge <PR> --merge --delete-branch` |
| `BLOCKED` only by the required review | `gh pr merge <PR> --merge --admin --delete-branch` (your ruleset bypass) |

**Stacked PRs first.** `--delete-branch` makes GitHub close, not retarget, any open PR based on the deleted branch. So before merging, the orchestrator lists the PRs based on this one (`gh pr list --base <branch>`) and moves each to `main` (`gh pr edit <N> --base main`).

## When the merge is refused

If the harness denies the command or GitHub rejects it, the factory does not retry through `gh api`, other flags or
another tool. It labels the PR and issue `factory:ready-to-merge`, removes the in-progress label, posts one comment
(`Ready to merge: all gates pass on <sha>.`, the fitting merge command, the gate summary) and moves on. See [Flag and continue](../concepts/flag-and-continue.md#ready-to-merge).

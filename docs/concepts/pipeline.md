---
title: "The pipeline"
description: "What happens to one issue: a Sonnet builder reused for plan, build and fixes, a fresh Opus reviewer, urgency triage and twelve merge gates."
---

# The pipeline

The skill runs in **your Claude Code session**. You (Opus) are the orchestrator: you pick the work, check plans,
verify findings and merge. The heavy lifting goes to two subagents per issue.

| Role | Model | Lifetime | Does |
|---|---|---|---|
| **Orchestrator** | Opus (your session) | the run | queue, plan check, gates, merge, run log |
| **Builder** | Sonnet | one per issue, **reused** via SendMessage | plan → build → fixes, keeps the PR's Handoff current |
| **Design reviewer** | Opus, fresh | critical issues only | reviews the plan before any code |
| **Code reviewer** | Opus, fresh | once per issue | inline findings while CI runs |

## One issue, step by step

1. **Plan.** The builder writes `.factory/designs/issue-N.md` (goal, approach, files and interfaces touched,
   dependencies, risks, tests) and opens a **draft PR** from `local/issue-N`.
2. **Plan check.** Standard issues: you read the plan. **Critical** issues (privacy or security code, a new
   dependency, a shared data contract, a public interface others build on) get a fresh Opus design reviewer.
3. **Build.** The same builder implements, runs the checks from `CLAUDE.md` until green, fills in the PR's
   **Handoff** and marks it ready.
4. **Review ∥ CI.** A fresh Opus reviewer reads the diff against the plan and `CLAUDE.md` and posts **inline
   comments** tagged `[blocker]`, `[improvement]` or `[question]`. Every blocker comes with a reproduction.
5. **Fix.** The builder fixes blockers. You verify each with its reproduction on the new commit and resolve the
   thread. There is no second full review.
6. **Gates and merge.** When all [twelve gates](../reference/gates.md) pass, you merge (merge commit) and the lane
   moves on.

## Urgency triage

`CLAUDE.md` has a **Release phase** line. In **`mvp`** (until a first formal release), only **blockers** are fixed in
a PR:

- the main path breaks on realistic input;
- private data leaks on realistic input;
- a hard rule is violated;
- irreversible loss or overwrite of the user's existing data (source files, history, remote repos) on a plausible invocation, even a mistaken one, when a cheap guard would prevent it;
- a test falsely passes.

Everything else is collected into **one `improvement` issue** per PR, usually in a hardening lane. Switch the line to
**`hardening`** before a release, and reviews become thorough again.

## Why this shape

The first version used cold subagents for every stage, re-reviewed after every fix and treated every finding as a
blocker. Two issues cost about 1.4M subagent tokens. Reusing one builder, using one fresh reviewer and triaging by
urgency cut most of that, without losing the defects that matter. Opus reviews reproduced bugs, including privacy
leaks, that an earlier Sonnet review had called "ready".

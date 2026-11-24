---
title: "Where knowledge lives"
description: "GitHub and the repo are the source of truth; the run log and Claude’s memory only point at them."
---

# Where knowledge lives

| Place | Holds | Who reads it |
|---|---|---|
| **Issues** | goals, acceptance criteria, lane structure, priorities, dependencies | everyone |
| **PRs** | the diff, the **Handoff**, review threads, questions and answers | everyone |
| **`.factory/designs/issue-N.md`** | the plan, shipped inside the PR | reviewers, future agents |
| **`CLAUDE.md`** | project context, release phase, checks, hard rules | every agent, every session |
| **`lesson` issues** in agent-factory | factory problems found while driving a project | the template's next release |
| **`.factory/runs/`** (gitignored) | run log: events, timings, token costs, **pointers** to PRs, commits and threads | the orchestrator, for resume and cost |
| **Claude's memory** (`~/.claude/projects/…`) | your preferences, how you like to work | your Claude sessions on this machine |

## The run log only points

The run log never holds a decision. It says things like "PR #34 asked a question, thread `…`" or "issue #4 merged
at `e24eea1`, 410k tokens". If the run log is deleted, nothing is lost but the cost history: the state can be
rebuilt from GitHub.

## The Handoff

Every PR body has a **`## Handoff`** section, kept current by the builder after every change:

- what changed and why;
- key decisions;
- how to test;
- what's open.

A fresh session, a fresh reviewer or you start from it, instead of from a transcript.

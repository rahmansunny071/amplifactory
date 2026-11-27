---
title: "Where knowledge lives"
description: "GitHub and the repo are the source of truth; the run log and the agent's memory only point at them."
---

# Where knowledge lives

| Place | Holds | Who reads it |
|---|---|---|
| **Issues** | goals, acceptance criteria, lane structure, priorities, dependencies | everyone |
| **PRs** | the diff, the **Handoff**, review threads, questions and answers | everyone |
| **`.factory/designs/issue-N.md`** | the plan, shipped inside the PR | reviewers, future agents |
| **`CLAUDE.md`** | project context, release phase, real-data access, checks, hard rules | every agent, every session |
| **[`lesson` issues](lessons.md)** in amplifactory | factory problems found while driving a project | the template's next release |
| **`.factory/runs/`** (gitignored) | run log: events, timings, token costs, **pointers** to PRs, commits and threads | the orchestrator, for resume and cost |
| **Claude Code's memory** (`~/.claude/projects/…`) | your preferences, how you like to work | your Claude Code sessions on this machine |
| **Copilot Memory** (on GitHub) | facts and preferences Copilot stores as it works; repository memories are shared with everyone who uses Copilot Memory in the repo | Copilot sessions. Listed in the repo's Settings > Copilot > Memory, and yours at [github.com/settings/copilot/memory](https://github.com/settings/copilot/memory). Off in `-p` runs unless `--enable-memory`; unused memories expire after 90 days by default ([docs](https://docs.github.com/copilot/concepts/prompting-and-context/copilot-memory)) |

A preference that is really a project rule belongs in `CLAUDE.md`, not in memory.

## The run log only points

The run log holds no decisions, only entries like "PR #34 asked a question, thread `…`" or "issue #4 merged at
`e24eea1`, 410k tokens". If you delete it you lose only the cost history; state is rebuilt from GitHub.

## The Handoff

Every PR body has a **`## Handoff`** section, which the builder keeps current after every change:

- what changed and why;
- key decisions;
- how to test;
- what's open.

A fresh session or reviewer starts from it, not from a transcript.

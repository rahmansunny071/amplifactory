---
title: "agent-factory"
description: "An issue-driven agent factory on Claude Code, packaged as a Copier template. You plan with Claude; agents design, build and review; you stay in the loop by exception."
hide:
  - toc
---

# agent-factory

agent-factory turns a repository into an **issue-driven agent factory**. You and Claude (Opus) turn an idea into
prioritized **lanes** of ordered GitHub issues. Then the factory takes each issue from plan to merged pull request:
a Sonnet **builder** plans, builds and fixes, a fresh Opus **reviewer** checks the work, and Opus **merges** when every
gate passes. When it needs a decision from you, it asks **on the pull request** and keeps working on other lanes.

It's a [Copier](https://copier.readthedocs.io/) template: one command creates a repo with the rules, the skill, CI,
labels, branch protection and a Project board. `copier update` brings later releases into existing projects.

```
idea ──► brainstorm with Opus ──► lanes (P0–P3) of ordered issues ──► you confirm once
                                                                          │
         ┌────────────────────────────────────────────────────────────────┘
         ▼   per lane, one issue at a time, in order
   Sonnet builder: plan ──► (Opus checks the plan) ──► build ──► PR
                                                                  │
   fresh Opus reviewer ∥ CI ──► builder fixes blockers ──► gates ──► merge
         │
         └─ needs you? question inline on the PR, lane paused, other lanes continue
```

## Get started

<div class="grid cards" markdown>

-   :material-rocket-launch-outline: **Create a project**

    ---

    Generate a repo from the template, then run `bootstrap.sh` for the GitHub side: labels, branch protection and the Project board.

    [:octicons-arrow-right-24: Create a project](getting-started/create-a-project.md)

-   :material-play-circle-outline: **Your first run**

    ---

    Brainstorm into lanes, then say "run the factory" and watch the first issue go from plan to merge.

    [:octicons-arrow-right-24: Your first run](getting-started/first-run.md)

</div>

## What makes it work

| Idea | In short |
|---|---|
| [**Lanes and priorities**](concepts/lanes.md) | Features become lanes, each a parent issue whose native sub-issues run in order. `P0`–`P3` priorities and "blocked by" links drive the schedule. One lane at a time unless you say otherwise. |
| [**The pipeline**](concepts/pipeline.md) | Two subagents per issue: one Sonnet builder reused for plan, build and fixes, and one fresh Opus reviewer. Only blockers are fixed in-PR before a first release; everything else becomes an `improvement` issue. |
| [**Flag and continue**](concepts/flag-and-continue.md) | Questions go on the PR as inline comments with options and a recommendation. You answer whenever you like, then say "resume". |
| [**Proposals become issues**](concepts/lanes.md#proposals-become-issues) | Follow-ups and audit results the orchestrator proposes are proposed as a sub-issue, lane or doc-debt issue, never as an ad-hoc fix. One confirmation creates it. |
| [**Where knowledge lives**](concepts/where-knowledge-lives.md) | GitHub and the repo are the source of truth: issues, PRs, review threads, plans, Handoffs. The run log and Claude's memory only point at them. |

## Where it came from

The factory started as a label-by-label GitHub Actions pipeline for one project. It was then generalized into this
template and hardened by driving a real project, pensieve-traces, through it. Every factory problem found along the
way became a `lesson` issue and then a release. The [changelog](reference/changelog.md) tells that story, one
release at a time.

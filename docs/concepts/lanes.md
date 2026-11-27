---
title: "Lanes and priorities"
description: "How work is shaped: lanes are parent issues whose native sub-issues run in order, ranked by P0–P3 and linked with “blocked by”."
---

# Lanes and priorities

A **lane** is a feature one builder can carry from start to finish: a parent issue labelled `lane`, whose **native
sub-issues, in order**, are the lane's issues.

```
Lane A: STS end-to-end (P0)      #4 ──► #6
Lane B: ConvoMem datasets (P1)   #5 ──► #8 ──► #9
Lane C: OpenRouter traces (P2)   #7
Lane D: Hardening (P3)           #17 ──► #18 ──► … ──► #25     (blocked by lane A)
```

## The rules

| | Rule |
|---|---|
| **Within a lane** | Strictly in order, one issue at a time. The **current issue** is the first open sub-issue; when it merges, the next becomes current. |
| **Across lanes** | Ranked by priority. **One lane at a time by default**; when more are ready, the skill asks once whether to run them in parallel. |
| **Priority** | Exactly one label: `P0` must have now, `P1` next, `P2` later, `P3` someday or hardening. No label counts as `P2`. |
| **Dependencies** | Native **"blocked by"** links, between issues or whole lanes. A "Depends on #N" line in the body is read as a fallback. |
| **Questions** | A question on a lane's PR pauses **only that lane**. A paused lane uses no slot, so the next lane may start. |
| **Merges** | Always one at a time. |

## The main path

Every brainstorm names a **main path**: the smallest chain of issues that makes the thing work end to end. It's a
`P0` lane. Polish and hardening go in a low-priority lane.

## Rolling-wave detail

Only the **next one or two issues per lane** are written in full. Later ones are stubs (title, one-line goal,
dependencies) detailed when they come close.

## Parallel lanes

With `--lanes N`, up to N lanes are active at once, each with its **own worktree and builder**.

- Before a second lane starts, a **collision guard** compares its plan's "Files & interfaces touched" with the files
  the other active lane is changing, and holds it if they overlap.
- After each merge, the other active lanes' branches are updated with `main` before their review.

## Proposals become issues

Work the orchestrator proposes becomes issues, not an ad-hoc fix. This covers audit results, recommended follow-ups
and fixes it would offer at the end of a turn or in the final report. It proposes a sub-issue of the lane that fits,
a new lane if none does, or a doc-debt sub-issue, naming the lane, title and priority. One confirmation creates them
with `gh issue create`, wired with `lanes.py`, and the work goes through the queue. Findings inside a run keep their
routes (`improvement` issues, doc-debt, PR questions). Problems in the factory template repo are `lesson` issues
(the `factory-lesson` skill).

## Seeing it

- `python3 .claude/skills/factory/scripts/queue.py` prints the lanes, each lane's current issue and state
  (`active`, `replied`, `ready`, `asked`, `blocked`, `waiting`, `done`), and **"Work now"**.
- The **Project board** created by `bootstrap.sh`: group by **Parent issue** for each lane with a progress bar; sort
  by **Priority**.
- [`scripts/lanes.py`](../reference/scripts.md#lanespy) creates and reorders lanes, sub-issues, dependencies and
  priorities.

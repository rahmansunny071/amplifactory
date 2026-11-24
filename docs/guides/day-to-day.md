---
title: "Day-to-day"
description: "The handful of things you say to Claude, and what you do on GitHub, in a normal week with the factory."
---

# Day-to-day

Always start Claude Code **from the repo's folder**, on Opus.

| You say | What happens |
|---|---|
| *"brainstorm X into issues"* | Two-level brainstorm: lanes, then ordered issues. You confirm once; everything is created and wired. |
| *"run the factory"* | `queue.py` picks the work; issues go plan → build → review → merge, lane by lane. |
| *"run lanes A and B in parallel"* | Raises the lane limit for this run (otherwise it asks when more lanes are ready). |
| *"resume the factory"* | Picks up replied questions first, then in-flight issues from the run log, then new work. |
| *"what's the factory doing?"* | `queue.py` plus `runlog.py status`: lanes, current issues, what's waiting on you. |
| *"how much did that cost?"* | `runlog.py summary`: wall time, fix rounds and tokens by model, per issue. |
| *"move #9 before #8"* | `lanes.py order`, then the lane runs in the new order. |

## On GitHub

- **Answer questions** in PR threads labelled `factory:needs-maintainer`, then say "resume".
- **Merge PRs the factory won't**: anything touching `.github/`, `CLAUDE.md` or `.claude/`.
- **Triage `improvement` issues** when a lane finishes: give them a priority, add them to a hardening lane, or close
  them.
- **Glance at the Project board**, grouped by Parent issue.

## Keeping sessions small

Start a fresh session every two or three issues. Nothing lives only in the session: GitHub has the work, the run
log has the pointers and costs, and "resume the factory" rebuilds the picture.

## Before a release

Change `CLAUDE.md`'s **Release phase** to `hardening` (in its own PR, which you merge). Reviews become thorough, and
improvements are fixed in-PR instead of filed.

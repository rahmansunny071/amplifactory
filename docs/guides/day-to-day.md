---
title: "Day-to-day"
description: "The handful of things you say to the factory, and what you do on GitHub, in a normal week."
---

# Day-to-day

Start GitHub Copilot or Claude Code **from the repo's folder**, on the orchestrator model (`CLAUDE.md`, "Models";
Claude Opus by default).

| You say | What happens |
|---|---|
| *"brainstorm X into issues"* | Lanes, then ordered issues. You confirm once; everything is created and wired. |
| *"run the factory"* | `queue.py` picks the work; issues go plan, build, review, merge, lane by lane. |
| *"run lanes A and B in parallel"* | Raises the lane limit for this run (otherwise it asks when more lanes are ready). |
| *"resume the factory"* | Replied questions first, then in-flight issues from the run log, then new work. |
| *"what's the factory doing?"* | `queue.py` plus `runlog.py status`: lanes, current issues, what waits on you. |
| *"how much did that cost?"* | `runlog.py summary`: wall time, fix rounds and tokens by model, per issue. |
| *"move #9 before #8"* | `lanes.py order`; the lane runs in the new order. |

## On GitHub

- **Answer questions** in PR threads labelled `factory:needs-maintainer`, then say "resume".
- **Merge PRs the factory won't:** anything touching `.github/`, `CLAUDE.md` or `.claude/`.
- **Triage `improvement` issues** when a lane finishes: prioritise, add to a hardening lane, or close.
- **Check the Project board**, grouped by Parent issue.

## Keep sessions small

Start a fresh session every two or three issues. GitHub and the run log hold the state; "resume the factory"
rebuilds the picture.

## Before a release

Set `CLAUDE.md`'s **Release phase** to `hardening`, in its own PR that you merge. Reviews become thorough, and
improvements are fixed in the PR instead of filed.

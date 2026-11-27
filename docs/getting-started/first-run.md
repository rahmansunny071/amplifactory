---
title: "Your first run"
description: "Brainstorm an idea into lanes with the orchestrator, confirm once, then say “run the factory” and follow the first issue from plan to merge."
---

# Your first run

The [Quick start](quick-start.md) runs this on one small project. This page explains each step.

Start GitHub Copilot or Claude Code **from inside the repo**, so it loads the repo's `CLAUDE.md` and the `factory`
skill, and check that it's on the orchestrator model (`CLAUDE.md` → "Models"; Claude Opus by default):

```bash
cd my-project
copilot         # GitHub Copilot CLI; then /model to confirm claude-opus-5.5
claude          # or Claude Code; then /model to confirm opus
```

In the GitHub Copilot app, start a session in the repo's folder.

## 0. Check bootstrap's output

Before you start, confirm `./scripts/bootstrap.sh` finished cleanly (it's safe to re-run):

- The factory labels exist in the repo, including `improvement`, `lane`, `P0`–`P3` and `documentation`.
- The Project board "&lt;project&gt; factory" exists, is linked to the repo and has a **Priority** field (P0–P3). If bootstrap said the board was skipped,
  run `gh auth refresh -h github.com -s project` and re-run it.
- The "Documentation tech-debt" tracking issue exists.
- On a private repo, look for the ruleset warning: on GitHub Free, rulesets aren't enforced on private repos, so
  `main` isn't protected. Make the repo public or use GitHub Pro if you want the protection. EMU (work) accounts can
  only create private repos; whether rulesets apply there is unverified, so check for the warning.

**CI on your Mac.** CI runs on GitHub-hosted runners unless you opt in to a runner pool on your Mac. Where they're off
(EMU personal repos, for one), say "set up CI runners". It's a few questions: how many jobs run at once,
whether to install the pool's login service, and whether to send this repo's CI to it. Then one CI run proves
it works. This happens once per Mac and once per repo. See [Local CI setup](../guides/local-ci-setup.md).

## 1. Brainstorm into lanes

> brainstorm a CLI that converts session logs into a dataset into issues

The orchestrator talks it through with you: goals, constraints, what's out of scope, and how you'll know it works. Then it
proposes **lanes**: features with a priority (`P0`–`P3`) and dependencies between them. Inside each lane are the
**ordered issues**. Only the next one or two per lane are written in full; the rest are stubs. You'll see a table
like this:

| Lane | Priority | Issues, in order | Depends on |
|---|---|---|---|
| A: core conversion | P0 | reader → writer + CLI | — |
| B: dataset builder | P1 | export → builder → validation | — |
| C: hardening | P3 | edge cases… | lane A |

**Confirm once.** It creates the lane issues, their sub-issues in order, the "blocked by" links, the priority labels
and the Project board items. See [Lanes and priorities](../concepts/lanes.md).

## 2. Run the factory

> run the factory

1. `queue.py` shows each lane's current issue and **"Work now"**. If several lanes are ready, it **asks you whether
   to run them in parallel**. The default is one lane.
2. A builder writes the plan (`.factory/designs/issue-N.md`) and opens a **draft PR** from a
   `local/issue-N` branch.
3. The orchestrator checks the plan. For a critical issue (privacy or security code, a new dependency, a shared data contract),
   a fresh design reviewer does it.
4. The same builder builds, updates the docs the change affects, fills in the PR's **Handoff** (with its
   **Docs** line), and marks the PR ready.
5. A fresh reviewer posts **inline comments** tagged `[blocker]`, `[improvement]`, `[doc-debt]` or
   `[question]`, while CI runs. Docs that disagree with the PR's code are a blocker.
6. The builder fixes blockers. Improvements become one `improvement` issue; `doc-debt` findings become
   sub-issues of the "Documentation tech-debt" issue.
7. When every [gate](../reference/gates.md) passes, including **all review threads resolved**, the orchestrator merges. The
   lane moves to its next issue. If the merge is refused, the PR is labelled `factory:ready-to-merge` with the
   merge command in a comment, and you merge it ([details](../concepts/flag-and-continue.md#ready-to-merge)).

## 3. When it needs you

You'll see a question on the PR with options and a recommendation, and the label `factory:needs-maintainer`. Only
that lane pauses. Reply in the thread whenever it suits you, then:

> resume the factory

See [Flag and continue](../concepts/flag-and-continue.md).

## 4. At the end

The run summary lists what was merged, what's waiting on you, which improvements were filed, and the **time and token
cost per issue** (`scripts/runlog.py summary`). Start a fresh session every two or three issues: GitHub and the run
log let it pick up where the last one stopped.

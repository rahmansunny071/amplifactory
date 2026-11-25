---
title: "Your first run"
description: "Brainstorm an idea into lanes with Opus, confirm once, then say “run the factory” and follow the first issue from plan to merge."
---

# Your first run

Start Claude Code **from inside the repo** so it loads the repo's `CLAUDE.md` and the `factory` skill, and check that
it's on Opus:

```bash
cd my-project
claude          # then /model to confirm Opus
```

## 0. Check bootstrap's output

Before you start, confirm `./scripts/bootstrap.sh` finished cleanly (it's safe to re-run):

- The factory labels exist in the repo, including `improvement`, `lane`, `P0`–`P3` and `documentation`.
- The Project board "&lt;project&gt; factory" exists and is linked to the repo. If bootstrap said the board was skipped,
  run `gh auth refresh -h github.com -s project` and re-run it.
- The "Documentation tech-debt" tracking issue exists.
- On a private repo, look for the ruleset warning: on GitHub Free, rulesets aren't enforced on private repos, so
  `main` isn't protected. Make the repo public or use GitHub Pro if you want the protection.

## 1. Brainstorm into lanes

> brainstorm a CLI that converts session logs into a dataset into issues

Opus talks it through with you: goals, constraints, what's out of scope, and how you'll know it works. Then it
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
2. A Sonnet builder writes the plan (`.factory/designs/issue-N.md`) and opens a **draft PR** from a
   `local/issue-N` branch.
3. Opus checks the plan. For a critical issue (privacy or security code, a new dependency, a shared data contract),
   a fresh Opus design reviewer does it.
4. The same builder builds, updates the docs the change affects, fills in the PR's **Handoff** (with its
   **Docs** line), and marks the PR ready.
5. A fresh Opus reviewer posts **inline comments** tagged `[blocker]`, `[improvement]`, `[doc-debt]` or
   `[question]`, while CI runs. Docs that disagree with the PR's code are a blocker.
6. The builder fixes blockers. Improvements become one `improvement` issue; `doc-debt` findings become
   sub-issues of the "Documentation tech-debt" issue.
7. When every [gate](../reference/gates.md) passes, including **all review threads resolved**, Opus merges. The
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

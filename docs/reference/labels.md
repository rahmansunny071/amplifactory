---
title: "Labels"
description: "Every label bootstrap.sh creates, and who may add it."
---

# Labels

| Label | Meaning | Added by |
|---|---|---|
| `lane` | A parent issue whose sub-issues run in order | the skill (brainstorm) |
| `P0` `P1` `P2` `P3` | Priority: now, next, later, someday | the skill, you |
| `improvement` | Non-blocking finding, filed instead of fixed (mvp phase) | the skill |
| `documentation` | The "Documentation tech-debt" tracking issue and its sub-issues (doc todos) | `bootstrap.sh`, the skill |
| `factory:needs-maintainer` | A question waits for you on this PR | the skill |
| `factory:ready-to-merge` | Every gate passed but the merge was refused: you merge it (the command is in the PR comment) | the skill |
| `factory:design` | **Actions mode:** start the design agent | **you only** |
| `design:approved` | **Actions mode:** start the build agent | **you only** |
| `factory:designing`, `factory:building`, `factory:review` | Actions-mode progress | workflows |
| `factory:blocked` | An agent stopped and needs you | workflows, the skill |
| `lesson` | (agent-factory only) a factory problem found in a project | you, the skill |

The label guard removes `factory:design` and `design:approved` when anyone but you adds them, and the skill never
adds them.

---
title: "The GitHub Actions mode"
description: "The original, asynchronous mode: labels you add start design, build and review agents on GitHub’s servers."
---

# The GitHub Actions mode

Every generated repo also contains the **Actions mode**: agents that run on GitHub's servers when **you** add labels.
It's slower than the skill but needs nothing running on your machine.

```
issue ──(you: factory:design)──► DESIGN agent ──► draft design PR
                                                      │  you review; "@claude revise …"
                              (you: design:approved) ─┘
                                                      ▼
                     BUILD agent on factory/issue-N: implement, run checks until green, open PR
                                                      ▼
                     REVIEW agent: PR vs. approved design + CLAUDE.md
                                                      ▼
                                              you merge
```

| Workflow | Runs when |
|---|---|
| `factory-design.yml` | you add `factory:design` to an issue |
| `factory-build.yml` | you add `design:approved` to a design PR |
| `factory-review.yml` | a build PR opens or updates |
| `factory-chat.yml` | you write `@claude` on an issue or PR |
| `factory-label-guard.yml` | anyone but you adds a trigger label: it's removed |

## GitHub quirks it works around

- **A PR that changes a workflow skips that workflow's stage.** Workflow changes go in their own PR.
- **PR runs use the merge ref from when the PR was last updated.** After a merge, click **Update branch** on open
  PRs, or they run old workflows.
- PRs authored by `claude[bot]` need `allowed_bots` and the right `restrict_untrusted_prs` setting, or the review
  job is green but does nothing.

## Using both

The skill and the Actions mode share `.claude/commands/` and `CLAUDE.md`. The skill never adds trigger labels or
writes `@claude`, and it works on `local/issue-N` branches while the Actions review only watches `factory/issue-*`,
so the two don't start each other. Pick one per issue. If you only ever use the skill, you can turn the Actions
workflows off with `gh workflow disable <workflow>`.

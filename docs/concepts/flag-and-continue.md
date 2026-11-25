---
title: "Flag and continue"
description: "When the factory needs a decision, it asks on the PR, pauses only that lane and keeps working. You answer when it suits you, then say “resume”."
---

# Flag and continue

The factory never stops the run to wait for you, unless nothing else can proceed.

## When it asks

- A plan or review raises a question only you can answer (scope, an interface, a dependency, a data source).
- A gate fails and the fix isn't mechanical.
- The orchestrator is **unsure** whether to merge (gate 12).
- More lanes are ready than you've allowed (asked once, in the session).

## How it asks

1. An **inline review comment** on the exact line, tagged `[question]`, with **options and a recommendation**.
2. The label **`factory:needs-maintainer`** on the PR.
3. The run log records it as `asked`, with a link to the thread.
4. **Only that lane pauses.** Other lanes, and anything that doesn't depend on this issue, carry on.

## How you answer

Reply in the thread on GitHub, whenever you like: from your phone, the next day, in a different session. Then, in
Claude Code:

> resume the factory

`queue.py` detects **your reply after the question** and lists that lane as `replied`, ahead of new work. The
orchestrator reads your answer, applies it (via the builder), resolves the thread, removes the label and continues
to the gates.

## Ready to merge

A refused merge is not a question, so it isn't `factory:needs-maintainer`. When every gate passes but the merge is
refused (the harness denies `gh pr merge`, or GitHub rejects it), the factory:

1. labels the PR and issue **`factory:ready-to-merge`** and removes the in-progress label;
2. posts one PR comment: the exact merge command and the gate summary;
3. moves on. The issue stays open, so its lane's later issues wait.

It never works around the refusal. `queue.py` lists these under "Waiting for you to merge", and the end-of-run
report lists them so you can merge in one batch.

**Opt in to merging itself.** You can add a permission rule to the repo's `.claude/settings.json` or your user
`~/.claude/settings.json`:

```json
{ "permissions": { "allow": ["Bash(gh pr merge * --merge --admin --delete-branch)"] } }
```

The syntax follows the [Claude Code permissions docs](https://code.claude.com/docs/en/permissions). The rule lets
the agent use your ruleset bypass, so it is your choice and the template doesn't ship it. The rule matches only commands that end in `--delete-branch`, which is how the skill runs the merge (gates.md "Merge"); variants with `-R` or other trailing flags aren't covered. A project rule applies only
after you accept the workspace trust prompt. How allow rules interact with auto mode is described in the [permission modes docs](https://code.claude.com/docs/en/permission-modes); check it, and try it in your own session.

## What never happens

- The skill never adds the Actions-mode trigger labels (`factory:design`, `design:approved`) and never writes
  `@claude`. Those would start GitHub Actions agents.
- PRs that change `.github/`, `CLAUDE.md` or `.claude/` are never merged by the factory. You merge those.

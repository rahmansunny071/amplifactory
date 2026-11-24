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

## What never happens

- The skill never adds the Actions-mode trigger labels (`factory:design`, `design:approved`) and never writes
  `@claude`. Those would start GitHub Actions agents.
- PRs that change `.github/`, `CLAUDE.md` or `.claude/` are never merged by the factory. You merge those.

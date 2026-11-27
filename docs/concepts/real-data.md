---
title: "Real data"
description: "The three real-data levels (never, with-approval, allowed), what agents may do at each, the rules that hold at every level, and how to change the level."
---

# Real data

The first line of the **Real data** section in `CLAUDE.md` sets how far agents may go. You choose it with the
`real_data_access` question when you [create the project](../getting-started/create-a-project.md); the skill reads it
from `main`.

## The three levels

| Line | What agents may do |
|---|---|
| `Real data: never` (default) | Agents do not read real user data. You run shapes-only scripts (record types, field names, counts, never values) and reply with the output. |
| `Real data: with-approval` | Agents may read real data on your machine only after you approve **one named data source** on the issue or PR, for that issue or PR only. Until then they ask, as a [question on the PR](flag-and-continue.md). |
| `Real data: allowed` | Agents may read real data on your machine when a task needs it. |

## Rules at every level

- Real data is **never committed** and **never quoted** in issues, PRs or commits; only field names, counts and
  shapes. This includes what [`factory-lesson`](../guides/keep-up-to-date.md#file-a-factory-problem-upstream) files
  upstream.
- Tests use **synthetic fixtures** only.
- Agents read data where it lives and never copy it into the repo or worktree (a scratch directory outside the repo
  is fine).
- A stricter hard rule in `CLAUDE.md` overrides the level.

## A missing or malformed line reads as `never`

If `CLAUDE.md` has no `Real data:` line, or the value isn't one of the three, the skill treats the repo as `never`.
A repo that [adopted just the skill](../guides/adopt-in-existing-repo.md) is safe by default.

## Changing the level

- **Copier answer (preferred):** `uvx copier update --data real_data_access=<level>`, in a PR of its own.
- **By hand:** edit the `Real data:` line in `CLAUDE.md`. `.copier-answers.yml` then still records the old level, and
  a later `copier update` can render the old line back.

[`sync-root-factory`](../guides/keep-up-to-date.md) never changes this value; it lists it under "Open".
See also [`real_data_access`](../reference/questions.md) in the Copier questions.

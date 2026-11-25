---
title: "Docs ship with the code"
description: "The hard rule that every code change brings its docs, the Handoff Docs line and merge gate, and the Documentation tech-debt tracker."
---

# Docs ship with the code

## The hard rule

A `CLAUDE.md` hard rule: every code change brings its documentation in the same PR, both new docs for new
behaviour and updates to any README, CHANGELOG, docstring or `.factory/designs/` text it makes wrong or stale.
A PR whose docs disagree with its code breaks a hard rule, so it's a **blocker**, even in `mvp`.

## The Handoff's Docs line and its gate

The builder's PR body has a **Handoff** with a **Docs** line naming the docs it updated, or "none needed" with a
reason. A diff that changes code but no docs needs that reason, and it must hold up. This is
[merge gate 12](../reference/gates.md): no Docs line, no merge. The reviewer also checks that docs and code agree
in the changed area.

## Documentation tech-debt

A mismatch that was already there before the PR isn't fixed in it. The reviewer tags it **`doc-debt`**, and the
orchestrator files it as a sub-issue of the repo's **"Documentation tech-debt"** tracking issue (labels
`documentation`, `P2`; each todo `documentation`, `improvement`, `P2`). Documentation todos that come up while
planning go there too, never as loose issues.

`lanes.py doc-debt` manages the tracker (`python3 .claude/skills/factory/scripts/lanes.py doc-debt ...`):

| Command | What it does |
|---|---|
| `find` | Prints the tracking issue's number, creating it if missing. |
| `add` | Files a doc todo (or links an existing issue) as a sub-issue. |
| `list` | Lists open todos (number and title), for the builder's ride-alongs. |

**Ride-alongs:** the plan step pastes the open todos into the plan. When the PR already touches a file one of them
is about, the builder fixes that too and the PR says `Closes #<todo>`; it never opens other files just for
doc-debt.

`bootstrap.sh` creates the tracking issue; in older repos `doc-debt find` creates it the first time it's needed.
This came from pensieve-traces, whose README still called a built feature "planned".

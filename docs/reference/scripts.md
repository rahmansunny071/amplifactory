---
title: "Scripts"
description: "The skill’s three helper scripts: queue.py, lanes.py and runlog.py. Stdlib only, using gh."
---

# Scripts

All three live in `.claude/skills/factory/scripts/`, need only Python ≥3.10 and `gh`, and have a `--self-test`.

## queue.py

The lane-aware queue: what to work on now.

```bash
python .claude/skills/factory/scripts/queue.py              # table + "Work now"
python .claude/skills/factory/scripts/queue.py --lanes 2    # two lanes in parallel
python .claude/skills/factory/scripts/queue.py --json
```

| Option | Meaning |
|---|---|
| `--lanes N` | lanes in parallel (default 1) |
| `--json` | machine-readable output |
| `--offline` | skip the reply and dependency API calls |
| `--maintainer LOGIN` | whose replies count (default: the logged-in `gh` user) |

Lane states, from the lane's current issue: `active` (a PR in progress), `replied` (you answered a question),
`ready`, `asked` (waiting on you; uses no slot), `blocked`, `waiting` (on another lane or issue), `done`.

## lanes.py

Creates and maintains lanes with native GitHub features. The sub-issue and dependency APIs need internal issue ids;
`lanes.py` looks them up.

| Command | Does |
|---|---|
| `sub-add LANE ISSUE…` | add issues to a lane, in this order (skips ones already there) |
| `order LANE ISSUE…` | reorder: these first, in this order |
| `block ISSUE --by ISSUE…` | add "blocked by" links (safe to re-run) |
| `priority ISSUE P0…P3` | set exactly one priority label |
| `project-add ISSUE… --project N` | add to the Project board and set its Priority field from the label |
| `show LANE` | print the lane's order |

## runlog.py

The run log in `.factory/runs/<date>.jsonl`: a local cache and cost ledger that **points at** GitHub.

```bash
python .claude/skills/factory/scripts/runlog.py add --issue 4 --step plan --actor builder \
    --model sonnet --tokens 41000 --ms 52000 --pr 29 --note "plan pushed"
python .claude/skills/factory/scripts/runlog.py status     # last step per issue
python .claude/skills/factory/scripts/runlog.py summary    # wall time, rounds, tokens by model
```

Steps: `plan`, `plan-check`, `design-review`, `build`, `review`, `fix`, `handoff-written`, `needs-maintainer`,
`resume`, `merge`, `blocked`, `decision`, `improvement-filed`.

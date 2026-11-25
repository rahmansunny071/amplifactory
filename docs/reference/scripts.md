---
title: "Scripts"
description: "The skills’ helper scripts: queue.py, lanes.py, runlog.py and sync.py. Stdlib only, using gh and git."
---

# Scripts

The first three live in `.claude/skills/factory/scripts/`, need only Python ≥3.10 and `gh`, and have a `--self-test`. `sync.py` belongs to the `sync-root-factory` skill and is described at the end.

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
`merge` (every gate passed but the merge was refused, so you merge it; uses no slot, and the lane's later issues wait), `ready`, `asked` (waiting on you; uses no slot), `blocked`, `waiting` (on another lane or issue), `done`.

*Issues* labelled `factory:ready-to-merge` are also listed under "Waiting for you to merge", each with its PR. A loose one appears only there, not under "Not in a lane". `--json` has the same list as `waiting_to_merge`: `[{"issue": N, "pr": M}]` (`pr` is `null` when no open PR is found).

The "Documentation tech-debt" tracking issue is never listed as work; its open sub-issues appear under "Not in a
lane" as improvements.

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
| `doc-debt find` | print the "Documentation tech-debt" issue's number, creating it (and its labels) if missing |
| `doc-debt add --title T --body-file F` | file a doc todo (`documentation`, `improvement`, `P2`) as a sub-issue of it |
| `doc-debt add --issue N` | label an existing issue as a doc todo and link it as a sub-issue |
| `doc-debt list [--json]` | the open doc todos, which the builder gets for ride-alongs |

The tracking issue is found by its **exact title**, open and labelled `documentation`; if there are several, the
oldest wins.

## runlog.py

The run log in `.factory/runs/<date>.jsonl`: a local cache and cost ledger that **points at** GitHub.

```bash
python .claude/skills/factory/scripts/runlog.py add --issue 4 --step plan --actor builder \
    --model sonnet --tokens 41000 --ms 52000 --pr 29 --note "plan pushed"
python .claude/skills/factory/scripts/runlog.py status     # last step per issue
python .claude/skills/factory/scripts/runlog.py summary    # wall time, rounds, tokens by model
```

Steps: `plan`, `plan-check`, `design-review`, `build`, `review`, `fix`, `handoff-written`, `needs-maintainer`,
`resume`, `merge`, `ready-to-merge`, `blocked`, `decision`, `improvement-filed`.

## sync.py

The guards of the `sync-root-factory` skill (`.claude/skills/sync-root-factory/scripts/`). It needs `git`, and `gh` only for `labels`. Run the **target tag's** copy, extracted outside the repo, with `--repo <repo root>` (see the adopt guide), not the one in the repo. Every command that refuses prints `refused: <why>`, exits 1. Each refuses before writing, with two exceptions that say so: `apply`'s post-write diff (undo with `git restore .` and `git clean -fd` on the folders it names) and `labels --create`, which reports the labels created before a failure.

```bash
python .claude/skills/sync-root-factory/scripts/sync.py preflight
python .claude/skills/sync-root-factory/scripts/sync.py status [--assume-tag vX.Y.Z]
python .claude/skills/sync-root-factory/scripts/sync.py branch vX.Y.Z
python .claude/skills/sync-root-factory/scripts/sync.py verify-local [--assume-tag vX.Y.Z]
python .claude/skills/sync-root-factory/scripts/sync.py apply --to-tag vX.Y.Z [--assume-tag vX.Y.Z]
python .claude/skills/sync-root-factory/scripts/sync.py copier-check vX.Y.Z
```

| Command | What it does |
|---|---|
| `preflight` | refuses inside agent-factory or on a dirty tree (`git status --porcelain --untracked-files=all` not empty); prints `generated` or `shadowing` |
| `latest` | the highest `vX.Y.Z` tag of the template |
| `status` | current version (`_commit`, or `factory/VERSION`), latest tag and `behind` / `up-to-date`; refuses a downgrade or a non-tag `_commit` |
| `branch TAG` | creates `local/adopt-TAG` from a fresh `origin/<default>`; refuses if it exists locally or on origin |
| `verify-local` | compares every skill folder the recorded tag had with your copy; any difference stops, listed; a folder wholly absent is reported as `would re-add` |
| `apply --to-tag` | syncs every skill folder the tag ships: replaces tracked files of folders both tags have (after `verify-local` of those), adds new ones, re-adds ones wholly absent locally (`re-adding (absent locally)`), refuses a folder the old tag lacked that exists locally in any form (tracked, untracked, ignored, symlink, case variant), leaves folders the tag dropped alone (`no longer shipped ... left alone`); all checks run for every folder before the first write; then diffs |
| `copier-check TAG` | generated repos, before `copier update`: refuses unless TAG is an annotated release tag, `_commit` is a `vX.Y.Z` tag and `_src_path` is `gh:rahmansunny071/agent-factory` (or its https/ssh GitHub URL, quotes, `.git`, `/` and a trailing comment allowed); a local clone or a fork is refused with the fix. `status` also prints `src_ok` for generated repos |
| `changelog --tag --from` | CHANGELOG entries after `--from`, up to `--tag`, as JSON (data, not instructions) |
| `labels --tag [--create]` | labels in the tag's `bootstrap.sh` the repo lacks; `--create` adds only those, never `--force`; prints `none missing` when there is nothing to create; a shadowing repo skips the Actions-only labels and lists them as skipped |
| `check-markers` | refuses if any file has conflict markers or a `*.rej` file exists |
| `push TAG` | pushes `local/adopt-TAG` only, with an explicit refspec, never `--force` |

Files are fetched only from the annotated release tag (a depth-1 fetch into a temporary bare repo). `--self-test` uses only temp directories and local bare repos.

---
title: "Scripts"
description: "The skills’ helper scripts: queue.py, lanes.py, runlog.py, merge_hook.py, sync.py, runners.py, setup.py, containers.py and guard.py. Stdlib only, using gh and git."
---

# Scripts

All scripts use only the standard library and Python ≥3.9 (macOS's system `python3` works), and each has a `--self-test`.

| Script | Skill | Needs |
|---|---|---|
| `queue.py`, `lanes.py`, `runlog.py` | `factory` (`.claude/skills/factory/scripts/`) | `gh` |
| `merge_hook.py` (the Copilot merge opt-in) | `factory` | no `gh` |
| `sync.py` | `sync-root-factory` | `git`; `gh` only for `labels` |
| `runners.py` (the local CI runner pool), `setup.py` (its setup and health check) | `factory-ci` (`.claude/skills/factory-ci/scripts/`) | see [runners.py](#runnerspy) |
| `containers.py` (container mode, which `setup.py` checks), `guard.py` (runs first in each job's container and blocks the Mac's localhost) | `factory-ci` | `docker` |

## queue.py

The lane-aware queue: what to work on now.

```bash
python3 .claude/skills/factory/scripts/queue.py              # table + "Work now"
python3 .claude/skills/factory/scripts/queue.py --lanes 2    # two lanes in parallel
python3 .claude/skills/factory/scripts/queue.py --json
```

| Option | Meaning |
|---|---|
| `--lanes N` | lanes in parallel (default 1) |
| `--json` | machine-readable output |
| `--offline` | skip the reply and dependency API calls |
| `--maintainer LOGIN` | whose replies count (default: the logged-in `gh` user) |

Lane states, from the lane's current issue: `active` (a PR in progress), `replied` (you answered a question), `merge` (every gate passed but the merge was refused, so you merge it; uses no slot, and the lane's later issues wait), `ready`, `asked` (waiting on you; uses no slot), `blocked`, `waiting` (on another lane or issue), `done`.

*Issues* labelled `factory:ready-to-merge` are also listed under "Waiting for you to merge", each with its PR. A loose one appears only there, not under "Not in a lane". `--json` has the same list as `waiting_to_merge`: `[{"issue": N, "pr": M}]` (`pr` is `null` when no open PR is found).

The "Documentation tech-debt" tracking issue is never listed as work; its open sub-issues appear under "Not in a
lane" as improvements.

**The CI pool line.** On a Mac that runs the [runner pool](../concepts/local-ci.md) (home: `$FACTORY_RUNNER_POOL_HOME`, else `~/.amplifactory/runner-pool`), the table ends with one line read from the pool's files, with no API call:

- `CI pool: up, 6/8 busy, 11 queued`: its heartbeat is under 120 s old;
- `CI pool: paused 2 h ago (on battery all day), 6/8 busy, 11 queued`: `paused.json` exists;
- `CI pool: down (no heartbeat for 12 min)`: it isn't running. A pool that's down and paused shows both;
- `…, admitting 4 of 8 slots: on battery (battery: half)` at the end of an up line: the Mac admits fewer slots than the pool has, from the heartbeat's `held`.

A pool from before `pause` (a fresh heartbeat with no `paused` key) ignores `paused.json`. Like `runners.py status`, the line then says up, with a note.

`--json` has it as `ci_pool` (`state`, `heartbeat_age_s`, `slots`, `busy`, `queued`, `paused`, `held`), or `null` without a pool home.

## lanes.py

Creates and maintains lanes with native GitHub features (sub-issues and dependencies; it looks up the internal issue ids they need).

| Command | Does |
|---|---|
| `sub-add LANE ISSUE…` | append issues to the end of a lane, in this order (keeps the existing order; skips ones already there) |
| `order LANE ISSUE…` | move these issues to the front, in this order (`sub-add` never does this) |
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
python3 .claude/skills/factory/scripts/runlog.py add --issue 4 --step plan --actor builder \
    --model sonnet --tokens 41000 --ms 52000 --pr 29 --note "plan pushed"
python3 .claude/skills/factory/scripts/runlog.py status     # last step per issue
python3 .claude/skills/factory/scripts/runlog.py summary    # wall time, rounds, tokens by model
```

`--step` and `--model` are free-form.

- `--step` values the factory uses: `plan`, `plan-check`, `design-review`, `build`, `review`, `fix`, `handoff-written`, `needs-maintainer`, `resume`, `merge`, `ready-to-merge`, `blocked`, `decision`, `improvement-filed`.
- `--model` is the id the role ran on, such as `sonnet` in Claude Code or `claude-sonnet-5.5` in GitHub Copilot (generated `CLAUDE.md` → "Models").

Two options go before the subcommand:
- `--dir DIR` uses that log directory instead of the repo's, for `add`, `status` and `summary` alike (`runlog.py --dir DIR status`);
- `--self-test` runs offline checks.

## merge_hook.py

The opt-in that lets the skill merge in GitHub Copilot: a `permissionRequest` hook command. It does nothing until you add the hook: see [Flag and continue](../concepts/flag-and-continue.md#ready-to-merge).

- It reads the permission request as JSON on stdin.
- It prints `{"behavior": "allow"}` only for a shell command that is, in full, `gh pr merge <N> --merge --delete-branch` or `gh pr merge <N> --merge --admin --delete-branch`.
- Anything else prints nothing, so Copilot's normal permission flow decides. That includes `-R`, `--squash`, a chained or piped command and malformed input.
- It never exits 2 (a deny).

```bash
python3 .claude/skills/factory/scripts/merge_hook.py < request.json
python3 .claude/skills/factory/scripts/merge_hook.py --self-test
```

## sync.py

The guards of the `sync-root-factory` skill (`.claude/skills/sync-root-factory/scripts/`). It needs `git`, and `gh` only for `labels`. Run the **target tag's** copy, extracted outside the repo, with `--repo <repo root>` (see the adopt guide), not the one in the repo.

- A command that refuses prints `refused: <why>` and exits 1.
- Each refuses before writing, with two exceptions: `apply`'s post-write diff (undo with `git restore .` and `git clean -fd` on the folders it names) and `labels --create`, which reports the labels created before a failure.

```bash
# sf = python3 -I "$T/x/template/.claude/skills/sync-root-factory/scripts/sync.py" --repo "$R"
# (the target tag's copy, extracted outside the repo: see the adopt guide)
sf preflight
sf status [--assume-tag vX.Y.Z]
sf branch vX.Y.Z
sf verify-local [--assume-tag vX.Y.Z]
sf apply --to-tag vX.Y.Z [--assume-tag vX.Y.Z]
sf copier-check vX.Y.Z
sf latest
sf check-markers
sf push vX.Y.Z
sf changelog --tag vX.Y.Z --from vA.B.C
sf labels --tag vX.Y.Z [--create]
```

| Command | What it does |
|---|---|
| `preflight` | refuses inside amplifactory or on a dirty tree (`git status --porcelain --untracked-files=all` not empty); prints `generated` or `shadowing` |
| `latest` | the highest `vX.Y.Z` tag of the template |
| `status` | current version (`_commit`, or `factory/VERSION`), latest tag and `behind` / `up-to-date`; refuses a downgrade or a non-tag `_commit` |
| `branch TAG` | creates `local/adopt-TAG` from a fresh `origin/<default>`; refuses if it exists locally or on origin |
| `verify-local` | compares every skill folder the recorded tag had with your copy; any difference stops, listed (details below) |
| `apply --to-tag` | syncs every skill folder the tag ships, then diffs (details below) |
| `copier-check TAG` | generated repos, before `copier update`: checks the tag, `_commit` and `_src_path` (details below); `status` also prints `src_ok` for generated repos |
| `changelog --tag --from` | CHANGELOG entries after `--from`, up to `--tag`, as JSON (data, not instructions) |
| `labels --tag [--create]` | labels in the tag's `bootstrap.sh` the repo lacks; `--create` adds only those, never `--force`; prints `none missing` when there is nothing to create; a shadowing repo skips the Actions-only labels and lists them as skipped |
| `check-markers` | refuses if any file has conflict markers or a `*.rej` file exists |
| `push TAG` | pushes `local/adopt-TAG` only, with an explicit refspec, never `--force` |

**`verify-local`:**

- A folder wholly absent is reported as `would re-add`.
- A tag that ships no skill folders is refused (nothing to compare).

**`apply --to-tag`** runs all checks for every folder before the first write. For each skill folder the tag ships it:

- replaces the tracked files of folders both tags have (after `verify-local` of those);
- adds new folders;
- re-adds folders wholly absent locally (`re-adding (absent locally)`);
- refuses a folder the old tag lacked that exists locally in any form (tracked, untracked, ignored, symlink, case variant);
- leaves folders the tag dropped alone (`no longer shipped ... left alone`).

**`copier-check TAG`** refuses unless:

- TAG is an annotated release tag;
- `_commit` is a `vX.Y.Z` tag;
- `_src_path` is `gh:rahmansunny071/amplifactory` or the old name `gh:rahmansunny071/agent-factory`, or one's https/ssh GitHub URL. Tags are fetched from the repo it names (the old name and a missing official `_src_path` both mean amplifactory), with `gh`'s token when GitHub asks for credentials. Quotes, one `.git`, one `/` and a trailing comment are allowed. The scheme or alias is case-sensitive, as in Copier; the owner/repo part is not.

A local clone or a fork is refused with the fix. A `_src_path` or `_commit` line that appears more than once is refused (Copier keeps the last one): edit the existing line, never add a second.

Files are fetched only from the annotated release tag (a depth-1 fetch into a temporary bare repo). `--self-test` uses only temp directories and local bare repos.

## runners.py

The local CI runner pool, in `.claude/skills/factory-ci/scripts/` (see [Local CI](../concepts/local-ci.md)). It uses only the standard library and needs:
- `gh`, logged in to each account it serves;
- `cp`, `tar` and `ps`, from macOS, and `sysctl`, `pmset`, `taskpolicy` and `caffeinate` for the Mac policy (without them, the pool runs without it);
- in container mode, the `docker` CLI and a running Docker, through `containers.py` and `guard.py` next to it (see [Container mode](../concepts/local-ci.md#container-mode-optional)).

```bash
python3 .claude/skills/factory-ci/scripts/runners.py repo add OWNER/REPO [--account LOGIN] [--weight W] [--cap N]
python3 .claude/skills/factory-ci/scripts/runners.py repo remove OWNER/REPO
python3 .claude/skills/factory-ci/scripts/runners.py repo list
python3 .claude/skills/factory-ci/scripts/runners.py config [--slots N] [--idle-timeout S] [--pr-cap N] [--aging S] [--battery full|half]
python3 .claude/skills/factory-ci/scripts/runners.py base [--install]
python3 .claude/skills/factory-ci/scripts/runners.py serve [--once]
python3 .claude/skills/factory-ci/scripts/runners.py status [--json]
python3 .claude/skills/factory-ci/scripts/runners.py pause [--reason TEXT]
python3 .claude/skills/factory-ci/scripts/runners.py resume
python3 .claude/skills/factory-ci/scripts/runners.py stats [--days N] [--json]
python3 .claude/skills/factory-ci/scripts/runners.py logs [-n N] [--run RUN_ID]
```

| Command | What it does |
|---|---|
| `repo add OWNER/REPO` | Serves the repo, using the token of `--account` (default: OWNER) from `gh auth token --user`. Run again to change how it's served; a registered repo keeps its account unless `--account` is given |
| `repo remove`, `repo list` | Stops serving a repo; lists the repos with their accounts, weights and caps |
| `config` | Shows the settings (under `this_mac`: the default slots for this Mac and the performance cores and RAM they come from); the options below change them |
| `base [--install]` | Shows the installed and the latest `actions/runner`. `--install` downloads the latest for this Mac, checks it against its published SHA-256 and swaps it in |
| `serve [--once]` | Runs the pool: one pass every 10 s while jobs are queued or running, every 30 s otherwise. `--once` runs one pass and prints its summary. One `serve` runs per pool home |
| `status [--json]` | Shows whether the pool is up, paused or down, and what it is running and queueing (below). Exits 0 up, 5 paused, 3 down (down wins over paused) |
| `pause [--reason TEXT]`, `resume` | Stop starting runners, and start again. Running jobs finish. Both are safe to repeat (details below) |
| `stats [--days N] [--json]` | From `jobs.jsonl`, the last N days (default 7), per repo and overall: jobs by conclusion, wait (queued to started) and run time at p50 and p95, and how busy the slots were (the run time inside the window, which starts at the first job when the ledger is younger) |
| `logs [-n N] [--run RUN_ID]` | The last N lines (default 50) of `pool.log`, or each runner log of a run with its `_diag` path |

`repo add` options (run it again to change them):

| Option | Meaning |
|---|---|
| `--account LOGIN` | the `gh` login whose token serves the repo |
| `--weight W` | the repo's share of the slots against other repos (default 1) |
| `--cap N` | at most N of its runners at once; `0` removes the cap |

`config` options (see [Fair share](runner-pool.md#fair-share) and [Operate the pool](../concepts/local-ci.md#operate-the-pool)):

| Option | Meaning | Default |
|---|---|---|
| `--slots N` | runners alive at once | enough to leave 4 performance cores and 16 GB of RAM free, at 2 GB per slot |
| `--idle-timeout S` | seconds a runner may wait for a job | 600 |
| `--pr-cap N` | live runners a PR gets before other PRs in its repo go first | 4 |
| `--aging S` | seconds a queued job waits to move up one P class | 600 |
| `--battery full\|half` | `half` uses half the slots on battery | `full` |

`status` shows:

- up, paused or down (no heartbeat for 2 minutes);
- the slots, and how many the Mac admits when it's fewer (memory pressure, or battery);
- the runners and the jobs queued without a runner, by repo, then PR or branch, with the issue a factory branch names and the oldest wait.

It reads the pool's files only, except in container mode: a pool that's down there gets a `docker:` line (`docker` in `--json`) saying to start Docker Desktop when Docker doesn't answer, which `status` asks for up to 10 s.

`pause` writes `paused.json` in the pool home (`since`, and `reason` with any line breaks made spaces). A pool serving an older `runners.py` ignores the pause: `pause` then exits 1 and says to run `setup.py service install --yes`.

`serve` logs go to `logs/pool.log`, which rotates. stderr gets them all on a terminal, but only warnings and errors otherwise. Under the service they go to `service.log`, which doesn't rotate.

The global options go before the command:
- `--home DIR` sets the pool home (default: `$FACTORY_RUNNER_POOL_HOME`, or `~/.amplifactory/runner-pool`);
- `-v` turns on debug logs;
- `--self-test` runs the offline checks.

`serve` installs the runner itself when it is missing.

## setup.py

Setup and health for the runner pool. It lives in `.claude/skills/factory-ci/scripts/` next to `runners.py`, which it imports. It never prompts: the agent asks the maintainer, and a step that changes a repo setting needs `--yes`.

```bash
python3 .claude/skills/factory-ci/scripts/setup.py doctor --repo OWNER/REPO
python3 .claude/skills/factory-ci/scripts/setup.py plan --repo OWNER/REPO [--json]
python3 .claude/skills/factory-ci/scripts/setup.py apply STEP --repo OWNER/REPO [--yes]
```

`doctor` checks, with at most 2 API calls:
- the repo variables;
- whether the pool serves the repo;
- the runner install;
- the pool's heartbeat (when it's up, and the Mac admits fewer slots than the pool has, the line says how many and why);
- in container mode, Docker and its VM, and whether the running pool still starts its jobs on the Mac (it predates container mode, or started in host mode before `container on`) or lets its jobs reach the Mac's own services (it predates the localhost block). Restart it then, stopping it first if you started it by hand;
- the oldest queued run.

Its exit code says what to do next:

| Exit | Meaning |
|---|---|
| 0 | Healthy, or `FACTORY_RUNNER` names another runner (the pool isn't used) |
| 1 | Error (the message says what) |
| 2 | Setup needed: run `plan`. With `FACTORY_RUNNER` unset it means hosted runners, the default, and the factory carries on without asking; with `FACTORY_RUNNER=local` and no finished pool on this Mac, the maintainer is told once to finish the setup or run `gh variable set FACTORY_RUNNER --body github` |
| 3 | The pool isn't running, or in container mode Docker isn't: start what the `next:` line names |
| 4 | CI is off (`FACTORY_CI=off`): gate merges on the local checks |

`plan` lists the setup steps. Each is `done`, `todo`, `answer` (ask the maintainer) or `blocked`, with its next command. `apply STEP` runs one:

| Step | Checks or does |
|---|---|
| `preflight` | macOS and its CPU; `git`, `gh`, `uv` and `python3` on the `PATH` the service will use (a copy only inside an app or the active virtualenv doesn't count); a gh login with the `repo` scope; admin on the repo |
| `actions` | GitHub Actions is on. `apply actions --yes` turns it on (a repo setting) |
| `self-hosted` | Self-hosted runners are allowed. `apply` registers a just-in-time probe runner, never started, and deletes it at once |
| `capacity` | How many jobs run at once: `apply capacity --slots N`. `plan` proposes N from the Mac's performance cores and RAM, leaving 4 cores and 16 GB free |
| `pool` | `apply pool --yes` installs `actions/runner` (checked against its SHA-256) and the login service |
| `register` | The pool serves this repo |
| `route` | `apply route --yes` sets the repo variable `FACTORY_RUNNER=local`, and warns if the repo is public |
| `smoke` | Runs `ci.yml` once (`workflow_dispatch`), watches it for up to 15 minutes, and reports how long it queued and ran. It passes only if every job ran on the pool. `--workflow FILE` runs another CI workflow, and `plan` asks which one when the repo has no `ci.yml` |
| `retire` | Lists the runners registered by hand for this repo, and the `svc.sh` commands for a local runner service. `apply retire --yes --runner NAME` removes one; pool runners are never touched |

`service` manages the launchd user agent `com.amplifactory.runner-pool`, which keeps `runners.py serve` running:

```bash
python3 .claude/skills/factory-ci/scripts/setup.py service install --yes
python3 .claude/skills/factory-ci/scripts/setup.py service status
python3 .claude/skills/factory-ci/scripts/setup.py service uninstall [--purge --yes]
```

| Command | What it does |
|---|---|
| `service install --yes` | Copies `runners.py` (and the `containers.py` and `guard.py` it uses in container mode) into the pool home's `bin/`, then writes the plist and loads it (details below). Needs the runner installed first (`runners.py base --install`) |
| `service status` | Shows whether the agent is loaded, and how old the pool's last heartbeat is (or how far ahead of this Mac's clock, when the clock was set back) |
| `service uninstall` | Unloads the agent and removes its plist. `--purge --yes` also removes the pool's runners, containers and home (details below) |

`service install` does, in order:

1. Copies the scripts into the pool home's `bin/`, unless the `runners.py` there is newer, so the pool doesn't depend on any one repo's checkout.
2. Writes `~/Library/LaunchAgents/com.amplifactory.runner-pool.plist`: `RunAtLoad`, `KeepAlive`, `ProcessType` Standard (so CI jobs aren't throttled), your `PATH` and your Python outside any virtualenv.
3. Unloads an old copy, waiting until it stops, then loads the new one with `launchctl bootstrap gui/$UID`.
4. Waits up to 60 s for a heartbeat from the new pool process.

The plist's `PATH` drops folders private to an app (inside an `.app` bundle, `~/Library/Caches` or `~/Library/Application Support`), so an agent app's own git or gh can't shadow yours: the Copilot app's git has no `submodule`, which `actions/checkout` runs. Folders listed in `/etc/paths` or `/etc/paths.d` stay (Postgres.app's `bin`, for one).

`service uninstall --purge --yes` also:

- stops the pool's runners, removes its containers and its Docker network (with Docker down, it warns and goes on);
- deletes this pool's runner registrations, never anyone else's;
- deletes the pool home, but only a folder that holds nothing besides pool files, and only when no `serve` holds it.

Running it again is harmless.

`container` runs each of the pool's jobs in its own Linux container of `ghcr.io/actions/actions-runner` (`containers.py`, next to `setup.py`; see [Container mode](../concepts/local-ci.md#container-mode-optional)):

```bash
python3 .claude/skills/factory-ci/scripts/setup.py container check [--slots N] [--cpus N] [--memory GB]
python3 .claude/skills/factory-ci/scripts/setup.py container on --yes [--cpus N] [--memory GB]
python3 .claude/skills/factory-ci/scripts/setup.py container off --yes
```

| Command | What it does |
|---|---|
| `container check` | Prints an `ok:`, `fix:` or `note:` line per check (below). Exits 0 when ready, 2 when something needs fixing |
| `container on --yes` | Runs the check, which must pass, then pulls the image for the installed runner version and runs it once (it must report that version). It saves `mode: container`, with `--cpus` and `--memory` when given, and restarts the pool's service, but only when that service runs this pool home |
| `container off --yes` | Saves `mode: host`: new jobs run on the Mac from the pool's next pass, and jobs already in containers finish there. When Docker doesn't answer, it also restarts the pool's service, so the pool stops waiting for Docker |

`container check` looks at:

- the `docker` CLI, on the `PATH` the service will use;
- Docker running, with a Linux VM;
- the VM's size: at least the CPUs each job gets, and at least slots × the memory each job gets, with 10% slack (Docker Desktop's "8 GB" VM reports 7.7 GB);
- the runner image for the installed runner version.

A `fix:` line names Docker Desktop's setting (Settings → Resources → Advanced, then Apply & restart), and the other ways out: fewer slots, or less memory per job.

| Option | Meaning |
|---|---|
| `--slots N`, `--cpus N`, `--memory GB` | stand in for the pool's settings, for a "what if". Defaults: the pool's slots, and 2 CPUs and 2 GB per job |

A value out of range is refused with its flag's name and range (`--cpus must be from 1 to 64`).

docker runs as it will in the service, with launchd's environment and the service's `PATH`. A shell's `DOCKER_HOST`, `DOCKER_CONTEXT` or `DOCKER_CONFIG` doesn't count: pick the Docker context the service should use with `docker context use`.

In container mode, `doctor` also asks Docker:

- Docker down is exit 3, with "start Docker Desktop" as the next step.
- A VM too small for the slots is a warning.
- A running pool whose jobs still run on the Mac or reach its own services is a warning (the container checks in the `doctor` list above); restart it then.

## Repo scripts (working on the template)

`scripts/check.sh [-j N] [repo-path]` in the amplifactory repo (not shipped to generated projects) is the local "checks pass". Options go before the repo path; anything after it is an error.

What it does:

1. Reads the Template CI variants and render steps from `.github/workflows/template-ci.yml` (the file GitHub runs).
2. Renders each variant with `--vcs-ref HEAD` (commit first), in parallel: one job per variant, capped by the CPU count. `-j N` or `CHECK_JOBS=N` runs them in batches of N; `-j 1` runs them one at a time. Each batch's results print as soon as it finishes, in the matrix order.
3. Runs each step with `bash -eo pipefail` (the first failing command fails the step).
4. Runs the `version` check (VERSION vs the first CHANGELOG bullet).

Behavior:

- It exits non-zero on any failure and warns when the tree is dirty.
- It keeps its temp dir (printing the path) only on failure. Ctrl-C stops the running renders and their children, and keeps the temp dir.
- `scripts/test-check.sh` tests it against fake repos in a few seconds; run it when you change `check.sh`.
- Template CI runs both: job `check-sh`, shown as "scripts/check.sh's tests", and on macOS again under `/bin/bash` (bash 3.2). It runs whenever Template CI does (every pull request, drafts included, and pushes to `main`).

See the root `CLAUDE.md` for the full definition, including the skill scripts' `--self-test` runs.

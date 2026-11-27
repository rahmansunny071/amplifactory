---
name: factory-ci
description: >-
  Run this repo's CI on the maintainer's Mac. Sets up the local runner pool (one per Mac, a fresh
  just-in-time runner per CI job), checks its health, pauses or resumes it, shows how busy it is, or
  removes it. Use when the user says "set up CI runners", "set up local CI", "is CI healthy?", "why is
  CI stuck in queued?", "pause CI", "resume CI", "how busy is CI?", "run CI in containers" or
  "remove the runner pool", or when the factory skill's step 0 (`setup.py doctor`) says the pool is
  down, or a repo's CI never starts because GitHub-hosted runners are off for the account.
---

# Factory CI

Some accounts have GitHub-hosted runners turned off; personal repos of Enterprise Managed User (EMU)
accounts are one example. On those, a repo's CI never starts.

CI runs on GitHub-hosted runners by default. This skill is opt-in: it puts CI on a **runner pool** on
the maintainer's Mac:
- one pool serves every factory repo on that Mac, and shares its slots fairly: first across repos, then
  across the PRs in each repo, by their P labels;
- each CI job gets a fresh just-in-time runner;
- the pool stays out of the maintainer's way: runners get low CPU priority, the Mac doesn't idle-sleep
  mid-job, and no new job starts while memory pressure is critical.

Jobs run as the maintainer, directly on the Mac, or each in its own Linux container in container mode.
Forks never run there.

Three scripts live in `scripts/`. All run on Python 3.9+ as `python3`, and none ever prompts. You ask
the maintainer, and the scripts take the answers as flags.

- `setup.py`:
  - `doctor` checks health;
  - `plan` and `apply` run the setup steps;
  - `service` manages the login service that keeps the pool running;
  - `container` checks this Mac for, and turns on or off, container mode.
- `runners.py` is the pool itself: `serve`, `repo` (with each repo's `--weight` and `--cap`), `config`
  (`--slots`, `--pr-cap`, `--aging`, `--battery`), `base`, `status`, `pause`, `resume`, `stats` and
  `logs`.
- `containers.py` runs the pool's runners in Linux containers, in container mode.

## Is CI healthy?

```bash
python3 .claude/skills/factory-ci/scripts/setup.py doctor --repo <owner/repo>
```

Report its lines. The exit code says what to do next:

| Exit | Meaning | Next |
|---|---|---|
| 0 | Healthy, or CI runs on GitHub-hosted runners by the maintainer's choice | Nothing |
| 2 | The pool isn't set up for this repo: `FACTORY_RUNNER` is unset (CI runs on GitHub-hosted runners, the default), or the setup isn't finished | Unset: nothing, unless the maintainer asked for the pool or CI runs never start (hosted runners off); then "Set up", below, or no CI (`gh variable set FACTORY_CI --body off`). Setup started: finish it |
| 3 | The pool isn't running, or in container mode Docker isn't | Do what the `next:` line says. Docker down: ask the maintainer to start Docker Desktop. The pool down: ask whether to start it, `setup.py service install --yes` |
| 4 | CI is off on purpose (`FACTORY_CI=off`) | Gate merges on the `CLAUDE.md` checks |
| 1 | Error | Say what failed |

For the detail, `python3 .claude/skills/factory-ci/scripts/runners.py status` shows:
- whether the pool is up, paused or down;
- its slots;
- the queue by repo, then PR, with how long jobs have waited.

It exits 0 when the pool is up, 5 when it is paused and 3 when it is down. `runners.py pause --reason …` and `resume`
hold and release new jobs; running jobs finish. `stats` reports wait and run times, and `logs --run <id>`
shows a run's runner logs.

## Set up

You set up the pool once per Mac, then add each repo.

1. Run `python3 .claude/skills/factory-ci/scripts/setup.py plan --repo <owner/repo>`. It lists 9 steps,
   each `done`, `todo`, `answer` or `blocked`, with the `next` command to run.
2. Take the steps in order:
   - **`todo`:** run its `next` command.
   - **`answer`:** ask the maintainer the line's question, with the options and your recommendation.
     Then run `next` with their answer, for example `--slots 6` or `--yes`.
   - **`blocked`:** stop, and tell the maintainer what the line says.

     For example, an enterprise policy may block self-hosted runners. The maintainer can ask the
     policy's owner, or set `FACTORY_CI=off` and gate merges on the local checks.
3. Run `plan` again after each step. You're done when every step is `done` and `doctor` exits 0.

Two steps take a while, so run them as long commands (see the factory skill's "Wait on a long
command"):
- `apply pool` downloads about 130 MB, then waits up to a minute for the service;
- `apply smoke` waits for one whole CI run, up to about 17 minutes.

The steps are:
1. preflight;
2. Actions on;
3. self-hosted runners allowed, checked with a probe runner that never starts;
4. capacity: jobs at once;
5. install the pool: the runner and its login service;
6. register the repo;
7. route CI to the pool (`FACTORY_RUNNER=local`);
8. a smoke test: one CI run;
9. retire hand-registered runners.

For a second repo on the same Mac, steps 4 and 5 are already done.

## Container mode

In container mode, each job runs in its own Linux container (`ghcr.io/actions/actions-runner`, Ubuntu
24.04) instead of on the Mac. A job can't read the maintainer's files or reach their logins. It gets its
own CPU and memory limits, and it can't reach other jobs. It needs Docker Desktop, with about 2 GB of
Docker's VM for each slot. It covers the whole pool, so every repo the pool serves runs its CI in
containers. Don't offer it while a repo the pool serves uses the Actions mode with Copilot: those agent
jobs need `gh`, which the image lacks.

When the maintainer asks for it:
1. Run `python3 .claude/skills/factory-ci/scripts/setup.py container check`, and report its lines.
   - Exit 2 means the Mac isn't ready, and each `fix:` line says how to get there. For example, the
     maintainer raises Docker Desktop's memory limit (Settings → Resources → Advanced). Or you offer
     fewer slots (`apply capacity`), or less memory per job (`--memory`).
   - Run the check again until it exits 0.
2. Ask: "Run every job of this Mac's pool in a Linux container?" Mention what stays open: a job has
   `sudo` inside its container, and can still reach Docker's VM and Mac services that listen on all
   interfaces (a container's port published without `127.0.0.1:`, say). The Mac's localhost is blocked
   (Docker Desktop only), and jobs get no IPv6.
3. On a yes: `setup.py container on --yes`, with `--cpus N` and `--memory GB` if the maintainer chose
   other limits. It pulls the image (about 440 MB), so run it as a long command. Then it restarts the
   pool.

`setup.py container off --yes`, after a yes, sends new jobs back to the Mac. Jobs already in containers
finish there.

## Remove the runner pool

Ask first.
- `setup.py service uninstall` stops the service.
- `setup.py service uninstall --purge --yes` also deletes this pool's runners, its containers and its
  home (`~/.amplifactory/runner-pool/`). It deletes only this pool's runner registrations and
  containers.

Then, in each repo, choose where CI goes:
- `gh variable set FACTORY_RUNNER --body github` sends CI back to GitHub-hosted runners, and `doctor` stops
  offering the pool;
- `gh variable set FACTORY_CI --body off` turns CI off.

## Rules

- **`--yes` only after the maintainer said yes**, in this session, to that change. `--yes` changes a
  repo setting or this Mac. Never add it on your own judgement.
- **Hand-registered runners** are removed by name only, one question each, after the maintainer agreed
  to that runner: `apply retire --yes --runner NAME`. A runner may belong to someone else.
- **Tokens** come from `gh`. Never print or store one.

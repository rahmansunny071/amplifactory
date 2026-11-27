---
title: "Local CI setup"
description: "Put a repo's CI on your Mac's runner pool, step by step: what each step asks and does, and how to undo it."
---

# Local CI setup

Use this when GitHub-hosted runners are off for your account (EMU personal repos, for example) and a
generated repo's CI never starts. The `factory-ci` skill puts CI on a [runner pool](../concepts/local-ci.md) on
your Mac.

The first "run the factory" in a repo asks where CI should run. If GitHub-hosted runners work, it records
`FACTORY_RUNNER=github` and stops asking; if not, it offers this setup. You can also say **"set up CI runners"**.

The agent runs `setup.py` (in `.claude/skills/factory-ci/scripts/`) and asks at each decision. Nothing that
changes a repo setting or your Mac happens without your yes. Every step is safe to repeat.

| # | Step | What it asks or does |
|---|---|---|
| 1 | Preflight | Checks macOS; `git`, `gh`, `uv` and `python3` where the pool's service will look for them (a copy only inside an app or a virtualenv doesn't count); a `gh` login with the `repo` scope; admin on the repo. Prints the fix for anything missing |
| 2 | Actions on | Asks to turn GitHub Actions on, if it's off |
| 3 | Self-hosted allowed | Registers a probe runner that never starts, then deletes it. If an enterprise policy blocks self-hosted runners, it says so: ask the policy's owner, or set `FACTORY_CI=off` and gate merges on the local checks |
| 4 | Capacity | Asks how many jobs may run at once; proposes a number that leaves 4 performance cores and 16 GB of RAM free |
| 5 | Install the pool | Asks to install `actions/runner` (checked against its SHA-256) and the login service `com.amplifactory.runner-pool` |
| 6 | Register the repo | Makes the pool serve this repo, with your `gh` login |
| 7 | Route CI | Asks to set the repo variable `FACTORY_RUNNER=local` (see [CI variables](../reference/ci-variables.md)). Warns if the repo is public |
| 8 | Smoke test | Runs CI once (`workflow_dispatch`) and reports queue and run time. Runs `ci.yml`; if your workflow has another name it asks which, and `--workflow FILE` picks it |
| 9 | Retire old runners | Lists runners you registered by hand for this repo and asks before removing each. For a local runner service, it gives you the `svc.sh` commands to stop it |

A second repo on the same Mac skips steps 4 and 5.

## Afterwards

Every "run the factory" starts with `setup.py doctor` (about a second). It reports one of four states:

- healthy;
- setup needed;
- the pool is down, and it offers to start it (in container mode, a Docker that isn't running counts as down; start
  Docker Desktop);
- CI is off.

## Container mode (optional)

To run each job in its own Linux container instead of on your Mac, say "run CI in containers". It needs Docker
Desktop, with about 2 GB of Docker's VM per slot. See [Security model](../reference/runner-pool.md#security-model)
for what it keeps out and what it doesn't.

```bash
S=.claude/skills/factory-ci/scripts/setup.py
python3 $S container check              # Docker, its VM's size, and how to fix them
python3 $S container on --yes           # pulls the image, then restarts the pool
python3 $S container off --yes          # new jobs run on the Mac again
```

## By hand

```bash
S=.claude/skills/factory-ci/scripts/setup.py
python3 $S plan --repo OWNER/REPO                       # each step's status and next command
python3 $S apply capacity --repo OWNER/REPO --slots 6
python3 $S apply pool --repo OWNER/REPO --yes
python3 $S apply register --repo OWNER/REPO
python3 $S apply route --repo OWNER/REPO --yes
python3 $S apply smoke --repo OWNER/REPO                # --workflow FILE if CI isn't ci.yml
python3 $S doctor --repo OWNER/REPO
```

## Undo

- **One repo back to GitHub-hosted runners:** `gh variable set FACTORY_RUNNER --body github`.
- **Stop the pool:** `python3 .claude/skills/factory-ci/scripts/setup.py service uninstall`.
- **Remove the pool and its files:** `... service uninstall --purge --yes`. It deletes only this pool's runner
  registrations, its containers, and its home, `~/.amplifactory/runner-pool/`.

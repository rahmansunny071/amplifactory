---
title: "Runner pool details"
description: "Fair share, caches, container-mode limits and the security model of the local runner pool."
---

# Runner pool details

For what the pool is and the everyday commands, see [Local CI](../concepts/local-ci.md). Flags are in the
[scripts reference](scripts.md#runnerspy).

## Fair share

Each free slot goes first to a repo, then to a job in it.

**Repo:** the one with the fewest live runners for its weight, among repos with a queued job that are below their cap.
Weights default to 1 and caps to none: `runners.py repo add OWNER/REPO --weight 2 --cap 4`. A repo may use slots the
others leave idle; running jobs are never stopped.

**Job in that repo,** by these keys, in order:

1. Pull requests under the per-PR cap (`pr_cap`, default 4). Over the cap, a PR still gets a slot when no other PR in
   the repo waits.
2. The PR's P label, P0 first. The factory copies the issue's P label onto its PR; a run without a PR, or a PR without a
   P label, counts as P2. A job moves up one class per `aging_s` (default 10 minutes) it waits.
3. Pushes to the default branch.
4. The PR with the fewest live runners.
5. The oldest job.

`runners.py config --pr-cap N --aging SECONDS` changes the cap and the aging step.

## Lifecycle

- **Queue.** `runners.py serve` polls each repo's queued and in-progress runs with conditional requests. Once a
  minute it also reads the newest 30 runs, because GitHub's queued list sometimes misses a queued run.
- **Retiring.** A runner that waits longer than `idle_timeout_s`, or whose run ended without it, is deregistered and
  then stopped.
- **Restart.** After a crash the pool adopts runners still running and cleans up the rest, including registrations it
  never saved.
- **Runner install.** `serve` installs `actions/runner` when missing and a newer release once a day, checking each
  download against the SHA-256 the release publishes.
- **Logs.** `logs/pool.log` gets everything and rotates; `service.log` gets only warnings and errors.

## Caches

Jobs on the Mac share uv's cache, uv's Python installs and the runner's tool cache under `cache/`. Each job starts
from a copy-on-write clone of the tool cache; what it finished caching is copied back afterwards. Don't use
setup-uv's `enable-cache` in pool jobs (it is off by default on self-hosted runners, and the template's CI leaves it
off). Once a day while no runner is alive, and once after start, the pool prunes the tool cache (3 newest versions per
tool) and runs `uv cache prune` with the newest uv the jobs use. uv's Python installs aren't pruned.

## Container mode limits

- 2 CPUs and 2 GB per job by default (`container on --cpus N --memory N`); disk is limited only by Docker's VM.
- The image has Python 3, git, curl and jq, but no `gh`, Node or compilers; setup actions add what a job needs.
- No Docker socket: `services:`, `container:` and Docker actions fail.
- `nproc` shows the VM's CPUs; `--cpus` limits CPU time, not the CPUs a job sees.
- With Docker down the pool runs nothing and waits; `setup.py doctor` and `runners.py status` say to start Docker
  Desktop. Turn on "Start Docker Desktop when you sign in". If the pool starts while Docker is down, it exits with one
  error line and launchd retries about every 10 s.
- `container on` restarts the pool; a pool started by hand keeps running jobs on the Mac until it restarts. `container
  off` applies to new jobs at once.
- Each runner release leaves an image of about 440 MB: `docker image rm ghcr.io/actions/actions-runner:<old version>`. Don't
  use `docker image prune -a` while the pool is idle: it removes the current image too.
- If your containers reach the internet only through Docker Desktop's proxy, container mode has no internet.

## Security model

On the Mac, a job runs as you.

- **Forks never run locally,** and neither does a `pull_request_target` run.
- **One run per runner.** Each runner has a job-started hook that fails any job from another run before its first step,
  because labels alone don't stop a job that asks only for `self-hosted`.
- **Scrubbed environment.** Each job gets its own `HOME` and `TMPDIR` in its slot and a short environment allowlist: no
  `GH_TOKEN`, no `SSH_AUTH_SOCK`, no system git config. This keeps your logins from being used by accident; it is
  not a boundary. The [Actions mode with Copilot](../concepts/actions-mode.md#with-copilot) runs agents on text from
  issues and comments, so its jobs use the pool only in a private repo.
- **Tokens stay in memory.** The pool gets a token with `gh auth token --user <account>` and never writes it to disk or
  a log.
- **Shared caches.** Jobs from every repo share the caches, so serve only repos you trust.

In container mode, no folder of yours is mounted and there is no Docker socket, so a job can't read your files or reach
your gh, git, SSH or keychain logins. Each job has its own container, on the pool's own network that doesn't let
containers reach each other, removed after the job. A guard (`guard.py`) blocks Docker Desktop's route to your Mac's
localhost (`host.docker.internal` and the proxy at `http.docker.internal:3128`), turns IPv6 off, drops `NET_RAW` and then
runs the runner without `NET_ADMIN`. If it can't set the block, the job doesn't start.

The block is for Docker Desktop. Still not a hard boundary: services on your Mac that listen on all interfaces stay reachable (including another
container's port published without `127.0.0.1:`); jobs have `sudo` in their container, so a container escape would
reach Docker's VM, which sees the folders Docker Desktop shares (by default `/Users`); and the JIT config is readable
inside the container (`/proc/1/environ`) and in `docker inspect` until the container is removed.

## Files

`~/.amplifactory/runner-pool/` (or `$FACTORY_RUNNER_POOL_HOME`): `config.json`, `bin/` (the service's copy of
`runners.py`, `containers.py`, `guard.py`), `base/`, `slots/`, `hooks/`, `cache/` (`uv/`, `uv-python/`, `tool/`),
`logs/`, `jobs.jsonl`, `state.json`, `heartbeat.json`, `paused.json` while paused. In container mode the pool's
containers and its network (`amplifactory-<pool_id>`) carry the label `amplifactory.pool=<pool_id>`; the pool removes
only those.

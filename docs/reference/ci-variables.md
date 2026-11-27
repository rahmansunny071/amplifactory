---
title: "CI variables"
description: "Two repository variables steer a generated repo's CI: FACTORY_RUNNER picks the runner, and FACTORY_CI turns CI off."
---

# CI variables

A generated repo's `.github/workflows/ci.yml` reads two repository variables. Set them in Settings → Secrets and variables → Actions → Variables, or with `gh variable set`. Changing them needs no workflow edit.

| Variable | Value | Effect |
|---|---|---|
| `FACTORY_RUNNER` | `local` | Jobs run on your Mac's [runner pool](../concepts/local-ci.md): `runs-on: [self-hosted, factory-run-<run_id>]`. Pull requests from forks still run on `ubuntu-latest` |
| | `github`, or anything else | Jobs run on `ubuntu-latest` |
| | unset | Jobs run on `ubuntu-latest`. The factory's step 0 (`setup.py doctor`) takes this to mean hosted runners and carries on without asking |
| `FACTORY_CI` | `off` | Every job is skipped. A job skipped by its `if:` reports Success, so required checks pass and pull requests can merge. The factory's merge gate runs the `CLAUDE.md` checks instead (below) |
| | unset, or anything else | CI runs normally |

The [Actions mode with Copilot](../concepts/actions-mode.md#with-copilot) routes its agent jobs the same way as `FACTORY_RUNNER=local`, but only in a private repo.

```bash
gh variable set FACTORY_RUNNER --body local   # CI on this Mac's runner pool
gh variable set FACTORY_RUNNER --body github  # GitHub-hosted runners
gh variable set FACTORY_CI --body off         # skip CI (the checks report Success)
gh variable delete FACTORY_CI                 # CI on again
```

Values are compared without regard to case, so `LOCAL` and `OFF` work too.

## The factory's CI gate

Before it merges a pull request, the factory reads both variables ([gate 1](gates.md)):

| Setting | What the factory does |
|---|---|
| `FACTORY_CI=off` | No run to wait for. The orchestrator runs the `CLAUDE.md` checks on the pull request's head commit and posts the result on it as a `factory/local-checks` commit status, `success` or `failure`. It merges only on `success` |
| `FACTORY_RUNNER=local` | On the Mac that runs the pool, asks the pool first (`runners.py status`): if it's down, offers to start it; if paused, offers to resume it. In container mode, when `status` says the pool can't reach Docker, asks for Docker Desktop to be started. Then waits on the run |
| otherwise | Waits on the CI run |

## Also in ci.yml

- **Drafts.** Jobs skip draft pull requests, and the skipped checks count as Success. Marking the pull request ready for review runs CI once.
- **Timeouts.** Tests stop after 20 minutes, lint after 10.
- **Check names.** `Tests (Python X)` and `Lint`, which bootstrap's ruleset requires. `Tests (Python X)` is one job per Python version, rendered by Copier from `python_versions`, not a matrix: a skipped matrix job would post only its unexpanded name, and the required `Tests (Python 3.11)` would wait forever.
- **Pushes cancel.** A new push cancels the run in progress.
- **Forks.** A pull request whose head isn't the repo itself routes to `ubuntu-latest` even with `FACTORY_RUNNER=local`, and the pool refuses fork runs as well.

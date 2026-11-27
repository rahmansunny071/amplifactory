---
title: "Create a project"
description: "Generate a repo from the template, then run bootstrap.sh to set up GitHub: labels, branch protection, fork approval and the Project board."
---

# Create a project

The [Quick start](quick-start.md) walks this path with one small project. This page is the reference for each step.

You need:

- Python 3.9 or newer (the skill's scripts run as `python3`)
- [uv](https://docs.astral.sh/uv/)
- [git](https://git-scm.com/)
- the [GitHub CLI](https://cli.github.com/) (`gh`, logged in)
- [GitHub Copilot](https://docs.github.com/copilot/how-tos/copilot-cli) (the CLI or the app), or
  [Claude Code](https://claude.com/claude-code)

The path is: copier, `gh` scopes, bootstrap, then Copilot (or Claude Code). The Actions mode comes last and is
optional: it's off unless Copier's `actions_agent` is `copilot` or `claude`.

## 1. Generate the repo

```bash
uvx copier copy gh:rahmansunny071/amplifactory my-project
```

Copier asks a few [questions](../reference/questions.md): the project and package names, a description, what the
agents should know about the project, project-specific hard rules, Python versions, the release phase, whether agents may read your real data (`never` by default) and whether to add the GitHub Actions mode (`actions_agent`, `none` by default). Defaults
are filled in.

!!! note "Private fork"
    The template repo is public, so Copier needs no credentials. If you copy from a private fork, Copier needs your
    GitHub credentials: run `gh auth setup-git` so the `gh:` shortcut (HTTPS) uses your `gh` login, or use the
    fork's `git@github.com:…` URL.

## 2. Give `gh` the scopes it needs

```bash
gh auth refresh -h github.com -s repo,workflow,project
```

- `repo`: creating the repo and pushing to it.
- `workflow`: pushing a repo that contains GitHub Actions workflows.
- `project`: the factory's Project board (bootstrap and `lanes.py project-add`).

## 3. Run the bootstrap

```bash
cd my-project
./scripts/bootstrap.sh --public      # or --private
```

`bootstrap.sh` does what a template can't. It's safe to re-run:

| Step | What it does |
|---|---|
| Lockfile and first commit | `uv lock`, then `git init` and the first commit |
| GitHub repo | creates and pushes it (`--public` or `--private`) |
| Labels | the factory labels, `improvement`, `lane`, `P0`–`P3` and `documentation` |
| Documentation tech-debt issue | the tracking issue whose sub-issues are documentation todos (labels `documentation`, `P2`) |
| Ruleset protecting `main` | no deletion or force push; PRs need 1 approval and code-owner review; **all conversations resolved**; required checks `Tests (Python X)` and `Lint`; admins can bypass for PRs only |
| Fork PRs | workflows from outside contributors need your approval |
| Project board | "&lt;project&gt; factory", linked to the repo, with a **Priority** field |

!!! warning "Rulesets need a public repo or GitHub Pro"
    On GitHub Free, rulesets aren't enforced on private repos. Then `main` isn't protected, and the script says so.
    Enterprise managed user (EMU) accounts, such as a work account, can create only private repos in their own
    namespace, so they use `--private`. Whether rulesets apply to those repos is unverified; the script warns if
    GitHub refuses one.

## 4. Start GitHub Copilot or Claude Code

```bash
copilot         # GitHub Copilot CLI, or open the repo in the Copilot app
claude          # or Claude Code
```

The `factory` skill works from here and needs no secret: it runs in your session, on the
orchestrator model from `CLAUDE.md` → "Models". Continue with [your first run](first-run.md).

## 5. Optional, last: the Actions mode

The [Actions mode](../concepts/actions-mode.md) runs agents on GitHub Actions when you add labels. It's off by default:
generate with `--data actions_agent=copilot` or `--data actions_agent=claude`, or add it later with
`uvx copier update --data actions_agent=…`, in a PR of its own.

**With Copilot** (`copilot`), the agents are the GitHub Copilot CLI, signed in with each workflow's own token: no secret, and their requests bill your Copilot seat. Bootstrap lets workflows open pull requests, which
the agents need; if you add the mode later with `copier update`, run the command the update prints instead. Where GitHub-hosted runners are off (EMU accounts), set up the runner pool first, so
`FACTORY_RUNNER` is `local`: the agents then run on your Mac, like CI.

**With Claude** (`claude`), the agents are Claude Code GitHub Actions, so this mode needs a Claude subscription, the
Claude GitHub App and a token secret. Then:

1. In Claude Code, run `/install-github-app`, pick the repo, and choose your subscription. That saves
   `CLAUDE_CODE_OAUTH_TOKEN`.
2. **Close the PR the installer opens** (`claude.yml`, `claude-code-review.yml`). The template's `factory-chat.yml`
   and `factory-review.yml` already cover those, with sender and fork checks.

## What a generated repo contains

| Path | Purpose |
|---|---|
| `CLAUDE.md` | Agent guide: project context, release phase, real-data access, each role's model for Copilot and Claude Code, "checks pass" definition (identical to CI), hard rules |
| `.claude/skills/factory/` | The `factory` skill: `SKILL.md`, `references/` (subagent briefs, merge gates, issue and lane format), `scripts/queue.py` (lane-aware queue), `scripts/lanes.py` (lanes, sub-issues, dependencies, priorities, Project board, the Documentation tech-debt issue), `scripts/runlog.py` (run log) |
| `.claude/commands/` | Stage prompts: `design-issue`, `build-from-design`, `review-against-design` (used by both modes) |
| `.github/workflows/factory-*.yml` | Only with an Actions mode: design, build, review and the label guard; with `actions_agent: claude`, chat (`@claude`) too |
| `.github/workflows/ci.yml` | Tests on each Python version, pinned ruff format and lint; on your Mac's runner pool when `FACTORY_RUNNER` is `local`, skipped for drafts and when `FACTORY_CI` is `off` ([CI variables](../reference/ci-variables.md)) |
| `.github/ISSUE_TEMPLATE/factory-task.yml` | Factory task template (never auto-applies `factory:design`) |
| `.github/CODEOWNERS` | You own every file, so with the "Protect main" ruleset each PR needs your approval |
| `.factory/designs/` | Plans, shipped inside each implementation PR |
| `.factory/runs/` | The skill's run log: a local cache and cost ledger pointing at PRs, commits and threads (gitignored) |
| `pyproject.toml`, package, `tests/` | Minimal uv/hatchling skeleton so the checks run from day one |
| `scripts/bootstrap.sh` | One-time GitHub setup (above) |

## Next

[Your first run →](first-run.md)

---
title: "Create a project"
description: "Generate a repo from the template, then run bootstrap.sh to set up GitHub: labels, branch protection, fork approval and the Project board."
---

# Create a project

You need:

- Python 3.9 or newer (the skill's scripts run as `python3`)
- [uv](https://docs.astral.sh/uv/)
- [git](https://git-scm.com/)
- the [GitHub CLI](https://cli.github.com/) (`gh`, logged in)
- [Claude Code](https://claude.com/claude-code)

The path is: copier, `gh` scopes, bootstrap, then `claude`. The Actions mode comes last and is optional.

## 1. Generate the repo

```bash
uvx copier copy gh:rahmansunny071/amplifactory my-project
```

Copier asks a few [questions](../reference/questions.md): the project and package names, a description, what the
agents should know about the project, project-specific hard rules, Python versions, the release phase and whether agents may read your real data (`never` by default). Defaults
are filled in.

!!! note "Private fork"
    The template repo is public, so Copier needs no credentials. If you copy from a private fork, Copier needs your
    GitHub credentials. Run `gh auth setup-git` so the `gh:`
    shortcut (HTTPS) uses your `gh` login, or use `git@github.com:rahmansunny071/amplifactory.git`.

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

## 4. Start Claude Code

```bash
claude          # on Opus
```

The `factory` skill works from here and needs no secret: it runs in your Claude Code session. Continue with
[your first run](first-run.md).

## 5. Optional, last: the GitHub Actions mode

The [Actions mode](../concepts/actions-mode.md) runs agents on GitHub's servers when you add labels. Only this mode
needs the Claude GitHub App and a token secret:

1. In Claude Code, run `/install-github-app`, pick the repo, and choose your subscription. That saves
   `CLAUDE_CODE_OAUTH_TOKEN`.
2. **Close the PR the installer opens** (`claude.yml`, `claude-code-review.yml`). The template's `factory-chat.yml`
   and `factory-review.yml` already cover those, with sender and fork checks.

## What a generated repo contains

| Path | Purpose |
|---|---|
| `CLAUDE.md` | Agent guide: project context, release phase, real-data access, "checks pass" definition (identical to CI), hard rules |
| `.claude/skills/factory/` | The `factory` skill: `SKILL.md`, `references/` (subagent briefs, merge gates, issue and lane format), `scripts/queue.py` (lane-aware queue), `scripts/lanes.py` (lanes, sub-issues, dependencies, priorities, Project board, the Documentation tech-debt issue), `scripts/runlog.py` (run log) |
| `.claude/commands/` | Stage prompts: `design-issue`, `build-from-design`, `review-against-design` (used by both modes) |
| `.github/workflows/factory-*.yml` | Actions mode: design, build, review, chat (`@claude`) and label guard |
| `.github/workflows/ci.yml` | Tests on each Python version, pinned ruff format and lint |
| `.github/ISSUE_TEMPLATE/factory-task.yml` | Factory task template (never auto-applies `factory:design`) |
| `.github/CODEOWNERS` | You own `.github/` |
| `.factory/designs/` | Plans, shipped inside each implementation PR |
| `.factory/runs/` | The skill's run log: a local cache and cost ledger pointing at PRs, commits and threads (gitignored) |
| `pyproject.toml`, package, `tests/` | Minimal uv/hatchling skeleton so the checks run from day one |
| `scripts/bootstrap.sh` | One-time GitHub setup (above) |

## Next

[Your first run →](first-run.md)

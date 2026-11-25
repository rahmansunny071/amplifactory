---
title: "Create a project"
description: "Generate a repo from the template, then run bootstrap.sh to set up GitHub: labels, branch protection, fork approval and the Project board."
---

# Create a project

You need [uv](https://docs.astral.sh/uv/), [git](https://git-scm.com/), the [GitHub CLI](https://cli.github.com/)
(`gh`, logged in) and [Claude Code](https://claude.com/claude-code).

## 1. Generate the repo

```bash
uvx copier copy gh:rahmansunny071/agent-factory my-project
```

Copier asks a few [questions](../reference/questions.md): the project and package names, a description, what the
agents should know about the project, project-specific hard rules, Python versions and the release phase. Defaults
are filled in.

!!! note "Private template"
    If the template repo is private, Copier needs your GitHub credentials. Run `gh auth setup-git` so the `gh:`
    shortcut (HTTPS) uses your `gh` login, or use `git@github.com:rahmansunny071/agent-factory.git`.

## 2. Give `gh` the scopes it needs

```bash
gh auth refresh -h github.com -s repo,workflow,project
```

- `workflow`: creating a repo that contains GitHub Actions workflows.
- `project`: the factory's Project board.

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

## 4. Optional: the GitHub Actions mode

The [Actions mode](../concepts/actions-mode.md) runs agents on GitHub's servers when you add labels. It needs the
Claude GitHub App and a token secret:

1. In Claude Code, run `/install-github-app`, pick the repo, and choose your subscription. That saves
   `CLAUDE_CODE_OAUTH_TOKEN`.
2. **Close the PR the installer opens** (`claude.yml`, `claude-code-review.yml`). The template's `factory-chat.yml`
   and `factory-review.yml` already cover those, with sender and fork checks.

The `factory` skill doesn't need any of this. It runs in your Claude Code session.

## Next

[Your first run →](first-run.md)

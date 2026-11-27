---
title: "GitHub Copilot and Claude Code"
description: "Run the factory in either harness: setup, which skills and commands work where, the model for each role, what differs, and the Actions mode with Claude or with Copilot."
---

# GitHub Copilot and Claude Code

The factory runs in **GitHub Copilot** (the CLI or the app) or in **Claude Code**. A generated repo has one set of
files for both: the `factory` skill, the stage prompts in `.claude/commands/` and `CLAUDE.md`. Each harness reads
them. What differs is the tool names, the permission system, the model names and the memory.

Pick one per session. You can use both on the same repo, one after the other: the state lives on GitHub and in the
repo, not in the session ([Where knowledge lives](../concepts/where-knowledge-lives.md)).

## Set up

You need Python 3.9+, [uv](https://docs.astral.sh/uv/), git and `gh` (logged in), plus one harness:

=== "GitHub Copilot"

    ```bash
    copilot --version     # the Copilot CLI; or open the project in the Copilot app
    ```

    Sign in once with the CLI's login command. Copilot needs an active Copilot plan. An organization's policy can
    limit the models on offer.

=== "Claude Code"

    ```bash
    claude --version
    ```

    Claude Code needs a Claude subscription or API access.

Then generate and bootstrap the project as in the [Quick start](../getting-started/quick-start.md), and start the
harness in the project folder (`copilot` or `claude`). Say **`brainstorm X into issues`**, then **`run the factory`**.
The first `run the factory` also asks where CI runs ([Local CI](../concepts/local-ci.md)).

!!! tip "Start on the orchestrator's model"
    Check `/model` before you start. The orchestrator model is in the generated `CLAUDE.md` under "Models".

## What works where

| What | GitHub Copilot | Claude Code |
|---|---|---|
| The `factory`, `factory-ci`, `factory-lesson` and `sync-root-factory` skills (`.claude/skills/`) | Yes: say what you want ("run the factory", "set up CI runners", "file a factory lesson: …", "sync the factory") | Yes, the same phrases |
| The stage prompts `/design-issue N`, `/build-from-design N`, `/review-against-design PR` (`.claude/commands/`) | Yes, as slash commands (the Actions mode runs them with `copilot -p`) | Yes |
| `CLAUDE.md` (project rules, models, checks) | Read by the session. Subagents don't load it on their own, so each brief tells them to read it | Read by the session and its subagents |
| Helper scripts (`queue.py`, `lanes.py`, `runlog.py`, `setup.py`, `sync.py`) | Yes: plain Python, run from the shell | Yes |
| Letting the skill merge by itself | A personal `permissionRequest` hook running `merge_hook.py` | A `permissions.allow` rule in `settings.json` |

The helper scripts are the same everywhere. See [Scripts](../reference/scripts.md).

## Which model for which role

The factory gives each role its own model and passes it explicitly when it spawns the role. The names differ by
harness. The generated `CLAUDE.md` has this table under "Models", and you can edit it:

| Role | Claude Code | GitHub Copilot |
|---|---|---|
| **Orchestrator**: your session; plans lanes, checks plans, runs the gates, merges | `opus` | `claude-opus-5.5` |
| **Builder**: plans, builds and fixes one issue (also the resume builder and issue writers) | `sonnet` | `claude-sonnet-5.5` |
| **Reviewer**: design and code reviewer, fresh for each issue | `opus` | `claude-opus-5.5` |

- `/model` shows and changes the session's model in both.
- The builder is the cheap role and the orchestrator and reviewer are the careful ones. A fresh reviewer per issue
  has no memory of how the builder reasoned, which is what makes the review independent.
- If a role's model isn't available to you, the factory uses the closest one that is and says so in its run summary.
- The Actions mode sets its own model: `agent_model` (Claude, default `sonnet`) or `copilot_model` (Copilot,
  default `claude-sonnet-5.5`). See [Copier questions](../reference/questions.md).

## What differs

| | GitHub Copilot | Claude Code |
|---|---|---|
| Spawn a role with a model | the `task` tool with `agent_type: "general-purpose"` and `model:`; the builder in the background | a subagent with `model:` set |
| Message the same builder again | `write_agent` | `SendMessage` |
| Read a subagent's result | `read_agent`, after it finishes | its final message |
| Ask you a question in the session | `ask_user`. In autopilot and `-p` runs nobody answers: the factory takes the safe default or flags the question on the PR | `AskUserQuestion` |
| Permissions | its own tool rules and approval prompts, and hooks | `permissions` rules in `settings.json`, and permission modes |
| Memory | Copilot Memory, hosted on GitHub; repository memories are shared with everyone who uses it in that repo | local and private, under `~/.claude/projects/…/memory` |

The skill's "Claude Code and Copilot" section maps every action to both tool sets, so the same steps run in each.

**Memory is a pointer, not a record.** Decisions, plans, handoffs and lessons live on GitHub and in the repo.
A preference that is really a project rule belongs in `CLAUDE.md`. When memory and GitHub disagree, GitHub wins.

### Permissions and merging

Neither harness may merge on its own unless you opt in, and the template ships no opt-in. When the gates pass but the
merge is refused, the PR gets `factory:ready-to-merge` and a comment with the exact command, and the lane moves on.

- **Claude Code:** one allow rule for `gh pr merge * --merge --admin --delete-branch`.
- **Copilot:** the Claude rule does nothing there, and a blanket `gh pr` allow would also approve `gh pr close`. Use
  the personal hook that runs `merge_hook.py`, which approves only the factory's exact merge command.

Both are in [Flag and continue: Let the factory merge](../concepts/flag-and-continue.md#let-the-factory-merge).

## The GitHub Actions mode, with Claude or with Copilot

The optional [Actions mode](../concepts/actions-mode.md) runs design, build and review agents on GitHub Actions when
you add labels. Copier's `actions_agent` question picks the agent: `none` (default), `claude` or `copilot`.

| | `actions_agent: claude` | `actions_agent: copilot` |
|---|---|---|
| Agent | Claude Code GitHub Actions | the GitHub Copilot CLI, installed per job at a pinned version |
| Sign-in | a Claude subscription: `/install-github-app` in Claude Code, which creates the `CLAUDE_CODE_OAUTH_TOKEN` secret | none: each workflow's own `GITHUB_TOKEN` signs in (`copilot-requests: write`) |
| Bills | your Claude subscription | the repo owner's Copilot seat |
| Model | `agent_model` | `copilot_model` |
| Workflows | design, build, review, chat, label guard | design, build, review, label guard |
| `@claude` chat on a PR | yes (`factory-chat.yml`) | no: edit the design branch yourself |
| Tool limits | an `--allowedTools` list per stage | a `preToolUse` hook (`tool_rules.py`) installed from the default branch, with a self-check that fails the job if it doesn't deny |
| PRs opened by a workflow | work as is | need "Allow GitHub Actions to create and approve pull requests" (bootstrap turns it on); an `approve-runs` job approves the held CI runs |
| Runners | `ubuntu-latest` | your Mac's [runner pool](../concepts/local-ci.md) when `FACTORY_RUNNER=local` and the repo is private, else `ubuntu-latest` |

Switch, add or remove it in a PR of its own:

```bash
uvx copier update --data actions_agent=copilot   # or claude, or none
```

The skill and the Actions mode don't start each other: the skill never adds trigger labels and works on
`local/issue-N` branches, while the Actions review takes only `factory/issue-*`. Use one per issue.

## Troubleshooting

- **Copilot says it can't find the skill or the commands:** start it in the project root, where `.claude/` is.
- **A role runs on the wrong model:** check `/model`, then the "Models" table in `CLAUDE.md`.
- **`ask_user` returns "user not available":** you are in autopilot or a `-p` run. Ask the question again in a normal
  session, or answer on the PR.
- More in [Troubleshooting](../reference/troubleshooting.md).

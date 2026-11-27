---
title: "The GitHub Actions mode"
description: "The original, asynchronous mode: labels you add start design, build and review agents on GitHub Actions."
---

# The GitHub Actions mode

Optional: agents run on GitHub Actions when **you** add labels. No session of yours is needed, but it is slower than
the skill. The agents are the **GitHub Copilot CLI** (signed in with each workflow's own token) or **Claude Code
GitHub Actions** (needs a Claude subscription and the `CLAUDE_CODE_OAUTH_TOKEN` secret).

## Turning it on or off

Copier's `actions_agent` question decides which workflows are rendered:

| `actions_agent` | What the repo gets |
|---|---|
| `none` (default for a new project) | No `factory-*.yml` workflows; you run the factory with the skill. |
| `copilot` | Design, build, review and the label guard, with the Copilot CLI ([below](#with-copilot)); the README's "Actions mode with Copilot (optional)". |
| `claude` | The five workflows below; the README's "Actions mode with Claude (optional)". |

```bash
uvx copier copy --data actions_agent=copilot gh:rahmansunny071/amplifactory my-project   # a new project with it
uvx copier update --data actions_agent=copilot   # add it, or switch agents, in a PR of its own
uvx copier update --data actions_agent=none      # remove it
```

A project generated before the question existed keeps its workflows; `copier update` (or `sync-root-factory`)
defaults the question to `claude`. The model is `agent_model` with `claude` (default `sonnet`) and `copilot_model`
with `copilot` (default `claude-sonnet-5.5`; any model your plan offers), for example
`--data agent_model=opus`. Each is asked only with its agent.

```
issue ──(you: factory:design)──► DESIGN agent ──► draft design PR
                                                      │  you review; edit it, or "@claude revise …"
                              (you: design:approved) ─┘
                                                      ▼
                     BUILD agent on factory/issue-N: implement, run checks until green, open PR
                                                      ▼
                     REVIEW agent: PR vs. approved design + CLAUDE.md
                                                      ▼
                                              you merge
```

| Workflow | Runs when |
|---|---|
| `factory-design.yml` | you add `factory:design` to an issue |
| `factory-build.yml` | you add `design:approved` to a design PR |
| `factory-review.yml` | a build PR opens or updates; with Copilot, also on demand (`gh workflow run factory-review.yml -f pr=N`) |
| `factory-chat.yml` | `claude` only: you write `@claude` on an issue or PR |
| `factory-label-guard.yml` | anyone but you adds a trigger label: it's removed |

## With Copilot

- **No secret.** Each workflow asks for `copilot-requests: write`; its `GITHUB_TOKEN` signs in the CLI. Requests bill
  the repo owner's Copilot seat.
- **Runners.** Jobs go to your Mac's [runner pool](local-ci.md) when `FACTORY_RUNNER` is `local` and the repo is
  **private**, otherwise to `ubuntu-latest`. In an internal repo where GitHub-hosted runners are off, the agent jobs
  wait in the queue: don't use this mode there. The pool is no security boundary (agent shell commands run as you),
  which is why it serves only repos where just you and your collaborators can write issues and comments.
- **Prompts.** The agents run `/design-issue N`, `/build-from-design N` and `/review-against-design PR` from
  `.claude/commands/`, with `--model` set to `copilot_model`. Each job installs a pinned Copilot CLI
  (`@github/copilot@1.0.116`) with auto-update off.
- **Workflows may open pull requests.** Bootstrap turns on "Allow GitHub Actions to create and approve pull requests"
  (Settings → Actions → General); without it the design and build agents can't open PRs. A `copier update` that adds
  the mode prints the command to run:
  `gh api -X PUT repos/OWNER/REPO/actions/permissions/workflow -F can_approve_pull_request_reviews=true`.
- **A missing PR fails the run.** The design and build jobs check that their PR is open and turn red if not, unless
  the agent marked the issue `factory:blocked` and commented why.
- **No chat.** There's no `@`-mention workflow: edit the design branch yourself.

### Approving the PR's runs

GitHub holds the `pull_request` runs (CI, review) of a PR that a workflow token opens until someone approves them,
and a `workflow_dispatch` CI run doesn't count as the required check. So `factory-build`'s second job,
`approve-runs`, approves them. It has `actions: write` (the only job that does) and runs no agent.

- It takes only a same-repo `factory/issue-N` PR into the default branch. Any other PR, or a second open PR from the
  branch, turns it red.
- It approves until three lists, 10 s apart, add no runs, for at most 3 minutes, then warns you to approve any run
  still held.
- `gh workflow run factory-review.yml -f pr=N` runs the review on demand, on a same-repo `factory/issue-N` PR only.

### Tool rules

Before each agent starts, its job installs a `preToolUse` hook, `.github/factory/tool_rules.py`, copied from the
default branch (never from a checkout an agent can write). It denies:

- a shell command outside the stage's allowlist (the design agent may run `gh pr create` but no other `gh pr`
  command; the build agent may run `uv run --no-sync pytest` and the pinned `uvx ruff@…`, not `uv add`);
- anything its small bash reader doesn't model: `$`, backticks, redirections, globs, subshells, background jobs
  (a heredoc with a quoted delimiter is fine);
- writes by the design agent outside `.factory/designs/`, and all writes by the review agent;
- `gh pr merge` for every agent, `gh pr review` for the build agent, approving or requesting changes for the review
  agent. `gh pr` flags go after the subcommand.

The job stops before the agent if the hook is missing or doesn't deny. Change it only in a reviewed PR.

### Only your approval merges

`.github/CODEOWNERS` is `* @you` and bootstrap's "Protect main" ruleset requires a code owner's review, so no token,
bot or collaborator can approve a PR into a merge. Your own PRs, the skill's included, merge with the ruleset's admin
bypass (`--admin`). Without that ruleset nothing enforces this, and bootstrap warns. Use the Actions mode only for
repos whose issue text you control.

## GitHub quirks it works around

- **A PR that changes a workflow skips that workflow's stage** (Claude). With Copilot, agents can't push workflow
  changes at all. Put workflow changes in their own PR.
- **PR runs use the merge ref from the PR's last update.** After a merge, click **Update branch** on open PRs, or
  they run old workflows.
- PRs authored by `claude[bot]` need `allowed_bots` and the right `restrict_untrusted_prs` setting, or the review job
  is green but does nothing (Claude).

## Using both

The skill and the Actions mode share `.claude/commands/` and `CLAUDE.md`. The skill never adds trigger labels or
writes `@claude`, and works on `local/issue-N` branches while the Actions review only takes `factory/issue-*`, so
they don't start each other. Pick one per issue. If you only use the skill, remove the Actions mode with
`uvx copier update --data actions_agent=none`, or turn single workflows off with `gh workflow disable <workflow>`.

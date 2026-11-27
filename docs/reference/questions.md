---
title: "Copier questions"
description: "Every question Copier asks when generating a project, its default and what it’s used for."
---

# Copier questions

| Question | Default | Used for |
|---|---|---|
| `project_name` | the destination folder's name, lower-cased with other characters turned into `-` | Repo and distribution name (kebab-case) |
| `package_name` | `project_name` with `_` | Python import name, package directory |
| `cli_name` | `project_name` | Console script and starter `cli.py`; empty for a library |
| `description` | — | pyproject, GitHub description, README tagline |
| `agent_context` | `description` | What the project is, at the top of `CLAUDE.md` |
| `extra_hard_rules` | none | Project-specific `CLAUDE.md` hard rules (one `- ` bullet each) |
| `design_risks` | privacy/data, licensing, dependencies, docs | What every design's Risks section must cover |
| `issue_areas` | `[core, cli, docs, tests, none]` | "Areas affected" dropdown in the issue template |
| `maintainer_github` | the template author's login: **set your own** | The only login the workflows obey; CODEOWNERS |
| `author_name`, `author_email`, `homepage` | the template author's: set your own | pyproject, README |
| `python_min`, `python_versions` | `3.11`, `["3.11", "3.12"]` | `requires-python`, one CI `Tests` job per version, required checks |
| `ruff_version` | `0.17.4` | Pinned ruff in CI, `CLAUDE.md` and the agents' tool allowlists |
| `release_phase` | `mvp` | `CLAUDE.md` "Release phase": `mvp` fixes only blockers in-PR; `hardening` reviews thoroughly |
| `real_data_access` | `never` | [Real data](../concepts/real-data.md) in `CLAUDE.md`: `never` (you run shapes-only scripts), `with-approval` (agents ask per issue, then read locally) or `allowed`. Real data is never committed or quoted at any level |
| `actions_agent` | `none` (`claude` when `agent_model` is already answered: `copier update` of an older project, or `--data agent_model=…`) | Whether the [GitHub Actions mode](../concepts/actions-mode.md) is rendered, and with which agent: `none` (no agent workflows; the skill runs the factory), `claude` (Claude Code GitHub Actions) or `copilot` (the GitHub Copilot CLI, signed in with each workflow's own token) |
| `agent_model` | `sonnet` | `--model` for the Actions-mode agents, asked only when `actions_agent` is `claude` (the skill's models are in the generated `CLAUDE.md` → "Models") |
| `copilot_model` | `claude-sonnet-5.5` | `copilot --model` for the Actions-mode agents, asked only when `actions_agent` is `copilot`: any model your Copilot plan offers (`copilot help config` lists them); letters, digits, dots, hyphens and underscores |

Answers are stored in `.copier-answers.yml` and reused by `copier update`. Change one with
`uvx copier update --data <question>=<value>`, for example `--data release_phase=hardening`,
`--data real_data_access=<level>` or `--data actions_agent=copilot`. Make the change in its own PR. Editing
`CLAUDE.md` by hand also works, but then `.copier-answers.yml` disagrees with it.

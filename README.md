# agent-factory

**A [Copier](https://copier.readthedocs.io/) template for an issue-driven agent factory on Claude Code.**

Every generated repo gets one process, with two ways to run it:

- **Interactive: the `factory` skill** (recommended). From a Claude Code session, brainstorm with Claude into dependency-linked issues, then let it take each ready issue from plan to merged PR. A Sonnet builder plans, builds and fixes; a fresh Opus reviewer checks the work; Opus merges when the gates pass. You're in the loop **by exception**: it stops and asks whenever it's unsure.
- **Async: GitHub Actions.** Label an issue, and agents on GitHub-hosted runners write a design, wait for your approval label, build and review. Slower, because it waits for you at every step, but nothing runs on your machine.

Either way you stay the only one who can start work, approve it, merge to `main` or publish. Every change lands with its plan (`.factory/designs/issue-N.md`), its PR checks and its review on record.

## Create a project

```bash
uvx copier copy gh:rahmansunny071/agent-factory my-project   # answers a few questions
cd my-project
./scripts/bootstrap.sh --public                              # or --private
```

The repo is private, so Copier needs your GitHub credentials. `gh auth setup-git` lets the `gh:` shortcut (HTTPS) use your `gh` login; or use `git@github.com:rahmansunny071/agent-factory.git`.

`bootstrap.sh` does what a template can't: `uv lock` and the first commit, creating and pushing the GitHub repo, the factory labels (including `improvement`), a ruleset protecting `main` (no deletion or force push, PR with your approval, required checks `Tests (Python X)` and `Lint`, admin bypass for PRs only), and fork-PR approval. Rulesets need a **public** repo or GitHub Pro.

For the GitHub Actions mode, once per repo in Claude Code: `/install-github-app` → pick the repo → choose your subscription. That saves the `CLAUDE_CODE_OAUTH_TOKEN` secret. If it says the GitHub CLI lacks the `workflow` scope, run `gh auth refresh -h github.com -s repo,workflow` first. **Close the installer's PR** (`claude.yml`, `claude-code-review.yml`): `factory-chat.yml` and `factory-review.yml` already cover those, with the sender and fork checks the installer's versions lack. The skill needs no secret: it runs in your session.

## Run the factory from Claude Code (the `factory` skill)

Open Claude Code in the repo, on Opus, and say what you want:

| You say | What happens |
|---|---|
| *"brainstorm X into issues"* | Claude brainstorms with you, identifies the **main path** (the smallest chain that makes X work end to end), proposes a breakdown, **you confirm once**, and it creates Factory-task issues with dependency links |
| *"run the factory"* / *"pick up #N"* | For each ready issue (`scripts/queue.py`, main path first): Sonnet **plans** → the plan is checked (by Opus inline, or by an Opus design review for critical issues) → the same Sonnet agent **builds** and opens the PR → a fresh Opus **reviews** in parallel with CI → the same builder fixes **blockers** → Opus checks the gates in `references/gates.md` and **merges** |

**How it keeps cost and latency down** (v0.3.0, from real runs):
- **2 subagents per issue** (3 for critical ones): the builder is one agent reused for plan, build and fixes, so it never re-reads context. Only the reviewer is fresh, on purpose.
- **Urgency triage.** In the `mvp` release phase (`CLAUDE.md`), only **blockers** are fixed inside a PR: the main path breaks on realistic input, private data leaks, a hard rule is broken, or a test falsely passes. Everything else becomes one `improvement` issue per PR, and main-path issues never wait on improvements. Switch to `hardening` before a first formal release.
- **No re-review** of fixes in mvp: CI plus the orchestrator's reproduction of each blocker.
- **Run log:** `.factory/runs/*.jsonl` (gitignored) records each step, so a fresh session resumes mid-issue, and `scripts/runlog.py summary` reports time and tokens per issue. Start a fresh session every 2–3 issues.

**It stops and asks** when it's unsure; when a reviewer isn't confident about a blocker; when blockers persist after two fix rounds; when a change touches workflows, unplanned dependencies, the version or `CLAUDE.md`; or when it would rely on an assumption about real data it can't verify. In that last case it asks you to run a shape-only check on your own data.

It never adds the Actions trigger labels (`factory:design`, `design:approved`) or writes `@claude`, so the two modes never duplicate work.

**Using the skill in a repo not generated from this template:** copy `template/.claude/skills/factory/` from a release tag into the repo's `.claude/skills/factory/`, and add a "Release phase" section to its `CLAUDE.md`. The skill expects the stage prompts in `.claude/commands/` and the labels above.

## Run the factory on GitHub Actions (async)

```
issue ──(you: factory:design)──► DESIGN agent ──► draft design PR
                                                      │  you review; "@claude revise …"
                              (you: design:approved) ─┘
                                                      ▼
                     BUILD agent on factory/issue-N: implement, run checks until green, open PR
                                                      ▼
                     REVIEW agent: PR vs. approved design + CLAUDE.md
                                                      ▼
                                              you merge (releases stay manual)
```

See the generated repo's README → "Changing the factory itself" for the GitHub quirks this mode has to work around: workflow changes need their own PR, and open PRs need **Update branch** after one merges.

## Keep projects up to date

Each generated repo records its answers in `.copier-answers.yml`. When the factory improves here, pull it into a project with:

```bash
cd my-project && uvx copier update      # three-way merge; conflicts are marked for you to resolve
```

Copier updates to the latest **tag** of this repo, so every template change is released as a tag.

## How the template evolves

The factory is tested by real use: run it on a project, and turn what goes wrong into template changes.

1. **A factory problem found while driving a project** (not a problem with the project's own code) is filed here as an issue labelled **`lesson`**, with what happened, the fix applied in that project, and the template files to change.
2. Lessons are batched into a **release**: a PR that closes them, a `CHANGELOG.md` entry, template CI green, then a tag (`v0.3.0`, …).
3. Projects pick up the release with `copier update`, or by copying the skill from the tag.

See [`CHANGELOG.md`](CHANGELOG.md) for what each release changed and why.

## Questions

| Question | Default | Used for |
|---|---|---|
| `project_name` | — | Repo and distribution name (kebab-case) |
| `package_name` | `project_name` with `_` | Python import name, package directory |
| `cli_name` | `project_name` | Console script and starter `cli.py`; empty for a library |
| `description` | — | pyproject, GitHub description, README tagline |
| `agent_context` | `description` | What the project is, at the top of `CLAUDE.md` |
| `extra_hard_rules` | none | Project-specific `CLAUDE.md` hard rules (one `- ` bullet each) |
| `design_risks` | privacy/data, licensing, dependencies, docs | What every design's Risks section must cover |
| `issue_areas` | `[core, cli, docs, tests, none]` | "Areas affected" dropdown in the issue template |
| `maintainer_github` | `rahmansunny071` | The only login the workflows obey; CODEOWNERS |
| `author_name`, `author_email`, `homepage` | Sajjadur Rahman's | pyproject, README |
| `python_min`, `python_versions` | `3.11`, `["3.11", "3.12"]` | `requires-python`, CI matrix, required checks |
| `ruff_version` | `0.17.4` | Pinned ruff in CI, `CLAUDE.md` and the agents' tool allowlists |
| `release_phase` | `mvp` | `CLAUDE.md` "Release phase": `mvp` fixes only blockers in-PR; `hardening` reviews thoroughly before a release |
| `agent_model` | `sonnet` | `--model` for the GitHub Actions agents (the skill picks its own models) |

## What a generated repo contains

| Path | Purpose |
|---|---|
| `CLAUDE.md` | Agent guide: project context, release phase, "checks pass" definition (identical to CI), hard rules |
| `.claude/skills/factory/` | The `factory` skill: `SKILL.md`, `references/` (subagent briefs, merge gates, issue format), `scripts/queue.py` (ready-issue queue), `scripts/runlog.py` (run log) |
| `.claude/commands/` | Stage prompts: `design-issue`, `build-from-design`, `review-against-design` (used by both modes) |
| `.github/workflows/factory-*.yml` | Actions mode: design, build, review, chat (`@claude`) and label guard |
| `.github/workflows/ci.yml` | Tests on each Python version, pinned ruff format and lint |
| `.github/ISSUE_TEMPLATE/factory-task.yml` | Factory task template (never auto-applies `factory:design`) |
| `.github/CODEOWNERS` | You own `.github/` |
| `.factory/designs/` | Plans, shipped inside each implementation PR |
| `.factory/runs/` | The skill's run logs (gitignored) |
| `pyproject.toml`, package, `tests/` | Minimal uv/hatchling skeleton so the checks run from day one |
| `scripts/bootstrap.sh` | One-time GitHub setup (above) |

## Guardrails (kept in every generated repo)

- **Only you advance work.** Every Actions trigger checks the sender is `maintainer_github`, and a label guard strips trigger labels anyone else adds. The skill runs in your session and stops to ask at defined points.
- **Forks can't run agents.** Build, review and chat refuse PRs from forks, and fork-PR workflows need your approval.
- **Least privilege per stage.** Reviewers can't push. The build step refuses design branches that touch anything outside `.factory/designs/`. Per-stage tool allowlists apply.
- **Merge gates** (skill): CI green, no open blockers, no workflow, unplanned dependency, version or `CLAUDE.md` changes, deviations disclosed. Merges use merge commits, so release tags stay on `main`.
- **Bounded cost.** `timeout-minutes`, `--max-turns` and per-issue `concurrency` on every Actions job; pinned models; the skill's urgency triage and run-log cost report.
- Agents may not touch `.github/workflows/`, bump versions or publish unless the approved plan says so. Workflow changes go in their own PR.

## Developing the template

Template files live in `template/`; files ending in `.jinja` are rendered. Copier variables use `[[ … ]]` and blocks `[% … %]`, so GitHub Actions expressions (`${{ … }}`) stay untouched. `template-ci.yml` renders three variants and runs each generated project's checks, plus assertions that every guardrail above is present.

```bash
uvx copier copy --defaults --vcs-ref HEAD --data project_name=demo --data "description=Demo." . /tmp/demo
```

Once tags exist, `copier copy .` without `--vcs-ref HEAD` renders the **latest tag**, not your working tree.

Created by **[Sajjadur Rahman](https://sajjadur.net/)**.

# agent-factory

**A [Copier](https://copier.readthedocs.io/) template for an issue-driven agent factory on Claude Code GitHub Actions.**

Every generated repo gets the same pipeline: agents write a short design, **you** sign off, then agents build, test, self-review and open PRs. You stay the only one who can start or advance work, merge to `main`, or publish anything.

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

## Create a project

```bash
uvx copier copy gh:rahmansunny071/agent-factory my-project   # answers a few questions
cd my-project
./scripts/bootstrap.sh --public                              # or --private
```

The repo is private, so Copier needs your GitHub credentials. `gh auth setup-git` lets the `gh:` shortcut (HTTPS) use your `gh` login; or use `git@github.com:rahmansunny071/agent-factory.git`.

`bootstrap.sh` does what a template can't: `uv lock` and the first commit, creating and pushing the GitHub repo, the six factory labels, a ruleset protecting `main` (no deletion or force push, PR with your approval, required checks `Tests (Python X)` and `Lint`, admin bypass for PRs only), and fork-PR approval. Rulesets need a **public** repo or GitHub Pro.

Then, once per repo, in Claude Code: `/install-github-app` → pick the repo → choose your subscription. That saves the `CLAUDE_CODE_OAUTH_TOKEN` secret. **Close the installer's PR** (`claude.yml`, `claude-code-review.yml`): `factory-chat.yml` and `factory-review.yml` already cover those, with the sender and fork checks the installer's versions lack.

## Keep projects up to date

Each generated repo records its answers in `.copier-answers.yml`. When the factory improves here (a guardrail fix, a better prompt), pull it into a project with:

```bash
cd my-project && uvx copier update      # three-way merge; conflicts are marked for you to resolve
```

Copier updates to the latest **tag** of this repo, so tag releases (`v0.2.0`, …) when the template changes.

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
| `agent_model` | `sonnet` | `--model` for every factory agent |

## What a generated repo contains

| Path | Purpose |
|---|---|
| `CLAUDE.md` | Agent guide: project context, "checks pass" definition (identical to CI), hard rules |
| `.claude/commands/` | Stage prompts: `design-issue`, `build-from-design`, `review-against-design` |
| `.github/workflows/factory-*.yml` | design, build, review, chat (`@claude`) and label guard |
| `.github/workflows/ci.yml` | Tests on each Python version, pinned ruff format + lint |
| `.github/ISSUE_TEMPLATE/factory-task.yml` | Factory task template (never auto-applies `factory:design`) |
| `.github/CODEOWNERS` | You own `.github/` |
| `.factory/designs/` | Approved designs, shipped inside each implementation PR |
| `pyproject.toml`, package, `tests/` | Minimal uv/hatchling skeleton so the checks run from day one |
| `scripts/bootstrap.sh` | One-time GitHub setup (above) |

## Guardrails (kept in every generated repo)

- Every trigger checks the sender is `maintainer_github`; a label guard strips trigger labels anyone else adds.
- Build, review and chat refuse PRs from forks; the chat gate posts a refusal.
- Per-stage tool allowlists: the design agent can't run code, the review agent can't push.
- `timeout-minutes`, `--max-turns` and per-issue `concurrency` on every job; the model is pinned.
- Agents may not touch `.github/workflows/`, bump versions or publish unless the approved design says so.

## Developing the template

Template files live in `template/`; files ending in `.jinja` are rendered. Copier variables use `[[ … ]]` and blocks `[% … %]`, so GitHub Actions expressions (`${{ … }}`) stay untouched. `template-ci.yml` renders three variants and runs each generated project's checks.

```bash
uvx copier copy --defaults --vcs-ref HEAD --data project_name=demo --data "description=Demo." . /tmp/demo
```

Created by **[Sajjadur Rahman](https://sajjadur.net/)**. Factory design adapted from the convomem-arena agent factory.

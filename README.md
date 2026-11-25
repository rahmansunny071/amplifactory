# amplifactory

**A [Copier](https://copier.readthedocs.io/) template for an issue-driven agent factory on Claude Code.**

📖 **Documentation:** https://rahmansunny071.github.io/amplifactory/

Every generated repo gets one process, with two ways to run it:

- **Interactive: the `factory` skill** (recommended). From a Claude Code session, brainstorm with Claude into prioritized lanes of ordered issues, then let it take each issue from plan to merged PR. A Sonnet builder plans, builds and fixes; a fresh Opus reviewer checks the work; Opus merges when the gates pass. When it needs you, it **asks on the PR** (inline comments, with options and a recommendation) and keeps working on other issues; you answer whenever you like, then say "resume".
- **Async: GitHub Actions.** Label an issue, and agents on GitHub-hosted runners write a design, wait for your approval label, build and review. Slower, because it waits for you at every step, but nothing runs on your machine.

Either way you stay the only one who can start work, approve it, merge to `main` or publish. Every change lands with its plan (`.factory/designs/issue-N.md`), its PR checks and its review on record.

## Create a project

```bash
uvx copier copy gh:rahmansunny071/amplifactory my-project   # answers a few questions
cd my-project
./scripts/bootstrap.sh --public                              # or --private
```

The repo is private, so Copier needs your GitHub credentials. `gh auth setup-git` lets the `gh:` shortcut (HTTPS) use your `gh` login; or use `git@github.com:rahmansunny071/amplifactory.git`.

`bootstrap.sh` does what a template can't: `uv lock` and the first commit, creating and pushing the GitHub repo, the factory labels (including `improvement`, `lane`, `P0`–`P3` and `documentation`), the **"Documentation tech-debt"** tracking issue, a **GitHub Project board** with a Priority field (needs the `project` scope: `gh auth refresh -h github.com -s project`), a ruleset protecting `main` (no deletion or force push, PR with your approval, required checks `Tests (Python X)` and `Lint`, admin bypass for PRs only), and fork-PR approval. Rulesets need a **public** repo or GitHub Pro.

For the GitHub Actions mode, once per repo in Claude Code: `/install-github-app` → pick the repo → choose your subscription. That saves the `CLAUDE_CODE_OAUTH_TOKEN` secret. If it says the GitHub CLI lacks the `workflow` scope, run `gh auth refresh -h github.com -s repo,workflow` first. **Close the installer's PR** (`claude.yml`, `claude-code-review.yml`): `factory-chat.yml` and `factory-review.yml` already cover those, with the sender and fork checks the installer's versions lack. The skill needs no secret: it runs in your session.

## Run the factory from Claude Code (the `factory` skill)

Open Claude Code in the repo, on Opus, and say what you want:

| You say | What happens |
|---|---|
| *"brainstorm X into issues"* | A two-level conversation with Opus: first **lanes** (features or swim lanes, their priorities `P0`–`P3`, and which lanes depend on which), then the **ordered issues** inside each lane (only the next one or two fully written, the rest as stubs). **You confirm once**, and it creates the lane issues, native **sub-issues** in order, "blocked by" links, priority labels, and Project board items |
| *"resume the factory"* | Picks up PRs where you've answered its questions: applies your answers (Opus directly for small edits, a Sonnet builder otherwise), checks them, resolves the threads and merges |
| *"run the factory"* / *"pick up #N"* | For each ready issue (`scripts/queue.py`, main path first): Sonnet **plans** → the plan is checked (by Opus inline, or by an Opus design review for critical issues) → the same Sonnet agent **builds** and opens the PR → a fresh Opus **reviews** in parallel with CI → the same builder fixes **blockers** → Opus checks the gates in `references/gates.md` and **merges** |

**How work is organised: lanes.** A lane is a parent issue (label `lane`) whose native GitHub sub-issues are processed **one at a time, in order**. Lanes are ranked by priority and can depend on each other (native "blocked by" links). `scripts/queue.py` shows each lane's current issue and what to work on now. **One lane at a time by default**; when more lanes are ready, the skill asks you whether to run them in parallel. A question on one lane pauses only that lane. `scripts/lanes.py` creates and reorders lanes, sub-issues, dependencies and priorities.

**How it keeps cost and latency down** (v0.3.0, from real runs):
- **2 subagents per issue** (3 for critical ones): the builder is one agent reused for plan, build and fixes, so it never re-reads context. Only the reviewer is fresh, on purpose.
- **Urgency triage.** In the `mvp` release phase (`CLAUDE.md`), only **blockers** are fixed inside a PR: the main path breaks on realistic input, private data leaks, a hard rule is broken, a plausible invocation (even a mistaken one) irreversibly loses or overwrites the user's existing data (source files, history, remote repos) when a cheap guard would prevent it, or a test falsely passes. Everything else becomes one `improvement` issue per PR, and main-path issues never wait on improvements. Switch to `hardening` before a first formal release.
- **No re-review** of fixes in mvp: CI plus the orchestrator's reproduction of each blocker.
- **Run log:** `.factory/runs/*.jsonl` (gitignored) records each step, so a fresh session resumes mid-issue, and `scripts/runlog.py summary` reports time and tokens per issue. Start a fresh session every 2–3 issues.

**It never waits on you in the session. It flags and continues.** When it's unsure, when a reviewer raises a question, when a gate fails (workflows, unplanned dependencies, the version, `CLAUDE.md`), or when it would rely on an assumption about real data it can't verify, it posts the question **inline on the PR** with options and a recommendation, labels the issue and PR `factory:needs-maintainer`, and moves on to independent work. Reply in the threads (or use `@claude` on the PR to have the Actions agent apply it right away), then say "resume the factory". Nothing merges until every review thread is resolved; `bootstrap.sh` also turns on GitHub's "require conversation resolution".

**Ready-to-merge hand-off.** If every gate passes but the merge is refused (the harness denies `gh pr merge`, or GitHub rejects it), the skill never works around it. It labels the PR and issue `factory:ready-to-merge`, posts one comment with the exact merge command and the gate summary, and moves on. `queue.py` lists these under "Waiting for you to merge", and the end-of-run report lists them so you can merge in one batch. To let the skill merge itself, you can opt in with a permission rule (see `template/README.md.jinja` → "Letting the factory merge for you"); the template doesn't ship one. The rule matches only commands that end in `--delete-branch`, which is how the skill runs the merge; variants with `-R` or other trailing flags aren't covered.

**Docs ship with the code** (v0.6.0). A `CLAUDE.md` hard rule: every code change updates, in the same PR, the docs it affects (README, CHANGELOG, docstrings, `.factory/designs/`). The builder's Handoff has a **Docs** line (what it updated, or "none needed" and why), the reviewer checks docs against code in the changed area, and a merge gate checks the Docs line. Docs that disagree with the PR's code are a **blocker**, even in `mvp`. A mismatch that predates the PR is tagged `doc-debt` and filed as a sub-issue of the repo's **"Documentation tech-debt"** issue (`scripts/lanes.py doc-debt`); the builder fixes open ones when its PR already touches that file. The skill creates the tracking issue in repos bootstrapped before v0.6.0.

**Where knowledge lives.** GitHub and the repo are the source of truth: issues, PRs, review threads, plans and implementation notes (`.factory/designs/`), `CLAUDE.md`. Each PR keeps a **Handoff** section (what changed and why, decisions, how to test, what's open), so a fresh session or a replacement agent starts from it rather than from scratch, and its gist lands in the design file at merge. The run log is only a local cache and cost ledger that points at GitHub.

The two modes never trigger each other: the skill works on **`local/issue-N`** branches (the Actions review only watches `factory/issue-*`), and it never adds the Actions trigger labels (`factory:design`, `design:approved`) or writes `@claude`. If you only ever use the skill, you can also disable the Actions workflows with `gh workflow disable`.

**Using the skill in a repo not generated from this template:** copy `template/.claude/skills/factory/` from a release tag into the repo's `.claude/skills/factory/`, and add a "Release phase" section, a "Real data" section whose first line is `Real data: never.` (or `with-approval` / `allowed`; without it the skill treats the repo as `never`) and the docs-with-code hard rule to its `CLAUDE.md`. The skill expects the stage prompts in `.claude/commands/` and the labels above.

**Keeping such a repo up to date:** do not copy anything into the repo first. Extract the new release tag's `sync-root-factory/` into a temp dir **outside** the repo (a depth-1 fetch of the annotated tag from GitHub, then `git archive`; the exact commands are in [Adopt in an existing repo](docs/guides/adopt-in-existing-repo.md#keeping-the-skill-up-to-date)) and run **its** `sync.py --repo <repo root>`; every sync runs the target tag's script, never the copy in the repo. The first time, your existing `factory/` has no `VERSION`, so pass `--assume-tag <the tag your existing factory/ came from>`; after the first sync, `factory/VERSION` records the version. Then say "sync the factory to the latest release". The skill syncs **every skill folder the tag ships** (`factory/`, `factory-lesson/`, `sync-root-factory/`, and any future one): a folder the old tag had is replaced only if it still matches the tag it claims (any local edit stops it and is listed), a new folder is added, and a folder the old tag lacked but you already have is refused, never overwritten. Your own other skills are never touched. It ports the CHANGELOG's "must do" lines into `CLAUDE.md`, `.claude/commands/` and `README.md` by editing them, leaves policy values (such as the `Real data:` level) for you under "Open", and opens a PR for you to merge. Its files come only from the release tag, never from `main`.

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

You can also ask Claude Code in the project: **"sync the factory to the latest release"**. The `sync-root-factory` skill (shipped in every generated repo) works on a new `local/adopt-<tag>` branch, first runs `sync.py copier-check <tag>` (annotated tag, `_commit` a release tag, `_src_path` the GitHub template, not a local clone), then `copier update --vcs-ref <tag> --conflict inline`, resolves only mechanical conflicts, lists the rest in the PR's Handoff and never merges. It works for repos that only carry a copy of the skill too (see below).

## How the template evolves

The factory is tested by real use: run it on a project, and turn what goes wrong into template changes.

1. **A factory problem found while driving a project** (not a problem with the project's own code) is filed here as an issue labelled **`lesson`**, with what happened, the fix applied in that project, and the template files to change.
2. Lessons are batched into a **release**: a PR that closes them, a `CHANGELOG.md` entry, template CI green, then a tag (`v0.3.0`, …).
3. Projects pick up the release with `copier update`, or by copying the skill from the tag.

In a generated repo, the **`factory-lesson` skill** files step 1 for you: say "file a factory lesson: ...", and it finds this template repo (from `.copier-answers.yml`), checks open and closed lessons for a duplicate (a fixed one points you to the release and to updating), drafts the issue, keeps real data and private paths out of it, and creates it after one confirmation (edits to the draft are re-checked for real data first).

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
| `real_data_access` | `never` | `CLAUDE.md` "Real data": `never` (you run shapes-only scripts), `with-approval` (agents ask per issue, then read locally) or `allowed`. Real data is never committed or quoted at any level |
| `agent_model` | `sonnet` | `--model` for the GitHub Actions agents (the skill picks its own models) |

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

## Guardrails (kept in every generated repo)

- **Only you advance work.** Every Actions trigger checks the sender is `maintainer_github`, and a label guard strips trigger labels anyone else adds. The skill runs in your session and stops to ask at defined points.
- **Forks can't run agents.** Build, review and chat refuse PRs from forks, and fork-PR workflows need your approval.
- **Least privilege per stage.** Reviewers can't push. The build step refuses design branches that touch anything outside `.factory/designs/`. Per-stage tool allowlists apply.
- **Merge gates** (skill): CI green, no open blockers, **all review threads resolved**, Handoff present with its Docs line, no workflow, unplanned dependency, version or `CLAUDE.md` changes, deviations disclosed. Merges use merge commits, so release tags stay on `main`.
- **Bounded cost.** `timeout-minutes`, `--max-turns` and per-issue `concurrency` on every Actions job; pinned models; the skill's urgency triage and run-log cost report.
- Agents may not touch `.github/workflows/`, bump versions or publish unless the approved plan says so. Workflow changes go in their own PR.

## Developing the template

The documentation site is built from `docs/` with MkDocs Material (`uvx --with mkdocs-material mkdocs serve`) and published by `.github/workflows/docs.yml`; its changelog page is generated from `CHANGELOG.md`.


Template files live in `template/`; files ending in `.jinja` are rendered. Copier variables use `[[ … ]]` and blocks `[% … %]`, so GitHub Actions expressions (`${{ … }}`) stay untouched. `template-ci.yml` renders three variants and runs each generated project's checks, plus assertions that every guardrail above is present.

```bash
uvx copier copy --defaults --vcs-ref HEAD --data project_name=demo --data "description=Demo." . /tmp/demo
```

Once tags exist, `copier copy .` without `--vcs-ref HEAD` renders the **latest tag**, not your working tree.

Created by **[Sajjadur Rahman](https://sajjadur.net/)**.

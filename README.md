# amplifactory

**A [Copier](https://copier.readthedocs.io/) template for an issue-driven agent factory on Claude Code.**

📖 **Documentation:** https://rahmansunny071.github.io/amplifactory/

You plan with Claude: an idea becomes prioritized lanes of ordered GitHub issues. Then the factory takes each issue from plan to merged PR: a Sonnet builder plans, builds and fixes, a fresh Opus reviewer checks the work, and Opus merges when the gates pass. When it needs you, it asks on the PR and keeps working. Every generated repo gets this process, with two ways to run it: the interactive **`factory` skill** in Claude Code (recommended), and an optional, async **GitHub Actions mode**. Either way you stay the only one who can start work, approve it, merge to `main` or publish.

## Quickstart

You need Python 3.9 or newer (the skill's scripts run as `python3`), [uv](https://docs.astral.sh/uv/), git, the [GitHub CLI](https://cli.github.com/) (`gh`, logged in) and [Claude Code](https://claude.com/claude-code).

```bash
uvx copier copy gh:rahmansunny071/amplifactory my-project   # 1. answers a few questions
cd my-project
gh auth refresh -h github.com -s repo,workflow,project       # 2. gh scopes
./scripts/bootstrap.sh --public                              # 3. or --private
claude                                                       # 4. on Opus, then the factory skill
```

`bootstrap.sh` creates and pushes the GitHub repo, the labels, the "Documentation tech-debt" issue, a Project board and a ruleset protecting `main` (rulesets need a public repo or GitHub Pro). From step 4 the skill works and needs no secret. Say "brainstorm X into issues", then "run the factory".

**Optional, last: Actions mode.** In Claude Code, once per repo: `/install-github-app`, pick the repo, choose your subscription, then close the installer's PR. If it fails on the `workflow` scope, see [Troubleshooting](https://rahmansunny071.github.io/amplifactory/reference/troubleshooting/).

To let the skill merge PRs itself you can opt in with a permission rule; the one canonical copy is in [Flag and continue: Ready to merge](https://rahmansunny071.github.io/amplifactory/concepts/flag-and-continue/#ready-to-merge). The template does not ship it.

## Learn more

- **Get started:** [Create a project](docs/getting-started/create-a-project.md) (including what a generated repo contains), [Your first run](docs/getting-started/first-run.md)
- **Concepts:** [Lanes and priorities](docs/concepts/lanes.md), [The pipeline](docs/concepts/pipeline.md), [Flag and continue](docs/concepts/flag-and-continue.md), [Real data](docs/concepts/real-data.md), [Docs ship with the code](docs/concepts/docs-with-code.md), [Where knowledge lives](docs/concepts/where-knowledge-lives.md), [The GitHub Actions mode](docs/concepts/actions-mode.md)
- **Guides:** [Day-to-day](docs/guides/day-to-day.md), [Adopt in an existing repo](docs/guides/adopt-in-existing-repo.md), [Keep up to date](docs/guides/keep-up-to-date.md), [Evolving the template](docs/guides/evolving-the-template.md)
- **Reference:** [Copier questions](docs/reference/questions.md), [Scripts](docs/reference/scripts.md), [Merge gates](docs/reference/gates.md), [Labels](docs/reference/labels.md), [Guardrails](docs/reference/guardrails.md), [Troubleshooting](docs/reference/troubleshooting.md), [`CHANGELOG.md`](CHANGELOG.md)

## Developing the template

The documentation site is built from `docs/` with MkDocs Material (`uvx --with mkdocs-material mkdocs serve`) and published by `.github/workflows/docs.yml`. Template files live in `template/`; files ending in `.jinja` are rendered. `template-ci.yml` renders three variants and runs each generated project's checks, plus assertions that every guardrail is present.

```bash
uvx copier copy --defaults --vcs-ref HEAD --data project_name=demo --data "description=Demo." . /tmp/demo
```

Without `--vcs-ref HEAD`, Copier renders the latest tag, not your working tree.

Created by **[Sajjadur Rahman](https://sajjadur.net/)**.

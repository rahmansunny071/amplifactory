# amplifactory: agent guide

amplifactory is a Copier template (`rahmansunny071/amplifactory`) that generates a repo with an agent
factory: a `factory` skill, helper scripts, workflows and docs. This file is for agents working on the
template itself. `copier.yml` sets `_subdirectory: template`, so only `template/` ships and this root
file never reaches generated projects; they get `template/CLAUDE.md.jinja`, rendered.

## Release phase
mvp. In mvp, only blockers are fixed inside a PR: the main path breaks on realistic
input; private data leaks on realistic input; a hard-rule violation; irreversible loss or overwrite of
the user's existing data (source files, history, remote repos) on a plausible invocation, even a
mistaken one, when a cheap guard would prevent it; or a test that falsely passes. Everything else is
filed as an `improvement` issue. Change this line to `hardening` before a first formal release.

## Real data
Real data: never. There is no user data in this repo. Agents do not read real user data, never commit
it, and never quote values from it in issues, PRs or commits. Tests use synthetic fixtures only.

## Where knowledge lives
- **GitHub and the repo are the source of truth:** issues, PRs, review threads, labels, `CHANGELOG.md`,
  `docs/` and this file. Decisions and their reasons go here, where the maintainer and every agent can
  see them.
- **Each PR's "## Handoff" section** says what changed and why, the key decisions, how to test, the docs
  and what's open. Keep it current after every change.
- **Questions for the maintainer** go on the PR as inline review comments, with options and a
  recommendation. Work continues elsewhere until they're answered.

## Layout
- `template/`: everything that ships. Files ending in `.jinja` are rendered by Copier; the rest are
  copied verbatim, including `template/.claude/` (the skills, commands and settings).
- Delimiters are `[[ var ]]`, `[% block %]` and `[# comment #]`, so GitHub Actions `${{ }}` is untouched.
- `docs/`: the MkDocs Material site (`mkdocs.yml` at the root). `hooks/` (`changelog.py` builds the
  changelog page from `CHANGELOG.md`) and `overrides/` are part of that site.
- `.github/workflows/template-ci.yml`: renders the variants and checks the generated project.
- `scripts/check.sh`: runs the same checks locally. `scripts/test-check.sh` tests it against fake repos
  (Template CI's `check-sh` job); run it when you change `check.sh`.
- `template/.claude/skills/factory/VERSION`: the released version.

## Definition of "checks pass"
Commit first: the render uses `--vcs-ref HEAD`. Then run all of these:
1. `scripts/check.sh`: renders every Template CI variant (read from `template-ci.yml`) in parallel,
   runs every render step on each, then `mkdocs build --strict` and the `version` check. `-j N` (or
   `CHECK_JOBS=N`) limits the parallel jobs; `-j 1` runs the variants one at a time.
2. Each skill script's `--self-test`, under Python 3.9 and 3.12:
   ```
   for py in 3.9 3.12; do
     for s in factory/scripts/queue factory/scripts/lanes factory/scripts/runlog \
              sync-root-factory/scripts/sync; do
       uv run --no-project --python $py python template/.claude/skills/$s.py --self-test
     done
   done
   ```

If `template-ci.yml` changes, `scripts/check.sh` follows by itself. Never "fix" a failure by weakening
a guard or skipping a check.

## CI gotchas
- Two adjacent closing square brackets in a rendered script trip the unrendered-syntax guard. Use a
  type alias or split them.
- Template CI greps some rendered text (`need FILE 'text'`). Change the guard together with the text.
- Generated projects run `ruff format --check .` and `ruff check .` on the whole rendered project; the
  line length of 100 comes from `[tool.ruff]` in `template/pyproject.toml.jinja`. For hand edits to the
  skill scripts (not rendered by that file), use `uvx ruff format --line-length 100 <file>`.
- `copier copy .` renders the latest tag, not your work. Use `--vcs-ref HEAD` (and commit).

## Release procedure
1. Unreleased work goes under one `- Unreleased:` entry at the top of `CHANGELOG.md`, with a sub-bullet
   per issue (`#N`) saying what changed and why.
2. The release PR (`release/vX.Y.Z`) renames that entry to `- vX.Y.Z (date)` and bumps
   `template/.claude/skills/factory/VERSION` in the same commit. The `version` CI job compares them.
3. After the merge, an annotated `vX.Y.Z` tag goes on the merge commit (`git tag -a`, then push it).
4. Verify with a remote `copier copy` of the tag into a temp dir.

## Compatibility
The old name `rahmansunny071/agent-factory` stays accepted as an official `_src_path` (#67; GitHub
redirects the URL). `sync.py` and `factory-lesson` handle both names; don't remove that. Skill folder
names stay as they are.

## Hard rules
- Never commit secrets, credentials, personal data or private datasets. No local absolute paths or
  personal names in committed files.
- Every code change ships its docs in the same PR (`docs/`, README, CHANGELOG, `SKILL.md` text).
- Changes to workflow behaviour (triggers, permissions, jobs, secrets) go in a PR of their own. Text
  guards in `template-ci.yml` change together with the text they check.
- No new dependencies without a plan. The skill scripts are stdlib only, Python 3.9+.
- Never bump `VERSION` or tag outside a release PR.
- Never add the labels `factory:design` or `design:approved`.

## The factory
Work is tracked as GitHub issues grouped in lanes (P0 to P3). Each issue is built on a `local/issue-N`
branch and a PR that says "Closes #N", with a Handoff section, then reviewed and merged. Use the
`factory` skill (`template/.claude/skills/factory/SKILL.md`) to plan and run it. Factory problems found
along the way are filed as `lesson` issues in this repo.

## Style
- Match existing style. Small, reviewable diffs, no drive-by refactors.
- Python: type hints, ruff formatting at line length 100.

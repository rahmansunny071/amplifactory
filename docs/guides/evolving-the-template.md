---
title: "Evolving the template"
description: "How the factory improves: lessons found while driving real projects become issues, releases and copier updates."
---

# Evolving the template

Flow: file a lesson, release, projects update.

## 1. File a lesson

File it **when it happens**: say "file a factory lesson: ..." in a generated repo. See
[Keep up to date](keep-up-to-date.md#file-a-factory-problem-upstream).

## 2. Release

1. Open a branch `release/vX.Y.Z` and a PR that closes the lessons.
2. Add a `CHANGELOG.md` entry saying what changed and why, and set `template/.claude/skills/factory/VERSION` to
   `X.Y.Z`. Rename the `Unreleased` entry to `vX.Y.Z (date)` in the **same commit** as the bump; the `version`
   check in `scripts/check.sh` compares `VERSION` with the first `- vX.Y.Z` bullet.
3. Template CI is green on the PR and `scripts/check.sh` passes locally. Both render every variant in
   `.github/workflows/template-ci.yml`, run each generated project's checks, and assert every guardrail.
4. Merge, then tag `vX.Y.Z`. Copier only sees tags.

## 3. Update projects

Projects pick the release up with `copier update` or the `sync-root-factory` skill. See
[Keep up to date](keep-up-to-date.md).

## Develop the template

- Start with the repo's root `CLAUDE.md` (layout, release procedure, hard rules). It never ships.
- Run the local "checks pass" with `scripts/check.sh` (commit first). It runs the variants in parallel (`-j N` or `CHECK_JOBS`; `-h` for usage), and `scripts/test-check.sh` tests it (Template CI job `check-sh`, on Ubuntu and macOS). The skill scripts' `--self-test` runs are
  listed in `CLAUDE.md`.
- Template files live in `template/`; files ending in `.jinja` are rendered.
- Copier uses `[[ ... ]]` and `[% ... %]`, so GitHub Actions `${{ ... }}` stays untouched.
- The guardrail check scans rendered files for leftover template syntax. Avoid look-alike sequences in Python,
  such as two closing square brackets in a row; use type aliases.
- To render the working tree instead of the latest tag, use `--vcs-ref HEAD`.
- The docs in `docs/` are the source of the docs site (GitHub Pages). A release publishes them with
  `scripts/publish_docs.py vX.Y.Z`; see `CLAUDE.md`.

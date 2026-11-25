---
title: "Evolving the template"
description: "How the factory improves: lessons found while driving real projects become issues, releases and copier updates."
---

# Evolving the template

The factory is tested by real use. A project driven through it is the **proving ground**; what goes wrong becomes
template changes.

## 1. File a lesson

File it **when it happens**, not at the end of the run. In a generated repo, ask Claude Code to "file a factory lesson: ..." and the `factory-lesson` skill does it; see [Keep up to date](keep-up-to-date.md#file-a-factory-problem-upstream).

The issue says what happened (with links), the fix applied in the project if any, and the template files to change.

## 2. Release

Lessons are batched into a release:

1. a branch `release/vX.Y.Z` and a PR that closes the lessons;
2. a `CHANGELOG.md` entry saying what changed **and why**, and `template/.claude/skills/factory/VERSION` set to `X.Y.Z`. Rename the `Unreleased` entry to `vX.Y.Z (date)` in the **same commit** as the bump; template CI's `version` job compares `VERSION` with the first `- vX.Y.Z` bullet, so a forgotten bump fails the PR;
3. template CI green: it renders three variants, runs each generated project's checks, and asserts every guardrail
   is present;
4. you merge, then tag `vX.Y.Z`. Copier only sees tags.

## 3. Update projects

Projects pick the release up with `copier update` or the `sync-root-factory` skill: see [Keep up to date](keep-up-to-date.md).

## Developing the template

- Template files live in `template/`; files ending in `.jinja` are rendered.
- Copier variables use `[[ … ]]` and blocks `[% … %]`, so GitHub Actions expressions (`${{ … }}`) stay untouched.
- The guardrail check also scans rendered files for leftover template syntax. Python code must avoid sequences that
  look like it, such as two closing square brackets in a row; use type aliases.
- To render the working tree instead of the latest tag, use `--vcs-ref HEAD`.
- This site is built from `docs/` with MkDocs Material. The changelog page is generated from `CHANGELOG.md`.

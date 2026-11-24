---
title: "Evolving the template"
description: "How the factory improves: lessons found while driving real projects become issues, releases and copier updates."
---

# Evolving the template

The factory is tested by real use. A project driven through it is the **proving ground**; what goes wrong becomes
template changes.

## 1. File a lesson

A **factory problem** (not a bug in the project's own code) found while driving a project is filed in agent-factory
as an issue labelled **`lesson`**:

- what happened, with links;
- the fix applied in the project, if any;
- the template files to change.

File it **when it happens**, not at the end of the run.

## 2. Release

Lessons are batched into a release:

1. a branch `release/vX.Y.Z` and a PR that closes the lessons;
2. a `CHANGELOG.md` entry saying what changed **and why**;
3. template CI green: it renders three variants, runs each generated project's checks, and asserts every guardrail
   is present;
4. you merge, then tag `vX.Y.Z`. Copier only sees tags.

## 3. Update projects

```bash
cd my-project && uvx copier update
```

Copier does a three-way merge and marks conflicts. For a project that only carries the skill, copy it from the tag
(see [Adopt in an existing repo](adopt-in-existing-repo.md#option-2-just-the-skill)) in a PR you merge.

## Developing the template

- Template files live in `template/`; files ending in `.jinja` are rendered.
- Copier variables use `[[ … ]]` and blocks `[% … %]`, so GitHub Actions expressions (`${{ … }}`) stay untouched.
- The guardrail check also scans rendered files for leftover template syntax. Python code must avoid sequences that
  look like it, such as two closing square brackets in a row; use type aliases.
- To render the working tree instead of the latest tag, use `--vcs-ref HEAD`.
- This site is built from `docs/` with MkDocs Material. The changelog page is generated from `CHANGELOG.md`.

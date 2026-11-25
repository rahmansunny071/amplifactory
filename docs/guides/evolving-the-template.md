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

In a generated repo, ask Claude Code to "file a factory lesson: ..." and the **`factory-lesson` skill** does it:

1. finds the template repo from `.copier-answers.yml` (`_src_path`), used only when it is a GitHub source (`gh:`, `https://github.com/`, `ssh://git@github.com/`, `git@github.com:`); a local path, an empty value or a missing file falls back to `rahmansunny071/agent-factory`, and confirms it with `gh repo view`; it never files in the current repo;
2. searches open `lesson` issues for a duplicate and offers to comment there instead;
3. drafts the body (What happened, Fix applied in the generated repo, Template files to change, Options, Takeaway) and checks it for real data values, secrets and private paths, since the issue goes to another repo;
4. asks you **once**, then creates it with the `lesson` label and reports the URL;
5. offers a comment on the downstream PR or issue linking the lesson, only if you agree.

It needs a `gh` login with access to the template repo (private repos need one).

## 2. Release

Lessons are batched into a release:

1. a branch `release/vX.Y.Z` and a PR that closes the lessons;
2. a `CHANGELOG.md` entry saying what changed **and why**, and `template/.claude/skills/factory/VERSION` set to `X.Y.Z`. Rename the `Unreleased` entry to `vX.Y.Z (date)` in the **same commit** as the bump; template CI's `version` job compares `VERSION` with the first `- vX.Y.Z` bullet, so a forgotten bump fails the PR;
3. template CI green: it renders three variants, runs each generated project's checks, and asserts every guardrail
   is present;
4. you merge, then tag `vX.Y.Z`. Copier only sees tags.

## 3. Update projects

```bash
cd my-project && uvx copier update
```

Copier does a three-way merge and marks conflicts. Or, in Claude Code inside the project, say
**"sync the factory to the latest release"**: the `sync-root-factory` skill does it on a `local/adopt-<tag>` branch
and opens a PR (mechanical conflicts resolved, the rest listed for you; it never merges). It also works for a
project that only carries the skill: it replaces `.claude/skills/factory/` from the tag after checking it matches
the version it claims, and builds a hand-port checklist from the CHANGELOG entries. See
[Adopt in an existing repo](adopt-in-existing-repo.md#keeping-the-skill-up-to-date) for the first-time setup.

## Developing the template

- Template files live in `template/`; files ending in `.jinja` are rendered.
- Copier variables use `[[ … ]]` and blocks `[% … %]`, so GitHub Actions expressions (`${{ … }}`) stay untouched.
- The guardrail check also scans rendered files for leftover template syntax. Python code must avoid sequences that
  look like it, such as two closing square brackets in a row; use type aliases.
- To render the working tree instead of the latest tag, use `--vcs-ref HEAD`.
- This site is built from `docs/` with MkDocs Material. The changelog page is generated from `CHANGELOG.md`.

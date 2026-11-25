---
title: "Adopt in an existing repo"
description: "Bring the factory into a repository that already exists, either the whole template or just the skill."
---

# Adopt in an existing repo

## Option 1: the whole template

Run Copier **into** the existing repo. It asks the same questions and shows a conflict for every file that already
exists:

```bash
cd existing-repo
uvx copier copy gh:rahmansunny071/amplifactory .
```

Keep your own `pyproject.toml`, package and tests where they conflict. Take `CLAUDE.md`, `.claude/`, `.factory/`,
`.github/` and `scripts/bootstrap.sh` from the template, then merge your existing CI into `ci.yml` and the "checks
pass" list in `CLAUDE.md`, which must stay identical. Run `./scripts/bootstrap.sh`: it skips what already exists.

Because `.copier-answers.yml` now exists, later releases come in with `uvx copier update`.

## Option 2: just the skill

If you only want the Claude Code skill:

```bash
URL=https://github.com/rahmansunny071/amplifactory.git; TAG=vX.Y.Z   # a release tag
T=$(mktemp -d)                                  # outside the repo
git init -q --bare "$T/src"
git -C "$T/src" fetch -q --depth 1 "$URL" "refs/tags/${TAG}:refs/tags/${TAG}"
test "$(git -C "$T/src" cat-file -t "refs/tags/$TAG")" = tag || { echo "not annotated: stop"; false; }  # must be tag
git -C "$T/src" archive "$TAG" template/.claude/skills | tar -x -k --strip-components=2 -C .claude/
```

Use a release **tag**, never `main`: the skill itself refuses anything else. Releases up to v0.7.0 have no
`sync-root-factory` folder, so use the first release that ships it. `tar -k` keeps files you already have, so a skill of the
same name is not overwritten (check `.claude/skills/` for one). Commit the result. This is for a repo that has
no factory skill yet; if you already have `factory/`, follow
[Keeping the skill up to date](keep-up-to-date.md#update-a-repo-that-only-carries-the-skill) instead.

Then add to your `CLAUDE.md`:

- a **Release phase** line (`mvp` or `hardening`) and the blocker definition;
- a **Real data** section whose first line is `Real data: never.` (or `with-approval` / `allowed`); without it the skill treats the repo as `never`;
- a **"checks pass"** list identical to your CI;
- the **hard rules** you want enforced (see the template's `CLAUDE.md.jinja`), including "every code change
  ships its docs in the same PR";
- **Where knowledge lives**.

And create the labels the skill uses: `factory:needs-maintainer`, `factory:ready-to-merge`, `improvement`, `lane`, `P0`–`P3`,
`documentation`. The skill creates the "Documentation tech-debt" issue itself the first time it needs it.

### Keeping the skill up to date

See [Keep up to date](keep-up-to-date.md#update-a-repo-that-only-carries-the-skill): the first sync (with `--assume-tag`), the check, and what the sync changes.


## Organise existing issues into lanes

> organise the open issues into lanes

Opus proposes lanes, an order and priorities; after your confirmation it wires them with `lanes.py`. That's how
pensieve-traces moved to lanes: four lanes, thirteen issues, four "blocked by" links.

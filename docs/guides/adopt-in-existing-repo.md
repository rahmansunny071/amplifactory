---
title: "Adopt in an existing repo"
description: "Bring the factory into a repository that already exists, either the whole template or just the skill."
---

# Adopt in an existing repo

## Option 1: the whole template

Run Copier **into** the repo. It shows a conflict for every file that already exists.

```bash
cd existing-repo
uvx copier copy gh:rahmansunny071/amplifactory .
```

- Keep your own `pyproject.toml`, package and tests where they conflict.
- Take `CLAUDE.md`, `.claude/`, `.factory/`, `.github/` and `scripts/bootstrap.sh` from the template.
- Merge your CI into `ci.yml` and the "checks pass" list in `CLAUDE.md`; they must stay identical.
- Run `./scripts/bootstrap.sh`. It skips what already exists.

Later releases come in with `uvx copier update`.

## Option 2: just the skill

For a repo with no factory skill yet. If you already have `factory/`, see
[Keep up to date](keep-up-to-date.md#update-a-repo-that-only-carries-the-skill) instead.

```bash
URL=gh:rahmansunny071/amplifactory; TAG=vX.Y.Z   # a release tag
T=$(mktemp -d)                                  # outside the repo
git init -q --bare "$T/src"
git --git-dir="$T/src" fetch -q --depth 1 "$URL" "refs/tags/${TAG}:refs/tags/${TAG}"
test "$(git --git-dir="$T/src" cat-file -t "refs/tags/$TAG")" = tag || { echo "not annotated: stop"; false; }  # must be tag
git --git-dir="$T/src" archive "$TAG" template/.claude/skills | tar -x -k --strip-components=2 -C .claude/
```

- Use a release **tag**, never `main`; the skill refuses anything else.
- Releases up to v0.7.0 have no `sync-root-factory`; use the first release that ships it.
- `tar -k` keeps files you already have, so a same-named skill is not overwritten.
- Commit the result.

Then add to your `CLAUDE.md`:

- **Release phase**: `mvp` or `hardening`, and the blocker definition;
- **Real data**: first line `Real data: never.` (or `with-approval` / `allowed`); without it the skill assumes
  `never`;
- **Models**: the model for each role (orchestrator, builder, reviewer) in Claude Code and GitHub Copilot, copied
  from the template's `CLAUDE.md.jinja`; without it the skill uses the template's defaults;
- **"checks pass"**: a list identical to your CI;
- **Hard rules** you want enforced (see `CLAUDE.md.jinja`), including "every code change ships its docs in the
  same PR";
- **Where knowledge lives**.

Create the labels the skill uses: `factory:needs-maintainer`, `factory:ready-to-merge`, `improvement`, `lane`,
`P0` to `P3`, `documentation`. The skill creates the "Documentation tech-debt" issue itself when needed.

To update later, see [Keep up to date](keep-up-to-date.md#update-a-repo-that-only-carries-the-skill).

## Organise existing issues into lanes

> organise the open issues into lanes

The orchestrator proposes lanes, an order and priorities; after you confirm, it wires them with `lanes.py`.

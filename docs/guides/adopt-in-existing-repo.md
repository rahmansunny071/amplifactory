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
uvx copier copy gh:rahmansunny071/agent-factory .
```

Keep your own `pyproject.toml`, package and tests where they conflict. Take `CLAUDE.md`, `.claude/`, `.factory/`,
`.github/` and `scripts/bootstrap.sh` from the template, then merge your existing CI into `ci.yml` and the "checks
pass" list in `CLAUDE.md`, which must stay identical. Run `./scripts/bootstrap.sh`: it skips what already exists.

Because `.copier-answers.yml` now exists, later releases come in with `uvx copier update`.

## Option 2: just the skill

If you only want the Claude Code skill:

```bash
git -C /path/to/agent-factory archive <tag> template/.claude/skills | tar -x --strip-components=2 -C .claude/
```

Use a release **tag**, never `main`: the skill itself refuses anything else. Releases up to v0.7.0 have no
`sync-root-factory` folder, so use the first release that ships it. `tar -x` overwrites a skill of the same name
you already have, so check `.claude/skills/` first. Commit the result. This is for a repo that has
no factory skill yet; if you already have `factory/`, follow
[Keeping the skill up to date](#keeping-the-skill-up-to-date) instead.

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

`.claude/skills/factory/VERSION` (written by the sync, and shipped from the first release that has it) records
which release the copy came from. Then, in Claude Code in the repo:

> sync the factory to the latest release

The `sync-root-factory` skill:

- works on a new branch `local/adopt-<tag>` cut from a fresh `origin/<default branch>`, and refuses a dirty tree;
- replaces **every skill folder the tag ships** (`factory/`, `factory-lesson/`, `sync-root-factory/` and any future
  one) byte for byte from the **tag**. A folder the old tag had is replaced only after it matches the version the
  repo claims; a new one is added; a folder the old tag did not have but you already have (your own skill, or a
  hand copy) is refused, never overwritten; your own other skills are never touched;
- turns the CHANGELOG entries since your version into a checklist; it edits `CLAUDE.md`, `.claude/commands/*`,
  `README.md` and `CHANGELOG.md` for the ports it can do safely, never sets a policy value (the `Real data:`
  level, the release phase), creates only the labels that are missing, and lists everything else under "Open";
- opens a PR with a Handoff and never merges it.

**Every sync, the first included:** run the **target tag's** `sync.py`, extracted outside the repo, never the copy in
the repo (which may predate the guards, and does not exist yet the first time). Nothing is committed before the
adopt branch exists, so this works on a protected `main`:

```bash
R=$(git rev-parse --show-toplevel); URL=https://github.com/rahmansunny071/agent-factory.git; TAG=vX.Y.Z  # latest release
T=$(mktemp -d)                                  # outside the repo
git init -q --bare "$T/src"
git -C "$T/src" fetch -q --depth 1 "$URL" "refs/tags/$TAG:refs/tags/$TAG"
git -C "$T/src" cat-file -t "refs/tags/$TAG"    # must print: tag  (annotated)
mkdir "$T/x" && git -C "$T/src" archive "$TAG" template/.claude/skills/sync-root-factory | tar -x -C "$T/x"
python3 -I "$T/x/template/.claude/skills/sync-root-factory/scripts/sync.py" --repo "$R" status --assume-tag v0.7.0
```

`--assume-tag` is needed only while `factory/VERSION` is missing (the first sync): it stands in for the file, and
the folder is still checked against that tag. Then ask Claude to sync; the skill runs the same extracted script.
The old way (copy `sync-root-factory/` into the repo and commit it) is no longer needed: the adopt branch is cut
from `origin/<default>`, which would not contain that commit. If you already committed one and the old tag did not ship it (before v0.8.0), `apply` refuses it; on `local/adopt-<tag>` run `git rm -r .claude/skills/sync-root-factory`, commit, and
rerun.

**If the check fails** (your copy matches no tag, for example because a `main`-only commit was layered on a tag):
the sync stops and lists the differing files. There is no override. If the edits are unwanted, restore the tag's
copy yourself (from the extracted tree above: `cp -R "$T/x/..."`, or `git archive "$TAG" template/.claude/skills/factory`
in the bare repo), commit,
commit, and rerun; if they are wanted, port them after the sync.

A repo generated by Copier doesn't need any of this: it records its version in `.copier-answers.yml`, and the
sync runs `copier update`, after `sync.py copier-check <tag>` confirms the tag is annotated, `_commit` is a release
tag and `_src_path` is the GitHub template (not a local clone).

## Organise existing issues into lanes

> organise the open issues into lanes

Opus proposes lanes, an order and priorities; after your confirmation it wires them with `lanes.py`. That's how
pensieve-traces moved to lanes: four lanes, thirteen issues, four "blocked by" links.

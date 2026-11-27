---
title: "Keep up to date"
description: "Update a generated or adopting repo to the latest amplifactory release with sync-root-factory, and file factory problems upstream with factory-lesson."
---

# Keep up to date

Both skills run in GitHub Copilot or Claude Code inside your project: **`sync-root-factory`** brings in releases,
**`factory-lesson`** files factory problems upstream.

## Update a generated repo

Copier records the version in `.copier-answers.yml`. Run `uvx copier update`, or say "sync the factory to the
latest release". The skill opens a PR for you to merge; it never merges.

- Before `copier update`, `sync.py copier-check <tag>` confirms the tag is annotated, `_commit` is a release tag,
  and `_src_path` is the GitHub template (not a local clone).
- Copier does a three-way merge and marks conflicts. The skill resolves the mechanical ones and lists the rest.
- Tags come from the template `_src_path` names: `gh:rahmansunny071/amplifactory`, or the old
  `gh:rahmansunny071/agent-factory` (it still works and means the same repo).

## Update a repo that only carries the skill

A repo that adopted only the `factory/` skill (see [Adopt in an existing repo](adopt-in-existing-repo.md)) has no
`.copier-answers.yml`. `.claude/skills/factory/VERSION` records the release the copy came from. Say "sync the
factory to the latest release". The skill:

- works on a new branch `local/adopt-<tag>` cut from a fresh `origin/<default branch>`, and refuses a dirty tree;
- replaces **every skill folder the tag ships** (`factory/`, `factory-lesson/`, `sync-root-factory/`, ...) byte for
  byte from the **tag**. A folder the old tag had is replaced only after it matches the version the repo claims. A
  new folder is added. A folder the old tag did not have but you already have (your own skill, a hand copy) is
  refused, never overwritten. Your other skills are never touched;
- turns the CHANGELOG entries since your version into a checklist. It edits `CLAUDE.md`, `.claude/commands/*`,
  `README.md` and `CHANGELOG.md` for the ports it can do safely, never sets a policy value (the `Real data:`
  level, the release phase), creates only missing labels, and lists everything else under "Open";
- opens a PR with a Handoff and never merges it.

Every sync, the first included, runs the **target tag's** `sync.py`, extracted outside the repo. Never run the
copy in the repo: it may predate the guards, and on the first sync it does not exist. Nothing is committed before
the adopt branch exists, so this works on a protected `main`.

```bash
R=$(git rev-parse --show-toplevel); TAG=vX.Y.Z  # latest release
URL=https://github.com/rahmansunny071/amplifactory.git
gitc() { GIT_TERMINAL_PROMPT=0 git -c 'credential.helper=!gh auth git-credential' "$@"; }  # gh's token, for a repo git can't read anonymously
T=$(mktemp -d)                                  # outside the repo
git init -q --bare "$T/src"
gitc --git-dir="$T/src" fetch -q --depth 1 "$URL" "refs/tags/${TAG}:refs/tags/${TAG}"
git --git-dir="$T/src" cat-file -t "refs/tags/$TAG"    # must print: tag  (annotated)
mkdir "$T/x" && git --git-dir="$T/src" archive "$TAG" template/.claude/skills/sync-root-factory | tar -x -C "$T/x"
python3 -I "$T/x/template/.claude/skills/sync-root-factory/scripts/sync.py" --repo "$R" status
# first sync only, while factory/VERSION is missing: add --assume-tag <tag the folder was copied from>
```

- `--assume-tag` stands in for the missing `factory/VERSION` on the first sync; the folder is still checked
  against that tag. Leave it out later; `sync.py` refuses one that disagrees with `VERSION`.
- Then ask the agent to sync; it runs the same extracted script.
- Do not copy `sync-root-factory/` into the repo and commit it: the adopt branch is cut from `origin/<default>`,
  which would not contain it. If you did and the old tag did not ship it (before v0.8.0), `apply` refuses it. On
  `local/adopt-<tag>`, run `git rm -r .claude/skills/sync-root-factory`, commit, and rerun.

### If the check fails

If your copy matches no tag (for example a `main`-only commit on top of a tag), the sync stops and lists the
differing files. There is no override.

- **Edits wanted:** port them after the sync.
- **Edits unwanted:** on `local/adopt-<target tag>` (a commit on local `main` is not on that branch), restore the
  **recorded** tag: the version in `factory/VERSION` or your `--assume-tag`, not the target tag. Then rerun the
  sync.

```bash
OLD=v0.7.0                                      # the recorded tag
git init -q --bare "$T/old"
git --git-dir="$T/old" fetch -q --depth 1 "$URL" "refs/tags/${OLD}:refs/tags/${OLD}"
test "$(git --git-dir="$T/old" cat-file -t "refs/tags/$OLD")" = tag || { echo "not annotated: stop"; false; }
rm -rf "$R/.claude/skills/factory"
git --git-dir="$T/old" archive "$OLD" template/.claude/skills/factory | tar -x --strip-components=2 -C "$R/.claude/"
git -C "$R" add -A .claude/skills/factory && git -C "$R" commit -m "restore factory/ to $OLD"
```

## File a factory problem upstream

A **factory problem** (not a bug in the project's own code) is filed in amplifactory as an issue labelled
**`lesson`**, **when it happens**, not at the end of the run. It states what happened (with links), the fix applied
in the project if any, and the template files to change. See [Evolving the template](evolving-the-template.md).

In a generated repo, say "file a factory lesson: ...". The **`factory-lesson` skill**:

1. Finds the template repo from `_src_path` in `.copier-answers.yml`, only if it is a GitHub source (`gh:`,
   `https://github.com/`, `ssh://git@github.com/`, `git@github.com:`). A local path, empty value or missing file
   falls back to `rahmansunny071/amplifactory`, the template's own repo. The old slug `rahmansunny071/agent-factory` (any case) maps to it. It confirms the slug with `gh repo view` and never files in the current repo.
2. Searches open and closed `lesson` issues for a duplicate. An open match gets a comment instead of a new issue. A
   closed match is already fixed: it points to the fixing release and to updating (`copier update` or
   `sync-root-factory`).
3. Drafts the body (What happened, Fix applied in the generated repo, Template files to change, Options, Takeaway)
   and checks it for real data, secrets and private paths, since the issue goes to another repo.
4. Asks you **once**. If you edit the draft, it re-checks the text and shows the result before filing. It creates
   the issue with the `lesson` label and reports the URL.
5. Offers a comment on the downstream PR or issue linking the lesson, only if you agree.

It needs a `gh` login with access to the template repo.

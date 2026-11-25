---
title: "Troubleshooting"
description: "Problems met while running the factory for real, and their fixes."
---

# Troubleshooting

??? question "Copier generates an old version of the template"
    Copier renders the **latest tag**, not your working tree. Tag a release, or pass `--vcs-ref HEAD` while
    developing the template.

??? question "`bootstrap.sh` skips the Project board"
    Your `gh` login lacks the `project` scope. Run `gh auth refresh -h github.com -s project` and re-run the script.

??? question "The ruleset isn't enforced"
    On GitHub Free, rulesets don't apply to private repos. Make the repo public or use GitHub Pro.

??? question "An Actions stage didn't run on a PR"
    A PR that changes a workflow skips that workflow's stage. Merge workflow changes in their own PR, then click
    **Update branch** on the open PRs: PR runs use the merge ref from their last update.

??? question "The Actions review job is green but posted nothing"
    PRs authored by `claude[bot]` need `allowed_bots` and the right `restrict_untrusted_prs` setting in the
    review workflow.

??? question "The factory won't merge although CI is green"
    Check the [gates](gates.md): usually an unresolved review thread (gate 11), a branch behind `main` (gate 2) or
    a change under `.github/`, `.claude/` or `CLAUDE.md` (gates 6 and 8), which you merge yourself.

??? question "A lane doesn't start"
    `queue.py` shows why: `waiting` means a "blocked by" link to an open issue or lane, `asked` means a question is
    waiting for you. Other ready lanes only start in parallel after you allow it.

??? question "`lanes.py` fails with a 404 or 422"
    The sub-issue and dependency APIs need the issue's internal `id`, which `lanes.py` looks up for you. A 422 on
    `sub-add` usually means the issue already has a different parent: an issue can only be in one lane.

??? question "`python: command not found`"
    The skill's scripts run as `python3`: stock macOS has no `python`. Use `python3 .claude/skills/factory/scripts/...`,
    and make sure Python 3.9 or newer is installed.

??? question "The Documentation tech-debt issue is missing"
    Repos bootstrapped before v0.6.0 don't have it, and a bootstrap that stopped early may not have created it.
    `python3 .claude/skills/factory/scripts/lanes.py doc-debt find` prints its number and creates it if missing.

??? question "`sync.py` prints `refused: …`"
    A guard fired and nothing was written. The common ones:

    - **`working tree is not clean`**: commit or stash, then rerun.
    - **`branch local/adopt-<tag> already exists`** (locally or on origin): an earlier sync left it. Continue on it,
      or delete it and rerun.
    - **`differs: <file>`** or an `extra:` file in a skill folder: your local copy no longer matches the tag it
      claims, so the sync won't overwrite your edits. Restore the file, or move your own changes out, then rerun.
    - **`<tag> is not an annotated tag`**: syncs trust only annotated release tags. Use a real `vX.Y.Z` release.
    - **`_src_path … is not … on GitHub (a local clone or a fork?)`**: `copier-check` wants `_src_path: gh:rahmansunny071/amplifactory`
      in `.copier-answers.yml` (edit the existing line, don't add a second). The old repo name is also accepted.
    - **`this is amplifactory itself (or a fork of it)`**: the sync never runs in the template repo.

??? question "`/install-github-app` fails with a workflow-scope error"
    The installer pushes workflow files, which needs the `workflow` scope on your `gh` login. Run
    `gh auth refresh -h github.com -s repo,workflow`, then run `/install-github-app` again.

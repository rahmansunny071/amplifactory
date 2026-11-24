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

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
    On GitHub Free, rulesets don't apply to private repos. Make the repo public or use GitHub Pro. EMU
    (enterprise-managed) accounts can only create private repos.

??? question "`bootstrap.sh --public` fails on a work account"
    EMU (enterprise-managed) accounts can't create public repos. Re-run `./scripts/bootstrap.sh --private`: the script
    is safe to re-run.

??? question "The repo has no `factory-*.yml` workflows"
    It was generated with `actions_agent: none`, the default, so the factory runs from the skill. To add the Actions
    mode, run `uvx copier update --data actions_agent=copilot` (or `=claude`) in a PR of its own. With `copilot`,
    then run the command the update prints, which lets workflows open pull requests.

??? question "An Actions stage didn't run on a PR"
    A PR that changes a workflow skips that workflow's stage. Merge workflow changes in their own PR, then click
    **Update branch** on the open PRs: PR runs use the merge ref from their last update.

??? question "Actions mode with Copilot: a build PR shows workflows awaiting approval"
    GitHub holds the runs of a PR the workflow token opens until someone approves them, and `factory-build`'s
    `approve-runs` job approves them. If that job failed or didn't run, or warned that runs were still appearing
    after 3 minutes, click **Approve workflows to run** on the PR.

    Only those runs count as the PR's required checks: a CI run you start with `gh workflow run` doesn't.

??? question "Actions mode with Copilot: the design job, or the build's `approve-runs` job, fails with \"opened no PR\""
    The agent stopped without its PR. Its log and the issue (or design PR) comments say why. If its `gh pr create`
    failed with "GitHub Actions is not permitted to create or approve pull requests", turn on **Allow GitHub Actions
    to create and approve pull requests** (Settings → Actions → General), or run
    `gh api -X PUT repos/OWNER/REPO/actions/permissions/workflow -F can_approve_pull_request_reviews=true`.

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
    A guard fired. Unless the message says otherwise, nothing was written. The common ones:

    - **`working tree is not clean`**: commit or stash, then rerun.
    - **`branch local/adopt-<tag> already exists`** (locally or on origin): an earlier sync left it. Continue on it,
      or delete it and rerun.
    - **`the skill folders differ from <tag>`** (items `modified:`, `missing locally:`, `untracked:`, `only local:`): your local copy no longer matches the tag it claims. Restore the files from that tag, or move your own changes out, then rerun.
    - **`diff -rq after apply failed`** (items `differs:`, `extra:`): files *were* written but don't match the tag.
      Run the undo command the message prints, then report it.
    - **`<tag> is not an annotated tag`**: use a real `vX.Y.Z` release tag.
    - **`_src_path … is not … on GitHub (a local clone or a fork?)`**: `copier-check` wants `_src_path: gh:rahmansunny071/amplifactory`
      in `.copier-answers.yml` (edit the existing line, don't add a second). The old repo name is also accepted.
    - **`this is amplifactory itself (or a fork of it)`**: the sync never runs in the template repo.

??? question "Actions mode with Claude: `/install-github-app` fails with a workflow-scope error"
    The installer pushes workflow files, which needs the `workflow` scope on your `gh` login. Run
    `gh auth refresh -h github.com -s repo,workflow`, then run `/install-github-app` again.

??? question "CI jobs wait forever (`Queued`) on a repo that uses the runner pool"
    Run `python3 .claude/skills/factory-ci/scripts/setup.py doctor --repo OWNER/REPO` and
    `python3 .claude/skills/factory-ci/scripts/runners.py status`.

    - **Container mode and Docker isn't running** (`doctor` or `status` shows a `docker:` line; the pool also looks
      down): start Docker Desktop, or `setup.py container off --yes`. Skip the next bullet.
    - **Pool down** (exit code 3, no heartbeat for 2 minutes): `python3 .claude/skills/factory-ci/scripts/setup.py service install --yes`
      restarts it, once the runner is installed (`runners.py base --install`).
    - **Paused**: `runners.py resume`.
    - **Repo not registered, or `FACTORY_RUNNER` not `local`**: `setup.py apply register --repo OWNER/REPO`, then
      `setup.py apply route --repo OWNER/REPO --yes`.
    - **PR from a fork**: it never runs on the pool.
    - **Draft PR**: CI skips drafts; mark it ready.

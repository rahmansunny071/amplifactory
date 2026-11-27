---
title: "Quick start"
description: "From nothing to a merged PR with CI on your Mac's runner pool: create a private project from the template, bootstrap it, plan one small CLI into issues and run the factory."
---

# Quick start

You'll create a private project from amplifactory, put its CI on your Mac's runner pool, turn one small idea
into issues and let the factory take them to merged PRs. The example is **slugify**, a CLI that turns a title into a
URL slug. It's offline, stdlib only and has trivial tests. It takes about 20 minutes plus the factory's run time.

The commands and output below are from a real run of this path on macOS with a GitHub Enterprise Managed User (EMU) account, where hosted runners are off.
Names are swapped for `slugify-demo`.

## 0. Check your tools

```bash
python3 --version          # 3.9 or newer
uv --version
git --version
gh auth status             # logged in to github.com
copilot --version          # GitHub Copilot CLI (or use the Copilot app)
```

!!! note "Where GitHub-hosted runners are off"
    That is the case for EMU personal repos. CI then needs the [runner pool](../concepts/local-ci.md) on your
    Mac. Step 4 sets it up from inside Copilot. If your account has hosted runners, skip step 4: CI runs on `ubuntu-latest`.

## 1. Generate the project

```bash
cd ~/scratch        # anywhere that is not inside another git repo
uvx copier copy gh:rahmansunny071/amplifactory slugify-demo
```

Copier uses the newest release tag. To pin one, add `--vcs-ref vX.Y.Z`. Press Enter for the defaults, except:

| Question | Answer |
|---|---|
| `project_name` | Enter: it defaults to the folder name, `slugify-demo`. The GitHub repo gets this name |
| `cli_name` | `slugify` |
| `description` | `Turn a title into a URL slug.` |
| `agent_context` | `A tiny offline CLI that turns a title into a URL slug.` |

Set `maintainer_github` to **your** GitHub login: it is the only login the workflows obey (the default is the
template author's). Set `author_name`, `author_email` and `homepage` to yours too. All the questions are in
[Copier questions](../reference/questions.md).

It ends with `Generated slugify-demo.` and the next steps. Check what it recorded:

```bash
cd slugify-demo
grep -E "_src_path|_commit|maintainer_github" .copier-answers.yml
```

```text
_commit: vX.Y.Z
_src_path: gh:rahmansunny071/amplifactory
maintainer_github: <you>
```

## 2. Give `gh` its scopes

```bash
gh auth refresh -h github.com -s repo,workflow,project
```

It changes nothing if your token already has them.

## 3. Bootstrap, private

```bash
./scripts/bootstrap.sh --private
```

It creates the GitHub repo, the labels, the "Documentation tech-debt" issue, the ruleset protecting `main` and the
Project board. It's safe to re-run. Check it:

```bash
gh repo view <you>/slugify-demo --json visibility,url
gh issue list -R <you>/slugify-demo          # #1 Documentation tech-debt
gh api repos/<you>/slugify-demo/rulesets --jq '.[] | .name + " " + .enforcement'
```

```text
Protect main active
```

If bootstrap warns that it couldn't create the ruleset, `main` is unprotected: see
[Troubleshooting](../reference/troubleshooting.md).

## 4. Start Copilot and set CI up on the pool

```bash
copilot       # in slugify-demo; /model should show claude-opus-5.5, the orchestrator in CLAUDE.md
```

Say **`set up CI runners`**. The `factory-ci` skill sets up the pool (it is opt-in; "run the factory" only checks
it afterwards). It asks before each step: the repo setting for Actions, how many jobs
may run at once, installing the pool's login service (once per Mac), pointing this repo at it, and one smoke-test CI
run. Details: [Local CI setup](../guides/local-ci-setup.md).

Check it from a normal terminal:

```bash
gh variable list -R <you>/slugify-demo
python3 .claude/skills/factory-ci/scripts/setup.py doctor --repo <you>/slugify-demo
```

```text
FACTORY_RUNNER  local
ok: FACTORY_RUNNER is local
ok: the pool serves this repo
ok: actions/runner 2.338.2 installed
ok: the pool is up (0/8 busy)
```

## 5. Brainstorm into lanes

In the same session:

> brainstorm a CLI that turns a title into a URL slug into issues

Answer its questions briefly: offline, standard library only, `slugify "Hello, World!"` prints `hello-world`, ASCII is
enough. It proposes a lane of ordered issues and asks you to **confirm once**. Then it creates them. From the run:

```bash
python3 .claude/skills/factory/scripts/queue.py
```

```text
  lane  pri  state     current    then              title
    #2  P0   ready     #3         #4                Title to URL slug CLI

Lanes in parallel: 1
Work now: lane #2 → #3 (ready)
Paused, waiting on the maintainer: none

CI pool: up, 0/8 busy, 0 queued
```

Lane #2 holds #3, "Define and test the title-to-slug conversion contract", then #4, "Expose title-to-slug conversion
through the CLI". See [Lanes and priorities](../concepts/lanes.md).

## 6. Run the factory

> run the factory

For each issue, in order: a builder writes the plan and opens a **draft PR** from `local/issue-N`, then builds and marks
it ready. CI runs **on the pool** while a fresh reviewer comments inline. The builder fixes the blockers, and the
orchestrator merges when every [gate](../reference/gates.md) passes. Watch from a second terminal:

```bash
gh pr list -R <you>/slugify-demo --state all
gh run list -R <you>/slugify-demo
```

If it asks you something, it's a PR comment with options and the label `factory:needs-maintainer`. Answer in the
thread and say **`resume the factory`**. If it can't merge, the PR gets `factory:ready-to-merge` and a merge command
in a comment: run it. See [Flag and continue](../concepts/flag-and-continue.md).

When it's done:

```bash
git pull
uv run slugify "Hello, World!"      # hello-world
```

## 7. What the factory keeps

The **run log**, `.factory/runs/<date>.jsonl` (gitignored), holds one line per orchestrator step: the plan, review and
merge for each issue, with timings, tokens and pointers to PRs and commits. It holds no decisions. GitHub does, so
deleting the log loses only the cost history.

```bash
python3 .claude/skills/factory/scripts/runlog.py status     # last step per issue
python3 .claude/skills/factory/scripts/runlog.py summary    # wall time, fix rounds and tokens per issue
```

```text
#3: done (last step: merge at 2026-11-27T07:38:21+00:00)
 issue      wall  fixes  tokens by model | by role
    #3       0m      0  claude-opus-5.5=52,000, claude-sonnet-5.5=38,000 | builder=38,000, reviewer=52,000
```

(The numbers above are example entries.) A new session picks up from the log and GitHub: say **`resume the factory`**.
Start a fresh session every two or three issues.

## 8. Found a factory problem? File a lesson

If the factory itself got something wrong (a bad default, a skill that misleads the agent, a gate that blocks a good
PR) and not your project's code, say it **when it happens**:

> file a factory lesson: the builder skipped the Docs line in the Handoff

The `factory-lesson` skill drafts an issue, checks it for private data, asks you once and files it with the `lesson`
label in `rahmansunny071/amplifactory` (the template's repo), never in your own project. A later release fixes it, and
`sync-root-factory` brings the fix into your project. See [Lessons](../concepts/lessons.md).

## Next

- [Day-to-day](../guides/day-to-day.md): the phrases you'll use, and what the factory does on each.
- [Your first run](first-run.md): the pipeline step by step, and what to expect from each one.
- [Create a project](create-a-project.md): what bootstrap does, the optional Actions mode, what a repo contains.
- [Adopt in an existing repo](../guides/adopt-in-existing-repo.md): add the factory to a repo you already have.

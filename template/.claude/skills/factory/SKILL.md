---
name: factory
description: >-
  Run this repo's agent factory end to end from Claude Code. Brainstorm a feature or roadmap with the
  maintainer and turn it into detailed, dependency-linked GitHub issues; then orchestrate each ready issue
  through design PR → design review → implementation PR → code review → auto-merge, using Sonnet subagents
  for issues, designs and code and Opus for brainstorming, orchestration, review and merge. Use this whenever
  the user wants to plan or break down work into issues, "run the factory", work through open issues, pick up
  issue #N, go from idea to merged PRs, or says the label-by-label factory is too slow, even if they don't
  name the skill.
---

# Factory

This repo is built by an issue-driven agent factory (see `README.md` → "How this repo is built" and
`CLAUDE.md`). The GitHub Actions version waits for the maintainer at every label. This skill runs the
same pipeline from a Claude Code session: you (Opus) are the orchestrator, Sonnet subagents do the
writing and coding, fresh Opus subagents review, and you merge when every gate passes. The maintainer
stays in the loop by exception: whenever you are genuinely unsure, you stop and ask.

The record must look the same as if the Actions factory had run it: issues in the Factory-task
format, `.factory/designs/issue-N.md`, a draft design PR, an implementation PR with "Checks run" and
"Deviations", review comments, and labels. Someone reading the repo later should be able to tell why
every change was made.

## Before you start

1. **Model.** You should be running on Opus. If `/model` shows something else, tell the user and
   suggest switching before orchestrating, since review and merge decisions rely on it.
2. **Repo state.** You need: `gh auth status` OK, a clean checkout of `main` that is up to date,
   `uv` available, and the checks in `CLAUDE.md` passing on `main` (run them once). If `main` is red,
   stop: nothing built on it can be trusted.
3. **Read** `CLAUDE.md` (project context, "Definition of checks pass", hard rules) and the three stage
   prompts in `.claude/commands/`. The subagents follow those prompts; you enforce them.
4. **Repo slug.** Get it once with `gh repo view --json nameWithOwner -q .nameWithOwner` and paste
   that exact value into every brief's `<owner/repo>`. Don't type it from memory: a one-character
   typo sends a subagent's `gh` calls to the wrong repo.
5. **The Actions review.** If `.github/workflows/factory-review.yml` exists and is enabled, ask the
   user once whether to disable it for this run (`gh workflow disable factory-review.yml`). It runs a
   second, weaker review on every push to `factory/issue-*`, and it spends usage. Re-enable it at
   the end if you disabled it.

## Mode 1: Brainstorm → issues

Use this when the user wants to explore an idea, plan a feature or roadmap, or "turn this into issues".

1. **Brainstorm with the user yourself** (Opus, main session). Ask about goals, users, constraints,
   what's out of scope, and how they'll know it works. Read the code it touches. Push back where the
   idea is vague or too big. This conversation is where most of the value is, so don't rush it into
   tickets.
2. **Propose a breakdown** as a table: for each issue, a short slug, title, one-line goal, dependencies
   (by slug), areas affected and size (S/M/L). Good issues are small enough that one design fits in
   ~400 words and one build fits in a single PR. Anything L should usually be split. Order them so
   foundations come first.
3. **Get one confirmation.** Creating issues is visible to others, so show the table and ask
   "create these N issues?" Adjust until the user agrees.
4. **Draft the bodies with Sonnet.** Write a brief to a scratch file: the brainstorm conclusions, the
   agreed table and relevant file paths. Spawn one Sonnet subagent (`model: "sonnet"`) per issue (in
   parallel for independent ones) with the "Issue writer" brief from `references/briefs.md`. Each
   returns a body in the format of `references/issue-format.md`, with dependencies as `{{slug}}`
   placeholders.
5. **Check and create, in dependency order.** Read every body. Fix anything vague, untestable or
   inconsistent with the table. You are accountable for issue quality, because every later stage builds
   on it. Then create the issues with `gh issue create` in order, replacing `{{slug}}` with the real
   `#N` as numbers become known. Report the list with links.

## Mode 2: Run the factory

Use this for "run the factory", "work through the issues" or "pick up #N".

### Pick the work

Run `python .claude/skills/factory/scripts/queue.py` (add `--json` for machine output). It lists open
issues with their state, dependencies and whether they are **ready** (every dependency closed, not
blocked, not already in progress). Work on ready issues in the order it gives, one at a time unless the
user asks for parallel runs. Each run spends the same usage allowance as this session, and serial runs
keep `main` simple. After each merge, run it again, because merging unlocks dependents.

If the user names an issue that isn't ready, say which dependency is open and ask whether to do that
first.

### For each issue N

Work in a git worktree per issue, so `main` stays clean and subagents can't collide:
`git worktree add ../<repo>-issue-N -b design/issue-N origin/main`. Pass the subagents **absolute
paths**. Remove the worktree after merge.

**1. Design (Sonnet).** Spawn a Sonnet subagent with the "Designer" brief. It follows
`.claude/commands/design-issue.md`: it writes `.factory/designs/issue-N.md`, pushes `design/issue-N`,
opens a **draft** PR "design: … (#N)" and sets `factory:designing`. Verify that the draft PR exists and
that its diff touches only `.factory/designs/`. If not, the step failed, even if the subagent says it
succeeded.

**2. Design review (Opus).** Spawn a fresh Opus subagent (`model: "opus"`) with the "Design reviewer"
brief. It returns `APPROVE`, `REVISE` (with concrete changes) or `ASK` (questions only the maintainer
can answer), plus its confidence, and posts its review on the design PR.
- `REVISE`: send the changes to a Sonnet subagent (the "Design reviser" brief) on the same branch, then
  run the **narrow** "Design final check" brief. It verifies the requested changes and checks for
  regressions without reopening settled points. Allow two rounds at most; if it still isn't
  converging, stop and ask.
- Reviewer `notes` (non-blocking) don't go back into the design: collect them and pass them to the
  Builder in its "Reviewer notes" slot.
- `ASK`, or confidence below high, or the design leaves real "Open questions": stop and ask the user
  (see "Stopping to ask").
- `APPROVE`: post a short comment on the design PR saying the orchestrator approved it and why. That is
  the record that replaces the human `design:approved` label. Don't add that label: it would also start
  the GitHub Actions build and do the work twice.

**3. Build (Sonnet).** Spawn a Sonnet subagent with the "Builder" brief, working in the issue's worktree.
It follows `.claude/commands/build-from-design.md`: it creates `factory/issue-N` from the design branch,
implements, runs every check in `CLAUDE.md` until green, opens "feat: … (#N)" with "Checks run" and
"Deviations", closes the design PR and sets `factory:review`. If it reports `factory:blocked` or can't
get checks green, stop and ask. Verify the implementation PR exists before moving on.

**4. CI.** `gh pr checks <PR> --watch`. If CI fails where local checks passed, give the failure to a
Sonnet "Fixer" (one round), then re-check. Never "fix" CI by changing lint rules, pins or tests'
intent.

**5. Code review (Opus).** Spawn a fresh Opus subagent with the "Code reviewer" brief. It follows
`.claude/commands/review-against-design.md`, verifies "Checks run" with `gh pr checks`, posts one review
comment and returns `READY`, `NEEDS_CHANGES` (with a list) or `ASK`, plus confidence and the gate
checklist. For `NEEDS_CHANGES`, run a Sonnet "Fixer" on the branch, then a **new** Opus review with
the "Code re-review" brief: verify the fixes, then review afresh. Allow two rounds at most.
Items the reviewer marks `(optional)` don't block the merge. File them as follow-up issues, with the
Issue writer brief, linked to this one. If a follow-up must land before another open issue (e.g. it
fixes something that issue relies on), add it to that issue's "Depends on" line.

**6. Merge (you).** Check every gate in `references/gates.md` yourself. Don't take the reviewer's word
for the mechanical ones: run the commands. If all pass:
`gh pr merge <PR> --merge --admin --delete-branch`. Use merge commits, never squash, so release tags
stay on `main`'s history. The `Closes #N` line closes the issue. Then post a one-line merge note
listing the gates, pull `main` and remove the worktree.

**7. Next.** Run `queue.py` again and continue with the next ready issue until none are left, the user
stops you, or something needs them.

## Stopping to ask

The maintainer chose autonomy with one condition: **if you are genuinely unsure about a design or an
implementation, stop and ask instead of guessing.** Stopping costs a few minutes; a confident wrong
merge costs much more. Stop when:

- the design reviewer says `ASK`, or a design's open questions need the maintainer's judgment (product
  scope, naming they'd care about, trade-offs with no clear winner);
- a reviewer's confidence is below high, or you disagree with a reviewer and can't settle it from the
  code;
- two revise or fix rounds don't converge, or checks can't be made green;
- any merge gate fails, especially changes to `.github/workflows/`, dependencies, the version,
  release tooling or `CLAUDE.md`;
- the work needs something `CLAUDE.md` forbids, or an external spec or decision you can't verify;
- a subagent added `factory:blocked`;
- **two consecutive review rounds find new defects in the same feature or area.** Each fix is
  exposing more of a problem, often a premise nobody verified. Ask whether to keep, simplify or
  split that part out before spending the remaining rounds on it.

**How to stop:** add `factory:blocked` to the issue, post a comment with the specific question and the
options you see (with your recommendation), and ask the user in the session with `AskUserQuestion`.
After they answer, record the decision on the issue or PR so the record stays complete, remove
`factory:blocked` and continue. While waiting, other ready issues that don't depend on it may continue
if the user agrees.

## Coexisting with the GitHub Actions factory

Both can live in the same repo. To avoid double work:
- Never add `factory:design` or `design:approved` from this skill; those start the Actions workflows.
  Status labels (`factory:designing`, `factory:building`, `factory:review`, `factory:blocked`) start
  nothing.
- Branches named `factory/issue-N` also start the Actions **review** job on every push. In practice
  it's a weaker second opinion: on its first real run it called a PR "Ready" that the Opus review
  then found six reproduced defects in. It also spends usage. That's why "Before you start" asks
  whether to disable it for the run.
- Don't write `@claude` in any comment you post: it would start `factory-chat`.

## When you're done

Summarize: issues merged (with PR links), issues blocked and why, decisions you made on the
maintainer's behalf (from the merge notes), and anything a reviewer flagged as a follow-up. Offer to
file those follow-ups as issues. If a problem looks like a factory problem rather than a project
problem, say so: it may belong in the factory template as a lesson.

## Reference files

- `references/issue-format.md`: the Factory-task issue body and the quality bar for it.
- `references/briefs.md`: the exact prompts for every subagent (issue writer, designer, design
  reviewer, design reviser, builder, fixer, code reviewer). Read it before spawning one.
- `references/gates.md`: the merge gates and how to check each one.
- `scripts/queue.py`: the ready-issue queue, from `gh` (stdlib only).

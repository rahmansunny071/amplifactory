# Merge gates

Merge an implementation PR only when **every** gate below holds. Check the mechanical gates yourself
with the commands shown. A reviewer's summary is evidence, not proof. If any gate fails, don't merge:
stop and ask (see SKILL.md → "Stopping to ask"), unless the fix is a mechanical one listed below.

| # | Gate | How to check |
|---|---|---|
| 1 | **CI is green** on the PR's latest commit | `gh pr checks <PR>`: every check is `pass`; none pending or skipped among the required ones |
| 2 | **Up to date with `main`** | `gh pr view <PR> --json mergeStateStatus` isn't `BEHIND` or `DIRTY`. If `BEHIND`: `gh api -X PUT repos/{owner}/{repo}/pulls/<PR>/update-branch`, wait for CI, then re-check gate 1. Fix `DIRTY` (conflicts) with a Fixer; if that isn't trivial, ask |
| 3 | **Code review: READY, high confidence** | The latest Opus code-review result on the current commit. A review of an earlier commit doesn't count |
| 4 | **No undisclosed deviations; disclosed ones are minor** | Review result plus the PR body's "Deviations". Minor means naming or file placement. Scope, interface or dependency changes aren't minor |
| 5 | **No hard-rule violations** | Review result, plus your own read of the diff against `CLAUDE.md`'s hard rules (secrets or private data, uploads, paid calls, …) |
| 6 | **No workflow changes** | `gh pr diff <PR> --name-only` contains nothing under `.github/`. Workflow changes always go to the maintainer, and agent stages skip on such PRs anyway |
| 7 | **No dependency, version or release changes the design didn't approve** | The diff of `pyproject.toml` (`dependencies`, `optional-dependencies`, `version`) and `uv.lock`. A lockfile change is fine only if the design listed the new dependency |
| 8 | **`CLAUDE.md` and `.claude/` unchanged** | `gh pr diff <PR> --name-only`. These steer every future agent, so the maintainer decides |
| 9 | **The PR closes its issue** | The PR body has `Closes #<N>` |
| 10 | **You aren't unsure** | Read the diff yourself. If anything still bothers you, it's a stop, not a merge |

## Merge

```bash
gh pr merge <PR> --merge --admin --delete-branch
```

- `--admin` uses the maintainer's ruleset bypass. The PR author (`claude[bot]` or the maintainer) can't
  approve their own PR, and this is the agreed route.
- Use `--merge`, never `--squash` or `--rebase`, so commits that release tags point to stay on `main`.

Afterwards, post one comment on the PR: `Merged by the factory orchestrator: CI green; Opus review
READY (high); no workflow/dependency/version/CLAUDE.md changes; deviations: <none | list>.` Then
`git -C <repo> pull --ff-only` and `git worktree remove <worktree>`.

## After merging something that touches the factory itself

That should only happen after the maintainer approved it (gates 6 and 8). Afterwards, open design or
implementation PRs still run the old workflows until their branches are updated with `main`. Update
them (gate 2's command) before continuing.

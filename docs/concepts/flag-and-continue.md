---
title: "Flag and continue"
description: "When the factory needs a decision, it asks on the PR, pauses only that lane and keeps working. You answer when it suits you, then say “resume”."
---

# Flag and continue

The factory doesn't stop the run to wait for you unless nothing else can proceed.

## When it asks

- A plan or review raises a question only you can answer (scope, an interface, a dependency, a data source).
- A gate fails and the fix isn't mechanical.
- The orchestrator is **unsure** whether to merge (gate 13).
- More lanes are ready than you've allowed (asked once, in the session).

## How it asks

1. An **inline review comment** tagged `[question]`, with **options and a recommendation**.
2. The label **`factory:needs-maintainer`** on the PR.
3. The run log records it as `asked`, with a link to the thread.
4. **Only that lane pauses.** Other lanes carry on.

## How you answer

Reply in the thread on GitHub, whenever you like. Then, in Copilot or Claude Code:

> resume the factory

`queue.py` lists the lane as `replied`, ahead of new work. The orchestrator applies your answer (via the builder),
resolves the thread, removes the label and continues to the gates.

## Ready to merge

A refused merge is not a question, so it doesn't get `factory:needs-maintainer`. When every gate passes but the merge
is refused (the harness denies `gh pr merge`, or GitHub rejects it; in Copilot that includes a "No" at the approval
prompt and a denial in autopilot or a `-p` run), the factory:

1. labels the PR and issue **`factory:ready-to-merge`** and removes the in-progress label;
2. posts one PR comment: the exact merge command and the gate summary;
3. moves on. The issue stays open, so its lane's later issues wait.

It never works around the refusal. `queue.py` lists these under "Waiting for you to merge", and the end-of-run
report lists them so you can merge in one batch.

### Let the factory merge

Opt in with a permission rule. The template doesn't ship one.

**Claude Code.** Add this to the repo's `.claude/settings.json` or your user `~/.claude/settings.json`:

```json
{ "permissions": { "allow": ["Bash(gh pr merge * --merge --admin --delete-branch)"] } }
```

- The rule lets the agent use your ruleset bypass.
- It matches only commands ending in `--delete-branch`, which is how the skill runs the merge (gates.md "Merge");
  variants with `-R` or other trailing flags aren't covered.
- A project rule applies only after you accept the workspace trust prompt.
- For how allow rules interact with auto mode, see the [permissions](https://code.claude.com/docs/en/permissions)
  and [permission modes](https://code.claude.com/docs/en/permission-modes) docs, and try it in your own session.

**GitHub Copilot.** The rule above does nothing: Copilot doesn't read `permissions` from `.claude/settings.json`, and
its own rules can't single out the merge (`--allow-tool='shell(gh pr)'`, or "Yes, and don't ask again" on a
`gh pr merge` prompt, approves every `gh pr` command, `gh pr close` included). Use a personal hook that runs the
skill's `merge_hook.py` ([scripts](../reference/scripts.md#merge_hookpy)). It approves only
`gh pr merge <N> --merge [--admin] --delete-branch`, typed exactly so, and leaves every other request to the normal
prompt.

1. Copy the script out of the repo, so no branch or file edit can change what it approves. Copy it again after a
   factory update.

   ```bash
   mkdir -p ~/.copilot/hooks && cp .claude/skills/factory/scripts/merge_hook.py ~/.copilot/hooks/
   ```

   On Windows, in PowerShell:

   ```powershell
   New-Item -ItemType Directory -Force ~/.copilot/hooks | Out-Null; Copy-Item .claude/skills/factory/scripts/merge_hook.py ~/.copilot/hooks/
   ```

2. Save this as `~/.copilot/hooks/factory-merge.json`. If you set `COPILOT_HOME`, Copilot reads personal hooks from
   `$COPILOT_HOME/hooks/` instead, so put both files there.

   ```json
   { "version": 1, "hooks": { "permissionRequest": [ { "type": "command", "matcher": "bash|powershell", "timeoutSec": 10,
     "bash": "test -f \"$COPILOT_HOOK_DIR/merge_hook.py\" && python3 \"$COPILOT_HOOK_DIR/merge_hook.py\" || true",
     "powershell": "$f = Join-Path $env:COPILOT_HOOK_DIR 'merge_hook.py'; if (Test-Path $f) { [Console]::In.ReadToEnd() | python $f }; exit 0" } ] } }
   ```

- The `bash` command runs on macOS and Linux, the `powershell` one on Windows (as `python`; use `py` if that's how
  Python runs there).
- It applies in every repo you use Copilot in, for that one command. A missing script or any error falls back to the
  normal prompt, and a merge that is still refused is handed off.
- Don't commit it as a repository hook (`.github/hooks/`): that would run the working tree's copy of the script,
  which any file edit could turn into an allow-all.
- See the [Copilot hooks reference](https://docs.github.com/en/copilot/reference/hooks-reference).

## What never happens

- The skill never adds the Actions-mode trigger labels (`factory:design`, `design:approved`) and never writes
  `@claude`. Those would start GitHub Actions agents.
- The factory never merges PRs that change `.github/`, `CLAUDE.md` or `.claude/`. You merge those.

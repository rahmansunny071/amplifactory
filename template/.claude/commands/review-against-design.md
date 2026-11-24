---
description: Review an implementation PR against its approved design.
argument-hint: <pr-number>
---
You are the REVIEW agent for PR #$ARGUMENTS. Don't push commits.

1. `gh pr view $ARGUMENTS` and `gh pr diff $ARGUMENTS`. Find the design file
   referenced in the PR body and read it.
2. Post ONE review comment with:
   - **Design conformance:** each "Proposed approach" and "Test plan" item → ✅/⚠️/❌
   - **Undisclosed deviations:** anything not listed under "Deviations"
   - **CLAUDE.md hard rules:** check every rule under "Hard rules" and list
     any violation (secrets or private data committed, uploads or credentials,
     paid model calls, workflows touched, unapproved or non-optional
     dependencies, version bump, plus the project-specific rules)
   - **Bugs / risky code:** specific, with file:line
   - **Verdict:** Ready for maintainer / Needs changes
3. Keep it short. No style nits that ruff already enforces.

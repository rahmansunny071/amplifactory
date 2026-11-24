# Changelog

- v0.2.0 (2026-11-24): **`factory` skill**: run the factory from a Claude Code session instead of label by label.
  - Brainstorm mode: Opus brainstorms with the maintainer, proposes an issue breakdown, gets one confirmation, Sonnet subagents draft Factory-task issues and Opus checks and creates them with dependency links.
  - Run mode: for each ready issue (`scripts/queue.py`, dependency-ordered), Sonnet designs → an Opus subagent reviews the design → Sonnet builds in a worktree → CI → a fresh Opus subagent reviews the code → Opus auto-merges (`--merge --admin`) when every gate in `references/gates.md` passes.
  - Stops and asks when unsure, on failed gates, or on workflow, dependency, version or `CLAUDE.md` changes. Never adds the Actions trigger labels, so the two modes don't duplicate work.
  - Template CI checks the skill files and runs `queue.py --self-test`.
  - Hardened by its first real run (pensieve-traces #2, #3), lessons #10, #11, #12 and #16: reviewers don't add unverifiable scope (it becomes a question); stop when two rounds find defects in the same area; narrow "Design final check" and "Code re-review" briefs; non-blocking `notes` and `(optional)` items (optional items are filed as linked follow-up issues); a Reviewer-notes slot in the Builder brief; repo slug taken from `gh`, not memory; a rule for installing outside packages; asks whether to disable the weaker Actions review for the run; design length cap by size (S ~400, M ~800 words); `design-issue` handles local-mode labels.
- v0.1.1 (2026-11-24): Fixes from driving the first generated factory (pensieve-traces) end to end.
  - Build and chat agents can run checks on PRs authored by `claude[bot]` (`restrict_untrusted_prs: "false"`), protected by a new build step that refuses design branches touching anything outside `.factory/designs/` (#3).
  - The review agent runs on implementation PRs opened by `claude[bot]` (`allowed_bots: "claude"`) (#5), and verifies "Checks run" against CI with `gh pr checks` instead of trying to run code (#6).
  - The build agent reports denied or missing commands on the design PR with `factory:blocked`, never a silent exit (#3).
  - Docs: a "Changing the factory itself" section in the generated README. It covers workflow changes in their own PR (#7), updating open factory PRs after a workflow change (#4), merging your own PRs, and treating "green but no artifact" as a failure. `CLAUDE.md` and the build prompt say the same.
  - Template CI asserts every guardrail above is present in the rendered project.
- v0.1.0 (2026-11-24): First release as a Copier template, generalized from pensieve-traces. Factory workflows, agent commands, issue template, CI, package skeleton, `scripts/bootstrap.sh` (labels, `main` ruleset, fork-PR approval), and a template CI that renders three variants and runs their checks.

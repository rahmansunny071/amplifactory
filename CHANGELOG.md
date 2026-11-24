# Changelog

- v0.1.1 (2026-11-24): Fixes from driving the first generated factory (pensieve-traces) end to end.
  - Build and chat agents can run checks on PRs authored by `claude[bot]` (`restrict_untrusted_prs: "false"`), protected by a new build step that refuses design branches touching anything outside `.factory/designs/` (#3).
  - The review agent runs on implementation PRs opened by `claude[bot]` (`allowed_bots: "claude"`) (#5), and verifies "Checks run" against CI with `gh pr checks` instead of trying to run code (#6).
  - The build agent reports denied or missing commands on the design PR with `factory:blocked`, never a silent exit (#3).
  - Docs: a "Changing the factory itself" section in the generated README. It covers workflow changes in their own PR (#7), updating open factory PRs after a workflow change (#4), merging your own PRs, and treating "green but no artifact" as a failure. `CLAUDE.md` and the build prompt say the same.
  - Template CI asserts every guardrail above is present in the rendered project.
- v0.1.0 (2026-11-24): First release as a Copier template, generalized from pensieve-traces. Factory workflows, agent commands, issue template, CI, package skeleton, `scripts/bootstrap.sh` (labels, `main` ruleset, fork-PR approval), and a template CI that renders three variants and runs their checks.

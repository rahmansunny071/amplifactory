---
title: "Guardrails"
description: "What every generated repo enforces, and what the template checks confirm is still there."
---

# Guardrails

- **Only you advance work.** The skill runs in your session. In the Actions mode (`actions_agent: claude` or `copilot`), every
  trigger checks the sender is `maintainer_github`, and a label guard strips trigger labels anyone else adds. With
  `none` (the default) no agent workflow is rendered.
- **Forks can't run agents.** In the Actions mode, build, review and chat refuse PRs from forks (with Copilot, an
  on-demand review runs only for you, on a same-repo `factory/issue-N` PR). Fork-PR workflows, including
  `ci.yml`, need your approval.
- **Least privilege per stage** (Actions mode). Reviewers can't push. The build step refuses design branches that touch
  anything outside `.factory/designs/`. Per-stage tool allowlists apply.
- **Merge gates** (skill): see [Merge gates](gates.md).
- **Protected `main`.** A ruleset: PRs with approval and code-owner review, **all conversations resolved**, required
  checks, no force push or deletion.
- **Bounded cost.** `timeout-minutes`, `--max-turns` and per-issue `concurrency` on every Actions-mode job; pinned
  models; urgency triage; the run log's cost report.
- **Hard rules in `CLAUDE.md`:** no secrets or personal data; a per-repo [real-data level](../concepts/real-data.md)
  (`never` by default; real data is never committed or quoted, tests use synthetic fixtures); no uploads; no paid
  model endpoints; dependencies only as planned; no version bumps or releases; no workflow changes unless planned
  (and then in their own PR).
- **You merge the factory itself.** PRs touching `.github/`, `CLAUDE.md` or `.claude/` are never merged by the skill.

Template CI (`.github/workflows/template-ci.yml`, also run locally by `scripts/check.sh`) renders every variant and
fails if any of these is missing from a generated repo. The Actions-mode guardrails are checked in the `actions_agent: claude`
and `actions_agent: copilot` variants; the other variants check that no agent workflow is rendered.

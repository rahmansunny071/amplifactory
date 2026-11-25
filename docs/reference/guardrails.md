---
title: "Guardrails"
description: "What every generated repo enforces, and what the template CI checks is still there."
---

# Guardrails

- **Only you advance work.** Every Actions trigger checks the sender is `maintainer_github`, and a label guard strips
  trigger labels anyone else adds. The skill runs in your session.
- **Forks can't run agents.** Build, review and chat refuse PRs from forks, and fork-PR workflows need your approval.
- **Least privilege per stage.** Reviewers can't push. The build step refuses design branches that touch anything
  outside `.factory/designs/`. Per-stage tool allowlists apply.
- **Merge gates** (skill): see [Merge gates](gates.md).
- **Protected `main`.** A ruleset: PRs with approval and code-owner review, **all conversations resolved**, required
  checks, no force push or deletion.
- **Bounded cost.** `timeout-minutes`, `--max-turns` and per-issue `concurrency` on every Actions job; pinned
  models; urgency triage; the run log's cost report.
- **Hard rules in `CLAUDE.md`:** no secrets or personal data, a per-repo real-data level (`never` by default; real data is never committed or quoted, tests use synthetic fixtures), no uploads, no paid model endpoints, dependencies
  only as planned, no version bumps or releases, no workflow changes unless planned (and then in their own PR).
- **You merge the factory itself.** PRs touching `.github/`, `CLAUDE.md` or `.claude/` are never merged by the skill.

The template CI renders three variants and fails if any of these is missing from a generated repo.

---
title: "Adopt in an existing repo"
description: "Bring the factory into a repository that already exists, either the whole template or just the skill."
---

# Adopt in an existing repo

## Option 1: the whole template

Run Copier **into** the existing repo. It asks the same questions and shows a conflict for every file that already
exists:

```bash
cd existing-repo
uvx copier copy gh:rahmansunny071/agent-factory .
```

Keep your own `pyproject.toml`, package and tests where they conflict. Take `CLAUDE.md`, `.claude/`, `.factory/`,
`.github/` and `scripts/bootstrap.sh` from the template, then merge your existing CI into `ci.yml` and the "checks
pass" list in `CLAUDE.md`, which must stay identical. Run `./scripts/bootstrap.sh`: it skips what already exists.

Because `.copier-answers.yml` now exists, later releases come in with `uvx copier update`.

## Option 2: just the skill

If you only want the Claude Code skill:

```bash
git -C /path/to/agent-factory archive v0.6.0 template/.claude/skills/factory \
  | tar -x --strip-components=2 -C .claude/
```

Then add to your `CLAUDE.md`:

- a **Release phase** line (`mvp` or `hardening`) and the blocker definition;
- a **"checks pass"** list identical to your CI;
- the **hard rules** you want enforced (see the template's `CLAUDE.md.jinja`), including "every code change
  ships its docs in the same PR";
- **Where knowledge lives**.

And create the labels the skill uses: `factory:needs-maintainer`, `improvement`, `lane`, `P0`–`P3`,
`documentation`. The skill creates the "Documentation tech-debt" issue itself the first time it needs it.

## Organise existing issues into lanes

> organise the open issues into lanes

Opus proposes lanes, an order and priorities; after your confirmation it wires them with `lanes.py`. That's how
pensieve-traces moved to lanes: four lanes, thirteen issues, four "blocked by" links.

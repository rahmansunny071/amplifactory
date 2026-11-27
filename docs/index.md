---
title: "amplifactory"
description: "An issue-driven agent factory for GitHub Copilot and Claude Code, packaged as a Copier template. You plan with the agent; agents design, build and review; you stay in the loop by exception."
hide:
  - toc
---

# amplifactory

amplifactory turns a repository into an **issue-driven agent factory**. In a GitHub Copilot or Claude Code
session (the **orchestrator**) you turn an idea into prioritized **lanes** of ordered GitHub issues. The factory then
takes each issue to a merged PR: a **builder** plans, builds and fixes, a fresh **reviewer** checks, and the
orchestrator merges when every gate passes. When it needs you, it asks on the PR and keeps working on other lanes.

It's a [Copier](https://copier.readthedocs.io/) template: one command creates a repo with the rules, the `factory`
skill, CI, labels, branch protection and a Project board. `copier update` brings in later releases.

```mermaid
%%{init: {"themeCSS": ".node.you .nodeLabel, .node.you .nodeLabel p, .node.agent .nodeLabel, .node.agent .nodeLabel p, .node.gh .nodeLabel, .node.gh .nodeLabel p, .node.done .nodeLabel, .node.done .nodeLabel p { color: #fff !important; } .node.ci .nodeLabel, .node.ci .nodeLabel p { color: #1F1300 !important; } .flowchart-link { stroke: var(--md-mermaid-edge-color) !important; } .marker { fill: var(--md-mermaid-edge-color) !important; stroke: var(--md-mermaid-edge-color) !important; } .edgeLabel, .edgeLabel p { background-color: var(--md-mermaid-label-bg-color) !important; color: var(--md-mermaid-label-fg-color) !important; } .cluster-label .nodeLabel, .cluster-label p { color: var(--md-mermaid-label-fg-color) !important; }"}}%%
flowchart LR
    you(["<b>You</b><br/>idea, answers, final say"]):::you
    orch["<b>Orchestrator</b><br/>your Copilot or Claude session"]:::agent
    lanes["<b>Lanes P0–P3</b><br/>ordered issues"]:::gh

    you -->|"idea"| orch
    orch -->|"brainstorm"| lanes
    lanes -->|"you confirm<br/>once per lane"| builder

    subgraph issue["Per issue, in order"]
        direction LR
        builder["<b>Builder</b><br/>plan, build, fix"]:::agent
        pr["<b>Pull request</b><br/>with a Handoff"]:::gh
        reviewer["<b>Fresh reviewer</b><br/>blocker, improvement,<br/>question comments"]:::agent
        actions["<b>GitHub Actions</b><br/>queues ci.yml jobs"]:::gh
        pool["<b>Runner pool</b><br/>on your Mac, one<br/>fresh runner per job"]:::ci
        gates["<b>13 gates</b><br/>CI green, no blockers,<br/>Handoff, docs"]:::agent
        merge(["<b>Merge</b>"]):::done

        builder -->|"opens"| pr
        pr --> reviewer
        reviewer -->|"blockers"| builder
        pr -->|"pull_request"| actions
        actions -->|"self-hosted"| pool
        pool -->|"checks"| pr
        pr --> gates
        gates -->|"all pass"| merge
    end

    needs["<b>Needs you?</b><br/>question inline on the PR;<br/>the lane pauses, others continue"]:::you
    builder -.->|"decision only you can make"| needs
    gates -.->|"not sure, or a gate fails"| needs
    needs -.->|"you answer, say resume"| orch
    merge -.->|"next issue"| builder

    classDef you fill:#00796B,stroke:#004D40,color:#fff,stroke-width:2px
    classDef agent fill:#5C5CE0,stroke:#3D3DB8,color:#fff
    classDef gh fill:#57606A,stroke:#30363D,color:#fff
    classDef ci fill:#F59E0B,stroke:#B45309,color:#1F1300,stroke-width:2px
    classDef done fill:#1A7F4B,stroke:#0F5132,color:#fff
    style issue fill:none,stroke:#00796B,stroke-width:2px,stroke-dasharray:6 4
```

*Blue: agents. Grey: GitHub. Amber: CI on the runner pool on your Mac. Teal: you. Dotted lines: when it needs you, and the next issue.*

## Get started

<div class="grid cards" markdown>

-   :material-flash-outline: **Quick start**

    ---

    One small CLI, from `copier copy` of the template to a merged PR with CI on your Mac's runner pool.

    [:octicons-arrow-right-24: Quick start](getting-started/quick-start.md)

-   :material-rocket-launch-outline: **Create a project**

    ---

    Generate a repo from the template, then run `bootstrap.sh` for the GitHub side: labels, branch protection and the Project board.

    [:octicons-arrow-right-24: Create a project](getting-started/create-a-project.md)

-   :material-play-circle-outline: **Your first run**

    ---

    Brainstorm into lanes, then say "run the factory" and watch the first issue go from plan to merge.

    [:octicons-arrow-right-24: Your first run](getting-started/first-run.md)

</div>

## What makes it work

| Idea | In short |
|---|---|
| [**Lanes and priorities**](concepts/lanes.md) | Features become lanes of ordered sub-issues; `P0`–`P3` and "blocked by" links set the schedule. |
| [**The pipeline**](concepts/pipeline.md) | One builder and one fresh reviewer per issue. Before a first release only blockers are fixed in the PR; the rest become `improvement` issues. |
| [**Flag and continue**](concepts/flag-and-continue.md) | Questions go on the PR with options and a recommendation. Answer whenever you like, then say "resume". |
| [**Real data**](concepts/real-data.md) | `never`, `with-approval` or `allowed`: how far agents may read real data. It is never committed or quoted. |
| [**Docs ship with the code**](concepts/docs-with-code.md) | Every code change brings its docs; the Handoff's Docs line is a merge gate. |
| [**Lessons**](concepts/lessons.md) | A factory problem found in your project becomes a `lesson` issue here, then a release, then an update in every project. |
| [**Copilot and Claude Code**](guides/copilot-and-claude.md) | Run the factory in either: setup, a model per role, what differs, and the Actions mode with each. |
| [**Keep up to date**](guides/keep-up-to-date.md) | `sync-root-factory` moves a repo to the latest release by PR; `factory-lesson` files factory problems in this repo. |
| [**Local CI**](concepts/local-ci.md) | One runner pool per Mac runs the CI of every factory repo on it, where GitHub-hosted runners are off. |
| [**Where knowledge lives**](concepts/where-knowledge-lives.md) | Issues, PRs, review threads, plans and Handoffs are the source of truth; the run log (`.factory/runs/`) and agent memory only point at them. |

## License

amplifactory is released under the [MIT License](https://github.com/rahmansunny071/amplifactory/blob/main/LICENSE). It covers the template; a project you generate is yours, under any license you choose (the template doesn't add one).

"""Factory work queue: what to do next, worked out from GitHub (the source of truth).

Work is organised in **lanes**: parent issues labelled `lane` whose native GitHub sub-issues are the
lane's issues, **in order**. Lanes run in parallel (up to --lanes, default 1); inside a lane, issues run
one at a time in sub-issue order. Priority labels P0–P3 rank lanes (and issues outside lanes); a missing
label counts as P2. Dependencies are native "blocked by" links plus "Depends on #N" lines in the body
(fallback). A lane that is blocked by another issue waits as a whole.

Per lane, the state of its **current** issue (the first open sub-issue) decides the lane's state:
- active: labelled factory:designing / factory:building / factory:review (uses a slot)
- replied: factory:needs-maintainer and the maintainer has answered (resume it; uses a slot)
- merge: factory:ready-to-merge: every gate passed but the merge was refused, so the maintainer merges
  it (uses no slot; nothing to do until the PR is merged, and later issues in the lane wait)
- ready: nothing blocks it (can start if a slot is free)
- asked: factory:needs-maintainer, no answer yet (paused; doesn't use a slot)
- blocked: factory:blocked
- waiting: open dependencies (its own or the lane's)

Issues not in any lane are listed separately with the same states (repos without lanes keep working).
The "Documentation tech-debt" tracking issue is never work itself and is left out; its open sub-issues
(doc todos, labelled `improvement`) are listed with the issues outside lanes.

On a machine that runs the CI runner pool (the factory-ci skill), the table ends with the pool's state,
read from its heartbeat file with no API call: `CI pool: up, 6/8 busy, 11 queued`, `paused …` or
`down (no heartbeat for 12 min)`. An up line ends with `admitting N of M slots: why` when the Mac
admits fewer slots than the pool has. `--json` has it as `ci_pool` (`null` without a pool).

    python3 .claude/skills/factory/scripts/queue.py              # table
    python3 .claude/skills/factory/scripts/queue.py --lanes 2    # plan for two lanes in parallel
    python3 .claude/skills/factory/scripts/queue.py --json
    python3 .claude/skills/factory/scripts/queue.py --offline    # skip reply/dependency API calls
    python3 .claude/skills/factory/scripts/queue.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

IN_PROGRESS = ("factory:designing", "factory:building", "factory:review")
NEEDS_MAINTAINER = "factory:needs-maintainer"
BLOCKED = "factory:blocked"
READY_TO_MERGE = "factory:ready-to-merge"
IMPROVEMENT = "improvement"
LANE = "lane"
DOC_DEBT_TITLE = "Documentation tech-debt"  # same exact title in lanes.py and bootstrap.sh
# The CI runner pool's home, as in the factory-ci skill's runners.py, and how fresh its heartbeat
# must be for "up" (setup.py's HEARTBEAT_FRESH_S).
POOL_HOME_ENV = "FACTORY_RUNNER_POOL_HOME"
POOL_DEFAULT_HOME = "~/.amplifactory/runner-pool"
POOL_FRESH_S = 120
_UTC = timezone(timedelta(0))
_PRIORITY = re.compile(r"^P([0-3])$")
_DEPENDS_LINE = re.compile(r"^\W*depends on\b(?P<rest>.*)$", re.IGNORECASE | re.MULTILINE)
_ISSUE_REF = re.compile(r"(?<![\w/])#(\d+)\b")
_CLOSES = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", re.IGNORECASE)
# (PR number, maintainer replied?). An alias, so no type hint ends in a double "]".
ReplyInfo = tuple[int, bool]


@dataclass
class Issue:
    number: int
    title: str
    state: str  # active | replied | merge | ready | asked | blocked | waiting
    priority: int = 2
    open_dependencies: list[int] = field(default_factory=list)
    improvement: bool = False
    pr: int | None = None


@dataclass
class Lane:
    number: int
    title: str
    priority: int
    state: str  # same states as its current issue; "done" when no open sub-issues
    current: Issue | None = None
    remaining: list[int] = field(default_factory=list)  # open sub-issues after the current one
    lane_dependencies: list[int] = field(default_factory=list)


# Aliases, so no type hint ends in two closing brackets in a row (the template CI flags
# that as leftover template syntax).
IntList = list[int]
LaneList = list[Lane]
IssueList = list[Issue]


def dependencies(body: str) -> list[int]:
    """Issue numbers named on the body's "Depends on …" line(s), in order, without duplicates."""
    found: list[int] = []
    for match in _DEPENDS_LINE.finditer(body or ""):
        found += [int(n) for n in _ISSUE_REF.findall(match.group("rest"))]
    return list(dict.fromkeys(found))


def priority(labels: list[str]) -> int:
    for label in labels:
        m = _PRIORITY.match(label)
        if m:
            return int(m.group(1))
    return 2


def has_new_reply(threads: list[dict], comments: list[dict], maintainer: str) -> bool:
    """True when the maintainer has spoken last somewhere on the PR.

    `threads`: unresolved review threads, each {"comments": [{"author": login, "createdAt": iso}, …]}.
    `comments`: PR conversation comments, each {"author": login, "createdAt": iso}.
    """
    for thread in threads:
        if thread.get("comments") and thread["comments"][-1].get("author") == maintainer:
            return True
    mine = [c["createdAt"] for c in comments if c.get("author") == maintainer]
    others = [c["createdAt"] for c in comments if c.get("author") != maintainer]
    return bool(mine) and (not others or max(mine) > max(others))


def classify(
    issue: dict,
    closed: set[int],
    native_deps: dict[int, IntList],
    replies: dict[int, ReplyInfo],
    extra_deps: list[int] | None = None,
) -> Issue:
    number = issue["number"]
    labels = [label["name"] for label in issue.get("labels", [])]
    deps = dependencies(issue.get("body", "")) + native_deps.get(number, []) + (extra_deps or [])
    waiting_on = sorted({d for d in deps if d != number and d not in closed})
    pr, replied = replies.get(number, (None, False))
    if BLOCKED in labels:
        state = "blocked"
    elif NEEDS_MAINTAINER in labels:
        state = "replied" if replied else "asked"
    elif READY_TO_MERGE in labels:
        state = "merge"
    elif any(label in labels for label in IN_PROGRESS):
        state = "active"
    elif waiting_on:
        state = "waiting"
    else:
        state = "ready"
    return Issue(
        number, issue["title"], state, priority(labels), waiting_on, IMPROVEMENT in labels, pr
    )


def is_doc_debt_tracker(issue: dict) -> bool:
    labels = {label["name"] for label in issue.get("labels", [])}
    return issue["title"] == DOC_DEBT_TITLE and "documentation" in labels


def plan(
    issues: list[dict],
    sub_issues: dict[int, IntList],
    native_deps: dict[int, IntList] | None = None,
    replies: dict[int, ReplyInfo] | None = None,
) -> tuple[LaneList, IssueList]:
    """Lanes (ranked) and issues outside lanes (ranked). `sub_issues` maps lane → ordered children."""
    native_deps = native_deps or {}
    replies = replies or {}
    by_number = {i["number"]: i for i in issues}
    closed = {i["number"] for i in issues if i["state"].upper() == "CLOSED"}
    is_open = {i["number"] for i in issues if i["state"].upper() == "OPEN"}
    lane_numbers = [
        n for n in is_open if any(label["name"] == LANE for label in by_number[n].get("labels", []))
    ]
    in_a_lane = {c for n in lane_numbers for c in sub_issues.get(n, [])}

    lanes = []
    for n in lane_numbers:
        lane_issue = by_number[n]
        lane_labels = [label["name"] for label in lane_issue.get("labels", [])]
        lane_deps = dependencies(lane_issue.get("body", "")) + native_deps.get(n, [])
        lane_waiting = sorted({d for d in lane_deps if d != n and d not in closed})
        children = [c for c in sub_issues.get(n, []) if c in is_open]
        if not children:
            lanes.append(Lane(n, lane_issue["title"], priority(lane_labels), "done"))
            continue
        head = children[0]
        current = classify(by_number[head], closed, native_deps, replies, lane_waiting)
        lanes.append(
            Lane(
                n,
                lane_issue["title"],
                priority(lane_labels),
                current.state,
                current,
                children[1:],
                lane_waiting,
            )
        )
    state_rank = {
        "active": 0,
        "replied": 1,
        "merge": 2,
        "ready": 3,
        "asked": 4,
        "blocked": 5,
        "waiting": 6,
    }
    lanes.sort(key=lambda lane: (lane.state == "done", lane.priority, lane.number))

    loose = [
        classify(by_number[n], closed, native_deps, replies)
        for n in sorted(is_open)
        if n not in in_a_lane and n not in lane_numbers and not is_doc_debt_tracker(by_number[n])
    ]
    loose.sort(key=lambda i: (state_rank[i.state], i.improvement, i.priority, i.number))
    return lanes, loose


def start_now(lanes: list[Lane], slots: int) -> tuple[LaneList, LaneList]:
    """(lanes to work on now, ready lanes left waiting for a slot). Active lanes use slots first,
    then lanes the maintainer has replied on, then ready lanes; asked lanes are paused."""
    active = [lane for lane in lanes if lane.state == "active"]
    candidates = [lane for lane in lanes if lane.state == "replied"] + [
        lane for lane in lanes if lane.state == "ready"
    ]
    free = max(0, slots - len(active))
    return active + candidates[:free], candidates[free:]


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def fetch_issues() -> list[dict]:
    fields = "number,title,state,labels,body"
    return json.loads(_gh("issue", "list", "--state", "all", "--limit", "500", "--json", fields))


def fetch_sub_issues(repo: str, lanes: list[int]) -> dict[int, IntList]:
    """Ordered sub-issue numbers per lane (native GitHub sub-issues)."""
    out = {}
    for n in lanes:
        items = json.loads(_gh("api", f"repos/{repo}/issues/{n}/sub_issues?per_page=100"))
        out[n] = [i["number"] for i in items]
    return out


def fetch_native_deps(repo: str, numbers: list[int]) -> dict[int, IntList]:
    """Native "blocked by" dependencies per issue (empty when the API isn't available)."""
    out = {}
    for n in numbers:
        try:
            items = json.loads(_gh("api", f"repos/{repo}/issues/{n}/dependencies/blocked_by"))
        except subprocess.CalledProcessError:
            return {}
        out[n] = [i["number"] for i in items]
    return out


_THREADS_QUERY = """
query($o:String!,$r:String!,$n:Int!){ repository(owner:$o,name:$r){ pullRequest(number:$n){
  reviewThreads(first:100){ nodes{ isResolved
    comments(first:50){ nodes{ author{login} createdAt } } } }
  comments(first:100){ nodes{ author{login} createdAt } } } } }
"""


def _open_prs() -> list[dict]:
    return json.loads(_gh("pr", "list", "--state", "open", "--json", "number,body,headRefName"))


def _pr_for(prs: list[dict], issue: int) -> int | None:
    """The open PR for an issue: its branch is local/issue-N or factory/issue-N, or it closes it."""
    return next(
        (
            p["number"]
            for p in prs
            if p["headRefName"] in (f"local/issue-{issue}", f"factory/issue-{issue}")
            or str(issue) in _CLOSES.findall(p.get("body") or "")
        ),
        None,
    )


def fetch_merge_prs(issue_numbers: list[int]) -> dict[int, ReplyInfo]:
    """For issues labelled factory:ready-to-merge: their open PR (no reply to look for)."""
    if not issue_numbers:
        return {}
    prs = _open_prs()
    return {n: (pr, False) for n in issue_numbers if (pr := _pr_for(prs, n)) is not None}


def fetch_replies(repo: str, issue_numbers: list[int], maintainer: str) -> dict[int, ReplyInfo]:
    """For issues waiting on the maintainer: their open PR and whether the maintainer has replied."""
    if not issue_numbers:
        return {}
    owner, name = repo.split("/", 1)
    prs = _open_prs()
    result: dict[int, ReplyInfo] = {}
    for issue in issue_numbers:
        pr = _pr_for(prs, issue)
        if pr is None:
            continue
        data = json.loads(
            _gh(
                "api",
                "graphql",
                "-f",
                f"query={_THREADS_QUERY}",
                "-f",
                f"o={owner}",
                "-f",
                f"r={name}",
                "-F",
                f"n={pr}",
            )
        )
        node = data["data"]["repository"]["pullRequest"]

        def flat(cs: list[dict]) -> list[dict]:
            return [
                {"author": (c.get("author") or {}).get("login"), "createdAt": c["createdAt"]}
                for c in cs
            ]

        threads = [
            {"comments": flat(t["comments"]["nodes"])}
            for t in node["reviewThreads"]["nodes"]
            if not t["isResolved"]
        ]
        result[issue] = (pr, has_new_reply(threads, flat(node["comments"]["nodes"]), maintainer))
    return result


def pool_home(environ=None) -> Path:
    """The CI runner pool's home on this machine (it may not exist)."""
    environ = os.environ if environ is None else environ
    return Path(environ.get(POOL_HOME_ENV) or POOL_DEFAULT_HOME).expanduser()


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _epoch(value) -> float | None:
    """Seconds since the epoch, from a number or an ISO 8601 time; None if it's neither."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):  # fromisoformat reads a "Z" only from Python 3.11
        text = text[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=_UTC)).timestamp()


def _count(value) -> int:
    """A count, from a number or a list of items."""
    if isinstance(value, list):
        return len(value)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _ago(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 2 * 3600:
        return f"{seconds // 60} min"
    if seconds < 2 * 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


def pool_status(home: Path, now: float) -> dict | None:
    """The CI runner pool's state from the files in its home, or None when there's no pool here.

    Down (no heartbeat newer than POOL_FRESH_S) wins over paused (`paused.json` exists): a stopped
    pool has to be started before a resume means anything. A pause still shows while it's down. A
    pool from before `pause` (a fresh heartbeat with no "paused" key) ignores `paused.json`, so it's
    up, as `runners.py status` says.
    """
    if not home.is_dir():
        return None
    beat = _read_json(home / "heartbeat.json")
    beat = beat if isinstance(beat, dict) else {}
    ts = _epoch(beat.get("ts"))
    age = None if ts is None else max(0.0, now - ts)
    paused = None
    if (home / "paused.json").exists():
        info = _read_json(home / "paused.json")
        info = info if isinstance(info, dict) else {}
        since = _epoch(info.get("since"))
        paused = {
            "since": info.get("since"),
            "age_s": None if since is None else int(max(0.0, now - since)),
            "reason": " ".join(str(info.get("reason") or "").split()),
        }
    up = age is not None and age < POOL_FRESH_S
    if paused and up and "paused" not in beat:
        paused["ignored"] = True
    state = ("paused" if paused and not paused.get("ignored") else "up") if up else "down"
    # the Mac admits fewer slots: how many, and why (runners.py), on an up line only, as in
    # `runners.py status`: a paused pool admits none, and a stale heartbeat says nothing now
    held = beat.get("held")
    held = held if state == "up" and isinstance(held, dict) and held.get("reason") else None
    return {
        "state": state,
        "heartbeat_age_s": None if age is None else int(age),
        "slots": _count(beat.get("slots")),
        "busy": _count(beat.get("busy")),
        "queued": _count(beat.get("queued")),
        "paused": paused,
        "held": held,
    }


def pool_line(pool: dict) -> str:
    """One line for the end of the table, e.g. `CI pool: up, 6/8 busy, 11 queued`."""
    pause = ""
    if pool.get("paused") and not pool["paused"].get("ignored"):
        age, reason = pool["paused"]["age_s"], pool["paused"]["reason"]
        pause = "paused" + (f" {_ago(age)} ago" if age is not None else "")
        pause += f" ({reason})" if reason else ""
    if pool["state"] == "down":
        age = pool["heartbeat_age_s"]
        down = "down (no heartbeat" + (f" for {_ago(age)})" if age is not None else ")")
        return f"CI pool: {down}" + (f", {pause}" if pause else "")
    line = f"CI pool: {pause or 'up'}, {pool['busy']}/{pool['slots']} busy, {pool['queued']} queued"
    held = pool.get("held")
    if held:
        line += f", admitting {held.get('slots')} of {pool['slots']} slots: {held['reason']}"
    ignored = pool.get("paused") and pool["paused"].get("ignored")
    return line + (" (it predates pause, so paused.json does nothing)" if ignored else "")


def render(lanes: list[Lane], loose: list[Issue], slots: int, pool: dict | None = None) -> str:
    lines = []
    if lanes:
        lines.append(f"{'lane':>6}  {'pri':<3}  {'state':<8}  {'current':<9}  {'then':<16}  title")
        for lane in lanes:
            cur = f"#{lane.current.number}" if lane.current else "-"
            then = " ".join(f"#{n}" for n in lane.remaining[:4]) or "-"
            extra = ""
            if lane.state == "waiting":
                waits = lane.current.open_dependencies if lane.current else lane.lane_dependencies
                extra = "  (waits on " + ", ".join(f"#{d}" for d in waits) + ")"
            lines.append(
                f"{'#' + str(lane.number):>6}  P{lane.priority:<2}  {lane.state:<8}  {cur:<9}  "
                f"{then:<16}  {lane.title}{extra}"
            )
        now, queued = start_now(lanes, slots)
        lines.append("")
        lines.append(f"Lanes in parallel: {slots}")
        work = [
            f"lane #{lane.number} → #{lane.current.number} ({lane.state})"
            for lane in now
            if lane.current
        ]
        lines.append(f"Work now: {'; '.join(work) if work else 'nothing'}")
        if queued:
            more = ", ".join(f"#{lane.number}" for lane in queued)
            lines.append(
                f"More lanes ready ({more}): ask the maintainer whether to run them in parallel"
            )
        asked = [f"#{lane.number}" for lane in lanes if lane.state == "asked"]
        lines.append(f"Paused, waiting on the maintainer: {', '.join(asked) if asked else 'none'}")
    loose_listed = [i for i in loose if i.state != "merge"]  # merge ones show under "Waiting"
    if loose_listed:
        if lines:
            lines.append("")
        lines.append("Not in a lane:")
        for issue in loose_listed:
            mark = " (improvement)" if issue.improvement else ""
            lines.append(
                f"{'#' + str(issue.number):>6}  P{issue.priority:<2}  {issue.state:<8}  "
                f"{issue.title}{mark}"
            )
    waiting = waiting_to_merge(lanes, loose)
    if waiting:
        if lines:
            lines.append("")
        lines.append("Waiting for you to merge (the command is in each PR's comment):")
        for issue in waiting:
            pr = f"PR #{issue.pr}" if issue.pr else "no open PR found"
            lines.append(f"{'#' + str(issue.number):>6}  {pr}  {issue.title}")
    text = "\n".join(lines) if lines else "No open issues."
    return f"{text}\n\n{pool_line(pool)}" if pool else text


def waiting_to_merge(lanes: list[Lane], loose: list[Issue]) -> list[Issue]:
    """Issues whose gates all passed but whose merge was refused: lane currents, then loose ones."""
    waiting = [lane.current for lane in lanes if lane.current and lane.current.state == "merge"]
    return waiting + [i for i in loose if i.state == "merge"]


def payload(lanes: list[Lane], loose: list[Issue], slots: int, pool: dict | None = None) -> dict:
    """What `--json` prints."""
    now, queued = start_now(lanes, slots)
    return {
        "lanes": [asdict(lane) for lane in lanes],
        "not_in_a_lane": [asdict(i) for i in loose],
        "waiting_to_merge": [
            {"issue": i.number, "pr": i.pr} for i in waiting_to_merge(lanes, loose)
        ],
        "work_now": [lane.number for lane in now],
        "ready_waiting_for_slot": [lane.number for lane in queued],
        "ci_pool": pool,
    }


def _self_test() -> None:
    def iss(n, title="t", state="OPEN", labels=(), body=""):
        return {
            "number": n,
            "title": title,
            "state": state,
            "labels": [{"name": label} for label in labels],
            "body": body,
        }

    issues = [
        iss(1, state="CLOSED"),
        # lane A (P0): 4 then 6; 4 is ready
        iss(10, "Lane A", labels=("lane", "P0")),
        iss(4, "sts writer", labels=("P0",), body="- Depends on #1 (model)."),
        iss(6, "upload", labels=("P1",)),
        # lane B (P1): 5 then 8; 5 asked the maintainer
        iss(11, "Lane B", labels=("lane", "P1")),
        iss(5, "export", labels=(NEEDS_MAINTAINER,)),
        iss(8, "qa builder"),
        # lane C (P2, no label): 7 is blocked natively by #4
        iss(12, "Lane C", labels=("lane",)),
        iss(7, "openrouter"),
        # lane D (P3): waits as a whole on lane A
        iss(13, "Lane D", labels=("lane", "P3"), body="Depends on #10"),
        iss(17, "hardening", labels=("improvement",)),
        # a finished lane
        iss(14, "Lane E", labels=("lane", "P1")),
        iss(20, "done", state="CLOSED"),
        # an issue outside any lane
        iss(30, "loose", labels=("P1",)),
        # the doc-debt tracker is never work; its todos are ordinary loose improvements
        iss(43, DOC_DEBT_TITLE, labels=("documentation", "P2")),
        iss(44, "README: stale status table", labels=("documentation", "improvement", "P2")),
    ]
    subs = {10: [4, 6], 11: [5, 8], 12: [7], 13: [17], 14: [20]}
    lanes, loose = plan(issues, subs, native_deps={7: [4]}, replies={})
    by = {lane.number: lane for lane in lanes}
    assert by[10].state == "ready" and by[10].current.number == 4 and by[10].remaining == [6]
    assert by[11].state == "asked" and by[11].current.number == 5
    assert by[12].state == "waiting" and by[12].current.open_dependencies == [4], by[12]
    assert by[13].state == "waiting" and by[13].lane_dependencies == [10], by[13]
    assert by[14].state == "done"
    # ranked by priority, done last
    assert [lane.number for lane in lanes] == [10, 11, 12, 13, 14], [lane.number for lane in lanes]
    assert [i.number for i in loose] == [30, 44], [i.number for i in loose]
    now, queued = start_now(lanes, slots=1)
    assert [lane.number for lane in now] == [10] and queued == []
    # with lane B answered, the single slot resumes B first and leaves A queued
    lanes2, _ = plan(issues, subs, native_deps={7: [4]}, replies={5: (40, True)})
    now2, queued2 = start_now(lanes2, slots=1)
    assert [lane.number for lane in now2] == [11] and [lane.number for lane in queued2] == [10]
    # two slots: both
    now2b, queued2b = start_now(lanes2, slots=2)
    assert [lane.number for lane in now2b] == [11, 10] and queued2b == []
    # an active lane uses the only slot
    issues_active = [
        dict(i, labels=i["labels"] + [{"name": "factory:building"}]) if i["number"] == 4 else i
        for i in issues
    ]
    lanes3, _ = plan(issues_active, subs, native_deps={7: [4]})
    now3, queued3 = start_now(lanes3, slots=1)
    assert [lane.number for lane in now3] == [10] and queued3 == []
    # reply detection
    bot, me = "claude[bot]", "maint"
    done_thread = {
        "comments": [{"author": bot, "createdAt": "1"}, {"author": me, "createdAt": "2"}]
    }
    assert has_new_reply([done_thread], [], me)
    assert not has_new_reply([{"comments": [{"author": bot, "createdAt": "1"}]}], [], me)
    assert "Work now: lane #10 → #4 (ready)" in render(lanes, loose, 1)
    # ready-to-merge: state "merge", no slot used, outranks ready, lane's later issues wait
    merge_issues = [
        iss(50, "Lane M", labels=("lane",)),
        iss(51, "first", labels=(READY_TO_MERGE, "factory:review")),
        iss(52, "second"),
        iss(53, "loose, waiting for a merge", labels=(READY_TO_MERGE,)),
        iss(54, "loose, ready"),
    ]
    lanes4, loose4 = plan(merge_issues, {50: [51, 52]}, replies={51: (70, False), 53: (71, False)})
    assert lanes4[0].state == "merge" and lanes4[0].current.number == 51, lanes4[0]
    assert lanes4[0].remaining == [52], lanes4[0]
    assert [(i.number, i.state) for i in loose4] == [(53, "merge"), (54, "ready")], loose4
    now4, queued4 = start_now(lanes4, slots=1)
    assert now4 == [] and queued4 == [], (now4, queued4)
    out4 = render(lanes4, loose4, 1)
    assert "Waiting for you to merge" in out4 and "PR #70" in out4 and "PR #71" in out4, out4
    assert "Waiting for you to merge" not in render(lanes, loose, 1)
    # #35: a loose merge issue is listed once (under "Waiting"), and the --json payload lists the PRs
    assert out4.count("#53 ") == 1, out4
    assert [(i.number, i.pr) for i in waiting_to_merge(lanes4, loose4)] == [(51, 70), (53, 71)]
    assert waiting_to_merge(lanes, loose) == []
    assert payload(lanes4, loose4, 1)["waiting_to_merge"] == [
        {"issue": 51, "pr": 70},
        {"issue": 53, "pr": 71},
    ]
    assert payload(lanes, loose, 1)["waiting_to_merge"] == []
    # the CI pool line, read from the files in a pool home
    with tempfile.TemporaryDirectory() as tmp:
        home, now = Path(tmp) / "pool", 1_800_000_000.0

        def write(name: str, data) -> None:
            text = data if isinstance(data, str) else json.dumps(data)
            (home / name).write_text(text, encoding="utf-8")

        def iso(ts: float, z: bool = False) -> str:
            text = datetime.fromtimestamp(ts, _UTC).isoformat()
            return text.replace("+00:00", "Z") if z else text

        assert pool_status(home, now) is None  # no pool home: no line, and null in --json
        assert "CI pool" not in render(lanes, loose, 1, None)
        assert payload(lanes, loose, 1)["ci_pool"] is None
        home.mkdir()
        assert pool_line(pool_status(home, now)) == "CI pool: down (no heartbeat)"
        write("heartbeat.json", "{not json")
        assert pool_status(home, now)["state"] == "down"
        write(
            "heartbeat.json", {"ts": now - 30, "slots": 8, "busy": 6, "queued": 11, "paused": None}
        )
        up = pool_status(home, now)
        assert up["state"] == "up" and up["heartbeat_age_s"] == 30, up
        line = "CI pool: up, 6/8 busy, 11 queued"
        assert pool_line(up) == line and render(lanes, loose, 1, up).endswith(f"\n\n{line}")
        assert payload(lanes, loose, 1, up)["ci_pool"]["busy"] == 6
        assert up["held"] is None
        held = {"slots": 4, "reason": "on battery (battery: half)"}
        write(
            "heartbeat.json",
            {"ts": now - 30, "slots": 8, "busy": 4, "queued": 3, "paused": None, "held": held},
        )
        busy = pool_status(home, now)
        line = "CI pool: up, 4/8 busy, 3 queued, admitting 4 of 8 slots: on battery (battery: half)"
        assert busy["held"] == held and pool_line(busy) == line, busy
        assert payload(lanes, loose, 1, busy)["ci_pool"]["held"] == held
        write("heartbeat.json", {"ts": now - 300, "slots": 8, "held": held})  # stale: down
        assert pool_status(home, now)["held"] is None
        for junk in ("x", [], {}, {"slots": 2}):  # not a `held` the pool writes: none
            write("heartbeat.json", {"ts": now - 30, "slots": 8, "paused": None, "held": junk})
            assert pool_status(home, now)["held"] is None, junk
        write("heartbeat.json", {"ts": now - 30, "slots": 8, "paused": None, "held": held})
        write("paused.json", {"since": now - 60})  # paused: it admits none, so no `held`
        assert pool_status(home, now)["held"] is None
        (home / "paused.json").unlink()
        write(
            "heartbeat.json", {"ts": now - 30, "slots": 8, "busy": 6, "queued": 11, "paused": None}
        )
        write("paused.json", {"since": iso(now - 7200, z=True), "reason": "on battery\n all day"})
        paused = pool_status(home, now)
        line = "CI pool: paused 2 h ago (on battery all day), 6/8 busy, 11 queued"
        assert paused["state"] == "paused" and pool_line(paused) == line, paused
        write("paused.json", {"since": now - 90})
        assert pool_line(pool_status(home, now)) == "CI pool: paused 90 s ago, 6/8 busy, 11 queued"
        write(
            "heartbeat.json", {"ts": now - 30, "slots": 8, "busy": 6, "queued": 11}
        )  # no "paused"
        old = pool_status(
            home, now
        )  # a pool from before pause ignores paused.json: up, like status
        line = "CI pool: up, 6/8 busy, 11 queued (it predates pause, so paused.json does nothing)"
        assert old["state"] == "up" and pool_line(old) == line, old
        write("heartbeat.json", {"ts": iso(now - 720), "slots": 8, "busy": 6, "queued": [{}, {}]})
        down = pool_status(home, now)
        assert down["state"] == "down" and down["queued"] == 2, down
        assert pool_line(down) == "CI pool: down (no heartbeat for 12 min), paused 90 s ago"
        (home / "paused.json").unlink()
        assert pool_line(pool_status(home, now)) == "CI pool: down (no heartbeat for 12 min)"
        write("heartbeat.json", {"ts": now - POOL_FRESH_S, "slots": 8})  # exactly 120 s: down
        assert pool_status(home, now)["state"] == "down"
        write("heartbeat.json", {"ts": now + 60, "slots": 8})  # a clock ahead of ours: up, age 0
        ahead = pool_status(home, now)
        assert ahead["state"] == "up" and ahead["heartbeat_age_s"] == 0, ahead
        assert _epoch(True) is None and _epoch("yesterday") is None
    assert pool_home({POOL_HOME_ENV: "/srv/pool"}) == Path("/srv/pool")
    assert pool_home({}) == Path(POOL_DEFAULT_HOME).expanduser()
    print("queue.py self-test: ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lanes", type=int, default=1, help="lanes in parallel (default 1)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    parser.add_argument("--offline", action="store_true", help="skip reply/dependency API calls")
    parser.add_argument("--maintainer", help="GitHub login (default: the logged-in gh user)")
    parser.add_argument("--self-test", action="store_true", help="run offline checks and exit")
    args = parser.parse_args(argv)
    if args.self_test:
        _self_test()
        return 0
    repo = _gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner").strip()
    issues = fetch_issues()
    open_issues = [i for i in issues if i["state"].upper() == "OPEN"]
    lane_ids = [i["number"] for i in open_issues if any(lb["name"] == LANE for lb in i["labels"])]
    subs = fetch_sub_issues(repo, lane_ids) if lane_ids else {}
    native, replies = {}, {}
    if not args.offline:
        native = fetch_native_deps(repo, [i["number"] for i in open_issues])

        def with_label(label: str) -> list[int]:
            return [
                i["number"] for i in open_issues if any(lb["name"] == label for lb in i["labels"])
            ]

        asked = with_label(NEEDS_MAINTAINER)
        if asked:
            maintainer = args.maintainer or _gh("api", "user", "-q", ".login").strip()
            replies = fetch_replies(repo, asked, maintainer)
        replies.update(fetch_merge_prs(with_label(READY_TO_MERGE)))
    lanes, loose = plan(issues, subs, native, replies)
    pool = pool_status(pool_home(), time.time())
    if args.json:
        print(json.dumps(payload(lanes, loose, args.lanes, pool), indent=2))
    else:
        print(render(lanes, loose, args.lanes, pool))
    return 0


if __name__ == "__main__":
    sys.exit(main())

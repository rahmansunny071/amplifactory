"""Factory CI setup: puts a repo's CI on this Mac's runner pool, step by step, and checks its health.

`doctor` is the quick check the factory skill runs before every "run the factory". `plan` lists the
setup steps, each `done`, `todo`, `answer` (the maintainer decides; the line says what to ask) or
`blocked`, with the next command. `apply STEP` runs one step. The script never prompts: the agent
(Claude Code or Copilot) asks the maintainer, and steps that change a repo setting need `--yes`.
Stdlib only, Python 3.9+; it uses runners.py from the same folder.

    python3 .claude/skills/factory-ci/scripts/setup.py doctor --repo OWNER/REPO
    python3 .claude/skills/factory-ci/scripts/setup.py plan --repo OWNER/REPO [--json]
    python3 .claude/skills/factory-ci/scripts/setup.py apply STEP --repo OWNER/REPO [--yes]
    python3 .claude/skills/factory-ci/scripts/setup.py service install|uninstall|status [--yes] [--purge]
    python3 .claude/skills/factory-ci/scripts/setup.py container check [--slots N] [--cpus N] [--memory GB]
    python3 .claude/skills/factory-ci/scripts/setup.py container on --yes [--cpus N] [--memory GB]
    python3 .claude/skills/factory-ci/scripts/setup.py container off --yes
    python3 .claude/skills/factory-ci/scripts/setup.py --self-test

doctor's exit code says what to do next:
    0  healthy (or FACTORY_RUNNER names another runner: the pool isn't used)
    1  error (the message says what)
    2  the pool isn't set up for this repo: unset FACTORY_RUNNER means GitHub-hosted runners, the
       default; to opt in, run `plan`
    3  the pool, or in container mode Docker, isn't running: the next: line says what to start
    4  CI is off (FACTORY_CI=off): gate merges on the local checks

Steps:
    preflight    macOS and its CPU; git, gh, uv and python3 on the service's PATH; a gh login with the
                 repo scope; repo admin
    actions      GitHub Actions is enabled (apply turns it on: a repo setting, so --yes)
    self-hosted  self-hosted runners are allowed: apply registers a just-in-time probe runner, never
                 started, and deletes it at once
    capacity     how many jobs run at once: apply --slots N (plan proposes N from the Mac's cores and
                 RAM, leaving 4 performance cores and 16 GB free)
    pool         the runner install and the login service (apply --yes)
    register     the pool serves this repo
    route        the repo variable FACTORY_RUNNER=local sends CI to the pool (apply --yes)
    smoke        one CI run on the pool, end to end (workflow_dispatch; it isn't a required check).
                 It runs ci.yml; apply --workflow FILE names another CI workflow
    retire       hand-registered runners of this repo: apply --yes --runner NAME removes one

The service is a launchd user agent (com.amplifactory.runner-pool) that keeps `runners.py serve`
running. It runs a copy of runners.py kept in the pool home, so it doesn't depend on any one repo's
checkout. `install` and `uninstall --purge` change this Mac, so they need --yes.

`container check` says whether this Mac can run the pool's jobs in Linux containers (containers.py):
the docker CLI on the service's PATH, Docker running, its VM big enough for the slots, and the
runner image. It exits 0 when ready, and 2 when something needs fixing; each `fix:` line says how.
`container on --yes` runs that check, pulls and tries the image, saves the mode and restarts the
pool's service; `container off --yes` sends new jobs back to the Mac. Both change where every job of
the pool runs, so they need --yes. In container mode, doctor also checks Docker: Docker down is exit
3, and the next step is to start it.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import io
import json
import os
import platform
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.parsers.expat
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import containers
import runners as pool

EXIT_OK, EXIT_ERROR, EXIT_SETUP, EXIT_DOWN, EXIT_CI_OFF = 0, 1, 2, 3, 4
HEARTBEAT_FRESH_S = 120
QUEUE_SLOW_S = 600
CONTAINER_POOL_VERSION = "7"  # the first runners.py POOL_VERSION with container mode
GUARD_POOL_VERSION = "16"  # the first whose containers can't reach the Mac's localhost
TOOLS = ("git", "gh", "uv", "python3")
DEFAULT_WORKFLOW = "ci.yml"  # what `apply smoke` runs; --workflow names another
WORKFLOW_RE = re.compile(r"[\w.-]+\.ya?ml")
STEPS = (
    "preflight",
    "actions",
    "self-hosted",
    "capacity",
    "pool",
    "register",
    "route",
    "smoke",
    "retire",
)
KEEP_CORES = 4  # performance cores left to the maintainer and agents
KEEP_RAM_GB = 16
GB_PER_SLOT = 2  # about what one CI job (uv sync, pytest) needs
SMOKE_FIND_S = 120
SMOKE_TIMEOUT_S = 900
SETUP_FILE = "setup.json"  # in the pool home: what setup has checked, per repo
EMU_POLICY = "Disable in all Enterprise Managed User (EMU) repositories"
_UTC = timezone(timedelta(0))
UNDECIDED = (
    "FACTORY_RUNNER is unset: CI runs on GitHub-hosted runners, the default (to say so, gh variable "
    "set FACTORY_RUNNER --body github); this Mac's pool is opt-in"
)


@dataclass
class Step:
    number: int
    name: str
    status: str  # done | todo | answer | blocked
    detail: str
    next: str = ""


def render(step: Step) -> str:
    line = f"{step.number} {step.name}: {step.status}: {step.detail}"
    return line + (f"\n    next: {step.next}" if step.next else "")


def stamp_s(stamp: str | None) -> float | None:
    if not stamp:
        return None
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_UTC).timestamp()


def propose_slots(cores: int, ram_gb: int) -> int:
    """Jobs at once that leave KEEP_CORES performance cores and KEEP_RAM_GB of RAM free."""
    return max(1, min(cores - KEEP_CORES, (ram_gb - KEEP_RAM_GB) // GB_PER_SLOT, 64))


def age_s(stamp: str, now: float) -> float:
    when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_UTC)
    return now - when.timestamp()


def load_setup(home: pool.Home) -> dict:
    data = pool.read_json(home.root / SETUP_FILE, {})
    return data if isinstance(data, dict) and isinstance(data.get("repos"), dict) else {"repos": {}}


def mark(home: pool.Home, repo: str, key: str, value) -> None:
    home.make()
    with pool.file_lock(home.root / "setup.lock"):
        data = load_setup(home)
        data["repos"].setdefault(repo, {})[key] = value
        pool.write_json(home.root / SETUP_FILE, data)


class Setup:
    """One repo's setup and health, against one pool home."""

    def __init__(
        self,
        repo: str,
        home: pool.Home,
        github: pool.GitHub | None = None,
        which=shutil.which,
        clock=time.time,
        system: str | None = None,
        machine: str | None = None,
        run=subprocess.run,
        sleep=time.sleep,
        service: Service | None = None,
        agents: Path | None = None,
        environ=None,
        docker: containers.Docker | None = None,
    ) -> None:
        pool.check_repo(repo)
        self.repo = repo
        self.home = home
        entry = pool.load_config(home)["repos"].get(repo) or {}
        self.account = entry.get("account") or repo.split("/", 1)[0]
        self.gh = github or pool.GitHub(pool.Tokens())
        self.which = which
        self.clock = clock
        self.system = system or platform.system()
        self.machine = machine or platform.machine()
        self.run = run
        self.sleep = sleep
        self.service = service or Service(home)
        self.agents = agents or Path.home() / "Library" / "LaunchAgents"
        self.environ = os.environ if environ is None else environ
        self.docker = docker

    def call(self, method: str, path: str, body=None, params=None):
        return self.gh.call(method, f"/repos/{self.repo}{path}", self.account, body, params)

    # --- doctor

    def variables(self) -> dict:
        try:
            data = self.call("GET", "/actions/variables", params={"per_page": 30}) or {}
        except pool.PoolError:
            # no gh login named after the owner: an org's repo, or gh signed in through GH_TOKEN.
            # For a repo this Mac's pool doesn't serve, gh's own login can read the variables.
            if self.repo in pool.load_config(self.home)["repos"]:
                raise
            data = self.gh_variables()
        return {v.get("name"): str(v.get("value", "")) for v in data.get("variables") or []}

    def gh_variables(self) -> dict:
        cmd = ["gh", "api", f"repos/{self.repo}/actions/variables?per_page=30"]
        try:
            out = self.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise pool.PoolError(f"could not run gh: {exc.__class__.__name__}") from None
        if out.returncode != 0:
            said = (out.stderr or "").strip().splitlines()
            raise pool.PoolError(
                f"gh could not read {self.repo}'s variables" + (f" (gh: {said[0]})" if said else "")
            )
        return json.loads(out.stdout or "{}") or {}

    def oldest_queued_s(self) -> float | None:
        data = self.call("GET", "/actions/runs", params={"status": "queued", "per_page": 100}) or {}
        listed = data.get("workflow_runs") or []
        stamps = [run.get("created_at") for run in listed]
        stamps = [stamp for stamp in stamps if stamp]
        return age_s(min(stamps), self.clock()) if stamps else None

    def doctor(self) -> tuple:
        """(exit code, report lines), with at most 2 API calls."""
        values = self.variables()
        if values.get("FACTORY_CI", "").lower() == "off":
            return EXIT_CI_OFF, [
                "CI is off (FACTORY_CI=off): gate merges on the checks in CLAUDE.md"
            ]
        runner = values.get("FACTORY_RUNNER")
        if runner is not None and runner.lower() != "local":
            return EXIT_OK, [f"CI runs on GitHub-hosted runners (FACTORY_RUNNER={runner})"]
        version = pool.base_version(self.home)
        checks = (
            (
                runner is not None,
                "FACTORY_RUNNER is local",
                UNDECIDED,
            ),
            (
                self.repo in pool.load_config(self.home)["repos"],
                "the pool serves this repo",
                "the pool doesn't serve this repo",
            ),
            (version is not None, f"actions/runner {version} installed", "no runner installed"),
        )
        lines = [("ok: " if ok else "setup: ") + (good if ok else bad) for ok, good, bad in checks]
        beat = pool.read_json(self.home.heartbeat, {})
        since = self.clock() - float(beat.get("ts") or 0)
        up = since < HEARTBEAT_FRESH_S
        if up:
            line = f"ok: the pool is up ({beat.get('busy', 0)}/{beat.get('slots')} busy)"
            held = beat.get("held")
            if isinstance(held, dict) and held.get("reason"):  # fewer slots admitted
                slots = f"{held.get('slots')} of {beat.get('slots')} slots"
                line += f", admitting {slots}: {held['reason']}"
            lines.append(line)
        else:
            lines.append("down: the pool isn't running (no recent heartbeat)")
        if not all(ok for ok, _, _ in checks):
            return EXIT_SETUP, lines + [f"next: setup.py plan --repo {self.repo}"]
        cfg = pool.load_config(self.home)
        opts = containers.settings(cfg)
        if opts["mode"] == "container":  # without Docker, nothing runs
            docker = self.docker or setup_docker(self.environ)
            try:
                info = docker.info()
            except pool.PoolError as exc:
                start = "next: start Docker Desktop, and turn on its Start when you sign in"
                return EXIT_DOWN, lines + [f"down: {exc}", start]
            vm = f"a VM of {info.get('NCPU')} CPUs and {containers.gib(info):.1f} GB"
            docker_ok = f"Docker runs ({info.get('OperatingSystem')}, {vm})"
            lines.append(f"ok: container mode is on, and {docker_ok}")
            ran = str(beat.get("version") or "")
            if up and pool.version_tuple(ran) < pool.version_tuple(CONTAINER_POOL_VERSION):
                lines.append(
                    f"warning: the pool that runs (version {ran or '?'}) predates container mode, "
                    "so its jobs still run on the Mac: restart it, setup.py service install --yes"
                )
            elif up and beat.get("launcher") == "host":  # it started before `container on`
                lines.append(
                    f"warning: the pool that runs (pid {beat.get('pid')}) started in host mode, "
                    "so its jobs still run on the Mac: restart it (stop it first if you started "
                    "it by hand), setup.py service install --yes"
                )
            elif up and pool.version_tuple(ran) < pool.version_tuple(GUARD_POOL_VERSION):
                lines.append(  # its containers start without guard.py
                    f"warning: the pool that runs (version {ran}) is older than the block of the "
                    "Mac's localhost, so its jobs can reach the Mac's own services: restart it "
                    "(stop it first if you started it by hand), setup.py service install --yes"
                )
            fits = containers.fit(
                info, cfg["slots"], opts["container_cpus"], opts["container_memory_gb"]
            )
            lines += [f"warning: {problem}" for problem in fits]
        if not up:
            return EXIT_DOWN, lines + ["next: start the pool: setup.py service install --yes"]
        try:
            oldest = self.oldest_queued_s()
        except (pool.PoolError, pool.GitHubError) as exc:
            return EXIT_OK, lines + [f"warning: couldn't read the queue ({exc})"]
        if oldest is not None and oldest > QUEUE_SLOW_S:
            lines.append(f"warning: a run has been queued for {int(oldest // 60)} min")
        return EXIT_OK, lines

    # --- the steps

    def scopes(self) -> set | None:
        """The OAuth scopes of the account's gh token; None when GitHub doesn't say (fine-grained)."""
        headers = {
            "Authorization": "Bearer " + self.gh.tokens.get(self.account),
            "Accept": "application/vnd.github+json",
            "User-Agent": "amplifactory-setup",
        }
        request = urllib.request.Request(self.gh.api + "/user", headers=headers)
        try:
            reply = self.gh.transport(request, 30.0)
        except (OSError, ValueError) as exc:
            raise pool.GitHubError("GET", "/user", 0, exc.__class__.__name__) from None
        if reply.status != 200:
            raise pool.GitHubError("GET", "/user", reply.status)
        raw = reply.headers.get("x-oauth-scopes")
        return None if raw is None else {scope.strip() for scope in raw.split(",") if scope.strip()}

    def preflight(self) -> Step:
        if self.system != "Darwin":
            return Step(
                1, "preflight", "blocked", f"the runner pool runs on macOS, not {self.system}"
            )
        try:
            cpu = pool.runner_platform(self.system, self.machine)
        except pool.PoolError as exc:
            return Step(1, "preflight", "blocked", str(exc))
        fixes = []
        # the service runs with service_path(), so look for the tools where it will
        found = [tool for tool in TOOLS if self.which(tool, path=self.environ.get("PATH"))]
        usable = {tool for tool in TOOLS if self.which(tool, path=service_path(self.environ))}
        missing = [tool for tool in TOOLS if tool not in found]
        hidden = [tool for tool in found if tool not in usable]
        if missing:
            fixes.append("install " + ", ".join(missing))
        if hidden:
            fixes.append(
                f"install {', '.join(hidden)} where the pool's service can find it (Homebrew, say): "
                "it doesn't use an app's own copy or the active virtualenv"
            )
        if not missing and not hidden:
            try:
                scopes = self.scopes()
                if scopes is not None and "repo" not in scopes:
                    fixes.append(
                        f"gh auth refresh -h github.com -s repo (with {self.account} active)"
                    )
                info = self.call("GET", "") or {}
                if not (info.get("permissions") or {}).get("admin"):
                    fixes.append(f"{self.account} needs admin access to {self.repo}")
            except (pool.PoolError, pool.GitHubError) as exc:
                fixes.append(str(exc))
        detail = f"macOS ({cpu}), gh login {self.account}"
        return Step(1, "preflight", "todo" if fixes else "done", detail, "; ".join(fixes))

    def actions(self) -> Step:
        try:
            perms = self.call("GET", "/actions/permissions") or {}
        except (pool.PoolError, pool.GitHubError) as exc:
            return Step(2, "actions", "blocked", f"can't read the Actions settings: {exc}")
        if perms.get("enabled"):
            return Step(2, "actions", "done", "GitHub Actions is enabled")
        return Step(
            2,
            "actions",
            "answer",
            "GitHub Actions is off for this repo, so no workflow runs. Ask: turn it on?",
            f"setup.py apply actions --repo {self.repo} --yes",
        )

    def self_hosted(self) -> Step:
        checked = load_setup(self.home)["repos"].get(self.repo, {}).get("self_hosted_ok")
        if checked:
            return Step(3, "self-hosted", "done", f"self-hosted runners allowed (probed {checked})")
        return Step(
            3,
            "self-hosted",
            "todo",
            "not checked yet: a probe runner is registered (never started) and deleted at once",
            f"setup.py apply self-hosted --repo {self.repo}",
        )

    def detect(self) -> tuple:
        """(performance cores, RAM in GB), from sysctl."""

        def sysctl(name: str) -> int:
            out = self.run(["sysctl", "-n", name], capture_output=True, text=True, check=False)
            text = (out.stdout or "").strip()
            return int(text) if out.returncode == 0 and text.isdigit() else 0

        cores = sysctl("hw.perflevel0.physicalcpu") or sysctl("hw.physicalcpu") or 1
        return cores, sysctl("hw.memsize") // 2**30

    def capacity(self) -> Step:
        chosen = load_setup(self.home)["repos"].get("*", {}).get("slots")
        if chosen:
            return Step(4, "capacity", "done", f"up to {chosen} jobs at once")
        cores, ram = self.detect()
        n = propose_slots(cores, ram)
        detail = (
            f"{cores} performance cores and {ram} GB: {n} jobs at once leave {KEEP_CORES} cores and "
            f"{KEEP_RAM_GB} GB free. Ask: {n} slots, or another number?"
        )
        return Step(
            4,
            "capacity",
            "answer",
            detail,
            f"setup.py apply capacity --repo {self.repo} --slots {n}",
        )

    def pool_step(self) -> Step:
        version = pool.base_version(self.home)
        beat = pool.read_json(self.home.heartbeat, {})
        if version and self.clock() - float(beat.get("ts") or 0) < HEARTBEAT_FRESH_S:
            return Step(5, "pool", "done", f"actions/runner {version}; the pool is up")
        detail = (
            "installs actions/runner (checked against its published SHA-256) and a login service "
            "that keeps the pool running. Ask: install it?"
        )
        return Step(5, "pool", "answer", detail, f"setup.py apply pool --repo {self.repo} --yes")

    def register(self) -> Step:
        if self.repo in pool.load_config(self.home)["repos"]:
            return Step(6, "register", "done", f"the pool serves {self.repo} as {self.account}")
        return Step(
            6,
            "register",
            "todo",
            "the pool doesn't serve this repo yet",
            f"setup.py apply register --repo {self.repo}",
        )

    def route(self) -> Step:
        try:
            values = self.variables()
            public = not (self.call("GET", "") or {}).get("private", True)
        except (pool.PoolError, pool.GitHubError) as exc:
            return Step(7, "route", "blocked", f"can't read the repo variables: {exc}")
        value = values.get("FACTORY_RUNNER")
        if values.get("FACTORY_CI", "").lower() == "off":
            detail = "CI is off (FACTORY_CI=off), so every job skips. Ask: turn CI back on?"
            return Step(
                7, "route", "answer", detail, f"gh variable delete FACTORY_CI -R {self.repo}"
            )
        if (value or "").lower() == "local":
            return Step(7, "route", "done", "CI runs on the pool (FACTORY_RUNNER=local)")
        detail = "CI asks for GitHub-hosted runners. Ask: send this repo's CI to the pool?"
        if public:
            detail += (
                " This repo is public: GitHub advises against self-hosted runners for public repos;"
                " fork PRs still never run here."
            )
        return Step(7, "route", "answer", detail, f"setup.py apply route --repo {self.repo} --yes")

    def smoke_step(self) -> Step:
        ran = load_setup(self.home)["repos"].get(self.repo, {}).get("smoke_ok")
        if ran:
            return Step(8, "smoke", "done", f"CI ran on the pool ({ran})")
        try:
            self.call("GET", f"/actions/workflows/{DEFAULT_WORKFLOW}")
        except pool.GitHubError as exc:
            if exc.status == 404:
                return Step(
                    8,
                    "smoke",
                    "answer",
                    f"this repo has no .github/workflows/{DEFAULT_WORKFLOW}. Ask: which workflow "
                    "runs its CI? Its jobs must ask for the pool when FACTORY_RUNNER is local "
                    "(amplifactory's docs/reference/ci-variables.md)",
                    f"setup.py apply smoke --repo {self.repo} --workflow FILE",
                )
        except pool.PoolError:
            pass  # can't tell from here; the smoke run itself says what's wrong
        return Step(
            8,
            "smoke",
            "todo",
            f"run CI ({DEFAULT_WORKFLOW}) once on the pool, end to end",
            f"setup.py apply smoke --repo {self.repo}",
        )

    def hand_runners(self) -> list:
        data = self.call("GET", "/actions/runners", params={"per_page": 100}) or {}
        return [
            reg
            for reg in data.get("runners") or []
            if not str(reg.get("name", "")).startswith("factory-")
        ]

    def local_services(self) -> list:
        """(plist, runner folder) of runner services installed by hand for this repo (svc.sh)."""
        owner, name = self.repo.split("/")
        found = []
        for path in sorted(self.agents.glob(f"actions.runner.{owner}-{name}.*.plist")):
            try:
                with open(path, "rb") as fh:
                    program = plistlib.load(fh).get("ProgramArguments") or [""]
            except (OSError, plistlib.InvalidFileException):
                program = [""]
            found.append((path, str(Path(program[0]).parent) if program[0] else "?"))
        return found

    def retire(self) -> Step:
        try:
            names = [str(reg.get("name")) for reg in self.hand_runners()]
        except (pool.PoolError, pool.GitHubError) as exc:
            return Step(9, "retire", "blocked", f"can't list the repo's runners: {exc}")
        services = self.local_services()
        if not names and not services:
            return Step(9, "retire", "done", "no hand-registered runners")
        stop = [
            f"`./svc.sh stop && ./svc.sh uninstall` in {folder} ({plist.name})"
            for plist, folder in services
        ]
        if not names:
            detail = "a runner service registered by hand still runs on this Mac: stop it"
            return Step(9, "retire", "todo", detail, "; ".join(stop))
        detail = (
            f"runners registered by hand: {', '.join(names)}. The pool doesn't need them, but one may "
            "be someone else's. Ask about each: remove it?"
        )
        if stop:
            detail += " Then stop the local service: " + "; ".join(stop) + "."
        return Step(
            9,
            "retire",
            "answer",
            detail,
            f"setup.py apply retire --repo {self.repo} --yes --runner NAME (one per yes)",
        )

    def plan(self) -> list:
        return [
            self.preflight(),
            self.actions(),
            self.self_hosted(),
            self.capacity(),
            self.pool_step(),
            self.register(),
            self.route(),
            self.smoke_step(),
            self.retire(),
        ]

    def apply(
        self,
        step: str,
        yes: bool,
        slots: int | None = None,
        runners=(),
        workflow: str = DEFAULT_WORKFLOW,
    ) -> str:
        if step == "capacity":
            if not slots:
                raise pool.PoolError("say how many: --slots N")
            pool.update_config(self.home, lambda cfg: cfg.update(slots=slots))
            mark(self.home, "*", "slots", slots)
            return f"the pool runs up to {slots} jobs at once"
        if step == "pool":
            return self.install_pool(yes)
        if step == "register":
            return pool.repo_add(self.home, self.repo, self.account)
        if step == "route":
            return self.set_route(yes)
        if step == "smoke":
            return self.smoke(workflow)
        if step == "retire":
            return self.remove_runners(yes, runners)
        if step == "preflight":
            return render(self.preflight())
        if step == "actions":
            if not yes:
                raise pool.PoolError(
                    "turning Actions on changes a repo setting: ask the maintainer, then add --yes"
                )
            try:
                self.call("PUT", "/actions/permissions", body={"enabled": True})
            except pool.GitHubError as exc:
                raise pool.PoolError(
                    f"GitHub refused to turn Actions on ({exc}); an enterprise or organization "
                    "policy may forbid it, so ask its owner"
                ) from None
            return "GitHub Actions is on"
        if step == "self-hosted":
            return self.probe()
        raise pool.PoolError(f"unknown step {step!r}; the steps are: {', '.join(STEPS)}")

    def probe(self) -> str:
        """Register a JIT runner that never starts, then delete it: GitHub shows no setting that
        says whether self-hosted runners are allowed, so this is how to find out."""
        name = f"factory-probe-{secrets.token_hex(3)}"
        body = {
            "name": name,
            "runner_group_id": 1,
            "labels": ["self-hosted", "factory-probe"],
            "work_folder": "_work",
        }
        try:
            reply = self.call("POST", "/actions/runners/generate-jitconfig", body=body) or {}
        except pool.GitHubError as exc:
            if exc.status in (403, 422):
                raise pool.PoolError(
                    f"self-hosted runners are blocked for {self.repo} ({exc}). If an enterprise "
                    f'policy blocks them (for EMU repos: "{EMU_POLICY}"), ask its owner; until '
                    "then set FACTORY_CI=off and gate merges on the local checks"
                ) from None
            raise pool.PoolError(f"the probe failed: {exc}") from None
        runner_id = (reply.get("runner") or {}).get("id")
        note = ""
        try:
            self.call("DELETE", f"/actions/runners/{runner_id}")
        except pool.GitHubError as exc:
            note = (
                f"; deleting the probe runner {name} failed ({exc}): remove it under Settings → "
                "Actions → Runners, or GitHub removes it within a day"
            )
        mark(self.home, self.repo, "self_hosted_ok", pool.iso(self.clock()))
        return "self-hosted runners are allowed (a probe runner was registered and deleted)" + note

    def install_pool(self, yes: bool) -> str:
        if not yes:
            raise pool.PoolError(
                "installing the pool adds a login service to this Mac: ask the maintainer, then add --yes"
            )
        lines = []
        if pool.base_version(self.home) is None:
            release = self.gh.call("GET", pool.RELEASE_PATH, self.account)
            asset = pool.release_asset(release, pool.runner_platform(self.system, self.machine))
            self.home.make()
            with pool.file_lock(self.home.root / "base.lock"):
                pool.install_base(self.home, asset)
            lines.append(f"installed actions/runner {asset['version']} (SHA-256 checked)")
        return "\n".join(lines + self.service.install(True))

    def set_route(self, yes: bool) -> str:
        if not yes:
            raise pool.PoolError(
                "routing CI changes a repo variable: ask the maintainer, then add --yes"
            )
        body = {"name": "FACTORY_RUNNER", "value": "local"}
        try:
            self.call("POST", "/actions/variables", body=body)
        except pool.GitHubError as exc:
            if exc.status != 409:  # 409: it exists, so update it
                raise
            self.call("PATCH", "/actions/variables/FACTORY_RUNNER", body=body)
        return "FACTORY_RUNNER=local: the repo's CI jobs now ask for the pool"

    def smoke(self, workflow: str = DEFAULT_WORKFLOW) -> str:
        """Run the CI workflow once (workflow_dispatch) and watch it, bounded."""
        if not WORKFLOW_RE.fullmatch(workflow):
            raise pool.PoolError(f"--workflow takes a file name such as ci.yml, not {workflow!r}")
        branch = (self.call("GET", "") or {}).get("default_branch") or "main"
        started = self.clock()
        self.call("POST", f"/actions/workflows/{workflow}/dispatches", body={"ref": branch})
        run = None
        while run is None and self.clock() < started + SMOKE_FIND_S:
            self.sleep(5)
            params = {"event": "workflow_dispatch", "per_page": 5}
            data = self.call("GET", f"/actions/workflows/{workflow}/runs", params=params) or {}
            fresh = [
                r
                for r in data.get("workflow_runs") or []
                if (stamp_s(r.get("created_at")) or 0) >= started - 60
            ]
            run = fresh[0] if fresh else None
        if run is None:
            raise pool.PoolError(
                "the smoke run didn't show up in 2 minutes: look at the Actions tab"
            )
        url = run.get("html_url", "")
        deadline = self.clock() + SMOKE_TIMEOUT_S
        while run.get("status") != "completed":
            if self.clock() > deadline:
                raise pool.PoolError(
                    f"the smoke run is still {run.get('status')} after 15 minutes ({url}): does "
                    f"`setup.py doctor --repo {self.repo}` show the pool up and serving this repo?"
                )
            self.sleep(10)
            run = self.call("GET", f"/actions/runs/{run.get('id')}") or run
        jobs = (self.call("GET", f"/actions/runs/{run.get('id')}/jobs") or {}).get("jobs") or []
        starts = [stamp_s(job.get("started_at")) for job in jobs if job.get("started_at")]
        ends = [stamp_s(job.get("completed_at")) for job in jobs if job.get("completed_at")]
        created = stamp_s(run.get("created_at"))
        if run.get("conclusion") != "success":
            raise pool.PoolError(f"the smoke run ended {run.get('conclusion')}: {url}")
        elsewhere = sorted(
            {
                str(job.get("runner_name") or job.get("conclusion"))
                for job in jobs
                if not str(job.get("runner_name") or "").startswith("factory-")
            }
        )
        if not jobs or elsewhere:
            raise pool.PoolError(
                f"the smoke run passed, but not on the pool (ran on: {', '.join(elsewhere) or 'none'}): "
                f"{url}. Is the route step done, and does {workflow} route by FACTORY_RUNNER?"
            )
        mark(self.home, self.repo, "smoke_ok", pool.iso(self.clock()))
        wait = f"{int(min(starts) - created)} s" if starts and created else "?"
        took = f"{int(max(ends) - min(starts))} s" if starts and ends else "?"
        return (
            f"CI ran on the pool: queued {wait}, ran {took} ({url}). A workflow_dispatch run isn't a "
            "pull request's required check; the next PR's CI is."
        )

    def remove_runners(self, yes: bool, names) -> str:
        if not yes or not names:
            raise pool.PoolError(
                "removing runners needs the maintainer's yes and their names: --yes --runner NAME"
            )
        lines = []
        listed = {str(reg.get("name")): reg for reg in self.hand_runners()}
        for name in names:
            if name.startswith("factory-"):
                raise pool.PoolError(f"{name} is a pool runner; the pool removes its own")
            reg = listed.get(name)
            if reg is None:
                lines.append(f"{name}: no such hand-registered runner")
                continue
            try:
                self.call("DELETE", f"/actions/runners/{reg.get('id')}")
                lines.append(f"{name}: removed")
            except pool.GitHubError as exc:
                lines.append(
                    f"{name}: not removed ({exc}); a busy runner can be removed after its job"
                )
        return "\n".join(lines)


# ------------------------------------------------------------------------------------ the service

LABEL = "com.amplifactory.runner-pool"
# what launchd gives an agent that docker may need: SSH_AUTH_SOCK for an ssh:// Docker context
LAUNCHD_ENV = ("HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "SSH_AUTH_SOCK")
HEARTBEAT_WAIT_S = 60
UNLOAD_WAIT_S = 30
# what a pool home may hold; --purge refuses a folder with anything else in it
POOL_FILES = {
    "base",
    "base.lock",
    "base.new",
    "base.old",
    "bin",
    "cache",
    "config.json",
    "config.lock",
    "downloads",
    "heartbeat.json",
    "hooks",
    "jobs.jsonl",
    "logs",
    "paused.json",
    "serve.lock",
    "setup.json",
    "setup.lock",
    "slots",
    "state.json",
}


def pool_version(path: Path) -> str | None:
    """The POOL_VERSION of a runners.py copy, or None when there is none."""
    try:
        found = re.search(
            r'^POOL_VERSION = "(\d+)"', path.read_text(encoding="utf-8"), re.MULTILINE
        )
    except OSError:
        return None
    return found.group(1) if found else None


def base_python() -> str:
    """The interpreter outside any virtualenv: a venv belongs to one repo, which may move."""
    if sys.prefix != sys.base_prefix:
        outside = Path(sys.base_prefix) / "bin" / "python3"
        if outside.exists():
            return str(outside)
    return sys.executable


def system_paths(etc: Path | None = None) -> set:
    """The PATH entries macOS gives every login shell (path_helper): /etc/paths and /etc/paths.d/*."""
    etc = etc or Path("/etc")
    try:
        files = [etc / "paths", *sorted((etc / "paths.d").glob("*"))]
    except OSError:
        files = [etc / "paths"]
    found = set()
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        found.update(line.strip().rstrip("/") for line in lines if line.strip())
    return found


def service_path(environ, public=None) -> str:
    """The PATH for the service: the installer's, minus an active virtualenv's bin and app-private
    tool folders. An agent app can put its own trimmed git or gh first on PATH (the Copilot app's
    git has no `submodule`, which `actions/checkout` runs), so folders inside an `.app` bundle,
    `~/Library/Caches` or `~/Library/Application Support` are dropped. Folders the system lists for
    every shell (`public`, by default /etc/paths and /etc/paths.d) stay: Postgres.app's bin, for one.
    """
    public = system_paths() if public is None else public
    venv = environ.get("VIRTUAL_ENV") or ""
    library = Path(environ.get("HOME") or Path.home()) / "Library"
    private = tuple(f"{library / sub}{os.sep}" for sub in ("Caches", "Application Support"))

    def app_private(part: str) -> bool:
        if part.rstrip("/") in public:
            return False
        return f"{part}{os.sep}".startswith(private) or ".app/" in f"{part}{os.sep}"

    keep = [
        part
        for part in (environ.get("PATH") or "").split(os.pathsep)
        if part and not (venv and part.startswith(venv)) and not app_private(part)
    ]
    return os.pathsep.join(keep) or "/usr/bin:/bin:/usr/sbin:/sbin"


def container_check(
    home: pool.Home, docker=None, slots=None, cpus=None, memory_gb=None, environ=None
) -> tuple:
    """Can this Mac run the pool's jobs in Linux containers? (exit code, lines): EXIT_OK when
    ready, EXIT_SETUP with a `fix:` line for each problem. `slots`, `cpus` and `memory_gb` stand
    in for the pool's settings, for a "what if". Docker is looked for on the service's PATH, where
    the pool will look for it."""
    cfg = pool.load_config(home)
    opts = containers.settings(cfg)
    slots = cfg["slots"] if slots is None else slots
    low, high = pool.SETTINGS["slots"][1:]
    if not low <= slots <= high:
        raise pool.PoolError(f"--slots must be from {low} to {high}")
    cpus = opts["container_cpus"] if cpus is None else cpus
    memory_gb = opts["container_memory_gb"] if memory_gb is None else memory_gb
    for flag, key, value in (
        ("--cpus", "container_cpus", cpus),
        ("--memory", "container_memory_gb", memory_gb),
    ):
        low, high = containers.SETTINGS[key][1:]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise pool.PoolError(f"{flag} must be from {low} to {high}")
    docker = docker or setup_docker(environ)
    if not docker.path:
        fix = "install Docker Desktop, which links docker into /usr/local/bin"
        return EXIT_SETUP, [f"fix: no docker CLI on the service's PATH: {fix}"]
    lines = [f"ok: docker at {docker.path}"]
    try:
        info = docker.info()
    except pool.PoolError as exc:
        fix = "start Docker Desktop, and turn on its Start when you sign in"
        return EXIT_SETUP, [*lines, f"fix: {exc}: {fix}"]
    vm = f"a VM of {info.get('NCPU')} CPUs and {containers.gib(info):.1f} GB"
    lines.append(f"ok: {info.get('OperatingSystem')} {info.get('ServerVersion')}, {vm}")
    problems = containers.fit(info, slots, cpus, memory_gb)
    if not problems:
        lines.append(f"ok: room for {slots} jobs of {cpus} CPUs and {memory_gb} GB")
    lines += [f"fix: {problem}" for problem in problems]
    version = pool.base_version(home)
    if not version:
        lines.append("note: no runner install yet, whose version picks the image")
    elif docker.present(f"{containers.IMAGE}:{version}"):
        lines.append(f"ok: {containers.IMAGE}:{version} is here")
    else:
        lines.append(f"note: {containers.IMAGE}:{version} isn't here yet; it's pulled when needed")
    return (EXIT_SETUP if problems else EXIT_OK), lines


def plist_env(environ) -> dict:
    """The plist's EnvironmentVariables: launchd's own PATH lacks gh, git and uv, which the pool
    and its jobs need."""
    return {"PATH": service_path(environ), "LANG": environ.get("LANG") or "en_US.UTF-8"}


def service_environ(environ) -> dict:
    """The environment the pool's service runs in: what launchd gives every agent (LAUNCHD_ENV),
    and the plist's PATH and LANG. A shell's DOCKER_HOST, DOCKER_CONTEXT or DOCKER_CONFIG isn't in
    it; one set with `launchctl setenv` would be, and isn't seen here."""
    env = {key: environ[key] for key in LAUNCHD_ENV if environ.get(key)}
    env.update(plist_env(environ))
    return env


def setup_docker(environ=None) -> containers.Docker:
    """A Docker that runs as it will in the service: in service_environ(), so it finds the CLI,
    its context and its credential helper where the service will."""
    environ = os.environ if environ is None else environ
    return containers.Docker(environ=service_environ(environ))


def restart(home: pool.Home, service=None) -> list:
    """Restart the pool's service so a new mode takes effect, but only the one that serves this
    pool home: there's one service per Mac."""
    svc = service or Service(home)
    if not svc.plist.exists():
        return ["no pool service on this Mac: a pool started by hand takes the mode when restarted"]
    if not svc.serves():
        why = "restart this pool for the mode to take effect"
        return [f"the pool service runs another pool home, so it wasn't restarted: {why}"]
    return svc.install(True)


def container_on(home: pool.Home, yes: bool, cpus=None, memory_gb=None, docker=None, service=None):
    """Run each of the pool's jobs in its own Linux container: the check must pass, the image
    is pulled and tried, then the mode is saved and the pool restarts."""
    if not yes:
        raise pool.PoolError(
            "container mode changes where every job of this pool runs: ask the maintainer, then "
            "add --yes"
        )
    docker = docker or setup_docker()
    code, lines = container_check(home, docker, None, cpus, memory_gb)
    if code != EXIT_OK:
        raise pool.PoolError("this Mac isn't ready for container mode:\n" + "\n".join(lines))
    version = pool.base_version(home)
    if not version:
        raise pool.PoolError(
            "no runner install yet, whose version picks the image: run `apply pool`"
        )
    ref = docker.pull_now(version)
    said = docker.version_of(ref)
    if said != version:
        raise pool.PoolError(f"{ref} says it's runner {said or 'nothing'}, not {version}")
    opts = containers.settings(pool.load_config(home))
    cpus = opts["container_cpus"] if cpus is None else cpus
    memory_gb = opts["container_memory_gb"] if memory_gb is None else memory_gb
    changes = {"mode": "container", "container_cpus": cpus, "container_memory_gb": memory_gb}
    pool.update_config(home, lambda cfg: cfg.update(changes))
    done = f"each job of the pool runs in a container of {ref}, with {cpus} CPUs and {memory_gb} GB"
    return [done, *restart(home, service)]


def container_off(home: pool.Home, yes: bool, docker=None, service=None) -> list:
    """Send new jobs back to the Mac. Jobs already in containers finish there; when Docker doesn't
    answer, the pool restarts, since watching containers would hold it up."""
    if not yes:
        raise pool.PoolError(
            "running jobs on the Mac again changes where every job of this pool runs: ask the "
            "maintainer, then add --yes"
        )
    pool.update_config(home, lambda cfg: cfg.update(mode="host"))
    docker = docker or setup_docker()
    try:
        docker.info()
    except pool.PoolError as exc:
        return [
            "new jobs run on this Mac, as you, once the pool restarts",
            f"{exc}, so the pool restarts to stop waiting for it",
            *restart(home, service),
        ]
    return [
        "new jobs run on this Mac, as you, from the pool's next pass",
        "jobs already in containers finish there, and Docker removes them",
    ]


class Service:
    """The launchd user agent that keeps `runners.py serve` running."""

    def __init__(
        self,
        home: pool.Home,
        agents: Path | None = None,
        run=subprocess.run,
        clock=time.time,
        sleep=time.sleep,
        uid: int | None = None,
        python: str | None = None,
        environ: dict | None = None,
        docker: containers.Docker | None = None,
    ) -> None:
        self.home = home
        self.agents = agents or Path.home() / "Library" / "LaunchAgents"
        self.plist = self.agents / f"{LABEL}.plist"
        self.run = run
        self.clock = clock
        self.sleep = sleep
        self.target = f"gui/{os.getuid() if uid is None else uid}"
        self.python = python or base_python()
        self.environ = os.environ if environ is None else environ
        self.docker = docker

    def launchctl(self, *args: str) -> subprocess.CompletedProcess:
        return self.run(["launchctl", *args], capture_output=True, text=True, check=False)

    def loaded(self) -> bool:
        return self.launchctl("print", f"{self.target}/{LABEL}").returncode == 0

    def unload(self) -> bool:
        """bootout, then wait until launchd lets go of the job: a pool that is mid-pass takes a
        moment to stop. False when it is still loaded after UNLOAD_WAIT_S."""
        self.launchctl("bootout", f"{self.target}/{LABEL}")
        deadline = self.clock() + UNLOAD_WAIT_S
        while self.loaded():
            if self.clock() > deadline:
                return False
            self.sleep(0.5)
        return True

    def serving(self) -> bool:
        """Is any `serve` (the service's, or one started by hand) holding this pool home?"""
        if not self.home.serve_lock.exists():
            return False
        with open(self.home.serve_lock, "a", encoding="utf-8") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
        return False

    def copy_pool(self) -> str:
        """Put this runners.py in the pool home, with the containers.py it imports in container
        mode and the guard.py that copies into each job's container, unless the
        runners.py there is newer: then all three stay."""
        target = self.home.root / "bin" / "runners.py"
        have = pool_version(target)
        if have and pool.version_tuple(have) > pool.version_tuple(pool.POOL_VERSION):
            return f"kept the pool's newer runners.py (version {have})"
        target.parent.mkdir(parents=True, exist_ok=True)
        guard = containers.GUARD
        # runners.py last: its version stands for all three
        for source in (guard, Path(containers.__file__), Path(pool.__file__)):
            dest = target.parent / source.name
            tmp = dest.with_suffix(".tmp")
            shutil.copyfile(source.resolve(), tmp)
            os.replace(tmp, dest)
        where = f"copied to {target.parent}, with containers.py and guard.py"
        return f"runners.py version {pool.POOL_VERSION} {where}"

    def serves(self) -> bool:
        """Does the installed service run this pool home? There's one service per Mac, so a
        restart for another home would take it over. A plist of any other shape serves none."""
        try:
            with open(self.plist, "rb") as fh:
                data = plistlib.load(fh)
        except (
            OSError,
            ValueError,
            AttributeError,  # a bad <date>, inside plistlib
            RecursionError,  # a crafted binary plist, nested thousands deep
            plistlib.InvalidFileException,
            xml.parsers.expat.ExpatError,
        ):
            return False
        args = data.get("ProgramArguments") if isinstance(data, dict) else None
        if not isinstance(args, list):
            return False
        at = args.index("--home") + 1 if "--home" in args else len(args)
        return args[at:][:1] == [str(self.home.root)]

    def plist_data(self) -> dict:
        log = str(self.home.logs / "service.log")
        program = [self.python, str(self.home.root / "bin" / "runners.py")]
        return {
            "Label": LABEL,
            "ProgramArguments": [*program, "--home", str(self.home.root), "serve"],
            "RunAtLoad": True,
            "KeepAlive": True,
            # Standard: Background would put the pool, and every CI job it starts, on the
            # slowest scheduling and throttled I/O (efficiency cores only on Apple silicon)
            "ProcessType": "Standard",
            "WorkingDirectory": str(self.home.root),
            "EnvironmentVariables": plist_env(self.environ),
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }

    def install(self, yes: bool) -> list:
        if not yes:
            raise pool.PoolError(
                "installing a login service changes this Mac: ask the maintainer, then add --yes"
            )
        if pool.base_version(self.home) is None:
            raise pool.PoolError("no runner installed yet: run `runners.py base --install` first")
        self.home.logs.mkdir(parents=True, exist_ok=True)
        lines = [self.copy_pool()]
        self.agents.mkdir(parents=True, exist_ok=True)
        with open(self.plist, "wb") as fh:
            plistlib.dump(self.plist_data(), fh)
        old_pid = pool.read_json(self.home.heartbeat, {}).get("pid")
        if self.loaded() and not self.unload():
            raise pool.PoolError(f"the old {LABEL} didn't stop in {UNLOAD_WAIT_S} s: try again")
        started = self.clock()
        out = self.launchctl("bootstrap", self.target, str(self.plist))
        if out.returncode != 0:
            said = (out.stderr or out.stdout or "").strip()
            raise pool.PoolError(f"launchctl bootstrap failed: {said}")
        lines.append(f"{LABEL} loaded from {self.plist}")
        while self.clock() < started + HEARTBEAT_WAIT_S:
            beat = pool.read_json(self.home.heartbeat, {})
            # a new process's heartbeat: a pool stopping just now may still write a last one
            if float(beat.get("ts") or 0) >= started and beat.get("pid") != old_pid:
                return lines + [
                    f"the pool is up (pid {beat.get('pid')}, {beat.get('slots')} slots)"
                ]
            self.sleep(1.0)
        raise pool.PoolError(
            f"{LABEL} is loaded, but the pool wrote no heartbeat in {HEARTBEAT_WAIT_S} s: see "
            f"{self.home.logs / 'service.log'}"
        )

    def status(self) -> list:
        state = "loaded" if self.loaded() else "not loaded"
        lines = [f"{LABEL}: {state}" + ("" if self.plist.exists() else " (no plist)")]
        beat = pool.read_json(self.home.heartbeat, {})
        if beat:
            since = self.clock() - float(beat.get("ts") or 0)
            # a clock set back leaves the heartbeat ahead of it: say so, not a negative age
            when = (
                f"{int(-since)} s ahead of this Mac's clock"
                if since <= -1
                else f"{int(since)} s ago"
            )
            busy = f"{beat.get('busy')}/{beat.get('slots')} busy"
            lines.append(f"heartbeat {when} (pid {beat.get('pid')}, {busy})")
        else:
            lines.append("no heartbeat yet")
        return lines

    def uninstall(self, purge: bool, yes: bool, github: pool.GitHub | None = None) -> list:
        if purge and not yes:
            raise pool.PoolError(
                "--purge deletes this pool's runners and its home (logs, jobs.jsonl): ask the "
                "maintainer, then add --yes"
            )
        gone = not self.home.root.exists()
        if purge and not gone:
            self.check_home()
        lines = []
        if not self.loaded():
            lines.append("the service wasn't loaded")
        elif self.unload():
            lines.append("service stopped")
        else:
            raise pool.PoolError(f"{LABEL} didn't stop in {UNLOAD_WAIT_S} s: try again")
        if self.plist.exists():
            self.plist.unlink()
            lines.append(f"removed {self.plist}")
        if not purge:
            return lines
        if gone:
            return lines + [f"{self.home.root} is already gone"]
        if self.serving():
            raise pool.PoolError(
                f"a pool still serves {self.home.root} (serve.lock is held, maybe a `runners.py "
                "serve` started by hand): stop it, then purge again"
            )
        return lines + self.purge(github)

    def check_home(self) -> None:
        """Refuse to purge a folder that isn't a pool home (a mistaken --home)."""
        root = self.home.root
        if not (root / "config.json").is_file():
            raise pool.PoolError(
                f"{root} has no config.json, so it isn't a pool home: not deleting"
            )
        extra = sorted(
            item.name
            for item in root.iterdir()
            if item.name not in POOL_FILES and not item.name.endswith(".tmp")
        )
        if extra:
            raise pool.PoolError(f"{root} holds files a pool doesn't make ({extra}): not deleting")

    def purge(self, github: pool.GitHub | None) -> list:
        lines = []
        launcher = pool.Launcher(self.home.slots)
        for slot, groups in sorted(launcher.scan().items()):
            launcher.stop(groups)
            lines.append(f"stopped the runner in {slot}")
        lines += self.purge_containers()
        cfg = pool.load_config(self.home)
        prefix = f"factory-{cfg.get('pool_id', '')}-"
        gh = github or pool.GitHub(pool.Tokens())
        for repo, entry in sorted(cfg["repos"].items()):
            path = f"/repos/{repo}/actions/runners"
            try:
                data = gh.call("GET", path, entry["account"], params={"per_page": 100}) or {}
            except (pool.GitHubError, pool.PoolError) as exc:
                lines.append(f"{repo}: can't list its runners ({exc}); GitHub drops unused ones")
                continue
            for reg in data.get("runners") or []:
                name = str(reg.get("name") or "")
                if not name.startswith(prefix):
                    continue  # not this pool's: a hand-registered runner is never touched
                try:
                    gh.call("DELETE", f"{path}/{reg.get('id')}", entry["account"])
                    lines.append(f"{repo}: deleted runner {name}")
                except pool.GitHubError as exc:
                    lines.append(f"{repo}: could not delete runner {name} ({exc})")
        shutil.rmtree(self.home.root)
        return lines + [f"deleted {self.home.root}"]

    def purge_containers(self) -> list:
        """Stop and remove this pool's containers and its Docker network, whatever the mode.
        With Docker down, warn and go on: running containers end with their jobs, and Docker
        removes them."""
        pool_id = str(pool.load_config(self.home).get("pool_id") or "")
        docker = self.docker or setup_docker(self.environ)
        if not pool.POOL_ID_RE.match(pool_id) or not docker.path:
            return []
        launcher = containers.ContainerLauncher(self.home, pool.Launcher(self.home.slots), docker)
        try:
            found = launcher.scan()
        except pool.PoolError as exc:
            return [f"warning: {exc}: containers still running end with their jobs"]
        lines = []
        for slot, groups in sorted(found.items()):
            launcher.stop(groups)
            lines.append(f"stopped the container in {slot}")
        network = f"amplifactory-{pool_id}"
        with contextlib.suppress(pool.PoolError):
            if docker("network", "rm", network, check=False).returncode == 0:
                lines.append(f"removed the Docker network {network}")
        return lines


class _Parser(argparse.ArgumentParser):
    """Usage errors exit 1 (EXIT_ERROR): argparse's own 2 would read as "setup needed"."""

    def error(self, message: str):
        self.print_usage(sys.stderr)
        self.exit(EXIT_ERROR, f"{self.prog}: error: {message}\n")


def main(argv: list | None = None) -> int:
    parser = _Parser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true", help="run offline checks and exit")
    parser.add_argument(
        "--home", help=f"pool home (default: ${pool.HOME_ENV} or {pool.DEFAULT_HOME})"
    )
    sub = parser.add_subparsers(dest="cmd")
    doctor = sub.add_parser("doctor", help="quick health check; the exit code says what next")
    doctor.add_argument("--repo", required=True)
    plan = sub.add_parser("plan", help="the setup steps and what each needs")
    plan.add_argument("--repo", required=True)
    plan.add_argument("--json", action="store_true")
    apply = sub.add_parser("apply", help="run one setup step")
    apply.add_argument("step", choices=STEPS)
    apply.add_argument("--repo", required=True)
    apply.add_argument("--yes", action="store_true", help="the maintainer agreed to the change")
    apply.add_argument("--slots", type=int, help="capacity: jobs at once")
    apply.add_argument("--runner", action="append", default=[], help="retire: a runner's name")
    apply.add_argument(
        "--workflow",
        default=DEFAULT_WORKFLOW,
        help=f"smoke: the CI workflow file to run (default {DEFAULT_WORKFLOW})",
    )
    service = sub.add_parser("service", help="the launchd service that keeps the pool running")
    service.add_argument("action", choices=("install", "uninstall", "status"))
    service.add_argument("--yes", action="store_true", help="the maintainer agreed to the change")
    service.add_argument(
        "--purge", action="store_true", help="uninstall: also this pool's runners and its home"
    )
    container = sub.add_parser("container", help="Linux containers per CI job")
    container.add_argument("action", choices=("check", "on", "off"))
    container.add_argument("--yes", action="store_true", help="the maintainer agreed to the change")
    container.add_argument("--slots", type=int, help="check: jobs at once (default: the pool's)")
    container.add_argument("--cpus", type=int, help="CPUs per job (default: the pool's, 2)")
    container.add_argument(
        "--memory", type=int, help="GB of memory per job (default: the pool's, 2)"
    )
    args = parser.parse_args(argv)
    if args.self_test:
        _self_test()
        return 0
    if not args.cmd:
        parser.print_help()
        return 0
    try:
        if args.cmd == "container":
            home = pool.resolve_home(args.home)
            if args.action == "check":
                code, lines = container_check(home, None, args.slots, args.cpus, args.memory)
                print("\n".join(lines))
                return code
            if args.action == "on":
                lines = container_on(home, args.yes, args.cpus, args.memory)
            else:
                lines = container_off(home, args.yes)
            print("\n".join(lines))
            return 0
        if args.cmd == "service":
            svc = Service(pool.resolve_home(args.home))
            if args.action == "install":
                lines = svc.install(args.yes)
            elif args.action == "uninstall":
                lines = svc.uninstall(args.purge, args.yes)
            else:
                lines = svc.status()
            print("\n".join(lines))
            return 0
        setup = Setup(args.repo, pool.resolve_home(args.home))
        if args.cmd == "doctor":
            code, lines = setup.doctor()
            print("\n".join(lines))
            return code
        if args.cmd == "plan":
            steps = setup.plan()
            if args.json:
                print(json.dumps([asdict(step) for step in steps], indent=1))
            else:
                print("\n".join(render(step) for step in steps))
            return 0
        print(setup.apply(args.step, args.yes, args.slots, args.runner, args.workflow))
        return 0
    except (pool.PoolError, pool.GitHubError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


# ------------------------------------------------------------------------------------- self-test


class _FakeAPI:
    """Replies by (method, path); every call must carry TOKEN-<account>."""

    def __init__(self, account: str) -> None:
        self.token = "Bearer TOKEN-" + account
        self.replies: dict = {}
        self.calls: list = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> pool.Reply:
        path = urllib.request.urlparse(request.full_url).path
        self.calls.append((request.get_method(), path))
        assert request.get_header("Authorization") == self.token, path
        status, headers, body = self.replies.get((request.get_method(), path), (404, {}, {}))
        if callable(body):
            body = body(json.loads(request.data) if request.data else None)
        return pool.Reply(status, headers, json.dumps(body).encode() if body is not None else b"")


def _fake_gh(cmd, **kwargs):
    assert not set(pool.TOKEN_ENV) & set(kwargs["env"])
    return subprocess.CompletedProcess(cmd, 0, "TOKEN-" + cmd[cmd.index("--user") + 1] + "\n", "")


def _no_sysctl(cmd, **kwargs):
    return subprocess.CompletedProcess(cmd, 1, "", "no sysctl in the self-test")


class _FakeService:
    def __init__(self) -> None:
        self.installed = 0

    def loaded(self) -> bool:
        return bool(self.installed)

    def install(self, yes: bool) -> list:
        assert yes
        self.installed += 1
        return ["the pool is up"]


def _setup(root: Path, api: _FakeAPI, now: float, **kwargs) -> Setup:
    def clock() -> float:
        return now

    tokens = kwargs.pop("tokens", None) or pool.Tokens(run=_fake_gh)
    gh = pool.GitHub(tokens, transport=api, clock=clock)
    kwargs.setdefault("which", lambda tool, path=None: "/usr/bin/" + tool)
    kwargs.setdefault("system", "Darwin")
    kwargs.setdefault("machine", "arm64")
    kwargs.setdefault("service", _FakeService())
    kwargs.setdefault("run", _no_sysctl)
    kwargs.setdefault("agents", root / "LaunchAgents")
    return Setup("octo/app", pool.Home(root), gh, clock=clock, **kwargs)


def _fails(fn, *args) -> str:
    try:
        fn(*args)
    except pool.PoolError as exc:
        return str(exc)
    raise AssertionError(f"{fn.__name__} did not refuse")


def _test_doctor(root: Path) -> None:
    now = datetime(2026, 1, 1, 1, 0, tzinfo=_UTC).timestamp()
    api = _FakeAPI("octo")
    home = pool.Home(root)
    variables = ("GET", "/repos/octo/app/actions/variables")
    runs = ("GET", "/repos/octo/app/actions/runs")

    def set_vars(**values):
        listed = [{"name": key, "value": value} for key, value in values.items()]
        api.replies[variables] = (200, {}, {"variables": listed})

    set_vars(FACTORY_CI="OFF", FACTORY_RUNNER="local")
    assert _setup(root, api, now).doctor()[0] == EXIT_CI_OFF
    set_vars(FACTORY_RUNNER="github")
    code, lines = _setup(root, api, now).doctor()
    assert code == EXIT_OK and "GitHub-hosted" in lines[0], lines
    set_vars()
    code, lines = _setup(root, api, now).doctor()
    assert code == EXIT_SETUP and lines[-1].startswith("next: setup.py plan"), lines
    assert sum(line.startswith("setup: ") for line in lines) == 3, lines
    assert "--body github" in lines[0], lines  # unset: hosted runners, the default
    assert lines[0].startswith("setup: FACTORY_RUNNER is unset"), lines  # step 0 reads it

    # no gh login named after the owner (an org's repo, or gh signed in through GH_TOKEN): for a
    # repo this Mac's pool doesn't serve, gh's own login reads the variables
    def no_login(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "no oauth token found\n")

    def gh_api(cmd, **kwargs):
        if cmd[:2] != ["gh", "api"]:
            return _no_sysctl(cmd, **kwargs)
        assert cmd[2] == "repos/octo/app/actions/variables?per_page=30", cmd
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"variables": []}), "")

    code, lines = _setup(root, api, now, tokens=pool.Tokens(run=no_login), run=gh_api).doctor()
    assert code == EXIT_SETUP and lines[0].startswith("setup: FACTORY_RUNNER is unset"), lines
    said = _fails(_setup(root, api, now, tokens=pool.Tokens(run=no_login)).doctor)
    assert "gh could not read octo/app's variables" in said, said
    set_vars(FACTORY_RUNNER="local")
    pool.repo_add(home, "octo/app", None)
    pool.write_json(home.root / "base" / pool.VERSION_FILE, "x")  # makes base/
    (home.base / pool.VERSION_FILE).write_text("2.338.2\n", encoding="utf-8")
    code, lines = _setup(root, api, now).doctor()
    assert code == EXIT_DOWN and "down:" in lines[-2], lines
    pool.write_json(home.heartbeat, {"ts": now - 30, "busy": 1, "slots": 4})
    api.replies[runs] = (200, {}, {"workflow_runs": [{"created_at": "2026-01-01T00:45:00Z"}]})
    code, lines = _setup(root, api, now).doctor()
    assert code == EXIT_OK and "1/4 busy" in lines[-2] and "15 min" in lines[-1], lines
    assert "admitting" not in lines[-2], lines
    api.replies[variables] = (403, {}, {"message": "Must have admin rights"})
    try:
        _setup(root, api, now).doctor()
        raise AssertionError("a 403 must fail the doctor")
    except pool.GitHubError as exc:
        assert exc.status == 403 and "TOKEN-" not in str(exc)
    assert len([c for c in api.calls if c == runs]) == 1  # only a healthy pool reads the queue
    set_vars(FACTORY_RUNNER="local")
    api.replies[runs] = (502, {}, {"message": "Bad gateway"})
    code, lines = _setup(root, api, now).doctor()
    assert code == EXIT_OK and lines[-1].startswith("warning: couldn't read the queue"), lines
    # the Mac admits fewer slots: the up line says how many, and why
    held = {"slots": 0, "reason": "memory pressure is critical"}
    pool.write_json(home.heartbeat, {"ts": now - 30, "busy": 1, "slots": 4, "held": held})
    code, lines = _setup(root, api, now).doctor()
    up = "ok: the pool is up (1/4 busy), admitting 0 of 4 slots: memory pressure is critical"
    assert code == EXIT_OK and lines[-2] == up, lines
    for junk in ("x", [], {}, {"slots": 2}):  # not a `held` the pool writes: no suffix, no crash
        pool.write_json(home.heartbeat, {"ts": now - 30, "busy": 1, "slots": 4, "held": junk})
        code, lines = _setup(root, api, now).doctor()
        assert code == EXIT_OK and lines[-2] == "ok: the pool is up (1/4 busy)", (junk, lines)
    pool.write_json(home.heartbeat, {"ts": now - 30, "busy": 1, "slots": 4})
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            main(["doctor"])
            raise AssertionError("a usage error must exit")
        except SystemExit as exc:
            assert exc.code == EXIT_ERROR, exc.code


def _test_steps(root: Path) -> None:
    now = 1_800_000_000.0
    api = _FakeAPI("octo")
    api.replies[("GET", "/user")] = (200, {"x-oauth-scopes": "gist, read:org, repo"}, {})
    api.replies[("GET", "/repos/octo/app")] = (200, {}, {"permissions": {"admin": True}})
    perms = ("GET", "/repos/octo/app/actions/permissions")
    api.replies[perms] = (200, {}, {"enabled": False})
    steps = _setup(root, api, now).plan()[:3]
    assert [(s.name, s.status) for s in steps] == [
        ("preflight", "done"),
        ("actions", "answer"),
        ("self-hosted", "todo"),
    ], steps
    assert _setup(root, api, now, system="Linux").preflight().status == "blocked"
    missing = _setup(
        root, api, now, which=lambda tool, path=None: None if tool == "uv" else "/bin/x"
    )
    assert missing.preflight().next == "install uv"
    app_gh = "/Users/me/Library/Caches/agent-gh-2.0"

    def only_app_gh(tool, path=None):  # this Mac's only gh is an app's own copy
        if tool != "gh":
            return "/usr/bin/" + tool
        return f"{app_gh}/gh" if app_gh in (path or "").split(os.pathsep) else None

    env = {"PATH": f"{app_gh}{os.pathsep}/usr/bin", "HOME": "/Users/me"}
    hidden = _setup(root, api, now, which=only_app_gh, environ=env).preflight()
    assert hidden.status == "todo" and hidden.next.startswith("install gh where the pool's"), hidden
    api.replies[("GET", "/user")] = (200, {"x-oauth-scopes": "gist"}, {})
    api.replies[("GET", "/repos/octo/app")] = (200, {}, {"permissions": {"admin": False}})
    fixes = _setup(root, api, now).preflight().next
    offline = _setup(root, api, now)

    def no_network(request, timeout):
        raise urllib.error.URLError("offline")

    offline.gh.transport = no_network
    assert "GET /user: no reply (URLError)" in offline.preflight().next
    assert "gh auth refresh -h github.com -s repo" in fixes and "needs admin" in fixes, fixes

    setup = _setup(root, api, now)
    assert "--yes" in _fails(setup.apply, "actions", False)
    put = ("PUT", "/repos/octo/app/actions/permissions")
    api.replies[put] = (409, {}, {"message": "Conflict: enterprise policy"})
    assert "ask its owner" in _fails(setup.apply, "actions", True)

    def enable(body):
        assert body == {"enabled": True}, body

    api.replies[put] = (204, {}, enable)
    assert setup.apply("actions", True) == "GitHub Actions is on" and api.calls[-1] == put

    jit = ("POST", "/repos/octo/app/actions/runners/generate-jitconfig")
    api.replies[jit] = (403, {}, {"message": "Self-hosted runners are disabled"})
    assert EMU_POLICY in _fails(setup.apply, "self-hosted", False)
    posted = []

    def accept(body):
        posted.append(body)
        return {"runner": {"id": 7, "name": body["name"]}, "encoded_jit_config": "JITSECRET"}

    api.replies[jit] = (201, {}, accept)
    api.replies[("DELETE", "/repos/octo/app/actions/runners/7")] = (500, {}, {"message": "oops"})
    said = setup.apply("self-hosted", False)
    assert "allowed" in said and posted[0]["name"] in said, said  # a failed delete is named
    assert ("DELETE", "/repos/octo/app/actions/runners/7") in api.calls
    api.replies[("DELETE", "/repos/octo/app/actions/runners/7")] = (204, {}, None)
    said = setup.apply("self-hosted", False)
    assert said == "self-hosted runners are allowed (a probe runner was registered and deleted)"
    assert posted[0]["labels"] == ["self-hosted", "factory-probe"]
    assert setup.self_hosted().status == "done"
    saved = (root / SETUP_FILE).read_text(encoding="utf-8")
    assert "JITSECRET" not in saved and "TOKEN-" not in saved
    assert "unknown step" in _fails(setup.apply, "nope", False)


def _test_service(root: Path) -> None:
    now = [1_800_000_000.0]
    home = pool.Home(root / "pool")
    agents = root / "LaunchAgents"
    loaded = [False]
    calls = []
    pids: list = []

    def launchctl(cmd, **kwargs):
        calls.append(cmd[1:])
        if cmd[1] == "print":
            return subprocess.CompletedProcess(cmd, 0 if loaded[0] else 113, "", "")
        if cmd[1] == "bootstrap":
            loaded[0] = True
            pids.append(4321 + len(pids))
            pool.write_json(home.heartbeat, {"ts": now[0] + 2, "pid": pids[-1], "slots": 2})
        if cmd[1] == "bootout":
            loaded[0] = False
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    env = {"PATH": "/opt/homebrew/bin:/usr/bin:/bin", "LANG": "en_US.UTF-8", "GH_TOKEN": "x"}
    svc = Service(
        home, agents, launchctl, clock, sleep, uid=501, python="/usr/bin/python3", environ=env
    )
    assert "--yes" in _fails(svc.install, False)
    assert "base --install" in _fails(svc.install, True)
    _fake_base_install(home)
    lines = svc.install(True)
    assert lines[-1].startswith("the pool is up (pid 4321"), lines
    assert ["bootstrap", "gui/501", str(svc.plist)] in calls, calls
    with open(svc.plist, "rb") as fh:
        data = plistlib.load(fh)
    copied = home.root / "bin" / "runners.py"
    assert data["ProgramArguments"] == [
        "/usr/bin/python3",
        str(copied),
        "--home",
        str(home.root),
        "serve",
    ]
    assert data["KeepAlive"] is True and data["RunAtLoad"] is True and data["Label"] == LABEL
    assert data["ProcessType"] == "Standard"
    assert data["EnvironmentVariables"] == {"PATH": env["PATH"], "LANG": "en_US.UTF-8"}
    assert pool_version(copied) == pool.POOL_VERSION
    copied.write_text('POOL_VERSION = "99"\n', encoding="utf-8")  # a newer pool stays
    calls.clear()
    assert svc.install(True)[0].startswith("kept the pool's newer runners.py (version 99)")
    target = f"gui/501/{LABEL}"
    assert calls[0] == ["print", target] and calls[1] == ["bootout", target], calls
    assert svc.status()[0] == f"{LABEL}: loaded"
    beat = pool.read_json(home.heartbeat, {})
    for ts, said in (
        (now[0] - 5, "heartbeat 5 s ago (pid"),
        (now[0] + 0.5, "heartbeat 0 s ago (pid"),  # under a second ahead: just now
        (now[0] + 30, "heartbeat 30 s ahead of this Mac's clock (pid"),  # the clock was set back
    ):
        pool.write_json(home.heartbeat, {**beat, "ts": ts})
        assert svc.status()[1].startswith(said), (said, svc.status())
    pool.write_json(home.heartbeat, beat)

    def silent(cmd, **kwargs):  # loads, but the pool never writes a heartbeat
        return subprocess.CompletedProcess(cmd, 0 if cmd[1] != "print" else 113, "", "")

    now[0] += 300  # the last heartbeat is old by now
    quiet = Service(home, agents, silent, clock, sleep, uid=501, environ=env)
    assert "no heartbeat" in _fails(quiet.install, True)

    def stale(cmd, **kwargs):  # a new pid, but an old heartbeat: not a fresh start either
        if cmd[1] == "bootstrap":
            pool.write_json(home.heartbeat, {"ts": now[0] - 5, "pid": 999, "slots": 2})
        return silent(cmd, **kwargs)

    assert "no heartbeat" in _fails(Service(home, agents, stale, clock, sleep, 501).install, True)
    assert service_path({"PATH": "/v/bin:/usr/bin", "VIRTUAL_ENV": "/v"}) == "/usr/bin"
    agent = [
        "/Applications/Agent.app/Contents/MacOS",
        "/Users/me/Library/Caches/agent-git-2.56.1/bin",
        "/Users/me/Library/Application Support/Agent/gh",
        "/Users/me/Library/pnpm",
        "/Users/me/Library/CachesX/bin",
        "/opt/foo.application/bin",
        "/Applications/Postgres.app/Contents/Versions/latest/bin",
        "/Users/me/.local/bin",
        "/opt/homebrew/bin",
        "/usr/bin",
    ]
    kept = agent[3:6] + agent[7:]
    env = {"PATH": ":".join(agent), "HOME": "/Users/me"}
    assert service_path(env, public=set()) == ":".join(kept), service_path(env, public=set())
    public = {"/Applications/Postgres.app/Contents/Versions/latest/bin"}  # /etc/paths.d lists it
    assert service_path(env, public) == ":".join(kept[:3] + agent[6:7] + kept[3:])
    etc = root / "etc"
    (etc / "paths.d").mkdir(parents=True)
    (etc / "paths").write_text("/usr/local/bin\n/usr/bin\n\n", encoding="utf-8")
    (etc / "paths.d" / "postgresapp").write_text(f"{agent[6]}/\n", encoding="utf-8")
    assert system_paths(etc) == {"/usr/local/bin", "/usr/bin", agent[6]}
    assert system_paths(root / "nowhere") == set()
    polls = [0]

    def slow(cmd, **kwargs):  # still loaded for two polls after bootout
        if cmd[1] == "print":
            polls[0] += 1
            return subprocess.CompletedProcess(cmd, 0 if polls[0] <= 3 else 113, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert Service(home, agents, slow, clock, sleep, 501).unload() and polls[0] == 4

    def stuck(cmd, **kwargs):  # never unloads
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert "didn't stop" in _fails(Service(home, agents, stuck, clock, sleep, 501).install, True)

    assert svc.uninstall(False, False)[0] == "service stopped" and not svc.plist.exists()
    assert "--yes" in _fails(svc.uninstall, True, False)
    pool.repo_add(home, "octo/app", None)  # a config.json alone doesn't make a folder a pool home
    (home.root / "notes.txt").write_text("mine\n", encoding="utf-8")
    assert "holds files a pool doesn't make" in _fails(svc.uninstall, True, True)
    assert (home.root / "notes.txt").exists()
    (home.root / "notes.txt").unlink()
    with open(home.serve_lock, "a", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX)  # a `serve` started by hand
        assert "serve.lock is held" in _fails(svc.uninstall, True, True)
    assert home.root.exists()
    pool_id = pool.load_config(home)["pool_id"]
    api = _FakeAPI("octo")
    regs = [{"id": 1, "name": f"factory-{pool_id}-9-abcd"}, {"id": 2, "name": "my-macbook"}]
    api.replies[("GET", "/repos/octo/app/actions/runners")] = (200, {}, {"runners": regs})
    api.replies[("DELETE", "/repos/octo/app/actions/runners/1")] = (204, {}, None)
    gh = pool.GitHub(pool.Tokens(run=_fake_gh), transport=api, clock=clock)
    made = {path.name for name, path in vars(home).items() if name != "root"}
    assert made <= POOL_FILES, made - POOL_FILES  # every file a pool makes may be purged
    (home.cache / "uv" / "wheels").mkdir(parents=True)  # a pool that has run a job
    lines = svc.uninstall(True, True, gh)
    assert ("DELETE", "/repos/octo/app/actions/runners/1") in api.calls
    assert ("DELETE", "/repos/octo/app/actions/runners/2") not in api.calls  # hand-registered
    assert not home.root.exists() and lines[-1] == f"deleted {home.root}", lines
    assert svc.uninstall(True, True, gh)[-1] == f"{home.root} is already gone"  # idempotent


def _fake_base_install(home: pool.Home) -> None:
    home.base.mkdir(parents=True, exist_ok=True)
    (home.base / pool.VERSION_FILE).write_text("2.338.2\n", encoding="utf-8")


def _test_walkthrough(root: Path) -> None:
    now = [stamp_s("2026-01-01T12:00:00Z") or 0.0]
    api = _FakeAPI("octo")
    home = pool.Home(root)

    def sysctl(cmd, **kwargs):
        values = {"hw.perflevel0.physicalcpu": "12", "hw.memsize": str(48 * 2**30)}
        return subprocess.CompletedProcess(cmd, 0, values.get(cmd[-1], "") + "\n", "")

    def sleep(seconds: float) -> None:
        now[0] += seconds

    service = _FakeService()
    setup = _setup(root, api, 0.0, run=sysctl, sleep=sleep, service=service)
    setup.clock = lambda: now[0]
    assert propose_slots(12, 48) == 8 and propose_slots(8, 16) == 1 and propose_slots(24, 128) == 20
    step = setup.capacity()
    assert (
        step.status == "answer"
        and "--slots 8" in step.next
        and "12 performance cores" in step.detail
    )
    assert "--slots" in _fails(setup.apply, "capacity", False)
    assert setup.apply("capacity", False, 6) == "the pool runs up to 6 jobs at once"
    assert pool.load_config(home)["slots"] == 6 and setup.capacity().status == "done"

    assert setup.pool_step().status == "answer"
    assert "--yes" in _fails(setup.apply, "pool", False)
    _fake_base_install(home)
    assert setup.apply("pool", True) == "the pool is up" and service.installed == 1
    pool.write_json(home.heartbeat, {"ts": now[0]})
    assert setup.pool_step().status == "done"

    assert setup.register().status == "todo"
    setup.apply("register", False)
    assert setup.register().status == "done"

    variables = ("GET", "/repos/octo/app/actions/variables")
    api.replies[variables] = (200, {}, {"variables": []})
    api.replies[("GET", "/repos/octo/app")] = (
        200,
        {},
        {"private": False, "default_branch": "trunk"},
    )
    step = setup.route()
    assert step.status == "answer" and "public" in step.detail, step
    assert "--yes" in _fails(setup.apply, "route", False)
    created = ("POST", "/repos/octo/app/actions/variables")
    api.replies[created] = (409, {}, {"message": "Already exists"})
    api.replies[("PATCH", "/repos/octo/app/actions/variables/FACTORY_RUNNER")] = (204, {}, None)
    assert "FACTORY_RUNNER=local" in setup.apply("route", True)
    assert api.calls[-1] == ("PATCH", "/repos/octo/app/actions/variables/FACTORY_RUNNER")
    api.replies[variables] = (
        200,
        {},
        {"variables": [{"name": "FACTORY_RUNNER", "value": "local"}]},
    )
    assert setup.route().status == "done"
    off = [{"name": "FACTORY_RUNNER", "value": "local"}, {"name": "FACTORY_CI", "value": "off"}]
    api.replies[variables] = (200, {}, {"variables": off})
    assert setup.route().next == "gh variable delete FACTORY_CI -R octo/app"
    api.replies[variables] = (200, {}, {"variables": off[:1]})

    dispatched = []
    api.replies[("POST", "/repos/octo/app/actions/workflows/ci.yml/dispatches")] = (
        204,
        {},
        lambda body: dispatched.append(body),
    )
    run = {"id": 5, "status": "queued", "created_at": "2026-01-01T12:00:03Z", "html_url": "u"}
    api.replies[("GET", "/repos/octo/app/actions/workflows/ci.yml/runs")] = (
        200,
        {},
        {"workflow_runs": [run]},
    )
    done = {**run, "status": "completed", "conclusion": "success"}
    api.replies[("GET", "/repos/octo/app/actions/runs/5")] = (200, {}, done)
    hosted = [{"runner_name": "GitHub Actions 12", "started_at": "2026-01-01T12:00:43Z"}]
    api.replies[("GET", "/repos/octo/app/actions/runs/5/jobs")] = (200, {}, {"jobs": hosted})
    assert "not on the pool (ran on: GitHub Actions 12)" in _fails(setup.apply, "smoke", False)
    step = setup.smoke_step()  # the fake API has no ci.yml yet: ask which workflow runs CI
    assert step.status == "answer" and step.next.endswith("--workflow FILE"), step
    api.replies[("GET", "/repos/octo/app/actions/workflows/ci.yml")] = (403, {}, {})
    assert setup.smoke_step().status == "todo"  # only a 404 means there's no ci.yml
    api.replies[("GET", "/repos/octo/app/actions/workflows/ci.yml")] = (200, {}, {"id": 1})
    assert setup.smoke_step().status == "todo"
    assert "a file name such as ci.yml" in _fails(
        setup.apply, "smoke", False, None, (), "../ci.yml"
    )
    jobs = [
        {
            "runner_name": "factory-abc123-5-beef",
            "started_at": "2026-01-01T12:00:43Z",
            "completed_at": "2026-01-01T12:01:48Z",
        },
        {
            "runner_name": "factory-abc123-5-cafe",
            "started_at": "2026-01-01T12:00:50Z",
            "completed_at": "2026-01-01T12:01:30Z",
        },
    ]
    api.replies[("GET", "/repos/octo/app/actions/runs/5/jobs")] = (200, {}, {"jobs": jobs})
    said = setup.apply("smoke", False)
    assert dispatched[-1] == {"ref": "trunk"} and "queued 40 s, ran 65 s" in said, said
    assert setup.smoke_step().status == "done"
    other = "/repos/octo/app/actions/workflows/template-ci.yml"  # a repo whose CI isn't ci.yml
    api.replies[("POST", f"{other}/dispatches")] = (204, {}, lambda body: dispatched.append(other))
    api.replies[("GET", f"{other}/runs")] = (200, {}, {"workflow_runs": [run]})
    said = setup.apply("smoke", False, None, (), "template-ci.yml")
    assert dispatched[-1] == other and "CI ran on the pool" in said, (dispatched, said)
    assert ("GET", f"{other}/runs") in api.calls  # the run is looked up in the same workflow

    runners = [{"id": 1, "name": "factory-abc123-5-beef"}, {"id": 2, "name": "my-macbook"}]
    api.replies[("GET", "/repos/octo/app/actions/runners")] = (200, {}, {"runners": runners})
    setup.agents.mkdir(parents=True, exist_ok=True)
    with open(setup.agents / "actions.runner.octo-app.my-macbook.plist", "wb") as fh:
        plistlib.dump({"ProgramArguments": ["/Users/me/actions-runner/runsvc.sh"]}, fh)
    step = setup.retire()
    assert step.status == "answer" and "my-macbook" in step.detail and "factory-" not in step.detail
    assert "/Users/me/actions-runner" in step.detail and "Ask about each" in step.detail
    assert step.next.endswith("--runner NAME (one per yes)")
    assert "--yes" in _fails(setup.apply, "retire", False, None, ["my-macbook"])
    assert "pool runner" in _fails(setup.apply, "retire", True, None, ["factory-abc123-5-beef"])
    api.replies[("DELETE", "/repos/octo/app/actions/runners/2")] = (204, {}, None)
    assert setup.apply("retire", True, None, ["my-macbook"]) == "my-macbook: removed"
    assert ("DELETE", "/repos/octo/app/actions/runners/1") not in api.calls
    api.replies[("GET", "/repos/octo/app/actions/runners")] = (200, {}, {"runners": runners[:1]})
    step = setup.retire()  # only the local service is left: its commands are the next step
    assert step.status == "todo" and "./svc.sh stop" in step.next, step
    assert len(setup.plan()) == len(STEPS)


def _test_container_check(root: Path) -> None:
    home = pool.Home(root)
    home.make()
    pool.write_json(home.config, {"slots": 8, "pool_id": "abc123"})
    fake = containers.FakeDocker()

    def check(docker=None, **kwargs) -> tuple:
        docker = docker or containers.Docker("/usr/local/bin/docker", fake.run, fake.popen, {})
        return container_check(home, docker, **kwargs)

    code, lines = check()  # Docker Desktop's "8 GB" VM, and 8 slots of 2 GB
    assert code == EXIT_SETUP and lines[0] == "ok: docker at /usr/local/bin/docker", lines
    small = [line for line in lines if line.startswith("fix: ")]
    assert len(small) == 1 and "16 GB" in small[0] and "Settings → Resources" in small[0], lines
    assert lines[-1] == "note: no runner install yet, whose version picks the image", lines
    home.base.mkdir()
    (home.base / pool.VERSION_FILE).write_text("2.338.2\n", encoding="utf-8")
    code, lines = check(slots=3)
    assert code == EXIT_OK and "ok: room for 3 jobs of 2 CPUs and 2 GB" in lines, lines
    assert lines[-1] == f"ok: {containers.IMAGE}:2.338.2 is here", lines
    fake.info["MemTotal"] = int(15.6 * containers.GIB)  # Docker Desktop's "16 GB"
    fake.tags.clear()
    code, lines = check()
    assert code == EXIT_OK and "isn't here yet" in lines[-1], lines
    assert check(cpus=17)[0] == EXIT_SETUP  # more than the VM's 16 CPUs
    fake.down = True
    code, lines = check()
    assert code == EXIT_SETUP and "Docker isn't running" in lines[-1], lines
    assert "start Docker Desktop" in lines[-1], lines
    for bad, why in (
        ({"slots": 0}, "--slots must be from 1 to 64"),
        ({"cpus": 0}, "--cpus must be from 1 to 64"),
        ({"memory_gb": 300}, "--memory must be from 1 to 256"),
        ({"cpus": True}, "--cpus must be from 1 to 64"),
    ):
        assert _fails(lambda kwargs=bad: check(**kwargs)) == why, bad

    # docker is looked for on the service's PATH: an app's private copy doesn't count
    bins = root / "bins"
    for folder in (bins / "Some.app" / "Contents" / "bin", bins / "bin"):
        folder.mkdir(parents=True)
        (folder / "docker").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        (folder / "docker").chmod(0o755)
    environ = {
        "PATH": f"{bins / 'Some.app' / 'Contents' / 'bin'}:{bins / 'bin'}",
        "HOME": str(root),
    }
    code, lines = container_check(home, environ=environ)
    assert lines[0] == f"ok: docker at {bins / 'bin' / 'docker'}" and code == EXIT_SETUP, lines
    (bins / "empty").mkdir()  # so the service's PATH isn't empty: its fallback has /usr/bin
    environ["PATH"] = f"{bins / 'Some.app' / 'Contents' / 'bin'}:{bins / 'empty'}"
    code, lines = container_check(home, environ=environ)
    assert code == EXIT_SETUP and lines[0].startswith("fix: no docker CLI"), lines

    # docker runs in the service's environment, not the shell's: its DOCKER_HOST, DOCKER_CONTEXT
    # and DOCKER_CONFIG don't count, and HOME (for ~/.docker) and LANG do
    given = {"USER": "alice", "LOGNAME": "alice", "SHELL": "/bin/zsh", "TMPDIR": "/t/"}
    given["SSH_AUTH_SOCK"] = "/t/agent.sock"
    shell = dict(environ, **given, LANG="C", GH_TOKEN="TOKEN-z", DOCKER_HOST="tcp://x:2375")
    shell.update(DOCKER_CONTEXT="other", DOCKER_CONFIG=str(root / "docker-config"), TERM="x")
    want = dict(given, HOME=str(root), PATH=service_path(shell), LANG="C")
    assert setup_docker(shell).environ == want, setup_docker(shell).environ


class _Restarts:
    """The pool's service, for the container tests: it counts restarts, and may serve another
    pool home."""

    def __init__(self, plist: Path) -> None:
        self.plist = plist
        self.mine = True
        self.restarts = 0

    def serves(self) -> bool:
        return self.mine

    def install(self, yes: bool) -> list:
        assert yes
        self.restarts += 1
        return ["the pool is up"]


def _test_container_on_off(root: Path) -> None:
    home = pool.Home(root / "home")
    home.make()
    pool.write_json(home.config, {"slots": 8, "pool_id": "abc123"})
    fake = containers.FakeDocker(tags=())
    docker = containers.Docker("/usr/local/bin/docker", fake.run, fake.popen, {})
    svc = _Restarts(root / "agent.plist")
    ref = f"{containers.IMAGE}:2.338.2"

    def mode() -> str:
        return pool.load_config(home).get("mode", "host")

    def on(*args) -> list:
        return container_on(home, True, *args, docker=docker, service=svc)

    def off(yes: bool = True) -> list:
        return container_off(home, yes, docker=docker, service=svc)

    assert "--yes" in _fails(lambda: container_on(home, False, docker=docker, service=svc))
    refused = _fails(on)  # 8 slots of 2 GB on Docker Desktop's "8 GB" VM
    assert "isn't ready" in refused and "16 GB" in refused and mode() == "host", refused
    fake.info["MemTotal"] = int(15.6 * containers.GIB)  # its "16 GB"
    assert "no runner install" in _fails(on) and mode() == "host"
    home.base.mkdir()
    (home.base / pool.VERSION_FILE).write_text("2.338.2\n", encoding="utf-8")
    fake.versions[ref] = "2.300.0"  # an image that isn't the runner its tag names
    assert "not 2.338.2" in _fails(on) and ["pull", "--quiet", ref] in fake.calls
    assert mode() == "host"
    del fake.versions[ref]

    lines = on(1, 1)  # no service yet: the mode is saved, and nothing restarts
    assert mode() == "container" and svc.restarts == 0 and "no pool service" in lines[-1], lines
    assert lines[0].endswith("with 1 CPUs and 1 GB"), lines
    svc.plist.write_text("", encoding="utf-8")
    assert on()[-1] == "the pool is up" and svc.restarts == 1  # the service of this home
    cfg = pool.load_config(home)
    assert (cfg["container_cpus"], cfg["container_memory_gb"]) == (1, 1), "kept when not given"
    svc.mine = False  # one service per Mac: never restart it for another pool home
    assert "another pool home" in on()[-1] and svc.restarts == 1
    svc.mine = True

    assert "--yes" in _fails(lambda: off(False)) and mode() == "container"
    lines = off()  # Docker answers: containers finish, and nothing restarts
    assert mode() == "host" and svc.restarts == 1 and "finish there" in lines[-1], lines
    fake.down = True  # Docker doesn't: the pool restarts, so it stops waiting for Docker
    lines = off()
    assert svc.restarts == 2 and "Docker isn't running" in lines[1], lines


def _test_container_doctor(root: Path) -> None:
    now = datetime(2026, 1, 1, 1, 0, tzinfo=_UTC).timestamp()
    api = _FakeAPI("octo")
    listed = [{"name": "FACTORY_RUNNER", "value": "local"}]
    api.replies[("GET", "/repos/octo/app/actions/variables")] = (200, {}, {"variables": listed})
    api.replies[("GET", "/repos/octo/app/actions/runs")] = (200, {}, {"workflow_runs": []})
    home = pool.Home(root)
    home.make()
    served = {"octo/app": {"account": "octo"}}
    pool.write_json(
        home.config, {"slots": 8, "pool_id": "abc123", "mode": "container", "repos": served}
    )
    home.base.mkdir()
    (home.base / pool.VERSION_FILE).write_text("2.338.2\n", encoding="utf-8")
    pool.write_json(home.heartbeat, {"ts": now - 30, "busy": 1, "slots": 8})
    fake = containers.FakeDocker()
    docker = containers.Docker("/usr/local/bin/docker", fake.run, fake.popen, {})
    setup = _setup(root, api, now, docker=docker)
    code, lines = setup.doctor()  # a pool from before container mode still runs jobs on the Mac
    assert code == EXIT_OK and "ok: container mode is on, and Docker runs" in lines[-3], lines
    assert lines[-2].startswith("warning: the pool that runs (version ?) predates"), lines
    assert lines[-1].startswith("warning: Docker's VM has 7.7 GB of memory"), lines
    first = int(CONTAINER_POOL_VERSION)
    assert pool.version_tuple(pool.POOL_VERSION) >= (first,), "runners.py has container mode"
    beat = {"ts": now - 30, "busy": 1, "slots": 8}
    pool.write_json(home.heartbeat, dict(beat, version=str(first - 1)))
    warning = f"warning: the pool that runs (version {first - 1}) predates container mode"
    assert setup.doctor()[1][-2].startswith(warning)
    pool.write_json(home.heartbeat, dict(beat, version=CONTAINER_POOL_VERSION))
    code, lines = setup.doctor()
    assert code == EXIT_OK and not [line for line in lines if "predates" in line], lines
    # a pool that started in host mode, before `container on`, still runs jobs on the Mac
    started = "warning: the pool that runs (pid 42) started in host mode, so its jobs still run"
    for launcher, warned in (("host", True), ("container", False), (None, False)):
        mine = dict(beat, version=pool.POOL_VERSION, pid=42)
        if launcher:  # a pool from before `launcher` has no such key: no warning
            mine["launcher"] = launcher
        pool.write_json(home.heartbeat, mine)
        code, lines = setup.doctor()
        assert code == EXIT_OK and any(line.startswith(started) for line in lines) == warned, lines
    # a pool from before the guard runs jobs that can reach the Mac's own services
    older = "is older than the block of the Mac's localhost"
    restart = (
        "so its jobs can reach the Mac's own services: restart it (stop it first if you started "
        "it by hand), setup.py service install --yes"
    )
    cases = (
        ("15", "container", True),
        ("9", None, True),  # from before `launcher`: versions compare as numbers
        (GUARD_POOL_VERSION, "container", False),
        (pool.POOL_VERSION, "container", False),
    )
    for version, launcher, warned in cases:
        mine = dict(beat, version=version)
        if launcher:
            mine["launcher"] = launcher
        pool.write_json(home.heartbeat, mine)
        code, lines = setup.doctor()
        assert code == EXIT_OK and any(older in line for line in lines) == warned, lines
        if warned:
            assert f"warning: the pool that runs (version {version}) {older}, {restart}" in lines
    pool.write_json(home.heartbeat, dict(beat, version="15", launcher="host", pid=42))
    code, lines = setup.doctor()  # started in host mode outranks the guard: one warning, that one
    assert any(line.startswith(started) for line in lines), lines
    assert not any(older in line for line in lines), lines
    assert pool.version_tuple(pool.POOL_VERSION) >= pool.version_tuple(GUARD_POOL_VERSION)
    gone = dict(beat, ts=now - 600, version="15", launcher="container")  # down: it runs nothing
    pool.write_json(home.heartbeat, gone)
    code, lines = setup.doctor()
    assert code == EXIT_DOWN and not any(older in line for line in lines), lines
    stale = dict(beat, ts=now - 600, version=pool.POOL_VERSION, pid=42, launcher="host")
    pool.write_json(home.heartbeat, stale)  # a pool that's down runs no jobs: no warning
    code, lines = setup.doctor()
    assert code == EXIT_DOWN and not any("started in host mode" in line for line in lines), lines
    pool.write_json(home.heartbeat, dict(beat, version=CONTAINER_POOL_VERSION))
    fake.down = True
    code, lines = setup.doctor()
    assert code == EXIT_DOWN and "Docker isn't running" in lines[-2], lines
    assert lines[-1].startswith("next: start Docker Desktop"), lines
    cfg = pool.load_config(home)
    pool.write_json(home.config, dict(cfg, mode="host"))
    calls = len(fake.calls)
    assert setup.doctor()[0] == EXIT_OK and len(fake.calls) == calls, "host mode never asks Docker"


def _test_container_service(root: Path) -> None:
    home = pool.Home(root / "home")
    home.make()
    pool.write_json(home.config, {"slots": 2, "pool_id": "abc123"})
    fake = containers.FakeDocker()
    docker = containers.Docker("/usr/local/bin/docker", fake.run, fake.popen, {})
    svc = Service(home, agents=root / "agents", docker=docker)
    assert svc.copy_pool().endswith("with containers.py and guard.py")
    for source in (Path(pool.__file__), Path(containers.__file__), containers.GUARD):
        assert (home.root / "bin" / source.name).read_bytes() == source.read_bytes(), source.name

    # the service serves the home its plist names, and no other
    svc.agents.mkdir()
    with open(svc.plist, "wb") as fh:
        plistlib.dump(svc.plist_data(), fh)
    assert svc.serves() and not Service(pool.Home(root / "other"), agents=svc.agents).serves()
    svc.plist.write_bytes(svc.plist.read_bytes()[:200])  # cut short: it serves nothing, no crash
    assert not svc.serves()
    # nor does a plist of another shape: a root array or string, odd ProgramArguments, a bad
    # <date>, or a --home with nothing after it
    head = b'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0">'
    home_arg = f"<string>--home</string><string>{svc.home.root}</string>".encode()
    for body in (
        b"<array>" + home_arg + b"</array>",
        b"<string>--home</string>",
        b"<dict><key>ProgramArguments</key><integer>7</integer></dict>",
        b"<dict><key>ProgramArguments</key><string>--home</string></dict>",
        b"<dict><key>ProgramArguments</key><dict/></dict>",
        b"<dict><key>When</key><date>soon</date></dict>",
        b"<dict><key>ProgramArguments</key><array><string>--home</string></array></dict>",
    ):
        svc.plist.write_bytes(head + body + b"</plist>")
        assert not svc.serves(), body

    # purge: this pool's containers and network go; an unlabelled one and another pool's stay
    mine = {containers.POOL_LABEL: "abc123", containers.SLOT_LABEL: "s01"}
    fake.containers["a" * 64] = {"state": "running", "labels": mine, "files": {}, "rm": True}
    fake.networks["amplifactory-abc123"] = {"labels": {}, "options": {containers.ICC: "false"}}
    lines = svc.purge_containers()
    assert lines == [
        "stopped the container in s01",
        "removed the Docker network amplifactory-abc123",
    ], lines
    assert "a" * 64 not in fake.containers and fake.untouched() and not fake.networks
    fake.down = True  # Docker down: warn, and the purge goes on
    assert svc.purge_containers()[0].startswith("warning: docker ps failed"), "warns"


def _self_test() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        _test_doctor(root / "doctor")
        _test_steps(root / "steps")
        _test_service(root / "service")
        _test_walkthrough(root / "walk")
        _test_container_check(root / "container")
        _test_container_on_off(root / "container-on")
        _test_container_doctor(root / "container-doctor")
        _test_container_service(root / "container-service")
    containers._self_test()
    print("setup.py self-test: ok")


if __name__ == "__main__":
    sys.exit(main())

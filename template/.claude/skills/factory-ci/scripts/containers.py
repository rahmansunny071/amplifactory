"""Container mode for the factory CI runner pool: each CI job runs in its own Linux container.

runners.py starts each just-in-time runner as a process in a slot on this Mac. In container mode
(config.json `"mode": "container"`), ContainerLauncher starts it in a container of the official
image, ghcr.io/actions/actions-runner, at the version of the pool's runner install. The container:
- gets nothing from the Mac: no mounts and no Docker socket, and only the JIT config, the job-started
  hook (copied in, to /opt) and LANG in its environment;
- can't reach the Mac's own services: guard.py, copied in and run first as root with
  NET_ADMIN, blocks Docker Desktop's subnets (the host gateway, host.docker.internal, and its HTTP
  proxy, which relay to the Mac's loopback), checks the block, then runs the runner as `runner`
  without NET_ADMIN, which sudo can't get back. NET_RAW is dropped too, so no raw sockets, and IPv6
  is off, since a socket bound to the interface gets past an IPv6 route;
- is on the pool's own Docker network, which lets it reach the internet but not other jobs;
- has the CPU and memory limits in config.json (container_cpus, container_memory_gb), and a limit
  on processes;
- is removed when its runner exits (`--rm`).

So a job can't read your files, reach your gh, git, SSH or keychain logins, or reach a server on
the Mac's 127.0.0.1. It has sudo inside its container, as on ubuntu-latest. The Docker VM's own
services and the Mac's services that listen on all interfaces stay reachable: a container's port
published without 127.0.0.1 at the pool network's gateway, and probably others through the Mac's
LAN address.

ContainerLauncher has runners.Launcher's interface (start, scan, reap, stop) and wraps the pool's
own Launcher: runners started on the Mac before a switch to containers stay watched, and while
"mode" is "host", start() runs new runners on the Mac. A mode it doesn't know fails each start, so
a typo never runs jobs on the Mac. Liveness comes from Docker. When `docker ps` fails, scan()
raises, so the pool cleans nothing up on a guess. A new runner release's image is pulled in the
background, and the newest image here runs meanwhile.

What it may delete: only containers labelled with this pool's id, by their 64-hex IDs, never by
name. It never copies anything out of a container, and the only file it writes on the Mac is each
runner's runner.log.

setup.py's `container check` uses Docker.info() and fit() to tell whether Docker's VM can hold the
pool's slots. Stdlib only, Python 3.9+; it imports runners.py from the same folder.

    python3 .claude/skills/factory-ci/scripts/containers.py --self-test
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import runners

IMAGE = "ghcr.io/actions/actions-runner"
MODES = ("host", "container")
# config.json settings: (default, lowest, highest); runners.py keeps keys it doesn't know
SETTINGS = {"container_cpus": (2, 1, 64), "container_memory_gb": (2, 1, 256)}
POOL_LABEL = "amplifactory.pool"
SLOT_LABEL = "amplifactory.slot"
LOG_LABEL = "amplifactory.log"
ICC = (
    "com.docker.network.bridge.enable_icc"  # off: containers on the network can't reach each other
)
JIT_ENV = "ACTIONS_RUNNER_INPUT_JITCONFIG"
HOOK_ENV = "ACTIONS_RUNNER_HOOK_JOB_STARTED"
HOOK_PATH = "/opt/factory-job-started.sh"  # inside the container, out of the runner's own folder
RUN_SH = "/home/runner/run.sh"
PYTHON = "/usr/bin/python3"  # the image's: it runs guard.py
GUARD_PATH = "/usr/local/lib/amplifactory-guard.py"  # root's, copied in before the start
GUARD_READY = "amplifactory guard: blocked "  # guard.READY: the block is set, the runner starts
GUARD = Path(__file__).with_name("guard.py")  # copied into each container; setup.py copies it too
PIDS_LIMIT = 4096
LOG_CAP = 256 << 20  # bytes of a runner.log on the Mac; checked each pass
# the log Docker keeps in its VM, for each container: a flood can't fill the VM's disk. The local
# driver compresses rotated files by default, and refuses to with a single file
VM_LOG = (
    "--log-driver",
    "local",
    "--log-opt",
    "max-size=16m",
    "--log-opt",
    "max-file=1",
    "--log-opt",
    "compress=false",
)
SLACK = 0.9  # of the VM memory the slots need: an "8 GB" Docker Desktop VM reports 7.65 GiB
RUNNING_S = 15.0  # how long start() waits for the guard to say the block is set
CALL_S = 60.0
PULL_S = 900.0  # a pull in the foreground (setup.py container on)
PULL_RETRY_S = 600.0  # after a background pull failed
ALIVE = ("running", "paused", "restarting")
# `docker ps` rows: ID, state, slot and log path, tab-separated
FORMAT = "\t".join(
    ("{{.ID}}", "{{.State}}", '{{.Label "' + SLOT_LABEL + '"}}', '{{.Label "' + LOG_LABEL + '"}}')
)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
ID_RE = re.compile(r"^[0-9a-f]{64}$")
GONE_RE = re.compile(r"No such container|already in progress")  # --rm got there first
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
GIB = 1 << 30

log = logging.getLogger("factory-ci.pool")  # runners.py's logger, so lines land in pool.log


def first_line(text: str | None) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[0][:300] if lines else ""


def settings(cfg: dict) -> dict:
    """The container settings of a pool config, with their defaults. PoolError for a bad one."""
    out = {"mode": cfg.get("mode", MODES[0])}
    if out["mode"] not in MODES:
        raise runners.PoolError(f"config: mode must be one of {', '.join(MODES)}")
    for key, (default, low, high) in SETTINGS.items():
        value = cfg.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise runners.PoolError(f"config: {key} must be a whole number from {low} to {high}")
        out[key] = value
    return out


def gib(info: dict) -> float:
    return int(info.get("MemTotal") or 0) / GIB


def fit(info: dict, slots: int, cpus: int, memory_gb: int) -> list:
    """What keeps Docker's VM from running `slots` jobs of `cpus` CPUs and `memory_gb` GB each,
    with how to fix it: one line per problem, none when it fits."""
    desktop = info.get("OperatingSystem") == "Docker Desktop"
    where = "Docker Desktop → Settings → Resources → Advanced" if desktop else "your Docker VM"
    problems = []
    if info.get("OSType") != "linux":
        problems.append(f"Docker runs {info.get('OSType') or 'unknown'} containers, not Linux ones")
    ncpu = int(info.get("NCPU") or 0)
    if ncpu < cpus:
        problems.append(
            f"Docker's VM has {ncpu} CPUs, fewer than the {cpus} each job gets: raise its CPU "
            f"limit in {where}, or give each job fewer (--cpus)"
        )
    need = slots * memory_gb
    if gib(info) < SLACK * need:
        restart = ", then Apply & restart" if desktop else ""
        problems.append(
            f"Docker's VM has {gib(info):.1f} GB of memory, and {slots} jobs × {memory_gb} GB "
            f"need {need} GB: set its memory to {need} GB or more in {where}{restart}. Or run "
            "fewer jobs at once, or give each less memory (--memory)"
        )
    return problems


class Docker:
    """The docker CLI, with a timeout on every call. A failure is a PoolError with Docker's own
    first line of error. Its environment is the caller's (the CLI finds its context and credential
    helper through HOME and PATH), never with a GitHub token in it."""

    def __init__(
        self,
        path: str | None = None,
        run=subprocess.run,
        popen=subprocess.Popen,
        environ=None,
        clock=time.monotonic,
    ) -> None:
        base = os.environ if environ is None else environ
        self.environ = {key: value for key, value in base.items() if key not in runners.TOKEN_ENV}
        self.path = path or shutil.which("docker", path=self.environ.get("PATH")) or ""
        self.run = run
        self.popen = popen
        self.clock = clock
        self.ready: set = set()  # image references known to be here
        self.networks: set = set()  # pool networks known to be right
        self.pulls: dict = {}  # image reference -> its background pull
        self.fallbacks: set = set()

    def __call__(self, *args: str, extra=None, timeout: float = CALL_S, check: bool = True):
        if not self.path:
            raise runners.PoolError(
                "no docker CLI on the pool's PATH: install Docker Desktop, or put docker on PATH"
            )
        env = dict(self.environ, **(extra or {}))
        try:
            out = self.run(
                [self.path, *args],
                capture_output=True,
                text=True,
                env=env,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise runners.PoolError(f"docker {args[0]} timed out after {int(timeout)} s") from None
        except OSError as exc:
            raise runners.PoolError(f"could not run docker: {exc.__class__.__name__}") from None
        if check and out.returncode != 0:
            why = first_line(out.stderr) or f"exit code {out.returncode}"
            raise runners.PoolError(f"docker {args[0]} failed: {why}")
        return out

    def info(self, timeout: float = CALL_S) -> dict:
        """The runtime and its VM: NCPU, MemTotal, OSType, Architecture, OperatingSystem and
        ServerVersion."""
        out = self("info", "--format", "{{json .}}", check=False, timeout=timeout)
        try:
            data = json.loads(out.stdout or "null") or {}
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        if out.returncode != 0 or data.get("ServerErrors") or not data.get("ServerVersion"):
            errors = data.get("ServerErrors") or []
            why = first_line(out.stderr) or (str(errors[0]) if errors else "no answer")
            raise runners.PoolError(f"Docker isn't running ({why[:200]})")
        keep = ("NCPU", "MemTotal", "OSType", "Architecture", "OperatingSystem", "ServerVersion")
        return {key: data.get(key) for key in keep}

    def ref(self, version: str | None) -> str:
        if not VERSION_RE.match(version or ""):
            raise runners.PoolError(
                f"no runner install to pick the image by (version {version!r}): run "
                "`runners.py base --install`"
            )
        return f"{IMAGE}:{version}"

    def present(self, ref: str) -> bool:
        if ref not in self.ready:
            found = self("image", "inspect", "--format", "{{.Id}}", ref, check=False)
            if found.returncode == 0:
                self.ready.add(ref)
        return ref in self.ready

    def tags(self) -> list:
        """The runner versions of the image that are here, oldest first."""
        out = self("image", "ls", "--format", "{{.Tag}}", IMAGE, check=False)
        found = {tag.strip() for tag in out.stdout.splitlines() if VERSION_RE.match(tag.strip())}
        return sorted(found, key=runners.version_tuple)

    def image(self, version: str | None) -> str:
        """The image for this runner version, without ever waiting for a pull: the one here, or
        else the newest one here while it's pulled in the background. With none here, PoolError,
        so the start fails and the pool backs off until the pull lands. A tag already here is
        never pulled again, so a moved tag can't change what runs."""
        ref = self.ref(version)
        if self.present(ref):
            return ref
        self.pull(ref)
        local = self.tags()
        if not local:
            raise runners.PoolError(f"pulling {ref}: runners start once it's here")
        older = f"{IMAGE}:{local[-1]}"
        if ref not in self.fallbacks:
            self.fallbacks.add(ref)
            log.warning("%s isn't here yet: pulling it, and using %s meanwhile", ref, older)
        return older

    def pull(self, ref: str) -> None:
        """Start `docker pull` in the background, unless one runs, or failed under PULL_RETRY_S
        ago. A failure is logged once, with Docker's reason."""
        job = self.pulls.get(ref)
        if job is not None:
            code = job["proc"].poll()
            if code is None:
                return
            if code != 0 and not job["told"]:
                job["told"] = True
                job["err"].seek(0)
                why = first_line(job["err"].read().decode("utf-8", "replace"))
                log.warning("could not pull %s: %s", ref, why or f"exit code {code}")
            if code != 0 and self.clock() - job["since"] < PULL_RETRY_S:
                return
            job["err"].close()
        err = tempfile.TemporaryFile()  # noqa: SIM115 (the pull writes to it after this returns)
        try:
            proc = self.popen(
                [self.path, "pull", "--quiet", ref],
                env=self.environ,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=err,
                start_new_session=True,
            )
        except OSError as exc:
            err.close()
            raise runners.PoolError(
                f"could not run docker pull: {exc.__class__.__name__}"
            ) from None
        self.pulls[ref] = {"proc": proc, "err": err, "since": self.clock(), "told": False}
        log.info("pulling %s in the background", ref)

    def pull_now(self, version: str | None) -> str:
        """Pull the image for this runner version and wait for it (setup.py container on)."""
        ref = self.ref(version)
        if not self.present(ref):
            self("pull", "--quiet", ref, timeout=PULL_S)
            self.ready.add(ref)
        return ref

    def version_of(self, ref: str) -> str:
        """The runner version an image reports (`./config.sh --version`, with no network), or ""."""
        out = self("run", "--rm", "--network", "none", ref, "./config.sh", "--version", timeout=120)
        found = [line.strip() for line in out.stdout.splitlines() if VERSION_RE.match(line.strip())]
        return found[-1] if found else ""

    def network(self, pool_id: str) -> str:
        """This pool's bridge network, made on first use: its containers reach the internet, but
        not each other. A network of that name that lets them is refused."""
        name = f"amplifactory-{pool_id}"
        if name in self.networks:
            return name
        option = '{{index .Options "' + ICC + '"}}'
        found = self("network", "inspect", "--format", option, name, check=False)
        if found.returncode != 0:
            label = f"{POOL_LABEL}={pool_id}"
            self(
                "network",
                "create",
                "--driver",
                "bridge",
                "--label",
                label,
                "--opt",
                f"{ICC}=false",
                name,
            )
        elif found.stdout.strip() != "false":
            raise runners.PoolError(
                f"Docker network {name} lets containers reach each other: remove it "
                f"(docker network rm {name}), and the pool makes it again"
            )
        self.networks.add(name)
        return name


class ContainerLauncher:
    """runners.Launcher's interface, for runners in containers (see the module docstring). Its
    process groups are container IDs (strings); the host launcher's are ints, and a slot never
    holds both."""

    kind = "container"  # where its runners run: the heartbeat's `launcher`

    def __init__(
        self,
        home,
        host,
        docker: Docker | None = None,
        popen=subprocess.Popen,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.home = home
        self.host = host
        self.docker = docker or Docker()
        self.popen = popen
        self.clock = clock
        self.sleep = sleep
        self.children: dict = {}  # pid of a `docker start --attach` -> (its process, container)
        self.codes: dict = {}
        self.tracked: set = set()  # this pool's containers that the next scan must look at
        self.scanned = False  # has a scan asked Docker yet?
        self.guard = GUARD  # the self-test points it elsewhere
        self.told: set = set()  # (container, message) already logged
        self.pool_id = ""

    def _pool_id(self, cfg: dict | None = None) -> str:
        """The pool's id, which every label and filter is built from. Without a valid one, no
        container is made or removed."""
        if not self.pool_id:
            cfg = cfg or runners.load_config(self.home)
            if not runners.POOL_ID_RE.match(str(cfg.get("pool_id") or "")):
                raise runners.PoolError(
                    "the pool has no valid pool_id, so its containers can't be told"
                )
            self.pool_id = str(cfg["pool_id"])
        return self.pool_id

    def start(self, slot: Path, env: dict, log_path: Path) -> int:
        cfg = runners.load_config(self.home)
        opts = settings(cfg)
        if opts["mode"] != "container":
            return self.host.start(slot, env, log_path)
        jit = str(env.get(JIT_ENV) or "")
        hook = Path(str(env.get(HOOK_ENV) or ""))
        if not jit or not hook.is_file():
            raise runners.PoolError("a container runner needs its JIT config and job-started hook")
        guard = self.guard
        if not guard.is_file():  # never a job without the block
            raise runners.PoolError(
                "container mode needs guard.py next to containers.py: run `setup.py service "
                "install --yes` again"
            )
        if any(char in f"{log_path}{slot.name}" for char in "\t\r\n"):
            raise runners.PoolError(f"a tab or a newline in {log_path} would break the scan")
        image = self.docker.image(runners.base_version(self.home))
        pool_id = self._pool_id(cfg)
        network = self.docker.network(pool_id)
        memory = f"{opts['container_memory_gb']}g"
        args = ["create", "--rm", "--pull", "never"]  # never a pull inside a start: image() pulls
        if NAME_RE.match(hook.stem):
            args += ["--name", hook.stem]  # the runner's name: the pool writes hooks/<name>.sh
        args += [
            "--label",
            f"{POOL_LABEL}={pool_id}",
            "--label",
            f"{SLOT_LABEL}={slot.name}",
            "--label",
            f"{LOG_LABEL}={log_path}",
            "--network",
            network,
            "--env",
            JIT_ENV,  # no value: docker copies it from its own environment, out of any argv
            "--env",
            f"{HOOK_ENV}={HOOK_PATH}",
            "--env",
            "LANG=C.UTF-8",
            "--cpus",
            str(opts["container_cpus"]),
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--pids-limit",
            str(PIDS_LIMIT),
            *VM_LOG,
            # the guard blocks the Mac's own services, then runs RUN_SH as runner without
            # NET_ADMIN, which sudo can't get back; no NET_RAW, so no raw frames; and no IPv6, as a
            # socket bound to the interface gets past an IPv6 route
            "--user",
            "root",
            "--cap-add",
            "NET_ADMIN",
            "--cap-drop",
            "NET_RAW",
            "--sysctl",
            "net.ipv6.conf.all.disable_ipv6=1",
            "--add-host",
            "host.docker.internal:host-gateway",
            image,
            PYTHON,
            GUARD_PATH,
            RUN_SH,
        ]
        try:
            made = self.docker(*args, extra={JIT_ENV: jit})
        except runners.PoolError:
            self.docker.networks.discard(network)  # checked again next time, in case they went
            self.docker.ready.discard(image)
            raise
        lines = made.stdout.strip().splitlines()
        cid = lines[-1].strip() if lines else ""
        if not ID_RE.match(cid):
            raise runners.PoolError("docker create printed no container ID")
        self.tracked.add(cid)
        try:
            self.docker("cp", str(guard), f"{cid}:{GUARD_PATH}")
            self.docker("cp", str(hook), f"{cid}:{HOOK_PATH}")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "ab") as out:
                offset = out.tell()  # a runner's log is appended to: only this start's part counts
                proc = self.popen(
                    [self.docker.path, "start", "--attach", cid],
                    env=self.docker.environ,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            self.children[proc.pid] = (proc, cid)
            self.wait_running(proc, cid, log_path, offset)
        except (OSError, runners.PoolError) as exc:
            self.remove(cid, "created")
            raise runners.PoolError(f"could not start the runner's container: {exc}") from None
        return proc.pid

    def wait_running(self, proc, cid: str, log_path: Path, offset: int = 0) -> None:
        """Wait until the guard says the block is set (GUARD_READY in runner.log), just before the
        runner starts. If `docker start --attach` ends first (Docker refused an option, no
        memory left, the guard failed, a runner that quits at once), raise with the last line it
        wrote, so the pool backs off instead of starting the same failing container on every
        pass: whatever its timing, a guard that fails never counts as started. Only what this
        start wrote (after `offset`) counts. Still starting after RUNNING_S counts as started:
        scan() keeps it while its `docker start --attach` runs."""
        deadline = self.clock() + RUNNING_S
        while self.clock() < deadline:
            done = proc.poll() is not None  # first: a container that has stopped never started
            with open(log_path, "rb") as log_file:
                log_file.seek(offset)
                lines = log_file.read().decode("utf-8", errors="replace").splitlines()
            if done:
                why = lines[-1].strip()[:300] if lines else f"exit code {proc.poll()}"
                raise runners.PoolError(f"its container stopped at once: {why}")
            if any(line.startswith(GUARD_READY) for line in lines):
                return
            self.sleep(0.2)

    def attached(self, cid: str) -> bool:
        """Does this process's `docker start --attach` of that container still run?"""
        return any(c == cid and proc.poll() is None for proc, c in self.children.values())

    def scan(self) -> dict:
        """Slot name -> what runs there: the host launcher's process groups, and this pool's live
        containers. This pool's leftovers (made by a pool that crashed, a start that gave up, or
        exited) are removed. A slot holds one kind: a live container is its slot's runner, so a
        process on the Mac that only names the slot (a `tail -f` in a terminal) is left out of
        the scan, and never signalled."""
        found = self.host.scan()
        if self.scanned and not self.tracked:
            return found  # none of this pool's containers can be running
        held = set(found)  # slots with a runner process on the Mac
        label = f"label={POOL_LABEL}={self._pool_id()}"
        out = self.docker("ps", "--all", "--no-trunc", "--filter", label, "--format", FORMAT)
        watch = set()
        for line in out.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 4 or not ID_RE.match(parts[0]):
                continue
            cid, state, slot, log_path = parts
            live = state in ALIVE or (state == "created" and self.attached(cid))
            if live and self.flooded(log_path):
                self.tell(cid, "removing container %s: its runner.log passed %s MB", LOG_CAP >> 20)
                live = False
            if live:
                if slot in held:
                    held.discard(slot)
                    found[slot] = (
                        set()
                    )  # the host's groups there aren't the runner: never signalled
                    self.tell(
                        cid, "container %s runs slot %s; a process on the Mac also names it", slot
                    )
                found.setdefault(slot, set()).add(cid)
                watch.add(cid)
            elif not self.remove(cid, state):
                watch.add(cid)  # not gone yet: the next scan looks again
        self.tracked = watch
        self.scanned = True
        return found

    def tell(self, cid: str, message: str, *args, level: int = logging.WARNING) -> None:
        """Log a message about a container once: the same message about it isn't repeated each
        scan, but another one is told."""
        if (cid, message) not in self.told:
            self.told.add((cid, message))
            log.log(level, message, cid[:12], *args)

    def flooded(self, log_path: str) -> bool:
        """Has this runner.log (under the pool's logs) passed LOG_CAP?"""
        path = Path(log_path)
        try:
            inside = path.resolve().is_relative_to(self.home.logs.resolve())
            return inside and path.stat().st_size > LOG_CAP
        except OSError:
            return False

    def remove(self, cid: str, state: str) -> bool:
        """`docker rm --force` one of this pool's containers, by its ID. True once it's gone. One
        Docker is still removing, or a failure, is logged once and never raised, so one stuck
        container can't stop a pass."""
        if state == "removing":
            self.tell(cid, "container %s is still being removed; looking again", level=logging.INFO)
            return False
        try:
            out = self.docker("rm", "--force", cid, check=False)
        except runners.PoolError as exc:
            why = str(exc)
        else:
            why = first_line(out.stderr)
            if out.returncode == 0 or "No such container" in why:
                return True
        self.tell(cid, "could not remove container %s (%s); trying again", why or "no reason")
        return False

    def _collect(self) -> None:
        for pid, (proc, _) in list(self.children.items()):
            code = proc.poll()
            if code is not None:
                self.codes[pid] = code
                del self.children[pid]

    def reap(self) -> dict:
        """Exit codes, by pid, of the host's runners and of each `docker start --attach` (which
        exits with its container's code) that ended since the last call."""
        done = dict(self.host.reap())
        self._collect()
        done.update(self.codes)
        self.codes = {}
        return done

    def stop(self, groups, grace: float = runners.STOP_GRACE_S) -> None:
        """Stop containers (one `docker stop` for all, then remove them) and hand process groups to
        the host launcher. Best effort, like a signal: a runner is deregistered before it's
        stopped, and a container a failed stop missed is seen by the next scan."""
        ids = sorted(group for group in groups if isinstance(group, str) and ID_RE.match(group))
        procs = {group for group in groups if isinstance(group, int)}
        if procs:
            self.host.stop(procs, grace)
        if not ids:
            return
        wait = str(max(1, math.ceil(grace)))
        names = [cid[:12] for cid in ids]
        try:
            for args, timeout in (
                (("stop", "-t", wait, *ids), grace + CALL_S),  # -t: --time, then --timeout
                (("rm", "--force", *ids), CALL_S),
            ):
                out = self.docker(*args, check=False, timeout=timeout)
                said = [line.strip() for line in (out.stderr or "").splitlines() if line.strip()]
                why = [line for line in said if not GONE_RE.search(line)]
                if out.returncode != 0 and (why or not said):
                    reason = why[0][:300] if why else f"exit code {out.returncode}"
                    log.warning("docker %s failed for containers %s: %s", args[0], names, reason)
        except runners.PoolError as exc:
            log.warning("could not stop containers %s: %s", names, exc)
        for proc, cid in list(self.children.values()):
            if cid in ids:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
        self._collect()


# ------------------------------------------------------------------------------------- self-test


INFO = {  # Docker Desktop's VM at its "8 GB" setting, as `docker info` reports it
    "NCPU": 16,
    "MemTotal": 8216215552,
    "OSType": "linux",
    "Architecture": "aarch64",
    "OperatingSystem": "Docker Desktop",
    "ServerVersion": "29.3.1",
}
VERSION = "2.338.2"


class _Clock:
    """Fake time: it moves only when the test (or a sleep) moves it."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


class _Proc:
    """A background process of the fake docker: running until its container stops or the test
    ends it."""

    def __init__(self, fake: FakeDocker, pid: int, cid: str = "", ref: str = "", err=None):
        self.fake = fake
        self.pid = pid
        self.cid = cid
        self.ref = ref
        self.err = err
        self.code = None

    def poll(self):
        if self.ref or self.code is not None:
            return self.code
        box = self.fake.containers.get(self.cid)
        if box is not None and "polls" in box:  # still running for that many polls, then exits 1
            if box["polls"] > 0:
                box["polls"] -= 1
                return None
            self.fake.exit(self.cid, 1)
            self.code = 1
            return 1
        if box is not None and box.get("failed"):
            return 1
        if box is not None and box["state"] in ALIVE + ("created",):
            return None
        return self.fake.codes.get(self.cid, 0)

    def wait(self, timeout=None):
        return self.poll()


_TOKENLESS: set = set()  # the docker commands FakeDocker checked for a GitHub token


class FakeDocker:
    """The docker CLI for the self-test: containers, images and networks in memory. Its `run`
    answers the calls Docker and ContainerLauncher make; `popen` runs `docker start --attach` and
    background pulls. Every call is kept with the environment it got. It always holds an
    unlabelled container and another pool's, which nothing may touch."""

    def __init__(self, tags=(VERSION,), info=None) -> None:
        self.tags = set(tags)
        self.info = dict(INFO if info is None else info)
        self.down = False
        self.start_error = ""  # `docker start` fails with this
        self.slow_start = False  # `docker start` leaves the container created, for now
        self.quits = False  # the runner quits at once (exit 1), and --rm removes its container
        self.unguarded = False  # the guard fails (exit 1) before the runner starts
        self.attach_error = False  # starting `docker start --attach` raises
        self.versions: dict = {}  # image reference -> the version it reports, if not its tag
        self.fail: dict = {}  # command -> its stderr, once
        self.containers: dict = {}  # id -> {"state", "labels", "args", "env", "files", "rm"}
        self.codes: dict = {}  # id -> exit code of a container that stopped
        self.networks: dict = {}  # name -> {"labels", "options"}
        self.pulling: list = []  # background pulls started, by image reference
        self.procs: list = []
        self.calls: list = []
        self.envs: list = []
        self.next = 0
        self.pid = 7000
        self.bystanders = {"f" * 64: "running", "e" * 64: "exited"}
        for cid, state in self.bystanders.items():
            labels = {} if cid[0] == "f" else {POOL_LABEL: "ffffff", SLOT_LABEL: "s01"}
            self.containers[cid] = {"state": state, "labels": labels, "files": {}, "rm": False}

    def answer(self, code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(["docker"], code, out, err)

    def tokenless(self, name: str, env: dict) -> None:
        """No docker call gets a GitHub token: the tests' Docker has two in its environ."""
        assert not set(runners.TOKEN_ENV) & set(env), f"docker {name} got a GitHub token"
        assert "TOKEN-" not in str(env), f"docker {name} got a GitHub token"
        _TOKENLESS.add(name)

    def run(self, cmd, **kwargs) -> subprocess.CompletedProcess:
        args = list(cmd[1:])
        self.calls.append(args)
        self.envs.append(dict(kwargs.get("env") or {}))
        name = " ".join(args[:2]) if args[0] in ("image", "network") else args[0]
        self.tokenless(name, self.envs[-1])
        if name in self.fail:
            return self.answer(1, err=self.fail.pop(name))
        if self.down:
            return self.answer(1, err="Cannot connect to the Docker daemon. Is it running?\n")
        if name == "info":
            return self.answer(0, json.dumps(self.info))
        if name == "image inspect":
            if args[-1].rsplit(":", 1)[-1] in self.tags:
                return self.answer(0, "sha256:abc\n")
            return self.answer(1, err=f"Error: No such image: {args[-1]}\n")
        if name == "image ls":
            return self.answer(0, "".join(f"{tag}\n" for tag in sorted(self.tags)) + "<none>\n")
        if name == "pull":
            self.tags.add(args[-1].rsplit(":", 1)[-1])
            return self.answer(0, args[-1] + "\n")
        if name == "network inspect":
            net = self.networks.get(args[-1])
            if net is None:
                return self.answer(1, err=f"Error: No such network: {args[-1]}\n")
            return self.answer(0, net["options"].get(ICC, "<no value>") + "\n")
        if name == "network rm":
            if self.networks.pop(args[-1], None) is None:
                return self.answer(1, err=f"Error: No such network: {args[-1]}\n")
            return self.answer(0, args[-1] + "\n")
        if name == "run":  # `docker run --rm --network none IMAGE ./config.sh --version`
            assert args[1:4] == ["--rm", "--network", "none"], args
            ref = args[4]
            if ref.rsplit(":", 1)[-1] not in self.tags:
                return self.answer(125, err=f"Unable to find image '{ref}' locally\n")
            said = self.versions.get(ref, ref.rsplit(":", 1)[-1])
            return self.answer(0, f"[RUNNER INFO Terminal] WRITE LINE: {said}\n{said}\n")
        if name == "network create":
            pairs = [args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "--label"]
            opts = [args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "--opt"]
            name = args[-1]
            self.networks[name] = {"labels": dict(pairs), "options": dict(opts)}
            return self.answer(0, "net-id\n")
        if name == "create":
            return self.create(args, dict(kwargs.get("env") or {}))
        if name == "cp":
            cid, dest = args[2].split(":", 1)
            box = self.containers.get(cid)
            if box is None:
                return self.answer(1, err=f"Error: No such container: {cid}\n")
            box["files"][dest] = Path(args[1]).read_text(encoding="utf-8")
            return self.answer(0)
        if name == "ps":
            return self.ps(args)
        if name == "inspect":
            box = self.containers.get(args[-1])
            if box is None:
                return self.answer(1, err=f"Error: No such object: {args[-1]}\n")
            return self.answer(0, box["state"] + "\n")
        if name == "stop":
            for cid in [arg for arg in args[1:] if ID_RE.match(arg)]:
                self.exit(cid, 143)
            return self.answer(0)
        if name == "rm":
            gone = [cid for cid in args[2:] if self.containers.pop(cid, None) is None]
            if gone:
                return self.answer(
                    1, err=f"Error response from daemon: No such container: {gone[0]}\n"
                )
            return self.answer(0)
        return self.answer(1, err=f"unknown command {name}\n")

    def create(self, args: list, env: dict) -> subprocess.CompletedProcess:
        self.next += 1
        cid = hashlib.sha256(str(self.next).encode()).hexdigest()
        labels = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "--label")
        self.containers[cid] = {
            "state": "created",
            "labels": labels,
            "args": args,
            "env": env,
            "files": {},
            "rm": "--rm" in args,
        }
        return self.answer(0, cid + "\n")

    def ps(self, args: list) -> subprocess.CompletedProcess:
        key, value = args[args.index("--filter") + 1].split("=", 1)[1].split("=", 1)
        rows = []
        for cid, box in self.containers.items():
            labels = box["labels"]
            if labels.get(key) == value:
                row = [cid, box["state"], labels.get(SLOT_LABEL, ""), labels.get(LOG_LABEL, "")]
                rows.append("\t".join(row) + "\n")
        return self.answer(0, "".join(rows))

    def popen(self, cmd, **kwargs) -> _Proc:
        assert kwargs.get("start_new_session"), "docker runs in its own session"
        self.calls.append(list(cmd[1:]))
        self.envs.append(dict(kwargs.get("env") or {}))
        self.tokenless(cmd[1], self.envs[-1])
        self.pid += 1
        if cmd[1] == "pull":
            self.pulling.append(cmd[-1])
            self.procs.append(_Proc(self, self.pid, ref=cmd[-1], err=kwargs["stderr"]))
            return self.procs[-1]
        assert cmd[1:3] == ["start", "--attach"], cmd
        if self.attach_error:
            raise OSError("no such file")
        cid = cmd[3]
        box = self.containers[cid]
        if self.start_error:
            kwargs["stdout"].write(f"{self.start_error}\n".encode())
            box["failed"] = True
        elif self.unguarded:  # its line is written, and it exits only at the next poll
            kwargs["stdout"].write(
                b"amplifactory guard: 192.168.65.254 port 9 is still reachable\n"
            )
            box["state"] = "running"
            box["polls"] = 1
        elif self.quits:
            kwargs["stdout"].write(f"{GUARD_READY}192.168.65.0/24\n".encode())
            kwargs["stdout"].write(b"Exiting runner...\n")
            self.exit(cid, 1)
        elif not self.slow_start:
            kwargs["stdout"].write(f"{GUARD_READY}192.168.65.0/24\n".encode())
            box["state"] = "running"
        self.procs.append(_Proc(self, self.pid, cid=cid))
        return self.procs[-1]

    def finish_pull(self, ref: str, ok: bool) -> None:
        proc = next(p for p in reversed(self.procs) if p.ref == ref)
        if ok:
            self.tags.add(ref.rsplit(":", 1)[-1])
        else:
            proc.err.write(b"Error response from daemon: manifest unknown\n")
        proc.code = 0 if ok else 1

    def exit(self, cid: str, code: int) -> None:
        """The container stops with `code`; with --rm, Docker removes it."""
        box = self.containers.get(cid)
        if box is None:
            return
        self.codes[cid] = code
        if box["rm"]:
            del self.containers[cid]
        else:
            box["state"] = "exited"

    def running(self) -> list:
        return [
            cid
            for cid, box in self.containers.items()
            if box["state"] == "running" and cid not in self.bystanders
        ]

    def untouched(self) -> bool:
        """Are the unlabelled container and the other pool's still as they were?"""
        return all(
            self.containers.get(cid, {}).get("state") == state
            for cid, state in self.bystanders.items()
        )


class _HostLauncher:
    """The pool's own launcher, for the self-test: processes the test puts in slots."""

    def __init__(self) -> None:
        self.groups: dict = {}  # slot -> set of process groups
        self.started: list = []
        self.stopped: list = []
        self.codes: dict = {}

    def start(self, slot: Path, env: dict, log_path: Path) -> int:
        self.started.append(slot.name)
        self.groups[slot.name] = {4242}
        return 4242

    def scan(self) -> dict:
        return {slot: set(groups) for slot, groups in self.groups.items()}

    def reap(self) -> dict:
        done, self.codes = self.codes, {}
        return done

    def stop(self, groups, grace: float = 0.0) -> None:
        self.stopped.append(set(groups))
        for slot, held in list(self.groups.items()):
            if held & set(groups):
                del self.groups[slot]


def _home(root: Path, **cfg):
    home = runners.Home(root)
    runners.update_config(home, lambda c: c.update(slots=2, mode="container", **cfg))
    home.base.mkdir(parents=True, exist_ok=True)
    (home.base / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (home.base / runners.VERSION_FILE).write_text(VERSION + "\n", encoding="utf-8")
    return home


def _docker(fake: FakeDocker, clock=None) -> Docker:
    environ = {"PATH": "/usr/bin", "HOME": "/h", "GH_TOKEN": "TOKEN-x", "GITHUB_TOKEN": "TOKEN-y"}
    return Docker("/usr/local/bin/docker", fake.run, fake.popen, environ, clock or _Clock())


def _launcher(home, fake: FakeDocker, clock=None):
    clock = clock or _Clock()
    docker = _docker(fake, clock)
    return ContainerLauncher(home, _HostLauncher(), docker, fake.popen, clock, clock.sleep)


def _runner_files(home, slot_name: str = "s01", name: str = "factory-abc123-77-0001"):
    """What Pool.launch() makes before it calls start(): a slot, the hook and the environment."""
    slot = home.slots / slot_name
    slot.mkdir(parents=True, exist_ok=True)
    hook = home.hooks / f"{name}.sh"
    runners.write_hook(hook, name, "alice/app", 77)
    env = runners.runner_env({"PATH": "/usr/bin"}, slot, "JITSECRET77", hook, home.cache)
    return slot, env, home.logs / "alice" / "app" / "77" / name / "runner.log"


def _fails(fn, *args) -> str:
    try:
        fn(*args)
    except runners.PoolError as exc:
        return str(exc)
    raise AssertionError(f"{fn.__name__} did not fail")


def _test_settings() -> None:
    assert settings({}) == {"mode": "host", "container_cpus": 2, "container_memory_gb": 2}
    mine = {"mode": "container", "container_cpus": 4, "container_memory_gb": 8}
    assert settings(mine) == mine
    for bad in ({"mode": "vm"}, {"container_cpus": 0}, {"container_memory_gb": True}):
        assert "config:" in _fails(settings, bad), bad


def _test_fit() -> None:
    small = fit(INFO, 8, 2, 2)
    assert len(small) == 1 and "7.7 GB" in small[0] and "16 GB" in small[0], small
    assert "Settings → Resources → Advanced" in small[0] and "Apply & restart" in small[0]
    big = dict(INFO, MemTotal=int(15.6 * GIB))  # its "16 GB" setting
    assert fit(big, 8, 2, 2) == []
    assert fit(big, 3, 2, 2) == [] and fit(INFO, 3, 2, 2) == []
    few = fit(dict(big, NCPU=1), 8, 2, 2)
    assert len(few) == 1 and "1 CPUs" in few[0], few
    other = fit(dict(INFO, OperatingSystem="Ubuntu 24.04", OSType="windows"), 8, 2, 2)
    assert "not Linux" in other[0] and "your Docker VM" in other[1], other


def _test_docker() -> None:
    fake = FakeDocker()
    docker = _docker(fake)
    info = docker.info()
    assert info["NCPU"] == 16 and info["OperatingSystem"] == "Docker Desktop", info
    fake.down = True
    assert "Docker isn't running" in _fails(docker.info)
    assert "no docker CLI" in _fails(Docker("", run=fake.run, environ={"PATH": ""}).info)

    def slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    assert "timed out" in _fails(Docker("/d", run=slow, environ={}).info)


def _test_image() -> None:
    clock = _Clock()
    fake = FakeDocker()
    docker = _docker(fake, clock)
    here, new, newer = (f"{IMAGE}:{v}" for v in (VERSION, "2.339.0", "2.340.1"))
    assert docker.image(VERSION) == here and docker.image(VERSION) == here
    assert sum(call[:2] == ["image", "inspect"] for call in fake.calls) == 1, "remembered"
    # a new release: pulled in the background, the newest image here runs meanwhile
    assert docker.image("2.339.0") == here and docker.image("2.339.0") == here
    assert fake.pulling == [new], "one pull, not one per start"
    fake.finish_pull(new, ok=True)
    assert docker.image("2.339.0") == new
    # a failed pull is told once and tried again after PULL_RETRY_S, not before
    assert docker.image("2.340.1") == new
    fake.finish_pull(newer, ok=False)
    assert docker.image("2.340.1") == new and fake.pulling == [new, newer]
    clock.t += PULL_RETRY_S + 1
    assert docker.image("2.340.1") == new and fake.pulling == [new, newer, newer]
    # nothing here: the start fails until the pull lands, and the pull isn't started twice
    empty = FakeDocker(tags=())
    first = _docker(empty)
    assert "pulling" in _fails(first.image, "2.1.0") and "pulling" in _fails(first.image, "2.1.0")
    assert empty.pulling == [f"{IMAGE}:2.1.0"]
    assert first.pull_now("2.2.0") == f"{IMAGE}:2.2.0" and first.present(f"{IMAGE}:2.2.0")
    assert "runners.py base --install" in _fails(docker.image, None)


def _test_network() -> None:
    fake = FakeDocker()
    docker = _docker(fake)
    assert docker.network("abc123") == "amplifactory-abc123"
    made = fake.networks["amplifactory-abc123"]
    assert made == {"labels": {POOL_LABEL: "abc123"}, "options": {ICC: "false"}}, made
    calls = len(fake.calls)
    assert docker.network("abc123") == "amplifactory-abc123" and len(fake.calls) == calls
    fake.networks["amplifactory-def456"] = {"labels": {}, "options": {}}
    assert "reach each other" in _fails(docker.network, "def456")
    assert docker.version_of(f"{IMAGE}:{VERSION}") == VERSION
    fake.versions[f"{IMAGE}:{VERSION}"] = "2.300.0"
    assert docker.version_of(f"{IMAGE}:{VERSION}") == "2.300.0"


def _test_start(root: Path) -> None:
    home = _home(root / "start", container_cpus=3, container_memory_gb=4)
    fake = FakeDocker()
    launcher = _launcher(home, fake)
    slot, env, log_path = _runner_files(home)
    pid = launcher.start(slot, env, log_path)
    assert pid == fake.pid and log_path.is_file()
    (cid,) = fake.running()
    box = fake.containers[cid]
    pool_id = runners.load_config(home)["pool_id"]
    assert box["args"] == [
        "create",
        "--rm",
        "--pull",
        "never",
        "--name",
        "factory-abc123-77-0001",
        "--label",
        f"{POOL_LABEL}={pool_id}",
        "--label",
        f"{SLOT_LABEL}=s01",
        "--label",
        f"{LOG_LABEL}={log_path}",
        "--network",
        f"amplifactory-{pool_id}",
        "--env",
        JIT_ENV,
        "--env",
        f"{HOOK_ENV}={HOOK_PATH}",
        "--env",
        "LANG=C.UTF-8",
        "--cpus",
        "3",
        "--memory",
        "4g",
        "--memory-swap",
        "4g",
        "--pids-limit",
        "4096",
        *VM_LOG,
        "--user",
        "root",
        "--cap-add",
        "NET_ADMIN",
        "--cap-drop",
        "NET_RAW",
        "--sysctl",
        "net.ipv6.conf.all.disable_ipv6=1",
        "--add-host",
        "host.docker.internal:host-gateway",
        f"{IMAGE}:{VERSION}",
        PYTHON,
        GUARD_PATH,
        RUN_SH,
    ], box["args"]
    words = " ".join(" ".join(call) for call in fake.calls).split()
    for banned in ("-v", "--volume", "--mount", "--privileged", "host"):
        assert banned not in words, banned
    added = [words[n + 1] for n, word in enumerate(words) if word == "--cap-add"]
    assert added == ["NET_ADMIN"], added  # for the guard alone, which drops it
    guard = GUARD.read_text(encoding="utf-8")
    assert box["files"][GUARD_PATH] == guard, "the guard is copied in before the start"
    assert not [word for word in words if "docker.sock" in word or "JITSECRET" in word]
    assert box["env"][JIT_ENV] == "JITSECRET77", "docker create must get the JIT config"
    for call, call_env in zip(fake.calls, fake.envs):
        assert (JIT_ENV in call_env) == (call[0] == "create"), "only create gets the JIT config"
    assert box["files"][HOOK_PATH] == Path(env[HOOK_ENV]).read_text(encoding="utf-8")
    assert 'if [ "$GITHUB_RUN_ID" = "77" ]' in box["files"][HOOK_PATH]
    attach = ["start", "--attach", cid]
    assert fake.calls[-1] == attach, fake.calls[-1:]  # started once the guard said so
    assert launcher.host.started == []

    # each failure after create removes the container; a create that fails leaves none
    def failed_start(reason: str) -> None:
        before = set(fake.containers)
        assert reason in _fails(launcher.start, slot, env, log_path)
        assert set(fake.containers) == before, set(fake.containers) - before

    fake.fail["cp"] = "Error: no space left on device"
    failed_start("no space left")
    fake.fail["create"] = "Error: invalid reference format"
    failed_start("invalid reference format")
    assert f"{IMAGE}:{VERSION}" not in launcher.docker.ready, "a create that fails: checked again"
    fake.attach_error = True
    failed_start("could not start the runner's container: no such file")
    fake.attach_error = False
    fake.start_error = "Error response from daemon: failed to create task for container"
    failed_start("failed to create task")
    fake.start_error = ""
    fake.quits = True  # a runner that quits at once: the same back-off
    failed_start("stopped at once: Exiting runner...")
    fake.quits = False
    # a guard that fails while its container still runs when start() first looks: its line isn't
    # the ready one, so start() waits, and fails when the container stops
    fake.unguarded = True
    failed_start("stopped at once: amplifactory guard: 192.168.65.254 port 9 is still reachable")
    fake.unguarded = False
    calls = len(fake.calls)
    launcher.guard = root / "no-guard.py"  # no guard.py: no container at all
    assert "needs guard.py" in _fails(launcher.start, slot, env, log_path)
    launcher.guard = GUARD
    assert len(fake.calls) == calls
    assert "tab or a newline" in _fails(launcher.start, slot, env, home.logs / "a\tb" / "r.log")
    no_jit = dict(env, **{JIT_ENV: ""})
    assert "JIT config" in _fails(launcher.start, slot, no_jit, log_path)
    assert fake.running() == [cid] and fake.untouched()

    # a mode it doesn't know fails each start, before any docker call: never a runner on the Mac
    calls = len(fake.calls)
    runners.update_config(home, lambda cfg: cfg.update(mode="Container"))
    assert "mode must be" in _fails(launcher.start, slot, env, log_path)
    assert launcher.host.started == [] and len(fake.calls) == calls

    # in host mode, the runner starts on the Mac
    runners.update_config(home, lambda cfg: cfg.update(mode="host"))
    assert launcher.start(slot, env, log_path) == 4242 and launcher.host.started == ["s01"]
    assert fake.running() == [cid]


def _test_scan(root: Path) -> None:
    clock = _Clock(100.0)
    home = _home(root / "scan")
    fake = FakeDocker()
    launcher = _launcher(home, fake, clock)
    assert launcher.scan() == {} and fake.calls[-1][0] == "ps"
    calls = len(fake.calls)
    assert launcher.scan() == {} and len(fake.calls) == calls, "nothing tracked: no docker call"

    slot, env, log_path = _runner_files(home)
    launcher.start(slot, env, log_path)
    (cid,) = fake.running()
    launcher.host.groups["s07"] = {555}  # a runner started on the Mac before the switch
    assert launcher.scan() == {"s01": {cid}, "s07": {555}}

    # this pool's leftovers go (exited, or created by another process); others are not ours
    pool_id = runners.load_config(home)["pool_id"]
    mine = {POOL_LABEL: pool_id, SLOT_LABEL: "s03", LOG_LABEL: ""}
    fake.containers["a" * 64] = {"state": "exited", "labels": mine, "files": {}, "rm": False}
    fake.containers["b" * 64] = {"state": "created", "labels": mine, "files": {}, "rm": True}
    assert launcher.scan() == {"s01": {cid}, "s07": {555}}
    assert "a" * 64 not in fake.containers and "b" * 64 not in fake.containers
    assert fake.untouched()

    # a slot holds one kind: a live container is its runner, and a process on the Mac that names
    # the slot (a `tail -f` in a terminal) is left out, never signalled, and the container kept
    in_held = dict(mine, **{SLOT_LABEL: "s07"})
    fake.containers["c" * 64] = {"state": "running", "labels": in_held, "files": {}, "rm": True}
    assert launcher.scan() == {"s01": {cid}, "s07": {"c" * 64}} and "c" * 64 in fake.containers
    assert launcher.host.stopped == [] and "c" * 64 in {key[0] for key in launcher.told}
    del fake.containers["c" * 64]
    # ...but a flooded one is removed, and the slot keeps what runs there: never an empty set
    flood_log = home.logs / "flood.log"
    with open(flood_log, "ab") as fh:
        fh.truncate(LOG_CAP + 1)
    flooded = dict(in_held, **{LOG_LABEL: str(flood_log)})
    fake.containers["8" * 64] = {"state": "running", "labels": flooded, "files": {}, "rm": True}
    assert launcher.scan() == {"s01": {cid}, "s07": {555}} and "8" * 64 not in fake.containers
    # a container told about once is still told why it's removed: a flood
    fake.containers["c" * 64] = {"state": "running", "labels": flooded, "files": {}, "rm": True}
    seen = _Capture()
    log.addHandler(seen)
    try:
        assert launcher.scan() == {"s01": {cid}, "s07": {555}} and "c" * 64 not in fake.containers
    finally:
        log.removeHandler(seen)
    flood = f"removing container {'c' * 12}: its runner.log passed {LOG_CAP >> 20} MB"
    assert seen.lines == [flood], seen.lines

    # one Docker is still removing is told once, and looked at again by the next scan
    fake.containers["9" * 64] = {"state": "removing", "labels": mine, "files": {}, "rm": True}
    launcher.scan()
    assert launcher.scan() == {"s01": {cid}, "s07": {555}} and "9" * 64 in launcher.tracked
    assert "9" * 64 in {key[0] for key in launcher.told}, "logged once"
    del fake.containers["9" * 64]

    # a removal that fails doesn't stop the pass, and the next scan tries again
    fake.containers["d" * 64] = {"state": "exited", "labels": mine, "files": {}, "rm": False}
    fake.fail["rm"] = "Error response from daemon: removal of container is already in progress"
    assert launcher.scan() == {"s01": {cid}, "s07": {555}} and "d" * 64 in fake.containers
    assert launcher.scan() == {"s01": {cid}, "s07": {555}} and "d" * 64 not in fake.containers

    # a container still starting after RUNNING_S counts while its `docker start` runs
    fake.slow_start = True
    old_log = home.logs / "slow.log"  # a runner name used again: its old log has the line
    old_log.write_text(f"{GUARD_READY}192.168.65.0/24\nExiting runner...\n", encoding="utf-8")
    launcher.start(home.slots / "s04", env, old_log)
    assert clock() >= 100.0 + RUNNING_S, "start() waited for this start's own line"
    (slow,) = [c for c, box in fake.containers.items() if box["state"] == "created"]
    assert launcher.scan() == {"s01": {cid}, "s07": {555}, "s04": {slow}}
    fake.containers[slow]["failed"] = True  # then `docker start` gives up
    assert launcher.scan() == {"s01": {cid}, "s07": {555}} and slow not in fake.containers
    fake.slow_start = False

    # a flood: the container whose log passed the cap is removed and left out
    with open(log_path, "ab") as fh:
        fh.truncate(LOG_CAP + 1)
    assert launcher.scan() == {"s07": {555}} and cid not in fake.containers
    log_path.write_bytes(b"")

    # Docker down: the scan raises, so the pool cleans nothing up
    launcher.start(slot, env, log_path)
    fake.down = True
    assert "Cannot connect" in _fails(launcher.scan)
    fake.down = False
    assert fake.untouched()


def _test_stop_reap(root: Path) -> None:
    """Stops: by ID, best effort; one that fails says so, and one that works or finds a container
    gone says nothing."""
    seen = _Capture()
    log.addHandler(seen)
    try:
        _stop_reap(root, seen)
    finally:
        log.removeHandler(seen)


def _stop_reap(root: Path, seen: _Capture) -> None:
    home = _home(root / "stop")
    fake = FakeDocker()
    launcher = _launcher(home, fake)
    slot, env, log_path = _runner_files(home)
    pid = launcher.start(slot, env, log_path)
    other_slot, other_env, other_log = _runner_files(home, "s02", "factory-abc123-77-0002")
    pid2 = launcher.start(other_slot, other_env, other_log)
    cid, cid2 = fake.running()
    launcher.host.groups["s09"] = {808}
    assert launcher.reap() == {}
    launcher.stop({cid, cid2, 808, "factory-abc123-77-0001"}, grace=10.0)
    assert ["stop", "-t", "10", cid, cid2] in fake.calls, "one stop for all, by ID only"
    assert ["rm", "--force", cid, cid2] in fake.calls
    assert launcher.host.stopped == [{808}] and fake.running() == [] and fake.untouched()
    assert launcher.reap() == {pid: 143, pid2: 143}
    assert launcher.scan() == {} and launcher.tracked == set()

    pid = launcher.start(slot, env, log_path)
    (cid,) = fake.running()
    fake.exit(cid, 0)  # the runner took its one job and exited; --rm removed it
    assert launcher.reap() == {pid: 0} and launcher.scan() == {}

    launcher.start(slot, env, log_path)
    (cid,) = fake.running()
    fake.down = True
    launcher.stop({cid}, grace=1.0)  # best effort: no exception, but it says so
    fake.down = False
    assert launcher.scan() == {"s01": {cid}}, "a container a stop missed is still seen"
    # a failed stop is named; a container `--rm` already took, or is taking, is not a failure
    denied = "Error response from daemon: cannot stop container: denied"
    fake.fail["stop"] = denied
    fake.fail["rm"] = f"Error response from daemon: No such container: {cid}"
    launcher.stop({cid}, grace=1.0)
    busy = f"Error response from daemon: removal of container {cid} is already in progress"
    fake.fail["rm"] = busy
    launcher.stop({cid}, grace=1.0)
    assert fake.running() == [], "the last stop worked"
    down = "Cannot connect to the Docker daemon. Is it running?"
    assert [line for line in seen.lines if "failed for containers" in line] == [
        f"docker stop failed for containers ['{cid[:12]}']: {down}",
        f"docker rm failed for containers ['{cid[:12]}']: {down}",
        f"docker stop failed for containers ['{cid[:12]}']: {denied}",
    ], seen.lines


def _test_pool(root: Path) -> None:
    """A real runners.Pool, with the fake GitHub and the fake docker: one job, start to ledger."""
    clock = _Clock(10000.0)
    home = _home(root / "pool")
    runners.repo_add(home, "alice/app", None)
    github = runners.FakeGitHub({"alice/app": "alice"})
    github.add_run("alice/app", 1, "2026-01-01T00:00:00Z", jobs=1)
    fake = FakeDocker()
    launcher = _launcher(home, fake, clock)
    pool = runners._pool(home, github, clock, launcher)
    pool.startup()
    assert pool.pass_once()["launcher"] == "container", "the heartbeat says where runners run"
    (cid,) = fake.running()
    (r,) = pool.live()
    assert fake.containers[cid]["labels"][SLOT_LABEL] == r.slot and r.pid == fake.pid
    made = fake.containers[cid]["args"]
    assert made[made.index("--name") + 1] == r.name
    github.take(1, 0, r.name)
    pool.pass_once()
    assert pool.runners[r.name].job_id == 10
    github.complete(1, 0, "success")
    fake.exit(cid, 0)
    pool.pass_once()
    lines = home.jobs.read_text(encoding="utf-8").splitlines()
    (line,) = [json.loads(text) for text in lines]
    assert line["conclusion"] == "success" and line["exit_code"] == 0, line
    assert not pool.runners and not (home.slots / r.slot).exists()
    assert fake.running() == [] and fake.untouched()


def _self_test() -> None:
    import guard  # next to this file: its self-test runs with this one's, in CI too

    assert guard.READY == GUARD_READY, "the line start() waits for"
    guard._self_test()
    _TOKENLESS.clear()  # only this module's tests count: setup.py's run tokenless Dockers first
    capture = _Capture()
    level, propagate = log.level, log.propagate
    log.addHandler(capture)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    saved = dict(runners._MACHINE)
    runners._MACHINE.update(cores=6, memory_gb=36, slots=2)  # no test reads this Mac's sysctl
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            _test_settings()
            _test_fit()
            _test_docker()
            _test_image()
            _test_network()
            _test_start(root)
            _test_scan(root)
            _test_stop_reap(root)
            _test_pool(root)
        missed = {"create", "cp", "start", "pull", "info"} - _TOKENLESS
        assert not missed, f"no docker {sorted(missed)} call was checked for a token"
        leaked = [line for line in capture.lines if "TOKEN-" in line or "JITSECRET" in line]
        assert not leaked, leaked
    finally:
        runners._MACHINE.clear()
        runners._MACHINE.update(saved)
        log.removeHandler(capture)
        log.setLevel(level)
        log.propagate = propagate
    print("containers.py self-test: ok")


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true", help="run offline checks and exit")
    args = parser.parse_args(argv)
    if args.self_test:
        _self_test()
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

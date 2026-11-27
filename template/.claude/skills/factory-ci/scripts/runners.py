"""Factory CI runner pool: CI jobs from every factory repo on this Mac, each on its own just-in-time
runner, never more than `slots` at once.

The routing contract (stable across template versions): when a repo's variable FACTORY_RUNNER is
`local`, its CI jobs ask for `runs-on: [self-hosted, factory-run-<run_id>]`; fork PRs never do. For
each such queued job the pool may start, it creates a just-in-time (JIT) runner labelled
`self-hosted` and that run's label, in a fresh APFS clone of a base `actions-runner` install, so the
pool decides which run goes next. Labels alone don't isolate runs (a job asking only for
`self-hosted` matches any pool runner), so each runner also gets a job-started hook that fails any
job from another run before its first step. Each runner takes one job; then its slot is deleted and
its `_diag` logs are kept. Runs from forks, and `pull_request_target` runs, never get a runner.

Fair share (see allocate): each free slot goes to the repo with the fewest live runners per unit of
its weight (weighted max-min; a running job is never stopped), then, in that repo, to a PR under the
per-PR cap before one over it, then by the PR's P label (P2 without one; a job moves up a class per
`aging_s` it waits), pushes to the default branch, the PR with the fewest live runners, and age.

A good citizen on the Mac: runners start under `taskpolicy -c utility`; `caffeinate -i` keeps the
Mac from idle sleep while any runner is alive; no runner starts while memory pressure is critical,
and with `battery: half` only half the slots run on battery. By default `slots` leaves 4
performance cores and 16 GB of RAM to you, at 2 GB per slot. Jobs share uv's cache and Pythons and
the runner tool cache (each slot gets a copy-on-write clone of it; finished tools are merged back).

Operating it: `status` shows whether the pool is up, paused or down (exit 0, 5 or 3), its slots, and
the runners and queued jobs by repo, then PR (with the issue a factory branch names), from the pool's
files only, except that a pool that's down in container mode also asks Docker (`docker info`, up to
10 s) and says when Docker doesn't answer. `pause` stops new runners until `resume`; running jobs finish. `stats` gives wait and
run-time percentiles and how busy the pool was, from jobs.jsonl; `logs` shows pool.log's tail or a
run's runner logs. Under a service (stderr not a terminal), only warnings and errors go to stderr.

Pool home: ~/.amplifactory/runner-pool/ ($FACTORY_RUNNER_POOL_HOME or --home override it):
    config.json     slots, timeouts, fair-share settings, the battery policy, and the repos served,
                    each with its gh account and optional weight and cap
    base/           the runner install that slots are cloned from (`base --install`)
    slots/sNN/      one clone per live runner, deleted when it exits
    hooks/          each live runner's job-started hook
    cache/          the caches jobs share: uv/, uv-python/ and tool/ (the runner tool cache, which
                    keeps each tool's last 3 versions)
    logs/           pool.log; <owner>/<repo>/<run>/<runner>/ with runner.log and _diag/
    jobs.jsonl      one line per finished runner: repo, run, job, PR, times, slot, conclusion
    state.json      the live runners, so a restarted pool adopts them
    heartbeat.json  written every pass: pid, time, slots, live and busy runners, the jobs queued
                    without a runner yet (`queue`, `queued`), the pause, why the Mac admits fewer
                    slots (`held`), where its runners run (`launcher`), and each repo's last error
    paused.json     there while the pool is paused (`pause`): since when, and why

    python3 .claude/skills/factory-ci/scripts/runners.py repo add OWNER/REPO [--account LOGIN]
                                                         [--weight W] [--cap N]
    python3 .claude/skills/factory-ci/scripts/runners.py repo remove OWNER/REPO
    python3 .claude/skills/factory-ci/scripts/runners.py repo list
    python3 .claude/skills/factory-ci/scripts/runners.py config [--slots N] [--idle-timeout S]
                                                         [--pr-cap N] [--aging S]
                                                         [--battery full|half]
    python3 .claude/skills/factory-ci/scripts/runners.py base [--install]
    python3 .claude/skills/factory-ci/scripts/runners.py serve [--once]
    python3 .claude/skills/factory-ci/scripts/runners.py status [--json]
    python3 .claude/skills/factory-ci/scripts/runners.py pause [--reason TEXT]
    python3 .claude/skills/factory-ci/scripts/runners.py resume
    python3 .claude/skills/factory-ci/scripts/runners.py stats [--days N] [--json]
    python3 .claude/skills/factory-ci/scripts/runners.py logs [-n N] [--run RUN_ID]
    python3 .claude/skills/factory-ci/scripts/runners.py --self-test

Tokens come from `gh auth token --hostname github.com --user <account>` (by default the repo's
owner) and stay in memory, never on disk or in a log; the JIT config reaches its runner only through
the environment (ACTIONS_RUNNER_INPUT_JITCONFIG). Jobs run as you, directly on the Mac, with their
own HOME and a scrubbed environment: that keeps your gh, git and Copilot logins out of reach by
accident, but it is not a security boundary. In container mode (config.json's `"mode": "container"`,
set by `setup.py container on`), containers.py runs each runner in its own Linux container instead.
Stdlib only, Python 3.9+.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import logging
import logging.handlers
import math
import os
import platform
import random
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

POOL_VERSION = "16"  # bumped when serve's behaviour changes; the heartbeat reports it
SCHEMA = 1  # config.json schema
API = "https://api.github.com"
HOME_ENV = "FACTORY_RUNNER_POOL_HOME"
DEFAULT_HOME = "~/.amplifactory/runner-pool"
RUN_LABEL = "factory-run-"
RELEASE_PATH = "/repos/actions/runner/releases/latest"
DOWNLOAD_PREFIX = "https://github.com/actions/runner/releases/download/"
VERSION_FILE = ".factory-runner-version"
# config.json settings: (default, lowest, highest)
SETTINGS = {
    "slots": (2, 1, 64),
    "idle_timeout_s": (600, 60, 86400),
    "poll_fast_s": (10, 2, 600),
    "poll_idle_s": (30, 5, 3600),
    "pr_cap": (4, 1, 64),  # a PR at this many live runners yields to PRs below it
    "aging_s": (600, 60, 86400),  # a queued job moves up one P class per this many seconds
}
# per-repo settings in config.json's repos table: (lowest, highest); weight defaults to 1, cap
# to none
REPO_SETTINGS = {"weight": (1, 100), "cap": (1, 64)}
PRIORITY_RE = re.compile(r"^P([0-3])$")
DEFAULT_RANK = 2  # a run without a PR, or a PR without a P label, counts as P2, as in the factory
BATTERY = ("full", "half")  # config.json's battery policy: every slot on battery, or half
GIB = 1 << 30
SYSCTL = "/usr/sbin/sysctl"
PMSET = "/usr/bin/pmset"
TASKPOLICY = "/usr/sbin/taskpolicy"
CAFFEINATE = "/usr/bin/caffeinate"
CRITICAL = 4  # kern.memorystatus_vm_pressure_level: 1 normal, 2 warning, 4 critical
DOCKER_ASK_S = 10  # how long `status` waits for Docker, for a pool in container mode that is down
DOCKER_DOWN = ("Docker isn't running", "docker info timed out")  # containers.Docker: no answer
HEARTBEAT_FRESH_S = 120  # an older heartbeat means the pool is down (as setup.py's doctor)
STATUS_UP, STATUS_DOWN, STATUS_PAUSED = 0, 3, 5  # `status` exit codes
ISSUE_BRANCH_RE = re.compile(r"^(?:local|factory)/issue-(\d+)$")
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9._-]+$")  # EMU owners have _
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
POOL_ID_RE = re.compile(r"^[0-9a-f]{6}$")
# the only variables a runner inherits; tokens, SSH_AUTH_SOCK and the rest stay out of jobs
ENV_KEEP = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "USER", "LOGNAME", "SHELL", "TZ")
TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
JIT_SPACING_S = 1.0  # between runner registrations, so GitHub doesn't see them as spam
STOP_GRACE_S = 10.0
FINISH_TRIES = 12  # successful lookups of an exited runner's job before it is written as unknown
FINISH_GIVE_UP_S = 86400
BACKOFF_MIN_S = 30
BACKOFF_MAX_S = 900
TOKEN_TTL_S = 600
CACHE_KEEP_S = 3600  # a cached reply nobody asked for in this long is forgotten
KEEPALIVE_S = 15  # during a long pass, or the wait after one, the heartbeat is rewritten this often
UNLISTED_S = 60  # how often the newest runs are read unfiltered, for runs the status lists miss
UNLISTED_PAGE = 30  # how many of the newest runs that read covers
BASE_CHECK_S = 86400
PRUNE_S = 86400  # the shared caches are pruned at most this often, while no runner is alive
TOOL_KEEP = 3  # versions of each tool the shared tool cache keeps: the ones added last
MERGE_TMP_RE = re.compile(r"^\..+\.\d+\.tmp$")  # merge_tools' temp folders: .<name>.<pid>.tmp
UV_PRUNE_TIMEOUT_S = 120  # a `uv cache prune` still running then is stopped, with its session
UV_LOCK_S = 30  # how long that prune waits for the uv cache's lock (UV_LOCK_TIMEOUT)
UV_VERSION_RE = re.compile(r"uv (\d+)\.(\d+)\.(\d+)")  # how `uv --version` starts
BASE_RETRY_S = 600
GONE = -999  # exit code of a runner that wasn't this process's child, so its code is unknown
_UTC = timezone(timedelta(0))

log = logging.getLogger("factory-ci.pool")


class PoolError(Exception):
    """Something the maintainer has to fix; printed as `error: ...` with exit code 1."""


class GitHubError(Exception):
    """A failed API call. Its text has the method, path, status and GitHub's message, never a
    token."""

    def __init__(
        self,
        method: str,
        path: str,
        status: int,
        message: str = "",
        retry_at: float | None = None,
        rate: bool = False,
    ) -> None:
        what = f"HTTP {status}" if status else "no reply"
        super().__init__(f"{method} {path}: {what}" + (f" ({message})" if message else ""))
        self.status = status
        self.retry_at = retry_at
        self.rate = rate


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, _UTC).isoformat(timespec="seconds") if ts else None


# ------------------------------------------------------------------------- pool home and config


class Home:
    """Where the pool keeps its files (see the module docstring)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.config = root / "config.json"
        self.state = root / "state.json"
        self.jobs = root / "jobs.jsonl"
        self.heartbeat = root / "heartbeat.json"
        self.base = root / "base"
        self.slots = root / "slots"
        self.hooks = root / "hooks"
        self.logs = root / "logs"
        self.cache = root / "cache"
        self.lock = root / "config.lock"
        self.serve_lock = root / "serve.lock"
        self.paused = root / "paused.json"

    def make(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)


def resolve_home(arg: str | None = None) -> Home:
    raw = arg or os.environ.get(HOME_ENV) or DEFAULT_HOME
    return Home(Path(raw).expanduser().resolve())


def read_json(path: Path, default):
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    try:
        return json.loads(text)
    except ValueError:
        raise PoolError(f"{path} is not valid JSON; fix or delete it") from None


def write_json(path: Path, data) -> None:
    """Replace path atomically: a reader sees the old file or the new one, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load_config(home: Home) -> dict:
    raw = read_json(home.config, {})
    if not isinstance(raw, dict):
        raise PoolError(f"{home.config} must hold a JSON object")
    schema = raw.get("schema", SCHEMA)
    if not isinstance(schema, int) or schema > SCHEMA:
        raise PoolError(
            f"{home.config} has schema {schema!r}, and this runners.py reads up to {SCHEMA}: "
            "update the pool"
        )
    cfg = {key: spec[0] for key, spec in SETTINGS.items()}
    cfg["battery"] = BATTERY[0]
    if "slots" not in raw:  # once saved, the value stays, whatever this Mac would suggest
        cfg["slots"] = machine()["slots"]
    cfg.update(raw)
    cfg["schema"] = SCHEMA
    cfg["repos"] = dict(raw.get("repos") or {})
    check_config(cfg)
    return cfg


def check_config(cfg: dict) -> None:
    for key, (_, low, high) in SETTINGS.items():
        value = cfg.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise PoolError(f"config: {key} must be a whole number from {low} to {high}")
    if cfg.get("battery", BATTERY[0]) not in BATTERY:
        raise PoolError(f"config: battery must be one of {', '.join(BATTERY)}")
    if "pool_id" in cfg and not POOL_ID_RE.match(str(cfg["pool_id"])):
        raise PoolError("config: pool_id must be 6 lowercase hex digits")
    for repo, entry in cfg["repos"].items():
        check_repo(repo)
        if not isinstance(entry, dict) or not ACCOUNT_RE.match(str(entry.get("account", ""))):
            raise PoolError(f"config: repos.{repo} needs an account (a GitHub login)")
        for key, (low, high) in REPO_SETTINGS.items():
            value = entry.get(key)
            bad = isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high
            if key in entry and bad:
                raise PoolError(
                    f"config: repos.{repo}.{key} must be a whole number from {low} to {high}"
                )


def check_repo(repo: str) -> None:
    if not REPO_RE.match(repo or "") or repo.split("/")[1] in (".", ".."):
        raise PoolError(f"not a repository: {repo!r} (want OWNER/REPO)")


def update_config(home: Home, change) -> dict:
    """Load, change and save config.json under a lock, so concurrent edits don't lose each other."""
    home.make()
    with file_lock(home.lock):
        cfg = load_config(home)
        change(cfg)
        cfg.setdefault("pool_id", secrets.token_hex(3))
        check_config(cfg)
        write_json(home.config, cfg)
    return cfg


def repo_add(
    home: Home, repo: str, account: str | None, weight: int | None = None, cap: int | None = None
) -> str:
    """Serve a repo, or change how it's served. A registered repo keeps its account unless one is
    given; `cap` 0 removes its cap."""
    check_repo(repo)
    if account is not None and not ACCOUNT_RE.match(account):
        raise PoolError(f"not a GitHub login: {account!r}")

    def change(cfg: dict) -> None:
        entry = dict(cfg["repos"].get(repo) or {})
        entry["account"] = account or entry.get("account") or repo.split("/", 1)[0]
        if weight is not None:
            entry["weight"] = weight
        if cap == 0:
            entry.pop("cap", None)
        elif cap is not None:
            entry["cap"] = cap
        cfg["repos"][repo] = entry

    entry = update_config(home, change)["repos"][repo]
    return f"{repo}: served by the pool, with the gh login of {entry['account']}" + share(entry)


def share(entry: dict) -> str:
    """A repo's weight and cap, as `repo add` and `repo list` show them."""
    cap = f", at most {entry['cap']} runners" if entry.get("cap") else ""
    return f" (weight {entry.get('weight', 1)}{cap})"


def repo_remove(home: Home, repo: str) -> str:
    if repo not in load_config(home)["repos"]:
        raise PoolError(f"{repo} is not registered")
    update_config(home, lambda cfg: cfg["repos"].pop(repo, None))
    return f"{repo}: removed; runners already started for it finish their jobs"


# --------------------------------------------------------------------------- tokens and the API


class Tokens:
    """`gh auth token` for each account, cached in memory only."""

    def __init__(
        self, gh: str = "gh", run=subprocess.run, clock=time.monotonic, ttl: float = TOKEN_TTL_S
    ) -> None:
        self.gh = gh
        self._run = run
        self._clock = clock
        self._ttl = ttl
        self._cache: dict = {}

    def get(self, account: str) -> str:
        hit = self._cache.get(account)
        now = self._clock()
        if hit and hit[1] > now:
            return hit[0]
        # without GH_TOKEN and friends, gh answers for the named account from its own login store
        env = {key: value for key, value in os.environ.items() if key not in TOKEN_ENV}
        cmd = [self.gh, "auth", "token", "--hostname", "github.com", "--user", account]
        try:
            out = self._run(cmd, capture_output=True, text=True, env=env, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PoolError(f"could not run gh for {account}: {exc.__class__.__name__}") from None
        token = (out.stdout or "").strip()
        if out.returncode != 0 or not token:
            said = (out.stderr or "").strip().splitlines()
            raise PoolError(
                f"no gh login for {account} on github.com: run `gh auth login` as {account}"
                + (f" (gh: {said[0]})" if said else "")
            )
        self._cache[account] = (token, now + self._ttl)
        return token

    def forget(self, account: str) -> None:
        self._cache.pop(account, None)


@dataclass
class Reply:
    status: int
    headers: dict
    body: bytes


def urlopen_reply(request: urllib.request.Request, timeout: float) -> Reply:
    """The real transport; the self-test swaps in a fake GitHub with the same signature."""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            headers = {key.lower(): value for key, value in resp.headers.items()}
            return Reply(resp.status, headers, resp.read())
    except urllib.error.HTTPError as exc:
        headers = {key.lower(): value for key, value in (exc.headers or {}).items()}
        try:
            body = exc.read() or b""
        except (OSError, ValueError):
            body = b""
        return Reply(exc.code, headers, body)


class GitHub:
    """REST calls. GETs can be conditional: a 304 answers from the cache and, authenticated, costs
    no rate limit. A 403/429 rate limit blocks that account until the time GitHub gives."""

    def __init__(self, tokens: Tokens, transport=urlopen_reply, api: str = API, clock=time.time):
        self.tokens = tokens
        self.transport = transport
        self.api = api
        self.clock = clock
        self.cache: dict = {}
        self.used: dict = {}  # cache key -> when it was last asked for
        self.blocked: dict = {}
        self.not_modified = 0
        self.progress = None  # called after each request: the pool keeps its heartbeat fresh

    def call(self, method: str, path: str, account, body=None, params=None, cache: bool = False):
        until = self.blocked.get(account, 0.0)
        if until > self.clock():
            raise GitHubError(method, path, 0, f"rate limited until {iso(until)}", until, True)
        url = self.api + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"amplifactory-runner-pool/{POOL_VERSION}",
        }
        if account:
            headers["Authorization"] = "Bearer " + self.tokens.get(account)
        key = (account, url)
        hit = self.cache.get(key) if cache else None
        if hit:
            headers["If-None-Match"] = hit[0]
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            reply = self.transport(request, 30.0)
        except (OSError, ValueError) as exc:  # URLError and timeouts are OSErrors
            raise GitHubError(method, path, 0, exc.__class__.__name__) from None
        finally:
            if self.progress is not None:
                self.progress()
        if cache:
            self.used[key] = self.clock()
        if reply.status == 304 and hit:
            self.not_modified += 1
            return hit[1]
        if 200 <= reply.status < 300:
            try:
                parsed = json.loads(reply.body) if reply.body else None
            except ValueError:
                raise GitHubError(method, path, reply.status, "the reply is not JSON") from None
            if cache and reply.headers.get("etag"):
                self.cache[key] = (reply.headers["etag"], parsed)
            return parsed
        raise self._error(method, path, account, reply)

    def _error(self, method: str, path: str, account, reply: Reply) -> GitHubError:
        try:
            message = str(json.loads(reply.body).get("message", ""))[:200]
        except (ValueError, AttributeError):
            message = ""
        retry_at = None
        if reply.status in (403, 429):
            after = str(reply.headers.get("retry-after", ""))
            reset = str(reply.headers.get("x-ratelimit-reset", ""))
            if after.isdigit():
                retry_at = self.clock() + int(after)
            elif reply.headers.get("x-ratelimit-remaining") == "0" and reset.isdigit():
                retry_at = float(reset)
            elif "rate limit" in message.lower():  # the secondary limits
                retry_at = self.clock() + 60
        if retry_at is not None:
            self.blocked[account] = retry_at
        if reply.status == 401 and account:
            self.tokens.forget(account)
        return GitHubError(method, path, reply.status, message, retry_at, retry_at is not None)

    def prune(self) -> None:
        """Forget cached replies nobody asked for in CACHE_KEEP_S: runs that are no longer active,
        PRs with nothing queued. A reply asked for less often than every pass (a PR's labels, read
        only when a slot frees up) keeps its ETag meanwhile, so its next read is still conditional."""
        cutoff = self.clock() - CACHE_KEEP_S
        self.used = {key: when for key, when in self.used.items() if when >= cutoff}
        self.cache = {key: hit for key, hit in self.cache.items() if key in self.used}


# ---------------------------------------------------------------------------- the runner install


def runner_platform(system: str | None = None, machine: str | None = None) -> str:
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    os_name = {"Darwin": "osx", "Linux": "linux"}.get(system)
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(machine)
    if not os_name or not arch:
        raise PoolError(f"actions/runner has no build for {system}/{machine}")
    return f"{os_name}-{arch}"


def release_asset(release: dict, plat: str) -> dict:
    """The runner archive for this platform, with the SHA-256 its release publishes. Both places
    GitHub publishes it count (the asset's digest, the release notes' marker); they must agree."""
    tag = str((release or {}).get("tag_name", ""))
    version = tag.removeprefix("v")
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        raise PoolError(f"unexpected actions/runner release tag {tag!r}")
    name = f"actions-runner-{plat}-{version}.tar.gz"
    assets = release.get("assets") or []
    asset = next((item for item in assets if item.get("name") == name), None)
    if asset is None:
        raise PoolError(f"actions/runner {tag} has no {name}")
    url = str(asset.get("browser_download_url", ""))
    if not url.startswith(DOWNLOAD_PREFIX):
        raise PoolError(f"unexpected download URL for {name}: {url}")
    shas = set()
    digest = str(asset.get("digest") or "")
    if digest.startswith("sha256:"):
        shas.add(digest[len("sha256:") :].lower())
    marker = re.escape(plat)
    pattern = rf"<!-- BEGIN SHA {marker} -->\s*([0-9a-fA-F]{{64}})\s*<!-- END SHA {marker} -->"
    found = re.search(pattern, str(release.get("body") or ""))
    if found:
        shas.add(found.group(1).lower())
    if not shas:
        raise PoolError(f"actions/runner {tag} publishes no SHA-256 for {name}; refusing it")
    if len(shas) > 1:
        raise PoolError(
            f"actions/runner {tag} publishes two different SHA-256s for {name}; refusing it"
        )
    return {"version": version, "name": name, "url": url, "sha256": shas.pop()}


def version_tuple(version: str | None) -> tuple:
    try:
        return tuple(int(part) for part in (version or "").split("."))
    except ValueError:
        return ()


def base_version(home: Home) -> str | None:
    try:
        return (home.base / VERSION_FILE).read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def download(url: str, dest: Path, opener=urllib.request.urlopen, progress=None) -> str:
    """Stream url into dest; return its SHA-256. `progress` is called after each MiB: the pool's
    keepalive, since the runner is over 100 MiB."""
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "amplifactory-runner-pool"})
    with opener(request, timeout=120) as resp, open(dest, "wb") as out:
        for chunk in iter(lambda: resp.read(1 << 20), b""):
            digest.update(chunk)
            out.write(chunk)
            if progress is not None:
                progress()
    return digest.hexdigest()


def install_base(
    home: Home, asset: dict, opener=urllib.request.urlopen, run=subprocess.run, progress=None
):
    """Download, verify and unpack the runner, then swap it in as base/. On any failure the old
    base stays as it was. `progress` is called during the download and after unpacking."""
    home.make()
    downloads = home.root / "downloads"
    downloads.mkdir(exist_ok=True)
    part = downloads / (asset["name"] + ".part")
    try:
        got = download(asset["url"], part, opener, progress)
    except (OSError, ValueError) as exc:
        part.unlink(missing_ok=True)
        raise PoolError(f"could not download {asset['name']}: {exc.__class__.__name__}") from None
    if got != asset["sha256"]:
        part.unlink(missing_ok=True)
        raise PoolError(
            f"{asset['name']} has SHA-256 {got}, not the published {asset['sha256']}; refusing it"
        )
    new = home.root / "base.new"
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir()
    out = run(["tar", "-xzf", str(part), "-C", str(new)], capture_output=True, text=True)
    part.unlink(missing_ok=True)
    if progress is not None:
        progress()
    if out.returncode != 0 or not (new / "run.sh").is_file():
        shutil.rmtree(new, ignore_errors=True)
        why = (out.stderr or "").strip()[:200] or "no run.sh in it"
        raise PoolError(f"could not unpack {asset['name']}: {why}")
    (new / VERSION_FILE).write_text(asset["version"] + "\n", encoding="utf-8")
    old = home.root / "base.old"
    shutil.rmtree(old, ignore_errors=True)
    if home.base.exists():
        os.replace(home.base, old)
    os.replace(new, home.base)
    shutil.rmtree(old, ignore_errors=True)


# --------------------------------------------------------------------------- slots and processes


def clone_tree(src: Path, dest: Path, run=subprocess.run) -> str:
    """Copy src to dest as an APFS clone (`cp -c`: copy-on-write, so instant and free until a file
    changes), or as a plain copy where the file system can't clone."""
    out = run(["cp", "-c", "-R", str(src), str(dest)], capture_output=True, text=True)
    if out.returncode == 0:
        return "clone"
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest, symlinks=True)
    return "copy"


def runner_env(base: dict, slot: Path, jitconfig: str, hook: Path, cache: Path) -> dict:
    """A runner's whole environment: an allowlist from the pool's own, plus its own HOME and TMPDIR,
    the JIT config, the job-started hook and the shared caches (see share_caches). GIT_CONFIG_NOSYSTEM
    drops the system git config, whose credential helper would hand jobs your keychain logins."""
    env = {key: base[key] for key in ENV_KEEP if base.get(key)}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    env.update(
        HOME=str(slot / "_home"),
        TMPDIR=str(slot / "_tmp") + "/",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        ACTIONS_RUNNER_INPUT_JITCONFIG=jitconfig,
        ACTIONS_RUNNER_HOOK_JOB_STARTED=str(hook),
        UV_CACHE_DIR=str(cache / "uv"),
        UV_PYTHON_INSTALL_DIR=str(cache / "uv-python"),
        RUNNER_TOOL_CACHE=str(slot / "_tool"),
    )
    return env


def share_caches(cache: Path, slot: Path, run=subprocess.run) -> None:
    """Give a new slot the pool's shared caches. uv's cache is safe for concurrent jobs, but
    setup-uv sets UV_CACHE_DIR to $HOME/.cache/uv on self-hosted runners, so that path is a link to
    the shared one. The runner tool cache isn't safe to share in place (@actions/tool-cache deletes
    a tool's folder before copying it in, with no lock), so the slot gets a copy-on-write clone of
    it, and merge_tools brings back what the job cached."""
    for name in ("uv", "uv-python", "tool"):
        (cache / name).mkdir(parents=True, exist_ok=True)
    link = slot / "_home" / ".cache" / "uv"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(cache / "uv", target_is_directory=True)
    clone_tree(cache / "tool", slot / "_tool", run)


def merge_tools(src: Path, dest: Path, run=subprocess.run) -> list:
    """Copy into the shared tool cache (`dest`) the tools a job cached in its slot (`src`) that the
    shared one lacks. @actions/tool-cache marks a finished <tool>/<version>/<arch> folder with
    <arch>.complete beside it; a folder without its marker is half-written and is skipped, and
    only that depth counts: a marker beside a version folder would replace every arch of it.
    Each folder is cloned under a temp name, renamed into place, then marked, so a slot
    cloned from `dest` never sees half a tool. Only the pool writes to `dest`. Returns the tools
    added."""
    added: list = []
    if not src.is_dir():
        return added
    inside = src.resolve()
    for marker in sorted(src.glob("*/*/*.complete")):
        name = marker.name[: -len(".complete")]
        if name in ("", ".", ".."):  # <version>/..complete names <version>; ...complete, <tool>
            continue
        folder = marker.parent / name
        rel = folder.relative_to(src)
        target = dest / rel
        done = target.with_name(target.name + ".complete")
        if done.exists() or folder.is_symlink() or not folder.is_dir():
            continue
        if not folder.resolve().is_relative_to(inside):  # a job's link out of its slot
            continue
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)  # a file in the way raises
            clone_tree(folder, tmp, run)
            shutil.rmtree(target, ignore_errors=True)  # half a folder, left by a crash
            os.replace(tmp, target)
            done.write_text("", encoding="utf-8")
        except OSError as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            log.warning("could not keep the cached tool %s: %s", rel.as_posix(), exc)
            continue
        added.append(rel.as_posix())
    return added


def prune_tools(tools: Path, keep: int = TOOL_KEEP) -> list:
    """Keep the `keep` versions of each tool in the shared tool cache that merge_tools marked
    last, and drop the rest, with what a crash in merge_tools left: its temp folders, and a
    version without a marker (tool-cache never uses one). A dropped version loses its markers
    first, so a slot cloned meanwhile would skip it as half-written. Links are never followed:
    only the pool writes here, between passes. Returns the versions dropped."""
    dropped: list = []
    if not tools.is_dir():
        return dropped

    def real_dirs(folder: Path) -> list:
        return sorted(p for p in folder.iterdir() if p.is_dir() and not p.is_symlink())

    for tool in real_dirs(tools):
        if tool.name.startswith("."):
            continue
        added: dict = {}
        unmarked: list = []
        for version in real_dirs(tool):
            if MERGE_TMP_RE.match(version.name):  # a crash in merge_tools left it
                shutil.rmtree(version, ignore_errors=True)
                continue
            if version.name.startswith("."):
                continue
            for tmp in real_dirs(version):
                if MERGE_TMP_RE.match(tmp.name):
                    shutil.rmtree(tmp, ignore_errors=True)
            marks = [tool / (version.name + ".complete"), *version.glob("*.complete")]
            times = [mark.stat().st_mtime for mark in marks if mark.is_file()]
            if times:
                added[version] = max(times)
            else:
                unmarked.append(version)
        ranked = sorted(added, key=lambda version: (added[version], version.name), reverse=True)
        for version in [*ranked[keep:], *unmarked]:
            (tool / (version.name + ".complete")).unlink(missing_ok=True)
            for mark in version.glob("*.complete"):
                mark.unlink(missing_ok=True)
            shutil.rmtree(version, ignore_errors=True)
            dropped.append(f"{tool.name}/{version.name}")
    return dropped


def newest_uv(tools: Path, path: str, run=subprocess.run, timeout: float = 10) -> tuple | None:
    """The newest uv the jobs use, as (version, its path): the uv on their PATH (`path`, the
    pool's own) or one setup-uv left in the shared tool cache (`uv/<version>/<arch>/uv`, finished,
    and still inside the cache once links are followed). Each one's version is what
    `uv --version` says, within `timeout`, and one that can't say is left out. None when there's
    none."""
    found = [shutil.which("uv", path=path)]
    inside = tools.resolve()
    for mark in sorted(tools.glob("uv/*/*.complete")):
        binary = mark.with_suffix("") / "uv"
        if binary.is_file() and binary.resolve().is_relative_to(inside):
            found.append(str(binary))
    best = None
    for uv in filter(None, found):
        try:
            out = run(
                [uv, "--version"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        match = UV_VERSION_RE.match(out.stdout or "")
        if out.returncode == 0 and match:
            version = tuple(int(part) for part in match.groups())
            if best is None or version > best[0]:
                best = (version, uv)
    return best


def prune_uv(
    uv: str, cache: Path, env: dict, progress=None, timeout: float = UV_PRUNE_TIMEOUT_S
) -> str:
    """`uv cache prune` of the shared uv cache by `uv`, without the user's uv config, in a session
    of its own. uv waits for the cache's lock at most UV_LOCK_S. It calls `progress` (the
    heartbeat's keepalive) each second uv runs; after `timeout` it stops the whole session, and
    waits for it a bounded time. Returns the last line uv printed; raises PoolError when uv fails
    or runs over."""
    proc = subprocess.Popen(
        [uv, "cache", "prune", "--cache-dir", str(cache)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        env=dict(env, UV_NO_CONFIG="1", UV_LOCK_TIMEOUT=str(UV_LOCK_S)),
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    out = None
    try:
        while out is None:
            try:
                out = proc.communicate(timeout=1.0)[0] or ""
            except subprocess.TimeoutExpired:
                if progress is not None:
                    progress()
                if time.monotonic() >= deadline:
                    why = f"uv cache prune ran over {timeout:.0f} s, so it was stopped"
                    raise PoolError(why) from None
    finally:
        if out is None:  # still running: a wrapper's children too, and never an endless wait
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.communicate(timeout=5)
    lines = out.strip().splitlines()
    last = lines[-1].strip() if lines else ""
    if proc.returncode != 0:
        raise PoolError(f"uv cache prune failed (exit {proc.returncode}): {last[:200]}")
    return last


# ------------------------------------------------------------------------------------- the Mac


def default_slots(cores: int, memory: int) -> int:
    """The slots that leave 4 performance cores and 16 GB of RAM to you, at 2 GB per slot."""
    return max(1, min(cores - 4, (memory // GIB - 16) // 2, SETTINGS["slots"][2]))


class Mac:
    """What the pool asks macOS. Each reading is None when it can't be had (another OS, a failed
    command), and the pool then admits as it would without the policy."""

    def __init__(self, run=subprocess.run, popen=subprocess.Popen) -> None:
        self._run = run
        self._popen = popen

    def _read(self, *cmd: str) -> str:
        try:
            out = self._run(list(cmd), capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return (out.stdout or "") if out.returncode == 0 else ""

    def _number(self, name: str) -> int | None:
        text = self._read(SYSCTL, "-n", name).strip()
        return int(text) if text.isdigit() else None

    def pressure(self) -> int | None:
        """The memory pressure level: 1 normal, 2 warning, 4 critical."""
        return self._number("kern.memorystatus_vm_pressure_level")

    def on_battery(self) -> bool | None:
        first = (self._read(PMSET, "-g", "batt").splitlines() or [""])[0]
        if "'Battery Power'" in first:
            return True
        return False if "'AC Power'" in first else None

    def capacity(self) -> tuple | None:
        """(performance cores, RAM in bytes). Intel Macs have no perflevel0."""
        cores = self._number("hw.perflevel0.physicalcpu") or self._number("hw.physicalcpu")
        memory = self._number("hw.memsize")
        return (cores, memory) if cores and memory else None

    def caffeinate(self, pid: int):
        """Start `caffeinate -i -w <pid>`: no idle sleep until that process exits. None where
        there's no caffeinate."""
        if not os.access(CAFFEINATE, os.X_OK):
            return None
        out = subprocess.DEVNULL
        return self._popen([CAFFEINATE, "-i", "-w", str(pid)], stdout=out, stderr=out, stdin=out)


_MACHINE: dict = {}  # this Mac's cores, RAM and default slots, read once (the self-test sets it)


def machine(run=subprocess.run) -> dict:
    """This Mac's performance cores, RAM (GB) and the slots they give by default."""
    if not _MACHINE:
        found = Mac(run).capacity()
        if found:
            cores, memory = found
            _MACHINE.update(cores=cores, memory_gb=memory // GIB, slots=default_slots(*found))
        else:
            _MACHINE.update(cores=None, memory_gb=None, slots=SETTINGS["slots"][0])
    return _MACHINE


def policy_prefix(path: str = TASKPOLICY) -> tuple:
    """Run runners under the utility QoS clamp, which yields to your interactive work. taskpolicy
    execs its program, so the runner keeps its pid, process group and exit code."""
    return (path, "-c", "utility") if os.access(path, os.X_OK) else ()


HOOK = """#!/bin/sh
# Job-started hook of the factory pool runner {name}. The runner runs it before the first step of
# the job GitHub gave it. The runner was made for run {run_id} of {repo}. Labels can't guarantee
# that it gets a job of that run (a job asking only for self-hosted matches it too, even from a
# fork), so this does: a job from any other run fails here, before any of its steps.
if [ "$GITHUB_RUN_ID" = "{run_id}" ]; then
  exit 0
fi
echo "factory runner pool: this runner serves run {run_id} of {repo} only" >&2
exit 1
"""


def write_hook(path: Path, name: str, repo: str, run_id: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HOOK.format(name=name, repo=repo, run_id=int(run_id)), encoding="utf-8")
    path.chmod(0o755)


def signal_group(group: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group, sig)


def group_alive(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def ps_slots(listing: str, slots: Path) -> dict:
    """Slot name -> the process groups its runner leads, from `ps -o pid=,pgid=,command=`. Only
    the runner's own process counts: a group leader running the slot's run.sh (directly, or by
    a shell) or its bin/Runner.Listener. Anything else that names a slot, such as a `tail -f` of
    its logs or a `less`, is never taken for a runner, or signalled, even when job control in a
    terminal makes it a group leader too."""
    own = re.compile(
        r"(?:(?:\S*/)?(?:ba|z|da)?sh(?:\s+-\S+)*\s+)?"  # the shell running run.sh, as ps shows it
        + re.escape(str(slots))
        + r"/(s\d+)/(?:run\.sh|bin/Runner\.Listener)(?:\s|$)"
    )
    found: dict = {}
    for line in listing.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3 or parts[0] != parts[1]:  # each runner's run.sh leads its session
            continue
        match = own.match(parts[2])
        if match:
            found.setdefault(match.group(1), set()).add(int(parts[1]))
    return found


class Launcher:
    """Starts runners in their own sessions, so a stop reaches the whole process tree. A runner is
    alive while its run.sh leads its process group (by command line, see ps_slots): that still
    holds after the pool restarts, when the runners are no longer its children."""

    kind = "host"  # where its runners run: the heartbeat's `launcher`

    def __init__(self, slots: Path, prefix: tuple = ()) -> None:
        self.slots = slots
        self.prefix = list(prefix)
        self.children: dict = {}
        self.codes: dict = {}

    def start(self, slot: Path, env: dict, log_path: Path) -> int:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as out:
            proc = subprocess.Popen(
                [*self.prefix, str(slot / "run.sh")],
                cwd=str(slot),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.children[proc.pid] = proc
        return proc.pid

    def _collect(self) -> None:
        for pid, proc in list(self.children.items()):
            code = proc.poll()
            if code is not None:
                self.codes[pid] = code
                del self.children[pid]

    def reap(self) -> dict:
        """Exit codes of this process's runners that have exited since the last call, by pid."""
        self._collect()
        done, self.codes = self.codes, {}
        return done

    def scan(self) -> dict:
        """Slot name -> the process groups led by that slot's runner (ps_slots)."""
        out = subprocess.run(
            ["ps", "-A", "-ww", "-o", "pid=,pgid=,command="],
            check=False,
            capture_output=True,
            text=True,
        )
        if out.returncode != 0:
            raise PoolError("ps failed, so the pool can't tell which runners are alive")
        return ps_slots(out.stdout, self.slots)

    def leads(self, pid, slot: str) -> bool:
        """Does `pid` still lead its process group, with a command inside `slot`? Pool.missed asks
        this of a runner the scan missed, before deleting its slot."""
        if not isinstance(pid, int) or pid <= 0:
            return False
        out = subprocess.run(
            ["ps", "-ww", "-o", "pgid=,command=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
        )
        parts = out.stdout.split(None, 1)
        inside = f"{self.slots / slot}/"
        return out.returncode == 0 and parts[:1] == [str(pid)] and inside in "".join(parts[1:])

    def stop(self, groups, grace: float = STOP_GRACE_S) -> None:
        """SIGTERM to each process group, then SIGKILL to those still there after `grace` s."""
        own = os.getpgrp()
        left = {group for group in groups if group > 1 and group != own}
        for group in left:
            signal_group(group, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while left and time.monotonic() < deadline:
            time.sleep(0.1)
            self._collect()
            left = {group for group in left if group_alive(group)}
        for group in left:
            signal_group(group, signal.SIGKILL)
        self._collect()


# ---------------------------------------------------------------------------------------- the pool


@dataclass
class Runner:
    """One JIT runner the pool started, as saved in state.json."""

    name: str
    repo: str
    account: str
    run_id: int
    runner_id: int
    slot: str
    pid: int = 0
    started: float = 0.0
    event: str = ""
    pr: int | None = None
    job_id: int | None = None
    job_name: str = ""
    queued_at: str = ""
    job_started_at: str = ""
    assigned: float = 0.0
    exited: float = 0.0
    exit_code: int | None = None
    stopped: str = ""
    deregistered: bool = False
    finish_tries: int = 0
    group: str = ""  # who it counts for in fair share: "pr:<n>" or "branch:<name>" (see run_group)
    branch: str = ""  # its run's head branch

    @classmethod
    def load(cls, data: dict) -> Runner:
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

    def share_group(self) -> str:
        """Its group; a runner saved by an older pool has none, so its PR stands in (else its run)."""
        return self.group or (f"pr:{self.pr}" if self.pr else f"run:{self.run_id}")


@dataclass
class RepoView:
    """One repo's active runs and their jobs, as read this pass."""

    ok: bool
    runs: dict
    jobs: list
    error: str = ""


def run_label(run_id) -> str:
    return f"{RUN_LABEL}{run_id}"


def label_names(registration: dict) -> list:
    labels = registration.get("labels") or []
    return [str(label.get("name")) for label in labels if isinstance(label, dict)]


def asks_pool(job: dict) -> bool:
    labels = job.get("labels") or []
    return any(str(label).lower().startswith(RUN_LABEL) for label in labels)


def wants_pool(job: dict) -> bool:
    """A queued job the pool serves: it asks for its own run's label, and nothing but that and
    `self-hosted`."""
    labels = {str(label).lower() for label in job.get("labels") or ()}
    mine = run_label(job.get("run_id"))
    return job.get("status") == "queued" and mine in labels and labels <= {"self-hosted", mine}


def refusal(run: dict) -> str:
    """Why a run must never get a pool runner ("" when it may)."""
    head = run.get("head_repository") or {}
    repo = run.get("repository") or {}
    if run.get("event") == "pull_request_target":
        return "pull_request_target runs can execute a fork's code"
    if not head or head.get("id") != repo.get("id"):
        return f"its head repository ({head.get('full_name', 'unknown')}) is a fork"
    return ""


# ------------------------------------------------------------------------------------ fair share


@dataclass(frozen=True)
class Want:
    """A queued job the pool could start a runner for, as the scheduler sees it."""

    repo: str
    group: str  # "pr:<n>", or "branch:<name>" for a run without a pull request
    rank: int  # the PR's P label: 0 (P0) to 3
    main: bool  # a push to the repo's default branch
    queued: float  # when the job was queued, in epoch seconds
    job_id: int


def allocate(
    slots: int, live: list, wants: list, repos: dict, pr_cap: int, aging_s: int, now: float
) -> list:
    """The queued jobs to start now, in order, and never more than the free slots. Pure: the same
    snapshot gives the same answer, whatever the order of `wants`.

    Each free slot goes to a repo, then to a job in it:
    - the repo with the fewest live runners per unit of weight, among repos with a queued job and
      below their cap (weighted max-min; running jobs are never stopped). Ties: the oldest job.
    - in it, the lowest of: at or over the per-PR cap (so a PR over it gets a slot only when no
      other PR waits); the rank, one class better per `aging_s` waited; not a push to the default
      branch; the PR's live runners (so PRs take turns); when it was queued; its job ID.

    `live` holds (repo, group) for each live runner; `repos` is config.json's repo table."""
    held_repo = Counter(repo for repo, _ in live)
    held_group = Counter(live)
    pending = sorted(wants, key=lambda want: (want.queued, want.job_id, want.repo))

    def job_key(want: Want) -> tuple:
        held = held_group[(want.repo, want.group)]
        aged = int(max(0.0, now - want.queued) // aging_s)
        rank = max(0, want.rank - aged)
        return (held >= pr_cap, rank, not want.main, held, want.queued, want.job_id)

    picked: list = []
    for _ in range(max(0, slots - len(live))):
        best = None
        for repo in {want.repo for want in pending}:
            entry = repos.get(repo) or {}
            if entry.get("cap") and held_repo[repo] >= entry["cap"]:
                continue
            oldest = next(want.queued for want in pending if want.repo == repo)
            key = (Fraction(held_repo[repo], entry.get("weight") or 1), oldest, repo)
            best = key if best is None or key < best else best
        if best is None:
            break
        repo = best[2]
        job = min((want for want in pending if want.repo == repo), key=job_key)
        pending.remove(job)
        picked.append(job)
        held_repo[repo] += 1
        held_group[(repo, job.group)] += 1
    return picked


def run_group(run: dict) -> str:
    """Who takes turns within a repo: the run's first pull request, or its branch without one."""
    prs = run.get("pull_requests") or []
    number = (prs[0] or {}).get("number") if prs else None
    return f"pr:{number}" if number else f"branch:{run.get('head_branch') or ''}"


def label_rank(labels) -> int:
    """The best P label among a PR's labels (P0 is 0), or DEFAULT_RANK without one."""
    names = (str(label.get("name") or "") for label in labels or () if isinstance(label, dict))
    ranks = [int(match.group(1)) for match in map(PRIORITY_RE.match, names) if match]
    return min(ranks, default=DEFAULT_RANK)


def epoch(stamp, default: float) -> float:
    """An API timestamp (2026-01-01T00:00:02Z, always UTC) in epoch seconds, or `default`."""
    try:
        when = datetime.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return default
    return when.replace(tzinfo=_UTC).timestamp()


class Pool:
    """One pass at a time: watch the runners, read the queue, retire idle runners, record finished
    ones, start new ones. `serve` loops over pass_once; the self-test drives it directly."""

    def __init__(
        self,
        home: Home,
        github: GitHub,
        launcher,
        clock=time.time,
        sleep=time.sleep,
        environ: dict | None = None,
        run=subprocess.run,
        opener=urllib.request.urlopen,
        mac: Mac | None = None,
    ) -> None:
        self.home = home
        self.gh = github
        self.gh.progress = self.keepalive
        self.launcher = launcher
        self.clock = clock
        self.sleep = sleep
        self.environ = dict(os.environ if environ is None else environ)
        self.run = run
        self.opener = opener
        self.cfg: dict = {}
        self.runners: dict = {}
        self.views: dict = {}
        self.procs: dict = {}
        self.exits: dict = {}
        self.backoff: dict = {}  # repo -> (until, streak): reading its queue failed
        self.jit_backoff: dict = {}  # repo -> (until, streak): starting a runner failed
        self.ranks: dict = {}  # (repo, PR) -> its rank, read once per pass
        self.branches: dict = {}  # repo -> its default branch, read once per pass
        self.unlisted: dict = {}  # repo -> {run id: when first seen} the status lists leave out
        self.unlisted_next: dict = {}  # repo -> when its newest runs are read unfiltered again
        self.mac = mac or Mac(run)
        self.critical = False  # memory pressure was critical when last read
        self.held: dict | None = None  # this pass: why the Mac admits fewer than all slots
        self.next_prune = 0.0  # when the shared caches may be pruned again
        self.awake = None  # the caffeinate process, while runners are alive
        self.was_paused = False
        self.noted: set = set()
        self.last_post = 0.0
        self.last_beat = 0.0  # when the heartbeat was last written
        self.passing = False  # startup, then the last pass, got through: keepalive may write
        self.last_base_check: float | None = None  # None: not yet checked

    def live(self) -> list:
        return [r for r in self.runners.values() if not r.exited]

    def note(self, key, level: int, message: str, *args) -> None:
        """Log something once (a refused run, an odd label set) instead of on every pass."""
        if key in self.noted:
            return
        if len(self.noted) > 10000:
            self.noted.clear()
        self.noted.add(key)
        log.log(level, message, *args)

    def log_dir(self, r: Runner) -> Path:
        owner, name = r.repo.split("/", 1)
        return self.home.logs / owner / name / str(r.run_id) / r.name

    def save_state(self) -> None:
        write_json(self.home.state, {"runners": [asdict(r) for r in self.runners.values()]})

    def scan(self) -> None:
        self.exits.update(self.launcher.reap())
        self.procs = self.launcher.scan()

    # --- start-up: whatever a previous pool process left behind

    def startup(self) -> None:
        """Adopt the runners a previous pool process left running, clean up after dead ones, stop
        processes in slots that no saved runner owns, and delete idle registrations this pool made
        but no longer tracks (a crash between registering a runner and saving it)."""
        self.cfg = update_config(self.home, lambda cfg: None)
        now = self.clock()
        self.scan()
        rows = read_json(self.home.state, {}).get("runners") or ()
        saved = [Runner.load(data) for data in rows]
        self.runners = {r.name: r for r in saved}
        # alive once it can see its runners, where a startup that can't (ps or docker ps fails,
        # and launchd restarts it every 10 s) fails first; what follows can take a while
        self.passing = True
        self.beat()
        for r in saved:
            if r.exited:
                continue
            if r.slot in self.procs or self.missed(r):
                r.pid = r.pid or min(self.procs[r.slot])
                log.info("adopted %s (%s, run %s, slot %s)", r.name, r.repo, r.run_id, r.slot)
            else:
                r.exited = now
                self.cleanup(r)
        owned = {r.slot for r in self.live()}
        for slot, groups in sorted(self.procs.items()):
            if slot not in owned:
                log.warning("stopping a runner in slot %s that no saved state names", slot)
                self.launcher.stop(groups)
                self.keepalive()  # each stop can take STOP_GRACE_S, with no request
        if self.home.slots.is_dir():
            for slot_dir in sorted(self.home.slots.iterdir()):
                if slot_dir.name not in owned:
                    orphans = self.home.logs / "orphans" / f"{slot_dir.name}-{int(now)}"
                    self.keep_diag(slot_dir, orphans)
                    shutil.rmtree(slot_dir, ignore_errors=True)
                    self.keepalive()  # a slot can hold a big checkout: seconds, no request
        if self.home.hooks.is_dir():
            names = {r.name for r in self.live()}
            for hook in self.home.hooks.glob("*.sh"):
                if hook.stem not in names:
                    hook.unlink(missing_ok=True)
        self.sweep_registrations()
        self.save_state()

    def sweep_registrations(self) -> None:
        prefix = f"factory-{self.cfg['pool_id']}-"
        mine = {r.name for r in self.runners.values() if not r.deregistered}
        for repo, entry in sorted(self.cfg["repos"].items()):
            try:
                data = self.gh.call(
                    "GET",
                    f"/repos/{repo}/actions/runners",
                    entry["account"],
                    params={"per_page": 100},
                )
            except (GitHubError, PoolError) as exc:
                log.warning("%s: could not list its runners to clean up: %s", repo, exc)
                continue
            for registration in (data or {}).get("runners") or []:
                name = str(registration.get("name") or "")
                if name.startswith(prefix) and name not in mine and not registration.get("busy"):
                    self.delete_registration(repo, entry["account"], registration.get("id"), name)

    # --- one pass

    def pass_once(self) -> dict:
        """One pass. Until a pass gets through again after one fails, the heartbeat isn't kept
        fresh (keepalive): a pool whose passes keep failing must read as down."""
        try:
            summary = self.one_pass()
        except BaseException:
            self.passing = False
            raise
        self.passing = True
        return summary

    def one_pass(self) -> dict:
        self.cfg = load_config(self.home)
        now = self.clock()
        self.maybe_refresh_base(now)
        self.scan()
        self.watch(now)
        self.views = self.snapshot(now)
        self.assign(now)
        self.reap(now)
        self.finish(now)
        started = self.start()
        if not self.live() and now >= self.next_prune:
            self.prune_caches(now)
        self.keep_awake(bool(self.live()))
        self.gh.prune()
        self.save_state()
        return self.beat(started)

    def beat(self, started: int = 0) -> dict:
        """Write the heartbeat, stamped when it's written: at the end of each pass, and between
        the slow steps of a long one (keepalive). A busy pass can take minutes (each runner start
        or clean-up takes seconds), and a heartbeat older than HEARTBEAT_FRESH_S reads as a pool
        that's down."""
        now = self.clock()
        summary = self.summary(now, started)
        write_json(self.home.heartbeat, summary)
        self.last_beat = now
        return summary

    def keepalive(self) -> None:
        """Rewrite the heartbeat if KEEPALIVE_S has passed since it was last written: after each
        API request (GitHub.progress; every runner start, clean-up and retirement makes one) and
        in serve's wait between passes (idle). Only while `passing`: a keepalive extends what a
        startup or pass that got through wrote, so a pool that fails before it gets that far
        (startup, or every pass) reads as down."""
        if self.passing and self.clock() - self.last_beat >= KEEPALIVE_S:
            try:
                self.beat()
            except (OSError, PoolError) as exc:  # it runs inside requests: never fail one
                self.note(("heartbeat", str(exc)), logging.WARNING, "no heartbeat: %s", exc)

    def idle(self, seconds: float, stop: list, monotonic=time.monotonic) -> None:
        """serve's wait between passes, in short sleeps so that a signal ends it soon. The
        heartbeat stays fresh meanwhile (poll_idle_s can be longer than HEARTBEAT_FRESH_S),
        unless the last pass failed (keepalive)."""
        end = monotonic() + seconds
        while not stop and monotonic() < end:
            self.sleep(0.2)
            self.keepalive()

    def keep_awake(self, busy: bool) -> None:
        """Hold one `caffeinate -i` while any runner is alive, so the Mac doesn't idle-sleep in
        the middle of a job. It waits on this process (-w), so a pool that dies lets go too."""
        if self.awake is not None and self.awake.poll() is not None:
            self.awake = None
        if busy and self.awake is None:
            try:
                self.awake = self.mac.caffeinate(os.getpid())
            except OSError as exc:
                self.note("caffeinate", logging.WARNING, "could not run caffeinate: %s", exc)
            if self.awake is None:
                self.note("no-caffeinate", logging.INFO, "no caffeinate: the Mac may sleep mid-job")
        elif not busy and self.awake is not None:
            self.awake.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.awake.wait(timeout=5)
            self.awake = None

    def missed(self, r: Runner) -> bool:
        """Does r still run, though the scan missed it? Its own process still leads its group in
        its slot: a later runner release whose run.sh execs another script, or the instant before
        taskpolicy execs run.sh (ps_slots knows run.sh and the Listener only). Then it's kept, and
        stopped like any runner, rather than its slot deleted under a job."""
        leads = getattr(self.launcher, "leads", None)
        if leads is None or not leads(r.pid, r.slot):
            return False
        self.procs.setdefault(r.slot, set()).add(r.pid)
        why = "%s (pid %s) still leads its process group in slot %s, though the scan missed it"
        self.note(("missed", r.name), logging.WARNING, why, r.name, r.pid, r.slot)
        return True

    def watch(self, now: float) -> None:
        """A runner with no process left in its slot has exited: clean up after it."""
        for r in self.live():
            if r.slot in self.procs or self.missed(r):
                continue
            code = self.exits.pop(r.pid, GONE)
            r.exited = now
            r.exit_code = None if code == GONE else code
            log.info("%s exited (%s)", r.name, "code unknown" if code == GONE else f"code {code}")
            self.cleanup(r)

    def snapshot(self, now: float) -> dict:
        views = {}
        for repo, entry in sorted(self.cfg["repos"].items()):
            if self.backoff.get(repo, (0.0, 0))[0] > now:
                views[repo] = RepoView(False, {}, [], "backing off after an error")
                continue
            try:
                views[repo] = self.view(repo, entry["account"], now)
                self.backoff.pop(repo, None)
            except (GitHubError, PoolError) as exc:
                views[repo] = RepoView(False, {}, [], str(exc))
                self.fail(repo, exc, now)
        return views

    def view(self, repo: str, account: str, now: float) -> RepoView:
        """Queued and in-progress runs (a run with one job running and one queued is in
        progress), any other run that hasn't completed (unlisted_runs), and the jobs of each run
        the pool may serve. Every GET is conditional."""
        runs: dict = {}
        for status in ("queued", "in_progress"):
            data = self.gh.call(
                "GET",
                f"/repos/{repo}/actions/runs",
                account,
                params={"status": status, "per_page": 100},
                cache=True,
            )
            for run in (data or {}).get("workflow_runs") or []:
                run_id = run["id"]
                runs[run_id] = run
        runs.update(self.unlisted_runs(repo, account, runs, now))
        jobs = []
        for run_id in sorted(runs):
            why = refusal(runs[run_id])
            if why:
                key = ("refused", repo, run_id)
                self.note(key, logging.WARNING, "%s: refused run %s: %s", repo, run_id, why)
                continue
            data = self.gh.call(
                "GET",
                f"/repos/{repo}/actions/runs/{run_id}/jobs",
                account,
                params={"per_page": 100},
                cache=True,
            )
            for job in (data or {}).get("jobs") or []:
                jobs.append(job)
                if job.get("status") == "queued" and asks_pool(job) and not wants_pool(job):
                    self.note(
                        ("labels", repo, job.get("id")),
                        logging.WARNING,
                        "%s: job %s of run %s asks for %s; the pool serves only self-hosted "
                        "plus factory-run-%s",
                        repo,
                        job.get("id"),
                        run_id,
                        job.get("labels"),
                        run_id,
                    )
        return RepoView(True, runs, jobs)

    def unlisted_runs(self, repo: str, account: str, listed: dict, now: float) -> dict:
        """Runs that haven't completed but that the status-filtered lists leave out, as GitHub's
        lists can do to a queued run for its whole life. Every UNLISTED_S the newest runs
        are read unfiltered too; each one there that neither list has is then read on its own,
        every pass, until it completes or a list has it. A failed read here (but a rate limit)
        doesn't hold up the runs the lists have: it's logged and tried again later."""
        follow = self.unlisted.setdefault(repo, {})
        if now >= self.unlisted_next.get(repo, now):
            self.unlisted_next[repo] = now + UNLISTED_S
            try:
                data = self.gh.call(
                    "GET",
                    f"/repos/{repo}/actions/runs",
                    account,
                    params={"per_page": UNLISTED_PAGE},
                    cache=True,
                )
            except GitHubError as exc:
                if exc.rate:
                    raise
                log.warning("%s: could not read its newest runs: %s", repo, exc)
                data = None
            for run in (data or {}).get("workflow_runs") or []:
                if run.get("status") != "completed":
                    follow.setdefault(run["id"], now)
        kept: dict = {}
        runs: dict = {}
        for run_id, since in sorted(follow.items()):
            if run_id in listed:
                continue
            try:
                run = self.gh.call(
                    "GET", f"/repos/{repo}/actions/runs/{run_id}", account, cache=True
                )
            except GitHubError as exc:
                if exc.status == 404:
                    continue
                if exc.rate:
                    raise
                key = ("unlisted-error", repo, run_id)
                self.note(key, logging.WARNING, "%s: could not read run %s: %s", repo, run_id, exc)
                kept[run_id] = since  # tried again at the next pass
                continue
            status = (run or {}).get("status")
            if not run or status == "completed":
                continue
            if status in ("queued", "in_progress") and now > since:  # not a moment's lag
                self.note(
                    ("unlisted", repo, run_id),
                    logging.WARNING,
                    "%s: run %s is %s, but GitHub's list of %s runs leaves it out; the pool "
                    "reads it on its own",
                    repo,
                    run_id,
                    status,
                    status.replace("_", "-"),
                )
            kept[run_id] = since
            runs[run_id] = run
        self.unlisted[repo] = kept
        return runs

    def assign(self, now: float) -> None:
        """Note which runners have taken a job: from the job lists, or their slot's Worker log."""
        mine = {r.name: r for r in self.runners.values()}
        for repo, view in self.views.items():
            for job in view.jobs:
                r = mine.get(str(job.get("runner_name") or ""))
                if r is None or r.repo != repo or r.job_id is not None:
                    continue
                r.job_id = job.get("id")
                r.job_name = str(job.get("name") or "")
                r.queued_at = str(job.get("created_at") or "")
                r.job_started_at = str(job.get("started_at") or "")
                r.assigned = r.assigned or now
                log.info("%s took job %s (%s) of run %s", r.name, r.job_id, r.job_name, r.run_id)
        for r in self.live():
            diag = self.home.slots / r.slot / "_diag"
            if not r.assigned and any(diag.glob("Worker_*.log")):
                r.assigned = now

    def reap(self, now: float) -> None:
        """Retire runners that wait in vain: their run has completed, or idle_timeout_s passed."""
        for r in self.live():
            if r.assigned:
                continue
            view = self.views.get(r.repo)
            why = ""
            if now - r.started > self.cfg["idle_timeout_s"]:
                why = f"no job after {self.cfg['idle_timeout_s']} s"
            elif view is not None and view.ok and r.run_id not in view.runs:
                why = self.run_done(r)
            if why:
                self.retire(r, why, now)

    def run_done(self, r: Runner) -> str:
        """The run left the view (both lists, and the runs followed on their own): confirm that
        it has completed."""
        try:
            run = self.gh.call("GET", f"/repos/{r.repo}/actions/runs/{r.run_id}", r.account)
        except GitHubError as exc:
            return "its run is gone" if exc.status == 404 else ""
        except PoolError:
            return ""
        return "its run has completed" if (run or {}).get("status") == "completed" else ""

    def retire(self, r: Runner, why: str, now: float) -> None:
        """Stop a runner that has no job without risking one: deregister it first. GitHub refuses
        that (422) while the runner is busy, and once it's gone no job can reach it. Any other
        failure counts as possibly busy: the runner is left alone and retired on a later pass."""
        try:
            self.gh.call("DELETE", f"/repos/{r.repo}/actions/runners/{r.runner_id}", r.account)
        except GitHubError as exc:
            if exc.status == 422:
                log.info("%s is busy (it took a job just now); keeping it", r.name)
                r.assigned = r.assigned or now
                return
            if exc.status != 404:
                log.warning("%s: could not deregister %s, retrying later: %s", r.repo, r.name, exc)
                return
        except PoolError as exc:
            log.warning("%s: could not deregister %s, retrying later: %s", r.repo, r.name, exc)
            return
        r.deregistered = True
        log.info("retiring %s (%s, run %s): %s", r.name, r.repo, r.run_id, why)
        groups = self.procs.get(r.slot)
        if groups:
            self.launcher.stop(groups)
            self.exits.update(self.launcher.reap())
        code = self.exits.pop(r.pid, GONE)
        r.exited = now
        r.exit_code = None if code == GONE else code
        r.stopped = why
        self.cleanup(r)

    def cleanup(self, r: Runner) -> None:
        """After a runner has exited: keep its _diag and the tools its job cached, delete its slot
        and hook, deregister it."""
        slot = self.home.slots / r.slot
        if slot.exists():
            self.keep_diag(slot, self.log_dir(r))
            added = merge_tools(slot / "_tool", self.home.cache / "tool", self.run)
            if added:
                log.info("the next jobs get %s from the shared tool cache", ", ".join(added))
            shutil.rmtree(slot, ignore_errors=True)
        (self.home.hooks / f"{r.name}.sh").unlink(missing_ok=True)
        if not r.deregistered:
            r.deregistered = self.delete_registration(r.repo, r.account, r.runner_id, r.name)

    def keep_diag(self, slot: Path, dest: Path) -> None:
        diag = slot / "_diag"
        if diag.is_dir():
            try:
                shutil.copytree(diag, dest / "_diag", dirs_exist_ok=True)
            except OSError as exc:
                log.warning("could not keep the logs of %s: %s", slot.name, exc)

    def delete_registration(self, repo: str, account: str, runner_id, name: str) -> bool:
        try:
            self.gh.call("DELETE", f"/repos/{repo}/actions/runners/{runner_id}", account)
            log.info("deleted the registration of %s", name)
        except GitHubError as exc:
            if exc.status != 404:  # 404: gone already (a runner that ran its job removes itself)
                log.warning("%s: could not delete the registration of %s: %s", repo, name, exc)
                return False
        except PoolError as exc:
            log.warning("%s: could not delete the registration of %s: %s", repo, name, exc)
            return False
        return True

    # --- finished runners

    def finish(self, now: float) -> None:
        """Write each exited runner's jobs.jsonl line once its job is known and has concluded."""
        for r in [r for r in self.runners.values() if r.exited]:
            if not r.deregistered:
                r.deregistered = self.delete_registration(r.repo, r.account, r.runner_id, r.name)
            ok, job = self.find_job(r)
            if ok:
                r.finish_tries += 1
            done = job is not None and job.get("status") == "completed"
            unused = ok and job is None and bool(r.stopped)
            gave_up = r.finish_tries >= FINISH_TRIES or now - r.exited > FINISH_GIVE_UP_S
            if done or unused or gave_up:
                self.record(r, job)
                del self.runners[r.name]

    def find_job(self, r: Runner) -> tuple:
        """(looked it up?, the job this runner ran or None), matched by runner name over every
        attempt of its run: a job that started and ended between two passes is found too."""
        try:
            data = self.gh.call(
                "GET",
                f"/repos/{r.repo}/actions/runs/{r.run_id}/jobs",
                r.account,
                params={"filter": "all", "per_page": 100},
            )
        except (GitHubError, PoolError) as exc:
            log.warning("%s: could not look up the job of %s: %s", r.repo, r.name, exc)
            return False, None
        for job in (data or {}).get("jobs") or []:
            if job.get("runner_name") == r.name:
                return True, job
        return True, None

    def record(self, r: Runner, job: dict | None) -> None:
        if self.recorded(r.name):
            return
        job = job or {}
        if job.get("conclusion"):
            conclusion = str(job["conclusion"])
        elif not job and r.stopped and r.job_id is None:
            conclusion = "unused"  # retired before it took a job
        else:
            conclusion = "unknown"
        line = {
            "ts": iso(self.clock()),
            "repo": r.repo,
            "run": r.run_id,
            "job": job.get("id", r.job_id),
            "job_name": job.get("name") or r.job_name or None,
            "pr": r.pr,
            "event": r.event or None,
            "queued_at": job.get("created_at") or r.queued_at or None,
            "started_at": job.get("started_at") or r.job_started_at or None,
            "finished_at": job.get("completed_at") or iso(r.exited),
            "slot": r.slot,
            "runner": r.name,
            "conclusion": conclusion,
            "exit_code": r.exit_code,
            "stopped": r.stopped or None,
        }
        with open(self.home.jobs, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True) + "\n")
        log.info("%s done: %s", r.name, conclusion)

    def recorded(self, name: str) -> bool:
        """Does jobs.jsonl already have this runner? A crash can fall between writing the line and
        saving the state; the ledger's tail is enough to tell."""
        try:
            with open(self.home.jobs, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 262144))
                tail = fh.read().decode("utf-8", "replace")
        except FileNotFoundError:
            return False
        return f'"runner": "{name}"' in tail

    # --- starting runners

    def fail(self, repo: str, exc: Exception, now: float, table: dict | None = None) -> None:
        """Back off a repo after an error: 30 s, doubling up to 15 min, or until the time GitHub
        gave for a rate limit. `table` is self.backoff (reading the queue; any good read resets
        it) or self.jit_backoff (starting runners; only a runner that starts resets it)."""
        table = self.backoff if table is None else table
        _, streak = table.get(repo, (0.0, 0))
        until = now + min(BACKOFF_MAX_S, BACKOFF_MIN_S * 2**streak)
        retry_at = getattr(exc, "retry_at", None)
        if retry_at:
            until = max(until, retry_at)
        table[repo] = (until, streak + 1)
        log.warning("%s: %s; next try at %s", repo, exc, iso(until))

    def waiting_on(self, repo: str) -> bool:
        """Is the repo backing off, after a failed read of its queue or a runner that didn't
        start?"""
        waits = (self.backoff.get(repo, (0.0, 0))[0], self.jit_backoff.get(repo, (0.0, 0))[0])
        return max(waits) > self.clock()

    def queue(self, slots: int) -> list:
        """Queued jobs to start runners for, in the order fair share gives them (see allocate),
        never more than the free slots. A run gets another runner only while its queued jobs
        outnumber its runners whose job isn't known yet (a runner GitHub just gave a job may still
        show that job as queued). Repos backing off sit out, so their share goes to the others."""
        now = self.clock()
        waiting = Counter((r.repo, r.run_id) for r in self.live() if r.job_id is None)
        self.ranks, self.branches = {}, {}
        jobs: dict = {}
        wants = []
        for repo, view in sorted(self.views.items()):
            if not view.ok or repo not in self.cfg["repos"] or self.waiting_on(repo):
                continue
            account = self.cfg["repos"][repo]["account"]
            queued = [job for job in view.jobs if wants_pool(job)]
            queued.sort(key=lambda job: (job.get("created_at") or "", job.get("id") or 0))
            for job in queued:
                key = (repo, job.get("run_id"))
                if waiting[key] > 0:
                    waiting[key] -= 1
                    continue
                run = view.runs.get(job.get("run_id")) or {}
                job_id = int(job.get("id") or 0)
                jobs[job_id] = (repo, job)
                rank = self.rank(repo, account, run)
                main = self.main_push(repo, account, run)
                queued_at = epoch(job.get("created_at"), now)
                wants.append(Want(repo, run_group(run), rank, main, queued_at, job_id))
        live = [(r.repo, r.share_group()) for r in self.live()]
        cfg = self.cfg
        picked = allocate(slots, live, wants, cfg["repos"], cfg["pr_cap"], cfg["aging_s"], now)
        for want in picked:
            log.debug(
                "fair share: job %s of %s (%s, P%s)", want.job_id, want.repo, want.group, want.rank
            )
        return [jobs[want.job_id] for want in picked]

    def rank(self, repo: str, account: str, run: dict) -> int:
        """The run's PR's P label (P0 is 0), read once per pass with a conditional GET, so an
        unchanged label set costs no rate limit. No PR, no P label or a failed read: P2."""
        prs = run.get("pull_requests") or []
        number = (prs[0] or {}).get("number") if prs else None
        if not number:
            return DEFAULT_RANK
        if (repo, number) not in self.ranks:
            path = f"/repos/{repo}/issues/{number}/labels"
            try:
                labels = self.gh.call("GET", path, account, params={"per_page": 100}, cache=True)
                self.ranks[(repo, number)] = label_rank(labels)
            except (GitHubError, PoolError) as exc:
                key = ("labels", repo, number)
                self.note(key, logging.WARNING, "%s: PR #%s counts as P2: %s", repo, number, exc)
                self.ranks[(repo, number)] = DEFAULT_RANK
        return self.ranks[(repo, number)]

    def main_push(self, repo: str, account: str, run: dict) -> bool:
        """A push to the repo's default branch (read once per pass, conditionally: a workflow
        run doesn't carry it). A failed read counts `main` as the default branch."""
        if run.get("event") != "push":
            return False
        if repo not in self.branches:
            try:
                data = self.gh.call("GET", f"/repos/{repo}", account, cache=True) or {}
                self.branches[repo] = str(data.get("default_branch") or "main")
            except (GitHubError, PoolError) as exc:
                key = ("default-branch", repo)
                self.note(
                    key, logging.WARNING, "%s: taking main as the default branch: %s", repo, exc
                )
                self.branches[repo] = "main"
        return run.get("head_branch") == self.branches[repo]

    def start(self) -> int:
        """Start a runner for each job fair share picks, while slots are free and the Mac admits
        more (see capacity)."""
        self.held = None
        paused = paused_info(self.home)
        if paused is not None:
            since = str(paused.get("since") or "?")
            message = "paused since %s: no new runners until `runners.py resume`"
            self.note(("paused", since), logging.INFO, message, since)
            self.was_paused = True
            return 0
        if self.was_paused:
            self.was_paused = False
            log.info("resumed: starting runners again")
        if not any(wants_pool(job) for view in self.views.values() for job in view.jobs):
            return 0
        slots = self.capacity()
        free = slots - len(self.live())
        if free <= 0:
            return 0
        if not (self.home.base / "run.sh").is_file():
            self.note(
                "no-base",
                logging.ERROR,
                "no runner install in %s: run `runners.py base --install`",
                self.home.base,
            )
            return 0
        started = 0
        for repo, job in self.queue(slots):
            if started >= free:
                break
            if self.waiting_on(repo):  # one of its runners failed to start earlier in this pass
                continue
            if self.launch(repo, job):
                started += 1
        return started

    def capacity(self) -> int:
        """The slots runners may fill now: none while memory pressure is critical, half of them
        (at least one) on battery with `battery: half`, else all. A reading that fails admits as
        usual. Running jobs are never stopped."""
        level = self.mac.pressure()
        if level is None:
            self.note(
                "no-pressure", logging.INFO, "can't read the memory pressure; not checking it"
            )
        critical = level is not None and level >= CRITICAL
        if critical != self.critical:
            self.critical = critical
            if critical:
                log.warning("memory pressure is critical: no new runners until it eases")
            elif level is None:
                log.warning("can't read the memory pressure any more: starting runners again")
            else:
                log.info("memory pressure eased: starting runners again")
        if critical:
            self.held = {"slots": 0, "reason": "memory pressure is critical"}
            return 0
        slots = self.cfg["slots"]
        if self.cfg["battery"] == "half":
            battery = self.mac.on_battery()
            if battery is None:
                self.note(
                    "no-battery", logging.INFO, "can't tell the power source; using all slots"
                )
            elif battery:
                half = max(1, slots // 2)
                if half < slots:
                    self.held = {"slots": half, "reason": "on battery (battery: half)"}
                return half
        return slots

    def prune_caches(self, now: float) -> None:
        """Once a day while no runner is alive, and once after the pool starts. The shared tool
        cache keeps each tool's last TOOL_KEEP versions. The shared uv cache is pruned by the
        newest uv the jobs use (newest_uv): `uv cache prune` drops every bucket its own version
        doesn't know, so an older uv's would empty the jobs' cache. uv's Pythons aren't
        pruned: CI asks for few."""
        self.next_prune = now + PRUNE_S
        self.prune_uv_cache()  # first: pruning the tool cache could drop the newest uv
        dropped = prune_tools(self.home.cache / "tool")
        if dropped:
            log.info("pruned the shared tool cache: %s", ", ".join(dropped))

    def prune_uv_cache(self) -> None:
        cache = self.home.cache / "uv"
        found = newest_uv(self.home.cache / "tool", self.environ.get("PATH", ""), self.run)
        if found is None or not cache.is_dir():
            return
        version, uv = found
        try:
            said = prune_uv(uv, cache, self.environ, self.keepalive)
        except (OSError, PoolError) as exc:
            log.warning("can't prune the shared uv cache: %s", exc)
            return
        log.info("pruned the shared uv cache with uv %s: %s", ".".join(map(str, version)), said)

    def free_slot(self) -> str:
        used = {r.slot for r in self.live()} | set(self.procs)
        for n in range(1, 1000):
            name = f"s{n:02d}"
            if name not in used and not (self.home.slots / name).exists():
                return name
        raise PoolError(f"no free slot name in {self.home.slots}")

    def launch(self, repo: str, job: dict) -> bool:
        """Register a JIT runner for the job's run, clone a slot for it and start it. The runner
        is saved before its process starts, so a crash at any point leaves nothing untracked:
        startup() cleans up a saved runner without a process, and deletes a registration made
        just before a crash."""
        account = self.cfg["repos"][repo]["account"]
        run_id = int(job["run_id"])
        run = self.views[repo].runs.get(run_id) or {}
        name = f"factory-{self.cfg['pool_id']}-{run_id}-{secrets.token_hex(2)}"
        wait = self.last_post + JIT_SPACING_S - self.clock()
        if wait > 0:
            self.sleep(wait)
        self.last_post = self.clock()
        body = {
            "name": name,
            "runner_group_id": 1,
            "labels": ["self-hosted", run_label(run_id)],
            "work_folder": "_work",
        }
        path = f"/repos/{repo}/actions/runners/generate-jitconfig"
        try:
            reply = self.gh.call("POST", path, account, body=body) or {}
        except (GitHubError, PoolError) as exc:
            self.fail(repo, exc, self.clock(), self.jit_backoff)
            return False
        jit = str(reply.get("encoded_jit_config") or "")
        runner_id = int((reply.get("runner") or {}).get("id") or 0)
        if not jit or not runner_id:
            why = PoolError(f"generate-jitconfig returned no runner or config for {name}")
            if runner_id:
                self.delete_registration(repo, account, runner_id, name)
            self.fail(repo, why, self.clock(), self.jit_backoff)
            return False
        prs = run.get("pull_requests") or []
        r = Runner(
            name=name,
            repo=repo,
            account=account,
            run_id=run_id,
            runner_id=runner_id,
            slot=self.free_slot(),
            started=self.clock(),
            event=str(run.get("event") or ""),
            pr=(prs[0] or {}).get("number") if prs else None,
            group=run_group(run),
            branch=str(run.get("head_branch") or ""),
        )
        self.runners[name] = r
        self.save_state()
        slot = self.home.slots / r.slot
        hook = self.home.hooks / f"{name}.sh"
        try:
            self.home.slots.mkdir(parents=True, exist_ok=True)
            with file_lock(self.home.root / "base.lock"):
                how = clone_tree(self.home.base, slot, self.run)
            (slot / "_home").mkdir(exist_ok=True)
            (slot / "_tmp").mkdir(exist_ok=True)
            share_caches(self.home.cache, slot, self.run)
            write_hook(hook, name, repo, run_id)
            env = runner_env(self.environ, slot, jit, hook, self.home.cache)
            r.pid = self.launcher.start(slot, env, self.log_dir(r) / "runner.log")
        except (OSError, PoolError) as exc:
            log.error("%s: could not start %s in slot %s: %s", repo, name, r.slot, exc)
            r.exited = self.clock()
            r.stopped = "could not start"
            self.cleanup(r)
            self.fail(repo, exc, self.clock(), self.jit_backoff)
            return False
        self.jit_backoff.pop(repo, None)
        self.save_state()
        log.info("started %s for run %s of %s in slot %s (%s)", name, run_id, repo, r.slot, how)
        return True

    # --- housekeeping

    def maybe_refresh_base(self, now: float) -> None:
        """Install the runner when it's missing, and a newer actions/runner release once a day. It
        happens between passes; runners already started keep their own clones."""
        if self.last_base_check is not None and now - self.last_base_check < BASE_CHECK_S:
            return
        self.last_base_check = now
        current = base_version(self.home)
        accounts = [entry["account"] for entry in self.cfg["repos"].values()]
        try:
            release = self.gh.call("GET", RELEASE_PATH, accounts[0] if accounts else None)
            asset = release_asset(release, runner_platform())
            if current and version_tuple(current) >= version_tuple(asset["version"]):
                return
            log.info("installing actions/runner %s (was %s)", asset["version"], current or "none")
            with file_lock(self.home.root / "base.lock"):
                install_base(self.home, asset, self.opener, self.run, self.keepalive)
        except (GitHubError, PoolError) as exc:
            log.warning("could not update the runner install: %s", exc)
            self.last_base_check = now - BASE_CHECK_S + BASE_RETRY_S

    def queued_jobs(self) -> list:
        """The jobs queued for the pool that have no runner yet, oldest first, for the heartbeat
        and `status`. As in queue(), a run's runners still waiting for their job cover that many
        of its queued jobs."""
        waiting = Counter((r.repo, r.run_id) for r in self.live() if r.job_id is None)
        out = []
        for repo, view in sorted(self.views.items()):
            queued = [job for job in view.jobs if wants_pool(job)]
            queued.sort(key=lambda job: (job.get("created_at") or "", job.get("id") or 0))
            for job in queued:
                key = (repo, job.get("run_id"))
                if waiting[key] > 0:
                    waiting[key] -= 1
                    continue
                run = view.runs.get(job.get("run_id")) or {}
                prs = run.get("pull_requests") or []
                entry = {
                    "repo": repo,
                    "run": job.get("run_id"),
                    "job": job.get("id"),
                    "name": str(job.get("name") or ""),
                    "pr": (prs[0] or {}).get("number") if prs else None,
                    "branch": str(run.get("head_branch") or ""),
                    "queued_at": job.get("created_at"),
                }
                out.append(entry)
        return out

    def summary(self, now: float, started: int) -> dict:
        live = self.live()
        queue = self.queued_jobs()
        return {
            "pid": os.getpid(),
            "time": iso(now),
            "ts": now,
            "version": POOL_VERSION,
            "launcher": getattr(self.launcher, "kind", "host"),
            "runner_version": base_version(self.home),
            "slots": self.cfg["slots"],
            "live": len(live),
            "busy": sum(1 for r in live if r.assigned),
            "queued": len(queue),
            "queue": queue,
            "paused": paused_info(self.home),
            "held": self.held,
            "started": started,
            "finishing": sum(1 for r in self.runners.values() if r.exited),
            "repos": {repo: {"ok": v.ok, "error": v.error} for repo, v in self.views.items()},
            "not_modified": self.gh.not_modified,
        }


# ------------------------------------------------------------------------- operating the pool


def paused_info(home: Home) -> dict | None:
    """The pause (`runners.py pause`): {"since", "reason"}, or None when the pool isn't paused. A
    paused.json that can't be read still pauses the pool: it fails safe."""
    try:
        text = home.paused.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        text = ""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    return (
        data if isinstance(data, dict) else {"since": None, "reason": "paused.json is unreadable"}
    )


def pause_pool(home: Home, reason: str | None, now: float) -> str:
    """Start no new runners until resume_pool; running jobs finish. A pause already there stays.
    A pool serving a runners.py from before pause (its heartbeat has no "paused") ignores it, so
    that is an error: the file is written, and the message says to update the pool."""
    current = paused_info(home)
    if current is None:
        home.make()
        write_json(home.paused, {"since": iso(now), "reason": one_line(reason)})
        said = "paused: running jobs finish, and no new runner starts until `runners.py resume`"
    else:
        said = f"already {pause_text(current)}"
    if ignores_pause(read_json(home.heartbeat, {}), now):
        raise PoolError(f"{said}; {OLD_POOL}")
    return said


def pause_text(paused: dict, unexplained: str = "") -> str:
    """A pause as paused_info gives it, as text: "paused since <when>: <why>"."""
    since = f" since {paused['since']}" if paused.get("since") else ""
    why = one_line(paused.get("reason")) or unexplained
    return f"paused{since}: {why}" if why else f"paused{since}"


OLD_POOL = (
    "but the pool serving now predates pause and keeps starting runners: update it with "
    "`setup.py service install --yes`"
)


def ignores_pause(beat: dict, now: float) -> bool:
    """Is a pool serving that doesn't know pause? Its heartbeat is fresh but has no "paused"."""
    fresh = bool(beat.get("ts")) and now - float(beat["ts"]) < HEARTBEAT_FRESH_S
    return fresh and "paused" not in beat


def resume_pool(home: Home) -> str:
    if paused_info(home) is None:
        return "not paused"
    home.paused.unlink(missing_ok=True)
    return "resumed: the pool starts runners again at its next pass"


def issue_of(branch: str | None) -> int | None:
    """The issue a factory branch (local/issue-N, factory/issue-N) works on."""
    found = ISSUE_BRANCH_RE.match(branch or "")
    return int(found.group(1)) if found else None


def one_line(text) -> str:
    """Text for a one-line message: each run of whitespace, line breaks too, becomes one space."""
    return " ".join(str(text or "").split())


def ago(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


def pool_status(home: Home, now: float) -> tuple:
    """(exit code, the status as a dict), from the pool's own files and with no API calls: up,
    paused or down (no heartbeat for HEARTBEAT_FRESH_S); the slots; and the runners and the jobs
    queued without one, by repo, then PR or branch (with the issue a factory branch names)."""
    beat = read_json(home.heartbeat, {})
    paused = paused_info(home)
    age = now - float(beat["ts"]) if beat.get("ts") else None
    ignored = paused is not None and ignores_pause(beat, now)
    if age is None or age >= HEARTBEAT_FRESH_S:
        state, code = "down", STATUS_DOWN
    elif paused is not None and not ignored:
        state, code = "paused", STATUS_PAUSED
    else:
        state, code = "up", STATUS_UP
    repos: dict = {}

    def group(repo: str, key: str, pr, branch: str) -> dict:
        blank = {"pr": pr, "branch": branch or None, "issue": issue_of(branch)}
        blank.update(running=0, starting=0, queued=0, oldest_wait_s=None)
        return repos.setdefault(repo, {}).setdefault(key, blank)

    saved = read_json(home.state, {}).get("runners") or ()
    live = [r for r in map(Runner.load, saved) if not r.exited]
    for r in live:
        entry = group(r.repo, r.share_group(), r.pr, r.branch)
        entry["running" if r.assigned else "starting"] += 1
    queue = beat.get("queue") or []
    for job in queue:
        branch = str(job.get("branch") or "")
        run = {"pull_requests": [{"number": job["pr"]}] if job.get("pr") else []}
        run["head_branch"] = branch
        entry = group(str(job.get("repo")), run_group(run), job.get("pr"), branch)
        entry["queued"] += 1
        wait = max(0.0, now - epoch(job.get("queued_at"), now))
        entry["oldest_wait_s"] = max(entry["oldest_wait_s"] or 0.0, wait)
    errors = {
        repo: info.get("error") or "unknown error"
        for repo, info in (beat.get("repos") or {}).items()
        if not info.get("ok")
    }
    view = {
        "state": state,
        "pid": beat.get("pid"),
        "version": beat.get("version"),
        # a heartbeat ahead of the clock (the clock was set back) counts as fresh, as in doctor
        "heartbeat_age_s": None if age is None else int(max(0.0, age)),
        "heartbeat_ahead_s": int(-age) if age is not None and age <= -1 else 0,
        "paused": paused,
        # this pass, the Mac admitted fewer than all slots: why, and how many (version 9 on)
        "held": beat.get("held") if state == "up" else None,
        "pause_ignored": ignored,
        "slots": beat.get("slots") or read_json(home.config, {}).get("slots"),
        "running": sum(1 for r in live if r.assigned),
        "starting": sum(1 for r in live if not r.assigned),
        # a pool older than `status` (POOL_VERSION < 4) writes only the count
        "queued": len(queue) if "queue" in beat else int(beat.get("queued") or 0),
        "queue_by_pr": "queue" in beat,
        "errors": errors,
        "repos": repos,
        "docker": None,  # `status` fills it in: why Docker can't run a pool in container mode
    }
    return code, view


def docker_problem(home: Home, docker=None) -> str | None:
    """Why a pool in container mode can't run: Docker doesn't answer, or there's no docker.
    None in host mode, or when Docker answers. With Docker down such a pool stops its passes, so
    its heartbeat goes stale and it reads as down: starting the service again wouldn't help."""
    try:
        cfg = read_json(home.config, {})
        docker = docker or import_containers().Docker()
    except PoolError:  # a config that can't be read, or no containers.py: nothing to say here
        return None
    if not isinstance(cfg, dict) or cfg.get("mode", "host") == "host":
        return None
    try:
        docker.info(timeout=DOCKER_ASK_S)
    except PoolError as exc:
        return str(exc)
    return None


def format_status(view: dict) -> list:
    """`status` as text."""
    age = view["heartbeat_age_s"]
    if view["state"] == "down":
        never = "it has never run" if age is None else f"no heartbeat for {ago(age)}"
        lines = [f"pool: down ({never})"]
        problem = view.get("docker")
        if problem:
            fix = ""
            if problem.startswith(DOCKER_DOWN):  # not a missing docker CLI: that says its own fix
                fix = (
                    ": this pool runs its jobs in containers, so start Docker Desktop. If the "
                    "pool still reads as down then, start it: setup.py service install --yes"
                )
            lines.append(f"docker: {problem}{fix}")
    else:
        head = f"pool: {view['state']} (pid {view['pid']}, version {view['version']}"
        ahead = view["heartbeat_ahead_s"]
        when = f"{ago(ahead)} ahead of this Mac's clock" if ahead else f"{ago(age)} ago"
        lines = [f"{head}, heartbeat {when})"]
    paused = view["paused"]
    if view["pause_ignored"]:
        lines.append(f"paused.json is there, {OLD_POOL}")
    elif paused is not None:
        lines.append(f"{pause_text(paused, 'no reason given')} (runners.py resume)")
    slots = f", {view['slots']} in all" if view["slots"] else ""
    lines.append(f"slots: {view['running']} running, {view['starting']} starting{slots}")
    held = view["held"]
    if held:
        lines.append(
            f"admitting {held.get('slots')} of {view['slots']} slots: {held.get('reason')}"
        )
    stale = " at the last heartbeat" if view["state"] == "down" else ""
    if age is None:  # it has never run: there's no queue to show
        pass
    elif view["queue_by_pr"]:
        lines.append(f"queued without a runner: {view['queued']}{stale}")
    else:  # a pool older than `status`: its count includes jobs a runner is starting for
        lines.append(f"queued: {view['queued']}{stale} (this pool doesn't list them by PR yet)")
    for repo, error in sorted(view["errors"].items()):
        lines.append(f"  {repo}: can't read its queue: {error}")
    for repo, groups in sorted(view["repos"].items()):
        totals = {key: sum(g[key] for g in groups.values()) for key in ("running", "queued")}
        lines.append(f"  {repo}: {totals['running']} running, {totals['queued']} queued")
        for key, g in sorted(groups.items()):
            kind, _, value = key.partition(":")
            if g["pr"]:
                name = f"PR #{g['pr']}"
            elif kind == "run":  # a runner saved before groups existed, without a PR
                name = f"run {value}"
            else:
                name = g["branch"] or value or "(no branch)"
            if g["issue"]:
                name += f" (issue #{g['issue']})"
            parts = [f"{g['running']} running"]
            if g["starting"]:
                parts.append(f"{g['starting']} starting")
            parts.append(f"{g['queued']} queued")
            if g["oldest_wait_s"] is not None:
                parts.append(f"waiting up to {ago(g['oldest_wait_s'])}")
            lines.append(f"    {name}: {', '.join(parts)}")
    return lines


def read_ledger(home: Home) -> list:
    """jobs.jsonl as dicts; a line that isn't a JSON object is skipped."""
    rows = []
    try:
        with open(home.jobs, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except FileNotFoundError:
        pass
    return rows


def percentile(values: list, q: float):
    """The nearest-rank percentile (q from 0 to 100) of the values, or None without any."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


def ledger_stats(rows: list, now: float, days: int, slots: int) -> dict:
    """Overall and per repo, the jobs that finished in the last `days`: how many, by conclusion;
    the wait (queued to started) and run time (started to finished), p50 and p95; and how busy
    the slots were: the run time inside the window over slots x the window. The window is the
    last `days`, or less when the whole ledger is younger: it then starts at the ledger's first
    job. Runners that never ran a job count only by conclusion."""
    start = now - days * 86400
    stamps = [epoch(row.get("queued_at") or row.get("finished_at"), now) for row in rows]
    since = max(start, min(stamps, default=now))
    window = max(1.0, now - since)
    rows = [row for row in rows if epoch(row.get("finished_at") or row.get("ts"), 0.0) >= start]

    def summarise(picked: list) -> dict:
        waits, runs, inside = [], [], 0.0
        for row in picked:
            queued = epoch(row.get("queued_at"), -1.0)
            began = epoch(row.get("started_at"), -1.0)
            ended = epoch(row.get("finished_at"), -1.0)
            if queued >= 0 and began >= 0:
                waits.append(max(0.0, began - queued))
            if began >= 0 and ended >= 0:
                runs.append(max(0.0, ended - began))
                inside += max(0.0, min(ended, now) - max(began, since))
        return {
            "jobs": len(picked),
            "conclusions": dict(Counter(str(row.get("conclusion")) for row in picked)),
            "wait_p50_s": percentile(waits, 50),
            "wait_p95_s": percentile(waits, 95),
            "run_p50_s": percentile(runs, 50),
            "run_p95_s": percentile(runs, 95),
            "busy": inside / (max(1, slots) * window),
        }

    out = summarise(rows)
    out.update(days=days, slots=slots, window_s=int(window), repos={})
    by_repo: dict = {}
    for row in rows:
        by_repo.setdefault(str(row.get("repo")), []).append(row)
    out["repos"] = {repo: summarise(picked) for repo, picked in sorted(by_repo.items())}
    return out


def format_stats(stats: dict) -> list:
    """`stats` as text."""
    if not stats["jobs"]:
        return [f"no jobs finished in the last {stats['days']} days"]

    def times(part: dict) -> str:
        def show(seconds) -> str:
            return "-" if seconds is None else ago(seconds)

        return (
            f"wait p50 {show(part['wait_p50_s'])}, p95 {show(part['wait_p95_s'])}; "
            f"run p50 {show(part['run_p50_s'])}, p95 {show(part['run_p95_s'])}"
        )

    def busy(part: dict) -> str:
        return f"{100 * part['busy']:.1f}%"

    ended = ", ".join(f"{key} {n}" for key, n in sorted(stats["conclusions"].items()))
    lines = [
        f"last {stats['days']} days: {stats['jobs']} jobs ({ended})",
        f"  {times(stats)}",
        f"  busy: {busy(stats)} of {stats['slots']} slots over {ago(stats['window_s'])}",
    ]
    for repo, part in stats["repos"].items():
        lines.append(f"  {repo}: {part['jobs']} jobs, busy {busy(part)}; {times(part)}")
    return lines


def tail_lines(path: Path, n: int) -> list:
    """The last n lines of a text file, reading at most its last MB."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - (1 << 20)))
            text = fh.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return []
    return text.splitlines()[-n:] if n > 0 else []


def cmd_logs(home: Home, n: int, run_id: int | None) -> int:
    """pool.log's last n lines, or, for a run, each of its runners' log tails and _diag folders."""
    if run_id is None:
        path = home.logs / "pool.log"
        if not path.is_file():
            raise PoolError(f"no pool.log in {home.logs} yet: the pool hasn't run")
        print("\n".join(tail_lines(path, n)))
        return 0
    found = sorted(path for path in home.logs.glob(f"*/*/{run_id}/*") if path.is_dir())
    if not found:
        raise PoolError(f"no runner logs for run {run_id} in {home.logs}")
    for folder in found:
        repo = f"{folder.parents[2].name}/{folder.parents[1].name}"
        print(f"== {repo}, run {run_id}, runner {folder.name}")
        for line in tail_lines(folder / "runner.log", n):
            print(line)
        if (folder / "_diag").is_dir():
            print(f"_diag: {folder / '_diag'}")
    return 0


def pool_slots(home: Home) -> int:
    """The pool's slots, as it last served (its heartbeat) or as configured, without asking the Mac."""
    slots = read_json(home.heartbeat, {}).get("slots") or read_json(home.config, {}).get("slots")
    return int(slots or SETTINGS["slots"][0])


# ------------------------------------------------------------------------------------ commands


def setup_logging(home: Home | None, verbose: bool, err=None) -> None:
    """pool.log gets every line (it rotates). stderr gets them all on a terminal, but only warnings
    and errors otherwise: under the launchd service it goes to service.log, which doesn't rotate."""
    err = sys.stderr if err is None else err
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream = logging.StreamHandler(err)
    stream.setFormatter(fmt)
    tty = getattr(err, "isatty", None)
    if not (tty and tty()):
        stream.setLevel(logging.WARNING)
    log.addHandler(stream)
    if home is not None:
        home.logs.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            home.logs / "pool.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        rotating.setFormatter(fmt)
        log.addHandler(rotating)


def serve(home: Home, once: bool) -> int:
    """The pool loop. One per pool home (serve.lock). SIGTERM ends it after the current pass;
    runners keep running in their own sessions, and the next `serve` adopts them."""
    home.make()
    with open(home.serve_lock, "a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise PoolError(f"a pool is already serving {home.root} (serve.lock is held)") from None
        return serve_locked(home, once)


def make_launcher(home: Home, host: Launcher):
    """The pool's launcher: `host`, which runs runners as processes on this Mac, or in container
    mode (config.json's mode) containers.py's ContainerLauncher, which wraps it. Any mode but
    host goes there, and a mode it doesn't know fails each start: never jobs on the Mac by mistake."""
    if load_config(home).get("mode", "host") == "host":
        return host
    return import_containers().ContainerLauncher(home, host)


def import_containers():
    """containers.py, next to this file: only container mode needs it."""
    # run as a script, this file is __main__, and containers.py's `import runners` must get it
    sys.modules.setdefault("runners", sys.modules[__name__])
    try:
        import containers
    except ImportError:
        raise PoolError(
            "container mode needs containers.py next to runners.py: run `setup.py service "
            "install --yes` again"
        ) from None
    return containers


def serve_locked(home: Home, once: bool) -> int:
    stop: list = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append("TERM"))
    signal.signal(signal.SIGINT, lambda *_: stop.append("INT"))
    prefix = policy_prefix()
    if not prefix:
        log.info("no %s here: runners start at normal priority", TASKPOLICY)
    pool = Pool(home, GitHub(Tokens()), make_launcher(home, Launcher(home.slots, prefix)))
    pool.startup()
    log.info("serving %s: %s slots, %s repos", home.root, pool.cfg["slots"], len(pool.cfg["repos"]))
    while True:
        summary: dict = {}
        try:
            summary = pool.pass_once()
        except PoolError as exc:
            log.error("%s", exc)
        except Exception:  # keep serving; launchd would restart a crashed pool anyway
            log.exception("the pass failed")
        if once:
            print(json.dumps(summary, indent=1, sort_keys=True))
            return 0
        if stop:
            log.info("stopping; live runners keep running and the next pool adopts them")
            return 0
        busy = summary.get("queued") or summary.get("live") or summary.get("finishing")
        pool.idle(pool.cfg["poll_fast_s" if busy else "poll_idle_s"], stop)


def cmd_base(home: Home, install: bool) -> int:
    current = base_version(home)
    cfg = load_config(home)
    accounts = [entry["account"] for entry in cfg["repos"].values()]
    release = GitHub(Tokens()).call("GET", RELEASE_PATH, accounts[0] if accounts else None)
    asset = release_asset(release, runner_platform())
    print(f"installed: {current or 'none'} ({home.base})")
    print(f"latest:    {asset['version']} ({asset['name']}, sha256 {asset['sha256']})")
    if not install:
        return 0
    if current and version_tuple(current) >= version_tuple(asset["version"]):
        print("up to date")
        return 0
    home.make()
    with file_lock(home.root / "base.lock"):
        install_base(home, asset)
    print(f"installed actions/runner {asset['version']}, checked against its published SHA-256")
    return 0


def cmd_config(home: Home, changes: dict) -> int:
    """Show the settings, after applying `changes` (config key -> value; None leaves it as is),
    and the slots this Mac would get by default, with the cores and RAM that gives them."""
    changes = {key: value for key, value in changes.items() if value is not None}
    cfg = update_config(home, lambda c: c.update(changes)) if changes else load_config(home)
    shown = {key: value for key, value in cfg.items() if key != "repos"}
    found = machine()
    shown["this_mac"] = {
        "default_slots": found["slots"],
        "performance_cores": found["cores"],
        "memory_gb": found["memory_gb"],
    }
    print(json.dumps(shown, indent=1, sort_keys=True))
    return 0


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true", help="run offline checks and exit")
    parser.add_argument("--home", help=f"pool home (default: ${HOME_ENV} or {DEFAULT_HOME})")
    parser.add_argument("-v", "--verbose", action="store_true", help="log debug detail")
    sub = parser.add_subparsers(dest="cmd")
    repo = sub.add_parser("repo", help="the repos the pool serves")
    repo_sub = repo.add_subparsers(dest="action")
    add = repo_sub.add_parser("add", help="serve OWNER/REPO")
    add.add_argument("repo")
    add.add_argument("--account", help="gh login whose token to use (default: OWNER, or as before)")
    add.add_argument("--weight", type=int, help="its share of the slots against other repos (1)")
    add.add_argument("--cap", type=int, help="at most this many of its runners at once (0: none)")
    remove = repo_sub.add_parser("remove", help="stop serving OWNER/REPO")
    remove.add_argument("repo")
    repo_sub.add_parser("list", help="the repos served, with their accounts, weights and caps")
    config = sub.add_parser("config", help="show or change the pool's settings")
    config.add_argument("--slots", type=int, help="runners alive at once")
    config.add_argument("--idle-timeout", type=int, help="seconds a runner may wait for a job")
    config.add_argument("--pr-cap", type=int, help="runners a PR gets before others go first")
    config.add_argument("--aging", type=int, help="seconds a job waits to move up one P class")
    config.add_argument("--battery", choices=BATTERY, help="slots on battery: all, or half")
    base = sub.add_parser("base", help="the runner install: show it, or --install the latest")
    base.add_argument("--install", action="store_true")
    run = sub.add_parser("serve", help="run the pool")
    run.add_argument("--once", action="store_true", help="one pass, then print its summary")
    status = sub.add_parser("status", help="up, paused or down; the slots; the queue by repo, PR")
    status.add_argument("--json", action="store_true", help="print JSON")
    pause = sub.add_parser("pause", help="start no new runners; running jobs finish")
    pause.add_argument("--reason", help="why, for status to show")
    sub.add_parser("resume", help="start runners again")
    stats = sub.add_parser("stats", help="wait and run times, and how busy, from jobs.jsonl")
    stats.add_argument("--days", type=int, default=7, help="the window in days (default 7)")
    stats.add_argument("--json", action="store_true", help="print JSON")
    logs = sub.add_parser("logs", help="pool.log's tail, or a run's runner logs")
    logs.add_argument("-n", type=int, default=50, help="lines from the end (default 50)")
    logs.add_argument("--run", type=int, help="a workflow run ID")
    args = parser.parse_args(argv)

    if args.self_test:
        _self_test()
        return 0
    home = resolve_home(args.home)
    try:
        if args.cmd == "repo" and args.action == "add":
            print(repo_add(home, args.repo, args.account, args.weight, args.cap))
        elif args.cmd == "repo" and args.action == "remove":
            print(repo_remove(home, args.repo))
        elif args.cmd == "repo" and args.action == "list":
            repos = load_config(home)["repos"]
            for name, entry in sorted(repos.items()):
                print(f"{name}  (gh login: {entry['account']}){share(entry)}")
            if not repos:
                print("no repos registered: runners.py repo add OWNER/REPO")
        elif args.cmd == "config":
            changes = {
                "slots": args.slots,
                "idle_timeout_s": args.idle_timeout,
                "pr_cap": args.pr_cap,
                "aging_s": args.aging,
                "battery": args.battery,
            }
            return cmd_config(home, changes)
        elif args.cmd == "base":
            return cmd_base(home, args.install)
        elif args.cmd == "serve":
            setup_logging(home, args.verbose)
            return serve(home, args.once)
        elif args.cmd == "status":
            code, view = pool_status(home, time.time())
            if code == STATUS_DOWN:
                view["docker"] = docker_problem(home)
            text = json.dumps(view, indent=1, sort_keys=True) if args.json else None
            print(text or "\n".join(format_status(view)))
            return code
        elif args.cmd == "pause":
            print(pause_pool(home, args.reason, time.time()))
        elif args.cmd == "resume":
            print(resume_pool(home))
        elif args.cmd == "stats":
            if args.days < 1:
                raise PoolError("--days must be 1 or more")
            got = ledger_stats(read_ledger(home), time.time(), args.days, pool_slots(home))
            text = json.dumps(got, indent=1, sort_keys=True) if args.json else None
            print(text or "\n".join(format_stats(got)))
        elif args.cmd == "logs":
            return cmd_logs(home, args.n, args.run)
        else:
            parser.print_help()
    except (PoolError, GitHubError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


# ------------------------------------------------------------------------------------- self-test


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


class _Clock:
    """Fake time: it moves only when the test (or the pool's sleep) moves it."""

    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeGitHub:
    """GitHub for the self-test, as the real client's transport: workflow runs, jobs and runner
    registrations in memory, ETags, and errors queued per (method, path), or per (method,
    path?query) to fail one query of a path. Every call for a repo must carry the token of that
    repo's account."""

    def __init__(self, owners: dict) -> None:
        self.owners = owners  # repo -> the account whose token its calls must carry
        self.runs: dict = {}
        self.jobs: dict = {}
        self.regs: dict = {}  # repo -> {runner id: registration}
        self.errors: dict = {}
        self.calls: list = []
        self.unchanged: list = []  # paths answered 304
        self.posts: list = []
        self.release: dict = {}
        self.labels: dict = {}  # (repo, PR number) -> its label names
        self.branches: dict = {}  # repo -> its default branch (main when not set)
        self.unlisted: set = set()  # run IDs the status-filtered lists leave out, as GitHub can
        self.next_id = 100

    def __call__(self, request: urllib.request.Request, timeout: float) -> Reply:
        url = urllib.parse.urlsplit(request.full_url)
        path, query = url.path, dict(urllib.parse.parse_qsl(url.query))
        method = request.get_method()
        self.calls.append((method, path))
        repo = "/".join(path.split("/")[2:4]) if path.startswith("/repos/") else ""
        if repo in self.owners:
            want = "Bearer TOKEN-" + self.owners[repo]
            assert request.get_header("Authorization") == want, (method, path)
        # an error queued for the path with its query (`?per_page=30`) comes first
        queued = self.errors.get((method, f"{path}?{url.query}")) or self.errors.get((method, path))
        if queued:
            status, headers, body = queued.pop(0)
            return Reply(status, headers, json.dumps(body).encode())
        out = self.route(method, path, query, request.data, repo)
        if isinstance(out, Reply):
            return out
        data = json.dumps(out, sort_keys=True).encode()
        etag = '"' + hashlib.sha256(data).hexdigest()[:16] + '"'
        if method == "GET" and request.get_header("If-none-match") == etag:
            self.unchanged.append(path)
            return Reply(304, {"etag": etag}, b"")
        return Reply(200, {"etag": etag}, data)

    def route(self, method: str, path: str, query: dict, data, repo: str):
        missing = Reply(404, {}, b'{"message": "Not Found"}')
        if path == RELEASE_PATH:
            return self.release or missing
        tail = path.split("/")[4:]
        if method == "GET" and repo in self.owners and not tail:
            return {"full_name": repo, "default_branch": self.branches.get(repo, "main")}
        if method == "GET" and tail[:1] == ["issues"] and tail[2:] == ["labels"]:
            names = self.labels.get((repo, int(tail[1])), [])
            return [{"id": n, "name": name} for n, name in enumerate(names)]
        if method == "GET" and tail == ["actions", "runs"]:
            status = query.get("status")
            runs = [
                dict(run)
                for run in sorted(self.runs.values(), key=lambda run: -run["id"])
                if run["repository"]["full_name"] == repo
                and (status is None or (run["status"] == status and run["id"] not in self.unlisted))
            ]
            runs = runs[: int(query.get("per_page", 30))]  # the newest first, as GitHub
            return {"total_count": len(runs), "workflow_runs": runs}
        if method == "GET" and tail[:2] == ["actions", "runs"] and len(tail) == 3:
            run = self.runs.get(int(tail[2]))
            return dict(run) if run else missing
        if method == "GET" and tail[:2] == ["actions", "runs"] and tail[3:] == ["jobs"]:
            jobs = [dict(job) for job in self.jobs.get(int(tail[2]), [])]
            return {"total_count": len(jobs), "jobs": jobs}
        if method == "GET" and tail == ["actions", "runners"]:
            regs = [dict(reg) for reg in self.regs.get(repo, {}).values()]
            return {"total_count": len(regs), "runners": regs}
        if method == "POST" and tail == ["actions", "runners", "generate-jitconfig"]:
            body = json.loads(data)
            self.posts.append((repo, body))
            self.next_id += 1
            wanted = body["labels"]
            labels = [{"name": name} for name in wanted]
            reg = {"id": self.next_id, "name": body["name"], "busy": False, "labels": labels}
            self.regs.setdefault(repo, {})[self.next_id] = reg
            return {"runner": dict(reg), "encoded_jit_config": f"JITSECRET{self.next_id}"}
        if method == "DELETE" and tail[:2] == ["actions", "runners"] and len(tail) == 3:
            regs = self.regs.get(repo, {})
            key = int(tail[2])
            reg = regs.get(key)
            if reg is None:
                return missing
            if reg["busy"]:
                return Reply(422, {}, b'{"message": "Bad request - Runner is busy"}')
            del regs[key]
            return Reply(204, {}, b"")
        return missing

    def add_run(
        self,
        repo,
        run_id,
        created,
        jobs=1,
        fork=False,
        event="push",
        labels=None,
        pr=None,
        branch=None,
    ):
        rid = sum(map(ord, repo))
        head = {"id": rid, "full_name": repo}
        if fork:
            head = {"id": rid + 1, "full_name": "mallory/" + repo.split("/")[1]}
        self.runs[run_id] = {
            "id": run_id,
            "status": "queued",
            "event": event,
            "head_branch": branch or (f"topic-{pr}" if pr else "main"),
            "repository": {"id": rid, "full_name": repo},
            "head_repository": head,
            "pull_requests": [{"number": pr}] if pr else [],
            "created_at": created,
        }
        self.jobs[run_id] = []
        for _ in range(jobs):
            self.add_job(run_id, created, labels)

    def add_job(self, run_id, created, labels=None):
        jobs = self.jobs[run_id]
        jobs.append(
            {
                "id": run_id * 10 + len(jobs),
                "run_id": run_id,
                "name": f"job {len(jobs)}",
                "status": "queued",
                "conclusion": None,
                "labels": labels or ["self-hosted", run_label(run_id)],
                "runner_name": None,
                "created_at": created,
                "started_at": None,
                "completed_at": None,
            }
        )

    def registration(self, name: str) -> dict:
        for regs in self.regs.values():
            for reg in regs.values():
                if reg["name"] == name:
                    return reg
        return {}

    def take(self, run_id: int, index: int, runner: str) -> None:
        """GitHub gives the run's job number `index` to the named runner."""
        job = self.jobs[run_id][index]
        job.update(status="in_progress", runner_name=runner, started_at="2026-01-01T00:10:00Z")
        self.runs[run_id]["status"] = "in_progress"
        if self.registration(runner):
            self.registration(runner)["busy"] = True

    def complete(self, run_id: int, index: int, conclusion: str) -> None:
        """The job ends, and its ephemeral runner removes its own registration."""
        job = self.jobs[run_id][index]
        job.update(status="completed", conclusion=conclusion, completed_at="2026-01-01T00:20:00Z")
        runner = job["runner_name"]
        for regs in self.regs.values():
            for reg_id in [i for i, reg in regs.items() if reg["name"] == runner]:
                del regs[reg_id]
        if all(other["status"] == "completed" for other in self.jobs[run_id]):
            self.runs[run_id]["status"] = "completed"

    def cancel(self, run_id: int) -> None:
        for job in self.jobs[run_id]:
            if job["status"] != "completed":
                job.update(status="completed", conclusion="cancelled")
        self.runs[run_id]["status"] = "completed"


class FakeLauncher:
    """Runner processes for the self-test: each one alive until exit() or stop()."""

    def __init__(self, fake: FakeGitHub | None = None) -> None:
        self.fake = fake
        self.alive: dict = {}  # slot name -> pid (and process group)
        self.codes: dict = {}
        self.envs: dict = {}
        self.stops: list = []  # (slot, the last API call before the stop)
        self.next_pid = 5000
        self.leading: dict = {}  # pid -> slot: a runner scan() misses, and leads() still sees

    def leads(self, pid, slot: str) -> bool:
        return self.leading.get(pid) == slot

    def start(self, slot: Path, env: dict, log_path: Path) -> int:
        assert Path(env["ACTIONS_RUNNER_HOOK_JOB_STARTED"]).is_file()
        self.next_pid += 1
        self.alive[slot.name] = self.next_pid
        self.envs[slot.name] = dict(env)
        return self.next_pid

    def reap(self) -> dict:
        done, self.codes = self.codes, {}
        return done

    def scan(self) -> dict:
        return {slot: {pid} for slot, pid in self.alive.items()}

    def stop(self, groups, grace: float = 0.0) -> None:
        for slot, pid in list(self.alive.items()):
            if pid in groups:
                last = self.fake.calls[-1] if self.fake and self.fake.calls else None
                self.stops.append((slot, last))
                del self.alive[slot]
                self.codes[pid] = -15

    def exit(self, slot: str, code: int) -> None:
        self.codes[self.alive.pop(slot)] = code


def _fake_gh_auth(cmd, **kwargs):
    """`gh auth token --user X` answers TOKEN-X; it must never see a token in its environment."""
    assert not set(TOKEN_ENV) & set(kwargs["env"]), "gh must run without GH_TOKEN"
    user = cmd[cmd.index("--user") + 1]
    if user == "nobody":
        return subprocess.CompletedProcess(cmd, 1, "", "no oauth token found for nobody\n")
    return subprocess.CompletedProcess(cmd, 0, f"TOKEN-{user}\n", "")


def _refuses(fn, *args) -> str:
    try:
        fn(*args)
    except PoolError as exc:
        return str(exc)
    raise AssertionError(f"{fn.__name__} did not refuse {args!r}")


def _api_error(fn, *args, **kwargs) -> GitHubError:
    try:
        fn(*args, **kwargs)
    except GitHubError as exc:
        return exc
    raise AssertionError(f"{fn.__name__} did not fail {args!r}")


def _fake_base(home: Home) -> None:
    home.base.mkdir(parents=True, exist_ok=True)
    (home.base / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (home.base / VERSION_FILE).write_text("2.338.2\n", encoding="utf-8")


def _pool(
    home: Home, fake: FakeGitHub, clock: _Clock, launcher, environ=None, opener=None, mac=None
):
    github = GitHub(Tokens(run=_fake_gh_auth, clock=clock), transport=fake, clock=clock)
    pool = Pool(
        home,
        github,
        launcher,
        clock=clock,
        sleep=clock.sleep,
        environ=environ or {"PATH": "/usr/bin:/bin"},
        opener=opener or urllib.request.urlopen,
        mac=mac or FakeMac(),
    )
    pool.last_base_check = clock.t
    return pool


class _FakeProc:
    """A caffeinate that only records: running until terminate()."""

    def __init__(self, args: list) -> None:
        self.args = args
        self.code = None

    def poll(self):
        return self.code

    def terminate(self) -> None:
        self.code = -15

    def wait(self, timeout=None):
        return self.code


class FakeMac(Mac):
    """macOS for the self-test: readings the test sets, and a caffeinate that only records."""

    def __init__(self, pressure=1, battery=False) -> None:
        super().__init__()
        self.level = pressure
        self.battery = battery
        self.awake: list = []

    def pressure(self):
        return self.level

    def on_battery(self):
        return self.battery

    def capacity(self):
        return (6, 36 * GIB)

    def caffeinate(self, pid: int):
        self.awake.append(_FakeProc([CAFFEINATE, "-i", "-w", str(pid)]))
        return self.awake[-1]


def _fake_mac(answers: dict):
    """A `run` that answers sysctl and pmset from `answers` (missing: the command fails)."""

    def run(cmd, **kwargs):
        key = cmd[-1] if cmd[0] == SYSCTL else "batt"
        if key not in answers:
            return subprocess.CompletedProcess(cmd, 1, "", "unknown oid\n")
        return subprocess.CompletedProcess(cmd, 0, answers[key], "")

    return run


def _ledger(home: Home) -> list:
    return [json.loads(line) for line in home.jobs.read_text(encoding="utf-8").splitlines()]


def _test_config(root: Path) -> None:
    home = Home(root)
    assert repo_add(home, "octo/app", None).startswith("octo/app: served")
    repo_add(home, "octo/lib", "ci-bot")
    repo_add(home, "octo_ent/app", None)  # an EMU owner
    cfg = load_config(home)
    want = {"account": "octo"}, {"account": "ci-bot"}, {"account": "octo_ent"}
    assert list(cfg["repos"].values()) == list(want), cfg["repos"]
    assert POOL_ID_RE.match(cfg["pool_id"]) and cfg["slots"] == 2 and cfg["schema"] == SCHEMA
    assert root.stat().st_mode & 0o777 == 0o700
    repo_remove(home, "octo/app")
    assert list(load_config(home)["repos"]) == ["octo/lib", "octo_ent/app"]
    for bad in ("octo", "octo/app/x", "../app", "octo/..", "-x/app", ""):
        assert "not a repository" in _refuses(repo_add, home, bad, None), bad
    assert "not a GitHub login" in _refuses(repo_add, home, "octo/app", "bad login")
    assert "not registered" in _refuses(repo_remove, home, "octo/none")
    for slots in (0, 65, True, "4"):
        assert "slots" in _refuses(update_config, home, lambda c, s=slots: c.update(slots=s))
    assert load_config(home)["slots"] == 2  # a refused change writes nothing
    # fair share: a repo's weight and cap; changing them keeps the repo's account
    said = repo_add(home, "octo/lib", None, weight=3, cap=2)
    assert said.endswith("gh login of ci-bot (weight 3, at most 2 runners)"), said
    repo_add(home, "octo/lib", None, cap=0)
    assert load_config(home)["repos"]["octo/lib"] == {"account": "ci-bot", "weight": 3}
    for key, bad in (("weight", 0), ("weight", 101), ("cap", 65), ("cap", -1)):
        refused = _refuses(lambda k=key, b=bad: repo_add(home, "octo/lib", None, **{k: b}))
        assert f"repos.octo/lib.{key}" in refused, refused
    for key, bad in (("pr_cap", 0), ("aging_s", 59), ("aging_s", True)):
        assert key in _refuses(update_config, home, lambda c, k=key, b=bad: c.update({k: b}))
    cfg = load_config(home)
    assert (cfg["pr_cap"], cfg["aging_s"], cfg["repos"]["octo/lib"]["weight"]) == (4, 600, 3)
    write_json(home.config, {"schema": SCHEMA + 1})
    assert "update the pool" in _refuses(load_config, home)
    home.config.write_text("{not json", encoding="utf-8")
    assert "not valid JSON" in _refuses(load_config, home)
    saved = os.environ.get(HOME_ENV)
    os.environ[HOME_ENV] = str(root / "from-env")
    try:
        assert resolve_home().root == (root / "from-env").resolve()
        assert resolve_home(str(root / "from-arg")).root == (root / "from-arg").resolve()
    finally:
        if saved is None:
            os.environ.pop(HOME_ENV, None)
        else:
            os.environ[HOME_ENV] = saved


def _test_client() -> None:
    asked = []

    def gh_auth(cmd, **kwargs):
        asked.append(cmd)
        return _fake_gh_auth(cmd, **kwargs)

    clock = _Clock(1000.0)
    tokens = Tokens(run=gh_auth, clock=clock)
    assert tokens.get("alice") == "TOKEN-alice" == tokens.get("alice") and len(asked) == 1
    assert asked[0][1:] == ["auth", "token", "--hostname", "github.com", "--user", "alice"]
    assert "gh auth login" in _refuses(tokens.get, "nobody")
    fake = FakeGitHub({"alice/app": "alice", "bob/lib": "bob"})
    fake.add_run("alice/app", 11, "2026-01-01T00:00:00Z")
    gh = GitHub(tokens, transport=fake, clock=clock)
    runs = "/repos/alice/app/actions/runs"
    params = {"status": "queued", "per_page": 100}
    first = gh.call("GET", runs, "alice", params=params, cache=True)
    assert gh.call("GET", runs, "alice", params=params, cache=True) == first
    assert gh.not_modified == 1 and first["workflow_runs"][0]["id"] == 11
    gh.call("GET", "/repos/bob/lib/actions/runs", "bob", params=params)  # with bob's token
    errors = []
    runners = "/repos/alice/app/actions/runners"
    limited = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "2000"}
    fake.errors[("GET", runners)] = [(403, limited, {"message": "API rate limit exceeded"})]
    errors.append(_api_error(gh.call, "GET", runners, "alice"))
    assert errors[-1].status == 403 and errors[-1].rate and errors[-1].retry_at == 2000.0
    sent = len(fake.calls)
    errors.append(_api_error(gh.call, "GET", runs, "alice"))
    assert errors[-1].rate and len(fake.calls) == sent  # blocked here, without asking GitHub
    gh.call("GET", "/repos/bob/lib/actions/runs", "bob", params=params)  # other accounts go on
    clock.t = 2001.0
    jit = "/repos/alice/app/actions/runners/generate-jitconfig"
    fake.errors[("POST", jit)] = [(429, {"retry-after": "7"}, {"message": "slow down"})]
    errors.append(_api_error(gh.call, "POST", jit, "alice", body={}))
    assert errors[-1].rate and errors[-1].retry_at == 2008.0
    clock.t = 2009.0
    secondary = {"message": "You have exceeded a secondary rate limit"}
    fake.errors[("POST", jit)] = [(403, {}, secondary)]
    errors.append(_api_error(gh.call, "POST", jit, "alice", body={}))
    assert errors[-1].rate and errors[-1].retry_at == 2069.0
    clock.t = 2070.0
    fake.errors[("POST", jit)] = [(422, {}, {"message": "spammed"})]
    errors.append(_api_error(gh.call, "POST", jit, "alice", body={}))
    assert not errors[-1].rate and str(errors[-1]) == f"POST {jit}: HTTP 422 (spammed)"
    assert not [err for err in errors if "TOKEN-" in str(err)]


def _test_install(root: Path) -> None:
    sha = "ab" * 32
    plat = "osx-arm64"
    name = f"actions-runner-{plat}-2.338.2.tar.gz"
    url = DOWNLOAD_PREFIX + "v2.338.2/" + name
    asset = {"name": name, "browser_download_url": url, "digest": "sha256:" + sha}
    release = {"tag_name": "v2.338.2", "assets": [asset], "body": ""}
    want = {"version": "2.338.2", "name": name, "url": url, "sha256": sha}
    assert release_asset(release, plat) == want
    marker = f"<!-- BEGIN SHA {plat} -->{sha}<!-- END SHA {plat} -->"
    only_marker = {**release, "assets": [{**asset, "digest": None}], "body": marker}
    assert release_asset(only_marker, plat)["sha256"] == sha
    assert release_asset({**release, "body": marker}, plat)["sha256"] == sha
    other = {**release, "body": marker.replace(sha, "cd" * 32)}
    assert "two different" in _refuses(release_asset, other, plat)
    assert "no SHA-256" in _refuses(release_asset, {**only_marker, "body": ""}, plat)
    moved = {**release, "assets": [{**asset, "browser_download_url": "https://example.com/r.tgz"}]}
    assert "unexpected download URL" in _refuses(release_asset, moved, plat)
    assert "has no" in _refuses(release_asset, release, "osx-x64")
    assert runner_platform("Darwin", "arm64") == "osx-arm64"
    assert runner_platform("Darwin", "x86_64") == "osx-x64"
    assert "no build" in _refuses(runner_platform, "Windows", "AMD64")
    assert version_tuple("2.339.0") > version_tuple("2.338.2") > version_tuple(None)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        script = b"#!/bin/sh\nexit 0\n"
        info = tarfile.TarInfo("run.sh")
        info.size, info.mode = len(script), 0o755
        tar.addfile(info, io.BytesIO(script))
    blob = buf.getvalue()

    def opener(request, timeout=None):
        assert request.full_url.startswith(DOWNLOAD_PREFIX), request.full_url
        return io.BytesIO(blob)

    home = Home(root / "manual")
    good = {**want, "sha256": hashlib.sha256(blob).hexdigest()}
    install_base(home, good, opener)
    assert base_version(home) == "2.338.2" and (home.base / "run.sh").is_file()
    progress: list = []  # the pool's keepalive: after each MiB, and after unpacking
    install_base(Home(root / "progress"), good, opener, progress=lambda: progress.append(1))
    assert len(progress) == 2, progress  # one chunk (the tarball is small), then tar
    bad = {**good, "version": "2.339.0", "sha256": sha}
    assert "refusing it" in _refuses(install_base, home, bad, opener)
    assert base_version(home) == "2.338.2" and not (home.root / "base.new").exists()

    # serve installs a missing runner by itself, from the latest release, checked the same way
    here = runner_platform()
    name = f"actions-runner-{here}-2.338.2.tar.gz"
    fake = FakeGitHub({})
    digest = "sha256:" + good["sha256"]
    download_url = DOWNLOAD_PREFIX + "v2.338.2/" + name
    fake.release = {
        "tag_name": "v2.338.2",
        "body": "",
        "assets": [{"name": name, "browser_download_url": download_url, "digest": digest}],
    }
    auto = Home(root / "auto")
    pool = _pool(auto, fake, _Clock(5000.0), FakeLauncher(), opener=opener)
    pool.last_base_check = None
    pool.startup()
    pool.pass_once()
    assert base_version(auto) == "2.338.2"


def _test_pool(root: Path, capture: _Capture) -> None:
    clock = _Clock(10000.0)
    home = Home(root)
    repo_add(home, "alice/app", None)
    repo_add(home, "bob/lib", None)
    repo_add(home, "carol/tool", "ci-bot")  # served with another login's token
    update_config(home, lambda cfg: cfg.update(slots=3, idle_timeout_s=600))
    _fake_base(home)
    fake = FakeGitHub({"alice/app": "alice", "bob/lib": "bob", "carol/tool": "ci-bot"})
    fake.add_run("alice/app", 1, "2026-01-01T00:00:02Z", jobs=2, event="pull_request", pr=7)
    fake.add_run("bob/lib", 2, "2026-01-01T00:00:01Z")
    fake.add_run("alice/app", 3, "2026-01-01T00:00:00Z", fork=True, event="pull_request")
    fake.add_run("alice/app", 4, "2026-01-01T00:00:00Z", event="pull_request_target")
    fake.add_run("bob/lib", 5, "2026-01-01T00:00:00Z", labels=["self-hosted", "macOS"])
    fake.add_run("bob/lib", 6, "2026-01-01T00:00:00Z", labels=["self-hosted", run_label(6), "gpu"])
    launcher = FakeLauncher(fake)
    outside = {"PATH": "/usr/bin:/bin", "HOME": "/Users/me", "GH_TOKEN": "x", "SSH_AUTH_SOCK": "/s"}
    pool = _pool(home, fake, clock, launcher, outside)
    pool.startup()

    first = pool.pass_once()
    # up to the 3 slots, by fair share: bob/lib's job (the oldest), alice/app's (its turn), then
    # alice/app's other one (the only one left); forks, pull_request_target and other labels: none
    started = [(repo, body["labels"][1]) for repo, body in fake.posts]
    want = [("bob/lib", "factory-run-2"), ("alice/app", "factory-run-1")]
    assert started == want + want[1:], started
    pool_id = pool.cfg["pool_id"]
    for _, body in fake.posts:
        assert body["labels"][0] == "self-hosted" and len(body["labels"]) == 2, body
        assert body["runner_group_id"] == 1 and body["work_folder"] == "_work", body
        assert body["name"].startswith(f"factory-{pool_id}-"), body
    # each queued job has a runner waiting for it, so none counts as queued without one
    assert (first["live"], first["busy"], first["queued"], first["queue"]) == (3, 0, 0, []), first
    bob1, alice0, alice1 = (body["name"] for _, body in fake.posts)
    assert sorted(r.slot for r in pool.live()) == ["s01", "s02", "s03"]
    env = launcher.envs[pool.runners[bob1].slot]
    assert env["ACTIONS_RUNNER_INPUT_JITCONFIG"].startswith("JITSECRET")
    assert env["HOME"] == str(home.slots / "s01" / "_home") and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert not {"GH_TOKEN", "SSH_AUTH_SOCK"} & set(env), env
    assert label_names(fake.registration(bob1)) == ["self-hosted", "factory-run-2"]

    # nothing changed: the GETs come back 304, nothing new starts, each refusal is logged once
    pool.pass_once()
    assert len(fake.posts) == 3 and pool.gh.not_modified >= 4, pool.gh.not_modified
    for needle in ("refused run 3:", "refused run 4:", "job 60 of run 6"):
        assert sum(needle in line for line in capture.lines) == 1, needle
    # the cap holds: a new job waits for a free slot
    fake.add_job(2, "2026-01-01T00:05:00Z")
    beat = pool.pass_once()
    assert len(fake.posts) == 3 and beat["queued"] == 1, beat
    assert beat["launcher"] == "host", beat  # its runners run on the Mac
    want = {"repo": "bob/lib", "run": 2, "job": 21, "pr": None, "branch": "main"}
    assert {key: beat["queue"][0][key] for key in want} == want, beat["queue"]

    # bob1 takes its job and exits: one ledger line, its _diag kept, its slot used again
    fake.take(2, 0, bob1)
    pool.pass_once()
    assert pool.runners[bob1].job_id == 20 and pool.runners[bob1].assigned
    diag = home.slots / "s01" / "_diag"
    diag.mkdir()
    (diag / "Worker_20260101-001000-utc.log").write_text("job 20\n", encoding="utf-8")
    fake.complete(2, 0, "success")
    launcher.exit("s01", 0)
    runner = pool.runners[bob1]
    pool.pass_once()
    line = _ledger(home)[-1]
    assert (line["runner"], line["job"], line["conclusion"], line["exit_code"]) == (
        bob1,
        20,
        "success",
        0,
    ), line
    assert (pool.log_dir(runner) / "_diag" / "Worker_20260101-001000-utc.log").is_file()
    assert not (home.hooks / f"{bob1}.sh").exists()
    bob2 = fake.posts[-1][1]["name"]
    assert len(fake.posts) == 4 and fake.posts[-1][0] == "bob/lib"
    assert pool.runners[bob2].slot == "s01"

    # a job that starts and ends between two passes is still found, by its runner's name
    fake.take(1, 1, alice0)
    fake.complete(1, 1, "failure")
    launcher.exit(pool.runners[alice0].slot, 1)
    pool.pass_once()
    line = _ledger(home)[-1]
    got = (line["runner"], line["job"], line["conclusion"], line["pr"], line["event"])
    assert got == (alice0, 11, "failure", 7, "pull_request"), line
    assert len(fake.posts) == 4

    # its run is cancelled: the runner still waiting for it is deregistered, then stopped
    fake.cancel(1)
    gone = pool.runners[alice1]
    pool.pass_once()
    line = _ledger(home)[-1]
    got = (line["runner"], line["conclusion"], line["stopped"])
    assert got == (alice1, "unused", "its run has completed"), line
    delete = ("DELETE", f"/repos/alice/app/actions/runners/{gone.runner_id}")
    assert launcher.stops[-1] == (gone.slot, delete), launcher.stops

    # idle too long, but GitHub gives it a job just as the pool retires it: kept, not stopped
    fake.registration(bob2)["busy"] = True
    clock.t += 601
    pool.pass_once()
    assert pool.runners[bob2].assigned and not pool.runners[bob2].exited
    assert [slot for slot, _ in launcher.stops] == [gone.slot]
    assert len(fake.posts) == 4  # it still stands for the job it is taking

    # idle too long with no job: retired as unused, and its run's job gets a new runner
    fake.add_run("alice/app", 8, "2026-01-01T00:30:00Z")
    pool.pass_once()
    idle = fake.posts[-1][1]["name"]
    assert len(fake.posts) == 5
    clock.t += 601
    pool.pass_once()
    line = _ledger(home)[-1]
    assert (line["runner"], line["conclusion"], line["stopped"]) == (
        idle,
        "unused",
        "no job after 600 s",
    ), line
    assert len(fake.posts) == 6 and fake.posts[-1][1]["labels"][1] == "factory-run-8"

    # a 422 from generate-jitconfig backs that repo off; the other repos carry on
    fake.add_run("bob/lib", 9, "2026-01-01T00:40:00Z")
    jit = "/repos/bob/lib/actions/runners/generate-jitconfig"
    fake.errors[("POST", jit)] = [(422, {}, {"message": "spammed"})]
    pool.pass_once()
    assert len(fake.posts) == 6 and pool.jit_backoff["bob/lib"][0] > clock.t
    before = len(fake.calls)
    pool.pass_once()  # its queue is still read; only starting runners waits
    assert ("POST", jit) not in fake.calls[before:]
    assert [call for call in fake.calls[before:] if call[1].startswith("/repos/bob/lib/")]
    clock.t += BACKOFF_MIN_S + 1
    fake.errors[("POST", jit)] = [(422, {}, {"message": "spammed"})]
    pool.pass_once()  # a second refusal in a row doubles the wait
    assert pool.jit_backoff["bob/lib"][0] - clock.t == 2 * BACKOFF_MIN_S
    clock.t += 2 * BACKOFF_MIN_S + 1
    pool.pass_once()
    assert len(fake.posts) == 7 and fake.posts[-1][1]["labels"][1] == "factory-run-9"
    assert "bob/lib" not in pool.jit_backoff

    # every call carried its repo's own token (FakeGitHub checks); no token or JIT config is
    # ever written to the pool's files
    assert [call for call in fake.calls if call[1].startswith("/repos/carol/tool/")]
    for path in home.root.rglob("*"):
        if path.is_file() and home.base not in path.parents:
            data = path.read_bytes()
            assert b"TOKEN-" not in data and b"JITSECRET" not in data, path
    assert len(_ledger(home)) == 4
    assert read_json(home.heartbeat, {})["live"] == len(pool.live())


def _want(repo: str, job_id: int, group="pr:1", rank=DEFAULT_RANK, main=False, queued=0.0) -> Want:
    return Want(repo, group, rank, main, float(queued), job_id)


def _per_repo(picked: list) -> dict:
    return dict(Counter(want.repo for want in picked))


def _test_allocate() -> None:
    names = ("a/x", "b/y", "c/z")
    flat = {repo: {"account": "o"} for repo in names}
    busy = [
        _want(repo, k * 100 + i, f"pr:{i % 3}", queued=i)
        for k, repo in enumerate(names)
        for i in range(10)
    ]
    # across repos: weighted max-min, using idle shares, never stopping a running job
    assert _per_repo(allocate(8, [], busy, flat, 4, 600, 0.0)) == {"a/x": 3, "b/y": 3, "c/z": 2}
    weighted = {"a/x": {"account": "o", "weight": 2}, "b/y": {"account": "o"}}
    two = [want for want in busy if want.repo != "c/z"]
    assert _per_repo(allocate(6, [], two, weighted, 4, 600, 0.0)) == {"a/x": 4, "b/y": 2}
    alone = [want for want in busy if want.repo == "a/x"]
    assert _per_repo(allocate(8, [], alone, flat, 4, 600, 0.0)) == {"a/x": 8}
    lone_pr = [_want("a/x", i, "pr:7") for i in range(6)]
    assert len(allocate(8, [], lone_pr, flat, 4, 600, 0.0)) == 6  # over its cap: nobody else waits
    full = [("a/x", "pr:0")] * 6
    assert _per_repo(allocate(8, full, busy, flat, 4, 600, 0.0)) == {"b/y": 1, "c/z": 1}
    capped = {**flat, "a/x": {"account": "o", "cap": 2}}
    assert _per_repo(allocate(8, [], alone, capped, 4, 600, 0.0)) == {"a/x": 2}
    assert allocate(2, [("a/x", "pr:0")] * 2, busy, flat, 4, 600, 0.0) == []
    assert allocate(1, [("a/x", "pr:0")] * 2, busy, flat, 4, 600, 0.0) == []  # slots went down
    late = _want("b/y", 999, queued=50)  # nothing running: it goes before older jobs of a/x
    assert allocate(8, [("a/x", "pr:0")] * 7, [*alone, late], flat, 4, 600, 0.0) == [late]

    # within a repo, one key at a time
    def first(live: list, wants: list, now: float = 0.0) -> int:
        return allocate(len(live) + 1, live, wants, flat, 4, 600, now)[0].job_id

    p0_old = _want("a/x", 1, "pr:1", rank=0, queued=0)
    p3_new = _want("a/x", 2, "pr:2", rank=3, queued=5)
    assert first([("a/x", "pr:1")] * 4, [p0_old, p3_new]) == 2  # 1. under the PR cap first
    assert first([("a/x", "pr:1")] * 3, [p0_old, p3_new]) == 1  # (below it, P0 goes first)
    p2_old = _want("a/x", 3, "pr:1", rank=2, queued=0)
    p1_new = _want("a/x", 4, "pr:2", rank=1, queued=5)
    assert first([], [p2_old, p1_new]) == 4  # 2. the P label
    push = _want("a/x", 5, "branch:main", rank=2, main=True, queued=5)
    assert first([], [p2_old, push]) == 5  # 3. pushes to the default branch
    turn = _want("a/x", 6, "pr:2", rank=2, queued=5)
    assert first([("a/x", "pr:1")], [p2_old, turn]) == 6  # 4. PRs take turns
    twin = _want("a/x", 7, "pr:1", rank=2, queued=0)
    assert first([], [twin, p2_old, _want("a/x", 8, "pr:1", queued=1)]) == 3  # 5. age, then ID
    p3_old = _want("a/x", 9, "pr:9", rank=3, queued=0)
    p0_now = _want("a/x", 10, "pr:10", rank=0, queued=1800)
    assert first([], [p3_old, p0_now], now=1799.0) == 10  # aged two classes, not three
    assert first([], [p3_old, p0_now], now=1800.0) == 9  # three: as good as P0, and older

    # the order of the input never changes the answer
    mixed = busy + [p0_old, p3_new, push, turn, p3_old]
    want = allocate(12, full, mixed, capped, 4, 600, 900.0)
    shuffler = random.Random(15)
    for _ in range(20):
        shuffler.shuffle(mixed)
        assert allocate(12, full, mixed, capped, 4, 600, 900.0) == want

    # within one call: each pick counts toward its PR, so PRs take turns and the cap binds
    two = [_want("a/x", i, "pr:1", queued=i) for i in range(10)]
    two += [_want("a/x", 100 + i, "pr:2", queued=50 + i) for i in range(10)]
    turns = [want.group for want in allocate(8, [], two, flat, 4, 600, 0.0)]
    assert turns == ["pr:1", "pr:2"] * 4, turns
    near = [_want("a/x", i, "pr:1", rank=0, queued=i) for i in range(5)]
    near += [_want("a/x", 100 + i, "pr:2", rank=1, queued=50 + i) for i in range(5)]
    order = [want.group for want in allocate(8, [("a/x", "pr:1")] * 3, near, flat, 4, 600, 0.0)]
    assert order == ["pr:1", "pr:2", "pr:2", "pr:2", "pr:2"], order  # pr:1 reaches its cap
    # the P label goes before a push to the default branch
    push2 = _want("a/x", 1, "branch:main", rank=2, main=True, queued=0)
    assert first([], [push2, _want("a/x", 2, "pr:2", rank=1, queued=5)]) == 2
    # aging stops at P0: two jobs both aged past it are equal, so turns decide (pr:2 has none)
    old0, old3 = (
        _want("a/x", 1, "pr:1", rank=0, queued=0),
        _want("a/x", 2, "pr:2", rank=3, queued=0),
    )
    assert first([("a/x", "pr:1")], [old0, old3], now=2400.0) == 2
    # the input order never changes the answer within repos either (no repo cap here)
    groups = [f"pr:{n}" for n in range(1, 6)] + ["branch:main"]
    mixed = []
    for i in range(40):
        group = groups[i % len(groups)]
        repo = "a/x" if i % 3 else "b/y"
        main = group == "branch:main"
        mixed.append(_want(repo, i, group, rank=i % 4, main=main, queued=float(i % 7)))
    live = [("a/x", "pr:1"), ("a/x", "pr:1"), ("b/y", "pr:2")]
    want = allocate(12, live, mixed, flat, 2, 600, 900.0)
    for _ in range(30):
        shuffler.shuffle(mixed)
        assert allocate(12, live, mixed, flat, 2, 600, 900.0) == want

    assert run_group({"pull_requests": [{"number": 7}], "head_branch": "x"}) == "pr:7"
    assert run_group({"pull_requests": [], "head_branch": "main"}) == "branch:main"
    assert label_rank([{"name": "bug"}, {"name": "P3"}, {"name": "P1"}]) == 1
    assert label_rank([{"name": "P9"}, {"name": "p0"}, "P0"]) == DEFAULT_RANK == label_rank(None)
    assert epoch("2026-01-01T00:00:02Z", 5.0) == 1767225602.0
    assert epoch(None, 5.0) == 5.0 == epoch("soon", 5.0)
    legacy = Runner("factory-abc123-5-beef", "a/x", "a", 5, 1, "s01", pr=9)
    assert (
        legacy.share_group() == "pr:9"
        and Runner.load({**asdict(legacy), "pr": None}).share_group() == "run:5"
    )


def _simulate(slots: int, arrivals: dict, repos: dict, ticks: int, aging_s=600, tick_s=60.0):
    """A pool where every job takes one tick: `arrivals` maps a tick to the Wants queued then.
    Returns each job's start tick, and the live runners per repo at each tick."""
    started: dict = {}
    held: list = []
    pending: list = []
    running: list = []
    for tick in range(ticks):
        running = [(end, want) for end, want in running if end > tick]
        pending += arrivals.get(tick, [])
        live = [(want.repo, want.group) for _, want in running]
        for want in allocate(slots, live, pending, repos, 4, aging_s, tick * tick_s):
            pending.remove(want)
            running.append((tick + 1, want))
            started[want.job_id] = tick
        held.append(Counter(want.repo for _, want in running))
    return started, held


def _test_simulation() -> None:
    # a burst of 40 jobs across 3 repos drains in ceil(40 / slots) job times
    repos = {name: {"account": "o"} for name in ("a/x", "b/y", "c/z")}
    burst = []
    for k, (repo, jobs) in enumerate((("a/x", 20), ("b/y", 12), ("c/z", 8))):
        burst += [_want(repo, k * 100 + i, f"pr:{i // 3}", rank=i % 4) for i in range(jobs)]
    for slots in (5, 6, 8):
        started, _ = _simulate(slots, {0: burst}, repos, 20)
        assert len(started) == 40 and max(started.values()) + 1 == math.ceil(40 / slots), slots

    # a P3 job facing a steady stream of new P0 jobs starts once it has aged to P0: within
    # 3 x aging_s plus one job time, and not before
    stream: dict = {0: [_want("a/x", 1, "pr:1", rank=3, queued=0)]}
    for tick in range(60):
        group = f"pr:{100 + tick}"
        new = [_want("a/x", 1000 + tick * 2 + i, group, rank=0, queued=tick * 60) for i in (0, 1)]
        stream.setdefault(tick, []).extend(new)
    started, _ = _simulate(2, stream, {"a/x": {"account": "o"}}, 60)
    assert 3 * 600 - 60 < started[1] * 60 <= 3 * 600 + 60, started[1]

    # a light repo next to a heavy one (weights 1 and 10) always runs something
    heavy = {"a/x": {"account": "o", "weight": 10}, "b/y": {"account": "o"}}
    endless = {
        tick: [
            _want(repo, k * 10000 + tick * 10 + i, "pr:1", queued=tick * 60)
            for k, repo in enumerate(heavy)
            for i in range(8)
        ]
        for tick in range(20)
    }
    _, held = _simulate(8, endless, heavy, 20)
    assert all(count["b/y"] >= 1 and sum(count.values()) == 8 for count in held), held


def _test_fair_pool(root: Path, capture: _Capture) -> None:
    """The pool starts runners in fair-share order, from the PRs' P labels, read conditionally."""
    clock = _Clock(40000.0)
    home = Home(root)
    repo_add(home, "alice/app", None)
    repo_add(home, "bob/lib", None)
    update_config(home, lambda cfg: cfg.update(slots=3))
    _fake_base(home)
    fake = FakeGitHub({"alice/app": "alice", "bob/lib": "bob"})
    fake.add_run("alice/app", 1, "2026-01-01T00:00:00Z", jobs=3, event="pull_request", pr=7)
    fake.add_run("alice/app", 2, "2026-01-01T00:00:05Z", jobs=2, event="pull_request", pr=8)
    fake.add_run("bob/lib", 3, "2026-01-01T00:00:10Z", jobs=2)  # a push to main
    fake.labels[("alice/app", 7)] = ["P3"]
    fake.labels[("alice/app", 8)] = ["bug", "P0"]
    launcher = FakeLauncher(fake)
    pool = _pool(home, fake, clock, launcher)
    pool.startup()
    pool.pass_once()
    # alice's oldest job first, from its P0 PR; then bob's turn; then alice's P0 PR again
    started = [(repo, body["labels"][1]) for repo, body in fake.posts]
    run2, run3 = ("alice/app", "factory-run-2"), ("bob/lib", "factory-run-3")
    assert started == [run2, run3, run2], started
    groups = sorted(r.group for r in pool.live())
    assert groups == ["branch:main", "pr:8", "pr:8"], groups
    assert ("GET", "/repos/bob/lib") in fake.calls  # its default branch

    # a slot frees up: the labels are read again, and come back 304
    taken = fake.posts[0][1]["name"]
    fake.take(2, 0, taken)
    fake.complete(2, 0, "success")
    launcher.exit(pool.runners[taken].slot, 0)
    pool.pass_once()
    assert fake.posts[-1][1]["labels"][1] == "factory-run-1", fake.posts[-1]
    assert "/repos/alice/app/issues/7/labels" in fake.unchanged, fake.unchanged

    # a label read that fails counts as P2 and is logged once
    labels7 = ("GET", "/repos/alice/app/issues/7/labels")
    fake.errors[labels7] = [(500, {}, {"message": "boom"})] * 2

    def bob_done(index: int, name: str) -> None:
        fake.take(3, index, name)
        fake.complete(3, index, "success")
        launcher.exit(pool.runners[name].slot, 0)
        pool.pass_once()

    bob_done(0, fake.posts[1][1]["name"])  # its slot goes to bob's next job; PR 7's labels fail
    assert pool.ranks[("alice/app", 7)] == DEFAULT_RANK and fake.posts[-1][0] == "bob/lib"
    bob_done(1, fake.posts[-1][1]["name"])  # they fail again: still one warning
    assert sum("PR #7 counts as P2" in line for line in capture.lines) == 1, capture.lines
    assert not fake.errors[labels7] and fake.posts[-1][1]["labels"][1] == "factory-run-1"

    # a repo backing off its runner starts sits out of fair share: its share goes to the others
    # in the same pass, instead of a slot it can't use
    for r in pool.live():
        launcher.exit(r.slot, 0)
    fake.add_run("alice/app", 9, "2026-01-01T00:01:00Z", jobs=3)
    fake.add_run("bob/lib", 10, "2026-01-01T00:00:00Z", jobs=3)
    pool.jit_backoff["bob/lib"] = (clock.t + 300, 1)
    before = len(fake.posts)
    pool.pass_once()
    added = fake.posts[before:]
    assert [repo for repo, _ in added] == ["alice/app"] * 3, added  # all 3 free slots
    pool.jit_backoff.clear()

    # a push to another branch than the default isn't a push to the default branch
    fake.branches["bob/lib"] = "trunk"
    pool.branches = {}
    assert not pool.main_push("bob/lib", "bob", {"event": "push", "head_branch": "main"})
    assert pool.main_push("bob/lib", "bob", {"event": "push", "head_branch": "trunk"})
    assert not pool.main_push("bob/lib", "bob", {"event": "pull_request", "head_branch": "trunk"})


def _test_cache_kept(root: Path) -> None:
    """A PR's labels are read only when a slot frees up, maybe many passes later. Their ETag is
    kept meanwhile, so that read is still conditional; a reply unused for CACHE_KEEP_S goes."""
    clock = _Clock(60000.0)
    home = Home(root)
    repo_add(home, "alice/app", None)
    update_config(home, lambda cfg: cfg.update(slots=1))
    _fake_base(home)
    fake = FakeGitHub({"alice/app": "alice"})
    fake.add_run("alice/app", 1, "2026-01-01T00:00:00Z", jobs=3, event="pull_request", pr=7)
    fake.labels[("alice/app", 7)] = ["P1"]
    launcher = FakeLauncher(fake)
    pool = _pool(home, fake, clock, launcher)
    pool.startup()
    labels = ("GET", "/repos/alice/app/issues/7/labels")
    pool.pass_once()  # a runner starts: the labels are read
    for _ in range(3):  # the pool is full: no reads, and each pass prunes the cache
        clock.t += 10
        pool.pass_once()
    assert fake.calls.count(labels) == 1 and len(fake.posts) == 1
    name = fake.posts[0][1]["name"]
    fake.take(1, 0, name)
    fake.complete(1, 0, "success")
    launcher.exit(pool.runners[name].slot, 0)
    clock.t += 10
    pool.pass_once()  # the freed slot: the labels again, answered 304
    assert fake.calls.count(labels) == 2 and fake.unchanged.count(labels[1]) == 1, fake.unchanged
    assert len(fake.posts) == 2
    clock.t += CACHE_KEEP_S + 1
    pool.gh.prune()
    cached = [url for _, url in pool.gh.cache]
    assert not [url for url in cached if labels[1] in url], cached


def _test_unlisted(root: Path, capture: _Capture) -> None:
    """A run GitHub's status-filtered lists leave out is found among the newest runs, read
    unfiltered every UNLISTED_S, and is then read on its own each pass until it completes."""
    clock = _Clock(70000.0)
    home = Home(root)
    repo_add(home, "alice/app", None)
    update_config(home, lambda cfg: cfg.update(slots=1))
    _fake_base(home)
    fake = FakeGitHub({"alice/app": "alice"})
    fake.add_run("alice/app", 1, "2026-01-01T00:00:00Z", jobs=2, event="pull_request", pr=7)
    fake.add_run("alice/app", 2, "2026-01-01T00:00:01Z")
    fake.cancel(2)  # completed: left alone
    fake.add_run("alice/app", 3, "2026-01-01T00:00:02Z")
    fake.take(3, 0, "someone-else")  # in progress, and its list has it: not read on its own
    fake.unlisted.update({1, 2})
    launcher = FakeLauncher(fake)
    pool = _pool(home, fake, clock, launcher)
    pool.startup()
    page = ("GET", "/repos/alice/app/actions/runs")
    alone = [("GET", f"/repos/alice/app/actions/runs/{n}") for n in (1, 2, 3)]
    logged = len(capture.lines)

    def pass_and_count() -> tuple:
        clock.t += 10
        pool.pass_once()
        return fake.calls.count(page), [fake.calls.count(call) for call in alone]

    # the two status lists and the newest runs: run 1's first job gets the one slot; no warning
    # yet, in case the lists were just a moment behind
    assert pass_and_count() == (3, [1, 0, 0]), fake.calls
    assert [body["labels"][1] for _, body in fake.posts] == [run_label(1)], fake.posts
    assert not [line for line in capture.lines[logged:] if "leaves it out" in line]
    first = fake.posts[0][1]["name"]
    assert (pool.runners[first].pr, pool.runners[first].group) == (7, "pr:7"), pool.runners
    fake.take(1, 0, first)
    fake.complete(1, 0, "success")
    launcher.exit(pool.runners[first].slot, 0)
    # within UNLISTED_S the newest runs aren't read again, but run 1 is, on its own: its second
    # job gets the slot the first one freed
    assert pass_and_count() == (5, [2, 0, 0]), fake.calls
    assert [body["labels"][1] for _, body in fake.posts] == [run_label(1)] * 2, fake.posts
    second = fake.posts[1][1]["name"]
    fake.take(1, 1, second)
    assert pass_and_count() == (7, [3, 0, 0]), fake.calls
    # both new reads are conditional: run 1 didn't change, so its read came back 304
    assert "/repos/alice/app/actions/runs/1" in fake.unchanged, fake.unchanged
    assert [url for _, url in pool.gh.cache if url.endswith("/actions/runs?per_page=30")]
    fake.complete(1, 1, "success")
    launcher.exit(pool.runners[second].slot, 0)
    assert pass_and_count() == (9, [4, 0, 0]), fake.calls  # completed: let go
    clock.t += UNLISTED_S
    assert pass_and_count() == (12, [4, 0, 0]), fake.calls  # completed in the newest runs too
    warned = "alice/app: run 1 is in_progress, but GitHub's list of in-progress runs leaves it out"
    assert sum(warned in line for line in capture.lines[logged:]) == 1, capture.lines[logged:]
    # a followed run that is deleted is let go, and the repo's queue is still read
    fake.add_run("alice/app", 4, "2026-01-01T00:00:03Z", labels=["ubuntu-latest"])
    fake.unlisted.add(4)
    clock.t += UNLISTED_S
    pass_and_count()
    del fake.runs[4]
    pass_and_count()
    assert pool.views["alice/app"].ok, pool.views
    pass_and_count()  # the 404 let it go: it isn't read again
    assert fake.calls.count(("GET", "/repos/alice/app/actions/runs/4")) == 2, fake.calls

    # an unlisted fork run is refused like a listed one; an unlisted run already in progress
    # when the newest runs are read is followed too
    fake.add_run("alice/app", 5, "2026-01-01T00:00:04Z", fork=True, event="pull_request")
    fake.add_run("alice/app", 6, "2026-01-01T00:00:05Z", jobs=2)
    fake.take(6, 0, "someone-else")
    fake.unlisted.update({5, 6})
    clock.t += UNLISTED_S
    pass_and_count()
    later = fake.posts[2:]
    assert [body["labels"][1] for _, body in later] == [run_label(6)], fake.posts
    assert sum("refused run 5:" in line for line in capture.lines[logged:]) == 1

    # a failed read of the newest runs, or of a followed run, doesn't hold up the runs the
    # lists have: it's logged and tried again later; a rate limit still backs the repo off
    update_config(home, lambda cfg: cfg.update(slots=3))

    def labels() -> list:
        return [body["labels"][1] for _, body in fake.posts]

    fake.add_run("alice/app", 7, "2026-01-01T00:00:06Z")
    fake.unlisted.add(7)
    fake.add_run("alice/app", 8, "2026-01-01T00:00:07Z")  # the lists have this one
    bad = (502, {}, {"message": "Bad Gateway"})
    fake.errors[("GET", "/repos/alice/app/actions/runs?per_page=30")] = [bad]
    clock.t += UNLISTED_S
    pass_and_count()
    assert pool.views["alice/app"].ok and run_label(8) in labels(), (pool.views, fake.posts)
    assert ("GET", "/repos/alice/app/actions/runs/7") not in fake.calls
    assert sum("could not read its newest runs" in line for line in capture.lines[logged:]) == 1
    assert {5, 6} <= set(pool.views["alice/app"].runs), pool.views  # still followed
    pages = fake.calls.count(page)
    assert pass_and_count()[0] == pages + 2, fake.calls  # the lists; the page waits a minute
    fake.errors[("GET", "/repos/alice/app/actions/runs/7")] = [bad, bad]
    clock.t += UNLISTED_S
    pass_and_count()  # the newest runs again: run 7 is followed, but its own read fails
    assert pool.views["alice/app"].ok and 7 in pool.unlisted["alice/app"], pool.unlisted
    assert run_label(7) not in labels(), fake.posts
    pass_and_count()  # it fails again: still followed, and not logged again
    assert run_label(7) not in labels() and 7 in pool.unlisted["alice/app"], fake.posts
    pass_and_count()  # tried again: run 7's job gets a runner
    assert run_label(7) in labels(), fake.posts
    assert sum("could not read run 7" in line for line in capture.lines[logged:]) == 1
    fake.add_run("alice/app", 9, "2026-01-01T00:00:08Z")
    fake.unlisted.add(9)
    limited = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(clock.t) + 600)}
    fake.errors[("GET", "/repos/alice/app/actions/runs/9")] = [(403, limited, {"message": "x"})]
    clock.t += UNLISTED_S
    pass_and_count()
    assert not pool.views["alice/app"].ok, pool.views
    # neither new read swallows a rate limit (the account is blocked now: each read raises one)
    pool.unlisted["alice/app"], pool.unlisted_next["alice/app"] = {}, 0.0
    assert _api_error(pool.unlisted_runs, "alice/app", "alice", {}, clock.t).rate  # the page
    pool.unlisted["alice/app"], pool.unlisted_next["alice/app"] = {9: clock.t}, clock.t + 1
    assert _api_error(pool.unlisted_runs, "alice/app", "alice", {}, clock.t).rate  # run 9
    assert run_label(5) not in labels(), fake.posts  # the fork never got a runner


def _test_held(root: Path) -> None:
    """The heartbeat's `held` says how many slots the Mac admits, and why, when that's fewer
    than all of them: only on a pass where a job waits, never while paused, and not when half
    of one slot on battery is still that one."""
    clock = _Clock(90000.0)
    home = Home(root)
    repo_add(home, "alice/app", None)
    update_config(home, lambda cfg: cfg.update(slots=4, battery="half"))
    _fake_base(home)
    fake = FakeGitHub({"alice/app": "alice"})
    fake.add_run("alice/app", 1, "2026-01-01T00:00:00Z", jobs=6)
    pool = _pool(home, fake, clock, FakeLauncher(fake), mac=FakeMac(battery=True))
    pool.startup()
    on_battery = {"slots": 2, "reason": "on battery (battery: half)"}
    assert pool.pass_once()["held"] == on_battery
    pause_pool(home, "test", clock.t)
    assert pool.pass_once()["held"] is None  # paused: nothing is admitted at all
    resume_pool(home)
    assert pool.pass_once()["held"] == on_battery
    fake.cancel(1)
    assert pool.pass_once()["held"] is None  # no job waits
    update_config(home, lambda cfg: cfg.update(slots=1))
    fake.add_run("alice/app", 2, "2026-01-01T00:01:00Z", jobs=2)
    assert pool.pass_once()["held"] is None  # half of one slot is still one


def _test_heartbeat(root: Path) -> None:
    """Under load a pass can take minutes. Here each API request takes 20 s, each clone 30 s and
    each runner stop 30 s, and `status`, read as each slow step starts, still finds the pool up:
    the heartbeat is written once startup can see its runners, after a request at most every
    KEEPALIVE_S, and at the end of a pass, stamped when it's written. A pool that fails in
    startup, or in every pass, still reads as down."""
    clock = _Clock(80000.0)
    home = Home(root)
    repo_add(home, "alice/app", None)
    update_config(home, lambda cfg: cfg.update(slots=3))
    _fake_base(home)
    fake = FakeGitHub({"alice/app": "alice"})
    launcher = FakeLauncher(fake)
    pool = _pool(home, fake, clock, launcher)
    seen: list = []

    def slow(step: float, fn):
        def call(*args, **kwargs):
            seen.append(pool_status(home, clock.t)[0])
            clock.t += step
            return fn(*args, **kwargs)

        return call

    def tool_in(slot: Path, n: int) -> None:  # a tool its job cached, merged back after it
        tool = slot / "_tool" / "tool" / f"1.{n}" / "arm64"
        tool.mkdir(parents=True)
        (tool.parent / "arm64.complete").write_text("", encoding="utf-8")

    # a previous pool left five runners that have since exited: startup cleans up after each
    dead = []
    for n in range(5):
        r = Runner(f"factory-old-{n}", "alice/app", "alice", 9, 9, f"s0{n + 1}")
        tool_in(home.slots / r.slot, n)
        dead.append(asdict(r))
    write_json(home.state, {"runners": dead})
    pool.gh.transport = slow(20, fake)
    pool.run = slow(30, subprocess.run)
    launcher.stop = slow(30, launcher.stop)
    pool.startup()
    fake.add_run("alice/app", 1, "2026-01-01T00:00:00Z", jobs=3)
    pool.pass_once()  # three runner starts
    assert len(fake.posts) == 3 and read_json(home.heartbeat, {})["ts"] == clock.t
    # the runners end, each with a tool to merge back: three clean-ups
    for n, r in enumerate(pool.live()):
        tool_in(home.slots / r.slot, 10 + n)
        launcher.exit(r.slot, 0)
    fake.cancel(1)
    pool.pass_once()
    # three more start, and their run is cancelled before they get a job: they're retired
    fake.add_run("alice/app", 2, "2026-01-01T00:10:00Z", jobs=3)
    pool.pass_once()
    fake.cancel(2)
    pool.pass_once()
    assert len(fake.posts) == 6 and len(launcher.stops) == 3, launcher.stops
    assert len(seen) > 40 and set(seen) == {STATUS_UP}, seen
    # between passes the heartbeat is rewritten every KEEPALIVE_S, and not more often
    written = read_json(home.heartbeat, {})["ts"]
    clock.t += KEEPALIVE_S - 1
    pool.keepalive()
    assert read_json(home.heartbeat, {})["ts"] == written
    clock.t += 1
    pool.keepalive()
    assert read_json(home.heartbeat, {})["ts"] == clock.t
    # a runner update with a slow download: each MiB takes 50 s, and status is read as each
    # arrives
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("run.sh", b"#!/bin/sh\n"), ("bin.dat", os.urandom(4 << 20))):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    blob = buf.getvalue()
    name = f"actions-runner-{runner_platform()}-2.339.0.tar.gz"
    asset = {"name": name, "browser_download_url": DOWNLOAD_PREFIX + "v2.339.0/" + name}
    asset["digest"] = "sha256:" + hashlib.sha256(blob).hexdigest()
    fake.release = {"tag_name": "v2.339.0", "body": "", "assets": [asset]}
    reads: list = []

    class SlowBody(io.BytesIO):
        def read(self, size=-1):
            reads.append(pool_status(home, clock.t)[0])
            clock.t += 50
            return super().read(size)

    pool.opener = lambda request, timeout=None: SlowBody(blob)
    pool.last_base_check = None
    pool.pass_once()
    assert base_version(home) == "2.339.0" and len(reads) > 4, reads
    assert set(reads) == {STATUS_UP}, reads
    # while its passes fail, an update doesn't make it read as up
    pool.passing = False
    clock.t += HEARTBEAT_FRESH_S
    newer = f"actions-runner-{runner_platform()}-2.340.0.tar.gz"
    url = DOWNLOAD_PREFIX + "v2.340.0/" + newer
    newer_asset = {**asset, "name": newer, "browser_download_url": url}
    fake.release = {"tag_name": "v2.340.0", "body": "", "assets": [newer_asset]}
    reads.clear()
    pool.last_base_check = None
    pool.maybe_refresh_base(clock.t)
    assert base_version(home) == "2.340.0" and set(reads) == {STATUS_DOWN}, reads
    pool.passing = True
    pool.beat()
    # a startup that stops four processes no saved runner owns, then cleans up their slots,
    # each step taking 50 s
    orphaned = Home(root / "orphans")
    repo_add(orphaned, "alice/app", None)
    _fake_base(orphaned)
    stray = FakeLauncher(fake)
    stray.alive = {f"s0{n}": 9000 + n for n in range(1, 5)}
    for slot_name in stray.alive:
        (orphaned.slots / slot_name).mkdir(parents=True)
    stops: list = []
    own = _Clock(clock.t)  # its own time: the pool above keeps its heartbeat

    def slow_stop(groups, grace=0.0):
        stops.append(pool_status(orphaned, own.t)[0])
        own.t += 50
        FakeLauncher.stop(stray, groups, grace)

    stray.stop = slow_stop
    starting = _pool(orphaned, fake, own, stray)

    def slow_keep(slot: Path, dest: Path) -> None:
        stops.append(pool_status(orphaned, own.t)[0])
        own.t += 50

    starting.keep_diag = slow_keep
    starting.startup()
    assert len(stops) == 8 and set(stops) == {STATUS_UP}, stops
    # a long wait between passes keeps it fresh, with a write every KEEPALIVE_S, and a signal
    # ends the wait at once
    during: list = []
    writes: list = []

    def sleep_and_look(seconds):
        clock.t += seconds
        during.append(pool_status(home, clock.t)[0])
        writes.append(read_json(home.heartbeat, {})["ts"])

    pool.sleep = sleep_and_look
    pool.idle(300, [], monotonic=lambda: clock.t)
    assert len(during) > 100 and set(during) == {STATUS_UP}, during
    assert len(set(writes)) <= 300 / KEEPALIVE_S + 1, len(set(writes))
    stop: list = []
    start = clock.t

    def sleep_then_signal(seconds):
        clock.t += seconds
        if clock.t - start > 5:
            stop.append("TERM")

    pool.sleep = sleep_then_signal
    pool.idle(300, stop, monotonic=lambda: clock.t)
    assert clock.t - start < 6, clock.t - start
    pool.sleep = clock.sleep
    # a request that fails outright (a 30 s timeout) is progress too: the pool is alive

    def timeout(request, wait):
        clock.t += 30
        raise OSError("timed out")

    pool.gh.transport = timeout
    for _ in range(5):
        _api_error(pool.gh.call, "GET", "/repos/alice/app/actions/runs", "alice")
    assert pool_status(home, clock.t)[0] == STATUS_UP
    # a heartbeat that can't be written never fails the request it runs in
    pool.gh.transport, pool.run = fake, subprocess.run
    home.heartbeat.unlink()
    home.heartbeat.mkdir()
    clock.t += KEEPALIVE_S
    params = {"status": "queued"}
    assert pool.gh.call("GET", "/repos/alice/app/actions/runs", "alice", params=params)
    assert [key for key in pool.noted if key[0] == "heartbeat"], pool.noted
    home.heartbeat.rmdir()
    pool.pass_once()
    # a pool whose passes keep failing reads as down, though each pass makes requests, and the
    # wait between passes doesn't keep it fresh either; a pass that gets through again does

    def broken(now):
        raise RuntimeError("a bug")

    pool.reap = broken
    for _ in range(15):
        clock.t += 10
        with contextlib.suppress(RuntimeError):
            pool.pass_once()
    assert pool_status(home, clock.t)[0] == STATUS_DOWN
    pool.idle(60, [], monotonic=lambda: clock.t)
    assert pool_status(home, clock.t)[0] == STATUS_DOWN
    del pool.reap
    pool.pass_once()
    assert pool_status(home, clock.t)[0] == STATUS_UP

    # a pool that can't see its runners (ps, or docker ps, fails) dies in startup: not up
    class Blind(FakeLauncher):
        def scan(self) -> dict:
            raise PoolError("ps failed")

    blind = Home(root / "blind")
    update_config(blind, lambda cfg: None)
    assert "ps failed" in _refuses(_pool(blind, fake, clock, Blind(fake)).startup)
    assert pool_status(blind, clock.t)[0] == STATUS_DOWN
    # a pool that hasn't started writes no heartbeat
    other = Home(root / "unstarted")
    _pool(other, fake, clock, FakeLauncher(fake)).keepalive()
    assert not other.heartbeat.exists()


def _test_ops(root: Path, capture: _Capture) -> None:
    """status, pause and resume, stats and logs: from the pool's own files, and from the CLI."""
    home = Home(root / "files")
    code, view = pool_status(home, 1000.0)
    assert code == STATUS_DOWN and view["heartbeat_age_s"] is None, view
    assert format_status(view)[0] == "pool: down (it has never run)"
    assert not [line for line in format_status(view) if "queued" in line], format_status(view)
    now = epoch("2026-01-01T00:10:00Z", 0.0)
    issue15 = {"repo": "a/x", "run": 1, "pr": 7, "branch": "local/issue-15"}
    queue = [
        {**issue15, "job": 11, "queued_at": "2026-01-01T00:06:00Z"},
        {**issue15, "job": 12, "queued_at": "2026-01-01T00:08:00Z"},
        {
            "repo": "b/y",
            "run": 2,
            "job": 21,
            "pr": None,
            "branch": "main",
            "queued_at": "2026-01-01T00:09:30Z",
        },
    ]
    beat = {"ts": now - 4, "pid": 42, "version": POOL_VERSION, "slots": 4, "queue": queue}
    beat["paused"] = None  # a pool that knows pause reports it, paused or not
    beat["repos"] = {"a/x": {"ok": True, "error": ""}, "c/z": {"ok": False, "error": "HTTP 502"}}
    write_json(home.heartbeat, beat)
    branch = "local/issue-15"
    saved = [
        Runner("r1", "a/x", "a", 1, 1, "s01", pr=7, group="pr:7", branch=branch, assigned=5.0),
        Runner("r2", "a/x", "a", 1, 2, "s02", pr=7, group="pr:7", branch=branch),
        Runner("r3", "b/y", "b", 3, 3, "s03", pr=9, assigned=1.0),  # saved before groups existed
        Runner("r5", "b/y", "b", 5, 5, "s05", group="branch:dev", assigned=1.0),  # no branch saved
        Runner("r6", "b/y", "b", 6, 6, "s06", assigned=1.0),  # no PR, from before groups
        Runner("r7", "b/y", "b", 7, 7, "s07", group="branch:", assigned=1.0),  # no head branch
        Runner("r4", "b/y", "b", 4, 4, "s04", assigned=1.0, exited=2.0),  # finishing: not live
    ]
    write_json(home.state, {"runners": [asdict(r) for r in saved]})
    code, view = pool_status(home, now)
    assert code == STATUS_UP, view
    assert (view["running"], view["starting"], view["queued"]) == (5, 1, 3), view
    pr7 = view["repos"]["a/x"]["pr:7"]
    got = (pr7["issue"], pr7["running"], pr7["starting"], pr7["queued"], pr7["oldest_wait_s"])
    assert got == (15, 1, 1, 2, 240.0), pr7
    assert view["repos"]["b/y"]["pr:9"]["running"] == 1
    assert view["repos"]["b/y"]["branch:main"]["queued"] == 1
    text = "\n".join(format_status(view))
    for needle in (
        f"pool: up (pid 42, version {POOL_VERSION}, heartbeat 4 s ago)",
        "slots: 5 running, 1 starting, 4 in all",
        "    dev: 1 running, 0 queued",
        "    run 6: 1 running, 0 queued",
        "    (no branch): 1 running, 0 queued",
        "queued without a runner: 3",
        "c/z: can't read its queue: HTTP 502",
        "a/x: 1 running, 2 queued",
        "PR #7 (issue #15): 1 running, 1 starting, 2 queued, waiting up to 4 min",
        "main: 0 running, 1 queued, waiting up to 30 s",
    ):
        assert needle in text, (needle, text)
    # a heartbeat ahead of the clock (it was set back) is 0 s old; slots nobody knows aren't None
    write_json(home.heartbeat, {**beat, "ts": now + 30, "slots": None})
    code, view = pool_status(home, now)
    lines = format_status(view)
    assert code == STATUS_UP and view["heartbeat_age_s"] == 0, view
    assert view["heartbeat_ahead_s"] == 30 and "slots: 5 running, 1 starting" in lines, lines
    assert lines[0].endswith("heartbeat 30 s ahead of this Mac's clock)"), lines
    # the Mac admits fewer slots: status says how many, and why; a stale heartbeat's doesn't count
    critical = {"slots": 0, "reason": "memory pressure is critical"}
    write_json(home.heartbeat, {**beat, "held": critical})
    code, view = pool_status(home, now)
    assert "admitting 0 of 4 slots: memory pressure is critical" in format_status(view), view
    assert pool_status(home, now + HEARTBEAT_FRESH_S)[1]["held"] is None
    write_json(home.heartbeat, beat)

    # pause and resume: idempotent; a stale heartbeat reads as down even while paused; the reason
    # is kept on one line
    assert pause_pool(home, "on\nbattery", now).startswith("paused:")
    assert read_json(home.paused, {})["reason"] == "on battery"  # stored as shown
    assert (
        pause_pool(home, "again", now)
        == "already paused since 2026-01-01T00:10:00+00:00: on battery"
    )
    code, view = pool_status(home, now)
    assert code == STATUS_PAUSED and "on battery (runners.py resume)" in "\n".join(
        format_status(view)
    )
    assert pool_status(home, now + HEARTBEAT_FRESH_S + 5)[0] == STATUS_DOWN
    # a pool from before `status` and pause: a count only, and it ignores paused.json, so status
    # says so instead of "paused", and pause fails (after writing the file) instead of pretending
    older = {key: value for key, value in beat.items() if key not in ("queue", "paused")}
    write_json(home.heartbeat, {**older, "version": "3", "queued": 6})
    code, view = pool_status(home, now)
    assert code == STATUS_UP and view["queued"] == 6 and "pr:7" in view["repos"]["a/x"], view
    text = "\n".join(format_status(view))
    assert "queued: 6 (this pool doesn't list them by PR yet)" in text, text
    assert "paused.json is there, but the pool serving now predates pause" in text, text
    assert "predates pause" in _refuses(pause_pool, home, "again", now)
    resume_pool(home)
    assert "predates pause" in _refuses(pause_pool, home, "x", now) and home.paused.exists()
    write_json(home.heartbeat, beat)
    assert pool_status(home, now)[0] == STATUS_PAUSED
    assert pool_status(home, now + HEARTBEAT_FRESH_S)[0] == STATUS_DOWN  # as doctor: 120 s is down
    home.paused.write_text("{not json", encoding="utf-8")
    code, view = pool_status(home, now)  # an unreadable pause still pauses
    assert code == STATUS_PAUSED, view
    unreadable = "paused: paused.json is unreadable (runners.py resume)"
    assert unreadable in format_status(view), format_status(view)
    assert pause_pool(home, "x", now) == "already paused: paused.json is unreadable"
    assert pause_text({"since": 1767225600, "reason": " a\n\tb "}) == "paused since 1767225600: a b"
    assert resume_pool(home).startswith("resumed") and resume_pool(home) == "not paused"

    # a paused pool starts nothing, but still reads the queue and writes its heartbeat
    clock = _Clock(70000.0)
    served = Home(root / "pool")
    repo_add(served, "alice/app", None)
    update_config(served, lambda cfg: cfg.update(slots=2))
    _fake_base(served)
    fake = FakeGitHub({"alice/app": "alice"})
    created = "2026-01-01T00:00:00Z"
    fake.add_run("alice/app", 1, created, jobs=2, event="pull_request", pr=7, branch=branch)
    pool = _pool(served, fake, clock, FakeLauncher(fake))
    pool.startup()
    pause_pool(served, "test", clock.t)
    for _ in range(2):
        beat = pool.pass_once()
    assert not fake.posts and beat["paused"]["reason"] == "test" and beat["queued"] == 2, beat
    assert sum("until `runners.py resume`" in line for line in capture.lines) == 1
    resume_pool(served)
    beat = pool.pass_once()
    assert len(fake.posts) == 2 and beat["paused"] is None and beat["queued"] == 0, beat
    assert sum("resumed: starting runners again" in line for line in capture.lines) == 1
    assert {r.branch for r in pool.live()} == {branch}

    # stats: percentiles, conclusions and how busy, over the window only
    rows = [
        {
            "repo": "a/x",
            "queued_at": "2026-01-01T00:00:00Z",
            "started_at": "2026-01-01T00:00:10Z",
            "finished_at": "2026-01-01T00:01:10Z",
            "conclusion": "success",
        },
        {
            "repo": "a/x",
            "queued_at": "2026-01-01T00:00:00Z",
            "started_at": "2026-01-01T00:01:40Z",
            "finished_at": "2026-01-01T00:03:40Z",
            "conclusion": "failure",
        },
        {
            "repo": "b/y",
            "queued_at": None,
            "started_at": None,
            "finished_at": "2026-01-01T00:02:00Z",
            "conclusion": "unused",
        },
        {
            "repo": "b/y",
            "queued_at": "2025-12-01T00:00:00Z",
            "started_at": "2025-12-01T00:00:01Z",
            "finished_at": "2025-12-01T00:00:02Z",
            "conclusion": "success",
        },  # before the window
    ]
    got = ledger_stats(rows, now, 7, 2)
    assert got["jobs"] == 3 and got["conclusions"] == {"success": 1, "failure": 1, "unused": 1}
    times = (got["wait_p50_s"], got["wait_p95_s"], got["run_p50_s"], got["run_p95_s"])
    assert times == (10.0, 100.0, 60.0, 120.0), got
    assert got["repos"]["b/y"]["wait_p50_s"] is None, got
    # the ledger is older than the window, so busy is over all 7 days
    assert got["window_s"] == 7 * 86400 and abs(got["busy"] - 180 / (2 * 7 * 86400)) < 1e-9, got
    # a ledger younger than the window: busy over its own 10 minutes, overall and per repo
    young = ledger_stats(rows[:3], now, 7, 2)
    assert young["window_s"] == 600 and abs(young["busy"] - 0.15) < 1e-9, young
    assert abs(young["repos"]["a/x"]["busy"] - 0.15) < 1e-9 and young["repos"]["b/y"]["busy"] == 0
    text = "\n".join(format_stats(young))
    assert "last 7 days: 3 jobs (failure 1, success 1, unused 1)" in text, text
    assert "busy: 15.0% of 2 slots over 10 min" in text, text
    assert "a/x: 2 jobs, busy 15.0%; wait p50 10 s, p95 100 s" in text, text
    # a quiet start doesn't inflate busy: a month-old ledger and one hour-long job in the window
    hour = {"repo": "a/x", "queued_at": "2026-01-01T07:59:00Z", "conclusion": "success"}
    hour.update(started_at="2026-01-01T08:00:00Z", finished_at="2026-01-01T09:00:00Z")
    month = epoch("2026-01-01T11:00:00Z", 0.0)
    got = ledger_stats([rows[3], hour], month, 7, 2)
    assert abs(got["busy"] - 3600 / (2 * 7 * 86400)) < 1e-9, got
    # a job longer than the window counts only its part inside it
    long = {**hour, "started_at": "2025-12-30T09:00:00Z"}
    got = ledger_stats([rows[3], long], month, 1, 1)
    assert abs(got["busy"] - 22 / 24) < 1e-9 and got["busy"] <= 1, got
    assert format_stats(ledger_stats([], now, 7, 2)) == ["no jobs finished in the last 7 days"]
    assert percentile(list(range(1, 101)), 95) == 95 and percentile([3], 50) == 3
    home.jobs.write_text(json.dumps(rows[0]) + "\nnot json\n[1]\n", encoding="utf-8")
    first_row = rows[0]
    assert read_ledger(home) == [first_row]

    # the CLI: exit codes, JSON, the logs
    cli = ["--home", str(home.root)]
    write_json(home.heartbeat, {**beat, "ts": time.time()})
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main([*cli, "status", "--json"]) == STATUS_UP
    assert json.loads(out.getvalue())["state"] == "up"
    with contextlib.redirect_stdout(io.StringIO()):
        assert main([*cli, "pause", "--reason", "x"]) == 0
        assert main([*cli, "status"]) == STATUS_PAUSED
        assert main([*cli, "resume"]) == 0
        assert main([*cli, "status"]) == STATUS_UP
    # down in container mode, with Docker down: `status` says to start Docker, as a reinstall of
    # the service wouldn't help; in host mode it never asks Docker
    bin_dir = home.root / "fake-bin"
    bin_dir.mkdir()
    daemon = "Cannot connect to the Docker daemon. Is the docker daemon running?"
    script = f"#!/bin/sh\necho {daemon!r} >&2\necho asked >> {bin_dir / 'asked'}\nexit 1\n"
    (bin_dir / "docker").write_text(script, encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)
    write_json(home.heartbeat, {**beat, "ts": time.time() - HEARTBEAT_FRESH_S - 5})
    path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(bin_dir)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            assert main([*cli, "status"]) == STATUS_DOWN
            assert not (bin_dir / "asked").exists()
            update_config(home, lambda cfg: cfg.update(mode="container"))
            write_json(home.heartbeat, {**beat, "ts": time.time()})
            assert main([*cli, "status"]) == STATUS_UP  # up: Docker isn't asked
            assert not (bin_dir / "asked").exists()
            write_json(home.heartbeat, {**beat, "ts": time.time() - HEARTBEAT_FRESH_S - 5})
            assert main([*cli, "status"]) == STATUS_DOWN
            assert main([*cli, "status", "--json"]) == STATUS_DOWN
    finally:
        os.environ["PATH"] = path
        update_config(home, lambda cfg: cfg.update(mode="host"))
    text, _, data = out.getvalue().partition("{")
    lines = text.splitlines()
    assert not [line for line in lines[:3] if line.startswith("docker:")], lines
    (said,) = [line for line in lines if line.startswith("docker:")]
    assert said.startswith(f"docker: Docker isn't running ({daemon}): this pool runs"), said
    assert "start Docker Desktop" in said and lines[lines.index(said) - 1].startswith("pool: down")
    assert json.loads("{" + data)["docker"] == f"Docker isn't running ({daemon})", data
    # a problem that says its own fix gets no Docker Desktop advice; a config that isn't an
    # object, or isn't JSON, gets no docker line (`status` still exits 3)
    view = pool_status(home, time.time())[1]
    view["docker"] = (
        "no docker CLI on the pool's PATH: install Docker Desktop, or put docker on PATH"
    )
    assert f"docker: {view['docker']}" in format_status(view), format_status(view)
    odd = Home(home.root / "odd")
    odd.root.mkdir()
    for text in ("[1]", "{"):
        odd.config.write_text(text, encoding="utf-8")
        assert docker_problem(odd) is None
    run_dir = home.logs / "a" / "x" / "1" / "factory-abc123-1-beef"
    (run_dir / "_diag").mkdir(parents=True)
    (run_dir / "runner.log").write_text("".join(f"step {i}\n" for i in range(9)), encoding="utf-8")
    (home.logs / "pool.log").write_text("one\ntwo\nthree\n", encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main([*cli, "logs", "-n", "2"]) == 0
        assert main([*cli, "logs", "--run", "1", "-n", "3"]) == 0
        assert main([*cli, "stats", "--days", "3650"]) == 0
    text = out.getvalue()
    assert text.startswith("two\nthree\n== a/x, run 1, runner factory-abc123-1-beef\n"), text
    assert "step 6\nstep 7\nstep 8\n" in text and "step 5" not in text, text
    assert f"_diag: {run_dir / '_diag'}" in text and "last 3650 days: 1 jobs" in text, text
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        assert main([*cli, "logs", "--run", "2"]) == 1
        assert main([*cli, "stats", "--days", "0"]) == 1
    assert "no runner logs for run 2" in err.getvalue() and "--days" in err.getvalue()
    assert (
        tail_lines(home.root / "none.log", 5) == [] and tail_lines(home.logs / "pool.log", 0) == []
    )


def _test_logging(root: Path) -> None:
    """under a service, stderr (service.log, which never rotates) gets only warnings and
    errors, while pool.log gets everything. On a terminal, stderr gets everything too."""

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    before, level = list(log.handlers), log.level
    service, terminal = io.StringIO(), Terminal()
    try:
        setup_logging(Home(root), False, service)
        setup_logging(None, False, terminal)
        log.info("a routine line")
        log.warning("a warning line")
    finally:
        for handler in [h for h in log.handlers if h not in before]:
            log.removeHandler(handler)
            handler.close()
        log.setLevel(level)
    assert "a warning line" in service.getvalue() and "a routine line" not in service.getvalue()
    assert "a routine line" in terminal.getvalue()
    pool_log = (root / "logs" / "pool.log").read_text(encoding="utf-8")
    assert "a routine line" in pool_log and "a warning line" in pool_log


def _test_launch_failure(root: Path) -> None:
    """A runner that can't start costs one registration (deleted again), then its repo waits."""
    clock = _Clock(30000.0)
    home = Home(root)
    repo_add(home, "octo_ent/app", None)
    _fake_base(home)
    fake = FakeGitHub({"octo_ent/app": "octo_ent"})
    fake.add_run("octo_ent/app", 1, "2026-01-01T00:00:00Z", jobs=3)
    launcher = FakeLauncher(fake)

    def broken(slot: Path, env: dict, log_path: Path) -> int:
        raise OSError(8, "Exec format error")

    launcher.start = broken
    pool = _pool(home, fake, clock, launcher)
    pool.startup()
    pool.pass_once()
    assert len(fake.posts) == 1 and not fake.regs.get("octo_ent/app"), fake.regs
    assert pool.jit_backoff["octo_ent/app"][0] > clock.t and not list(home.slots.iterdir())
    pool.pass_once()
    assert len(fake.posts) == 1
    line = _ledger(home)[-1]
    assert (line["conclusion"], line["stopped"]) == ("unused", "could not start"), line


def _test_recovery(root: Path) -> None:
    clock = _Clock(20000.0)
    home = Home(root)
    repo_add(home, "alice/app", None)
    _fake_base(home)
    pool_id = load_config(home)["pool_id"]
    fake = FakeGitHub({"alice/app": "alice"})
    fake.add_run("alice/app", 1, "2026-01-01T00:00:00Z", jobs=2)
    names = {key: f"factory-{pool_id}-1-{key * 4}" for key in "abce"}
    regs = fake.regs.setdefault("alice/app", {})
    for rid, name, busy in (
        (1, names["a"], True),  # adopted: still running
        (2, names["b"], False),  # its process is gone
        (3, names["c"], False),  # registered just before a crash, never saved
        (4, "my-macbook", False),  # registered by hand: never touched
        (5, "factory-ffffff-1-dddd", False),  # another pool's
    ):
        regs[rid] = {"id": rid, "name": name, "busy": busy, "labels": [{"name": "self-hosted"}]}
    fake.take(1, 1, names["a"])
    fake.jobs[1][0].update(status="completed", conclusion="success", runner_name=names["e"])
    saved = [
        Runner(names["a"], "alice/app", "alice", 1, 1, "s01", pid=4242, started=clock.t),
        Runner(names["b"], "alice/app", "alice", 1, 2, "s02", pid=4343, started=clock.t),
        Runner(
            names["e"],
            "alice/app",
            "alice",
            1,
            6,
            "s03",
            started=clock.t,
            exited=clock.t - 5,
            exit_code=0,
            deregistered=True,
        ),
    ]
    write_json(home.state, {"runners": [asdict(r) for r in saved]})
    for slot in ("s01", "s02", "s07"):
        (home.slots / slot / "_diag").mkdir(parents=True)
    launcher = FakeLauncher(fake)
    launcher.alive = {"s01": 4242, "s09": 999}  # s09: a process in a slot no saved runner owns
    pool = _pool(home, fake, clock, launcher)
    pool.startup()
    adopted, dead, done = (pool.runners.get(names[key]) for key in "abe")
    assert adopted and not adopted.exited and dead and dead.exited and done and done.exited
    assert [slot for slot, _ in launcher.stops] == ["s09"], launcher.stops
    assert sorted(path.name for path in home.slots.iterdir()) == ["s01"]
    assert (home.logs / "orphans").is_dir()
    assert (pool.log_dir(dead) / "_diag").is_dir()
    assert sorted(regs) == [1, 4, 5], sorted(regs)
    pool.pass_once()
    assert [(x["runner"], x["conclusion"]) for x in _ledger(home)] == [(names["e"], "success")]
    assert adopted.job_id == 11
    for _ in range(FINISH_TRIES):
        pool.pass_once()
    last = _ledger(home)[-1]
    assert (last["runner"], last["conclusion"]) == (names["b"], "unknown"), last
    pool.record(saved[2], None)  # a crash between its ledger line and state.json: written once
    assert sum(x["runner"] == names["e"] for x in _ledger(home)) == 1


def _test_process(root: Path) -> None:
    # what a runner's group leader looks like in ps counts, and nothing else that names a slot
    listing = (
        "11 11 /bin/bash /p/my pool/slots/s01/run.sh\n"
        "12 12 bash -e /p/my pool/slots/s02/run.sh --once\n"
        "13 13 /p/my pool/slots/s03/bin/Runner.Listener run\n"
        "14 9 /p/my pool/slots/s08/bin/Runner.Listener run\n"  # not a group leader
        "15 15 tail -f /p/my pool/slots/s04/_diag/Runner_1.log\n"
        "16 16 less /p/my pool/slots/s05/run.sh\n"
        "17 17 /bin/bash /p/my pool/slots/s06/run.sh.bak\n"
        "18 18 /bin/bash /p/other/slots/s07/run.sh\n"
    )
    got = ps_slots(listing, Path("/p/my pool/slots"))
    assert got == {"s01": {11}, "s02": {12}, "s03": {13}}, got
    hook = root / "hooks" / "factory-abc123-77-beef.sh"
    write_hook(hook, "factory-abc123-77-beef", "alice/app", 77)
    for run_id, code in (("77", 0), ("78", 1), ("", 1)):
        env = {"GITHUB_RUN_ID": run_id} if run_id else {}
        got = subprocess.run(["/bin/sh", str(hook)], check=False, env=env, capture_output=True)
        assert got.returncode == code, (run_id, got.returncode)
    base = root / "base"
    base.mkdir()
    script = '#!/bin/sh\nenv > "$HOME/env.txt"\nsleep 30 &\nwait\n'
    (base / "run.sh").write_text(script, encoding="utf-8")
    (base / "run.sh").chmod(0o755)
    slots = root / "slots"
    slots.mkdir()
    slot = slots / "s01"
    assert clone_tree(base, slot) in ("clone", "copy") and (slot / "run.sh").is_file()

    def no_clone(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "cp: clonefile failed\n")

    assert clone_tree(base, root / "copied", no_clone) == "copy"
    assert (root / "copied" / "run.sh").read_bytes() == (base / "run.sh").read_bytes()
    (slot / "_home").mkdir()
    (slot / "_tmp").mkdir()
    outside = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GH_TOKEN": "x", "HOME": "/x"}
    outside["SSH_AUTH_SOCK"] = "/tmp/agent.sock"
    launcher = Launcher(slots)
    pid = launcher.start(
        slot, runner_env(outside, slot, "JITSECRET-real", hook, root / "cache"), root / "r.log"
    )
    seen = slot / "_home" / "env.txt"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and "JITSECRET" not in (
        seen.read_text(encoding="utf-8") if seen.exists() else ""
    ):
        time.sleep(0.05)
    text = seen.read_text(encoding="utf-8")
    assert "ACTIONS_RUNNER_INPUT_JITCONFIG=JITSECRET-real" in text
    assert f"HOME={slot / '_home'}" in text and f"ACTIONS_RUNNER_HOOK_JOB_STARTED={hook}" in text
    assert f"UV_CACHE_DIR={root / 'cache' / 'uv'}" in text
    assert f"RUNNER_TOOL_CACHE={slot / '_tool'}" in text
    assert "GH_TOKEN" not in text and "SSH_AUTH_SOCK" not in text
    # a process that only names the slot is not a runner, in another process group or leading
    # its own, as job control in a terminal makes a `tail -f` of the slot's logs
    bystander = subprocess.Popen(["/bin/sh", "-c", f"sleep 30; true {slot}/_diag/x"])
    (slot / "_diag").mkdir()
    (slot / "_diag" / "Runner.log").write_text("", encoding="utf-8")
    tail = subprocess.Popen(
        ["tail", "-f", str(slot / "_diag" / "Runner.log")],
        stdout=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:

        def command(pid: int) -> str:
            ps = ["ps", "-o", "command=", "-p", str(pid)]
            return subprocess.run(ps, check=False, capture_output=True, text=True).stdout

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and "tail -f" not in command(tail.pid):
            time.sleep(0.05)
        found = launcher.scan()
        assert found.get("s01") == {pid}, found
        assert launcher.leads(pid, "s01") and not launcher.leads(bystander.pid, "s01")
        launcher.stop(found["s01"], grace=2.0)
        assert launcher.reap().get(pid) is not None
        assert "s01" not in launcher.scan() and bystander.poll() is None and tail.poll() is None
    finally:
        for proc in (bystander, tail):
            proc.kill()
            proc.wait()
    # a runner whose run.sh execs another script: scan() misses it, but its own pid still leads
    # its group in its slot, which Pool.missed asks before deleting the slot
    later = slots / "s03"
    later.mkdir()
    execs = '#!/bin/sh\nexec /bin/sh "$(dirname "$0")/helper.sh"\n'
    (later / "run.sh").write_text(execs, encoding="utf-8")
    (later / "run.sh").chmod(0o755)
    (later / "helper.sh").write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    pid = launcher.start(later, {"PATH": "/usr/bin:/bin"}, root / "later.log")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and "helper.sh" not in command(pid):
        time.sleep(0.05)
    assert "s03" not in launcher.scan() and launcher.leads(pid, "s03")
    assert not launcher.leads(pid, "s01") and not launcher.leads(None, "s03")
    launcher.stop({pid}, grace=2.0)
    assert launcher.reap().get(pid) is not None and not launcher.leads(pid, "s03")
    # under taskpolicy's utility clamp (on macOS): the same pid leads its group, and stops alike
    if policy_prefix():
        low = slots / "s02"
        clone_tree(base, low)
        (low / "_home").mkdir()
        (low / "_tmp").mkdir()
        clamped = Launcher(slots, policy_prefix())
        env = runner_env(outside, low, "JITSECRET-low", hook, root / "cache")
        pid = clamped.start(low, env, root / "low.log")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (low / "_home" / "env.txt").exists():
            time.sleep(0.05)
        found = clamped.scan()
        assert found.get("s02") == {pid}, found
        # taskpolicy execs the runner rather than forking it: the group leader is run.sh itself
        leader = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, check=False
        ).stdout
        assert "run.sh" in leader and "taskpolicy" not in leader, leader
        clamped.stop(found["s02"], grace=2.0)
        assert clamped.reap().get(pid) is not None and "s02" not in clamped.scan()
    assert policy_prefix(str(root / "no-taskpolicy")) == ()


def _test_mac(root: Path) -> None:
    ac = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t100%; charged; present: true\n"
    answers = {
        "kern.memorystatus_vm_pressure_level": "4\n",
        "hw.perflevel0.physicalcpu": "12\n",
        "hw.memsize": f"{48 * GIB}\n",
        "batt": ac.replace("AC Power", "Battery Power"),
    }
    mac = Mac(_fake_mac(answers))
    assert (mac.pressure(), mac.on_battery(), mac.capacity()) == (4, True, (12, 48 * GIB))
    intel = Mac(_fake_mac({"hw.physicalcpu": "8\n", "hw.memsize": f"{16 * GIB}\n", "batt": ac}))
    assert (intel.pressure(), intel.on_battery(), intel.capacity()) == (None, False, (8, 16 * GIB))
    unknown = Mac(_fake_mac({}))
    assert (unknown.on_battery(), unknown.capacity()) == (None, None)

    def broken(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    assert Mac(broken).pressure() is None and Mac(broken).on_battery() is None
    # caffeinate -i -w <pid>, with no terminal, where macOS has it
    spawned: list = []

    def popen(cmd, **kwargs):
        spawned.append((cmd, kwargs))
        return "caffeinate"

    awake = Mac(_fake_mac({}), popen).caffeinate(123)
    if os.access(CAFFEINATE, os.X_OK):
        assert awake == "caffeinate" and spawned[0][0] == [CAFFEINATE, "-i", "-w", "123"]
        assert {spawned[0][1][key] for key in ("stdin", "stdout", "stderr")} == {subprocess.DEVNULL}
    else:
        assert awake is None and not spawned
    assert [default_slots(c, m * GIB) for c, m in ((4, 8), (6, 36), (12, 48))] == [1, 2, 8]
    assert default_slots(64, 1024 * GIB) == 60 and default_slots(128, 1024 * GIB) == 64
    # the default slots come from this Mac, read once; a saved value stays
    saved = dict(_MACHINE)
    try:
        _MACHINE.clear()
        assert machine(_fake_mac(answers)) == {"cores": 12, "memory_gb": 48, "slots": 8}
        assert machine(broken)["slots"] == 8  # read once
        home = Home(root)
        assert load_config(home)["slots"] == 8
        repo_add(home, "alice/app", None)
        _MACHINE["slots"] = 3
        assert load_config(home)["slots"] == 8  # saved at the first change
        _MACHINE.clear()
        assert machine(broken) == {"cores": None, "memory_gb": None, "slots": 2}
    finally:
        _MACHINE.clear()
        _MACHINE.update(saved)
    assert "battery" in _refuses(update_config, Home(root), lambda c: c.update(battery="off"))


def _test_tools(root: Path) -> None:
    slot, shared = root / "slot", root / "shared"
    for rel, marked in (("uv/0.9.5/arm64", True), ("uv/0.9.6/arm64", False), ("node/22.1.0", True)):
        (slot / rel).mkdir(parents=True)
        (slot / rel / "bin").write_text(rel, encoding="utf-8")
        if marked:
            (slot / (rel + ".complete")).write_text("", encoding="utf-8")
    (shared / "uv/0.9.5/arm64").mkdir(parents=True)  # half a folder from a crash: replaced
    outside = root / "outside" / "1.0"  # a job links a tool folder out of its slot: not copied
    (outside / "x64").mkdir(parents=True)
    (outside / "x64.complete").write_text("", encoding="utf-8")
    (slot / "evil").symlink_to(root / "outside", target_is_directory=True)
    assert merge_tools(slot, shared) == ["uv/0.9.5/arm64"]  # node's marker is beside its version
    assert not (shared / "node").exists()
    assert (shared / "uv/0.9.5/arm64/bin").read_text(encoding="utf-8") == "uv/0.9.5/arm64"
    assert (shared / "uv/0.9.5/arm64.complete").is_file() and not (shared / "uv/0.9.6").exists()
    assert merge_tools(slot, shared) == []  # already there
    assert not [path for path in shared.rglob("*") if path.name.endswith(".tmp")]
    assert merge_tools(root / "missing", shared) == []
    # a marker that names no arch folder (<version>/.complete names nothing, ..complete the
    # version itself, ...complete its tool) never replaces the shared version or tool: a version
    # the slot lacks stays
    (shared / "uv/0.9.4/arm64").mkdir(parents=True)
    (shared / "uv/0.9.4/arm64.complete").write_text("", encoding="utf-8")
    for odd in ("uv/0.9.5/.complete", "uv/0.9.5/..complete", "uv/0.9.5/...complete"):
        (slot / odd).write_text("", encoding="utf-8")
    assert merge_tools(slot, shared) == []
    assert (shared / "uv/0.9.4/arm64.complete").is_file() and (shared / "uv/0.9.5/arm64").is_dir()
    # a marker beside a version folder never replaces the shared version: its other arch stays
    (shared / "uv/0.9.7/x64").mkdir(parents=True)
    (shared / "uv/0.9.7/x64.complete").write_text("", encoding="utf-8")
    (slot / "uv/0.9.7/arm64").mkdir(parents=True)
    (slot / "uv/0.9.7.complete").write_text("", encoding="utf-8")
    assert merge_tools(slot, shared) == [] and (shared / "uv/0.9.7/x64.complete").is_file()
    # a file where a version folder would go skips that tool instead of failing the clean-up,
    # which in startup would crash-loop the service
    (shared / "uv/1.0.complete").write_text("", encoding="utf-8")
    (slot / "uv/1.0.complete/arm64").mkdir(parents=True)
    (slot / "uv/1.0.complete/arm64.complete").write_text("", encoding="utf-8")
    assert merge_tools(slot, shared) == [] and (shared / "uv/1.0.complete").is_file()

    # pruning keeps each tool's TOOL_KEEP versions marked last (whatever their names, by their
    # newest marker), and drops the older ones, any version without a marker and merge_tools'
    # temp folders; other tools stay, and links aren't followed
    tools = root / "prune"
    for n, version in enumerate(("1.0.0", "1.1.0", "1.2.0", "1.3.0")):
        (tools / "uv" / version / "arm64").mkdir(parents=True)
        mark = tools / "uv" / version / "arm64.complete"
        mark.write_text("", encoding="utf-8")
        os.utime(mark, (1003.0 - n, 1003.0 - n))  # 1.0.0 was marked last
    beside = tools / "uv" / "1.1.0.complete"  # an older marker too: the newest one counts
    beside.write_text("", encoding="utf-8")
    os.utime(beside, (900.0, 900.0))
    (tools / "uv" / "1.4.0" / "arm64").mkdir(parents=True)  # half-written: no marker
    (tools / "node" / "22.1.0").mkdir(parents=True)  # marked beside its folder
    (tools / "node" / "22.1.0.complete").write_text("", encoding="utf-8")
    (tools / "uv" / ".1.5.0.77.tmp").mkdir()
    (tools / "uv" / "1.0.0" / ".arm64.77.tmp").mkdir()  # in a version that stays
    (tools / "uv" / ".notes").mkdir()  # not a temp folder of merge_tools: it stays
    (tools / "uv" / "1.0.0" / ".cache.tmp").mkdir()  # nor this
    elsewhere = root / "elsewhere"  # outside the cache, reached only through links
    for n in range(4):
        (elsewhere / f"2.{n}" / "x64").mkdir(parents=True)
        (elsewhere / f"2.{n}" / "x64.complete").write_text("", encoding="utf-8")
    (elsewhere / "2.4").mkdir()
    (elsewhere / ".keep.1.tmp").mkdir()
    (elsewhere / "2.0" / ".keep.2.tmp").mkdir()
    (tools / "linked").symlink_to(elsewhere, target_is_directory=True)
    (tools / "uv" / "linked").symlink_to(elsewhere / "2.0", target_is_directory=True)
    before = sorted(path.relative_to(elsewhere) for path in elsewhere.rglob("*"))
    assert prune_tools(tools) == ["uv/1.3.0", "uv/1.4.0"]
    kept = sorted(path.name for path in (tools / "uv").iterdir())
    assert kept == [".notes", "1.0.0", "1.1.0", "1.1.0.complete", "1.2.0", "linked"], kept
    assert sorted(path.name for path in (tools / "uv" / "1.0.0").iterdir()) == [
        ".cache.tmp",
        "arm64",
        "arm64.complete",
    ]
    assert (tools / "node" / "22.1.0").is_dir()
    assert sorted(path.relative_to(elsewhere) for path in elsewhere.rglob("*")) == before
    assert prune_tools(tools) == [] and prune_tools(root / "nowhere") == []


def _test_policy(root: Path, capture: _Capture) -> None:
    """Admission, caffeinate and the shared caches, on a fake Mac."""
    clock = _Clock(50000.0)
    home = Home(root)
    repo_add(home, "alice/app", None)
    update_config(home, lambda cfg: cfg.update(slots=4, battery="half"))
    _fake_base(home)
    fake = FakeGitHub({"alice/app": "alice"})
    fake.add_run("alice/app", 1, "2026-01-01T00:00:00Z", jobs=6)
    mac = FakeMac(pressure=CRITICAL, battery=True)
    launcher = FakeLauncher(fake)
    pool = _pool(home, fake, clock, launcher, mac=mac)
    pool.startup()
    pool.pass_once()
    beat = pool.pass_once()
    assert not fake.posts and pool.critical and not mac.awake  # critical pressure: nothing starts
    assert beat["held"] == {"slots": 0, "reason": "memory pressure is critical"}, beat
    assert sum("memory pressure is critical" in line for line in capture.lines) == 1
    mac.level = 2  # a warning admits; on battery with battery: half, 2 of the 4 slots
    beat = pool.pass_once()
    assert len(fake.posts) == 2 and not pool.critical
    assert beat["held"] == {"slots": 2, "reason": "on battery (battery: half)"}, beat
    assert sum("memory pressure eased" in line for line in capture.lines) == 1
    assert len(mac.awake) == 1 and mac.awake[0].args[-1] == str(os.getpid())
    mac.battery = False  # on AC power: all 4, and still one caffeinate
    beat = pool.pass_once()
    assert len(fake.posts) == 4 and len(mac.awake) == 1 and mac.awake[0].code is None
    assert beat["held"] is None, beat

    # each slot shares the pool's caches
    name = fake.posts[0][1]["name"]
    first = pool.runners[name]
    slot = home.slots / first.slot
    link = slot / "_home" / ".cache" / "uv"
    assert link.is_symlink() and link.resolve() == (home.cache / "uv").resolve()
    env = launcher.envs[first.slot]
    assert env["UV_CACHE_DIR"] == str(home.cache / "uv")
    assert env["UV_PYTHON_INSTALL_DIR"] == str(home.cache / "uv-python")
    assert env["RUNNER_TOOL_CACHE"] == str(slot / "_tool") and (slot / "_tool").is_dir()
    # its job caches a tool (and leaves half of another) and a wheel, then ends: the finished
    # tool is merged into the shared tool cache, and the shared uv cache outlives the slot
    for rel, marked in (("uv/0.9.5/arm64", True), ("uv/0.9.6/arm64", False)):
        (slot / "_tool" / rel).mkdir(parents=True)
        (slot / "_tool" / rel / "uv").write_text("uv", encoding="utf-8")
        if marked:
            (slot / "_tool" / (rel + ".complete")).write_text("", encoding="utf-8")
    (link / "wheels").mkdir()
    (link / "wheels" / "x.whl").write_text("x", encoding="utf-8")
    fake.take(1, 0, first.name)
    fake.complete(1, 0, "success")
    launcher.exit(first.slot, 0)
    mac.level = mac.battery = None  # readings that fail admit as usual, and say so once
    pool.pass_once()
    assert (home.cache / "uv" / "wheels" / "x.whl").is_file()  # the shared uv cache outlives it
    assert (home.cache / "tool" / "uv/0.9.5/arm64.complete").is_file()
    assert not (home.cache / "tool" / "uv/0.9.6").exists()
    assert len(fake.posts) == 5  # the freed slot took the next job, despite the failed readings
    latest = fake.posts[-1][1]["name"]
    newest = home.slots / pool.runners[latest].slot
    assert newest == slot and not (slot / "_tool" / "uv/0.9.6").exists()  # a fresh clone in it
    assert (newest / "_tool" / "uv/0.9.5/arm64/uv").is_file()  # the next job starts with the tool
    pool.pass_once()
    for needle in ("can't read the memory pressure", "can't tell the power source"):
        assert sum(needle in line for line in capture.lines) == 1, needle

    # the run is cancelled and every runner gone: caffeinate lets go
    fake.cancel(1)
    for r in pool.live():
        launcher.exit(r.slot, 0)
    pool.pass_once()
    assert not pool.live() and len(fake.posts) == 5
    assert pool.awake is None and mac.awake[0].code == -15 and len(mac.awake) == 1
    # a reading that fails during a critical episode admits again, without saying it eased
    fake.add_run("alice/app", 2, "2026-01-01T00:30:00Z")
    mac.level = CRITICAL
    pool.pass_once()
    assert len(fake.posts) == 5
    mac.level = None
    pool.pass_once()
    assert not pool.critical and len(fake.posts) == 6
    assert sum("memory pressure eased" in line for line in capture.lines) == 1

    # the shared caches are pruned once a day, and only while no runner is alive: the tool
    # cache, and the uv cache by the newest uv the jobs use, here setup-uv's 0.9.5
    pool.next_prune = 0.0
    pool.environ["PATH"] = str(root / "no-uv")
    calls = root / "uv-calls"
    cached_uv = _fake_uv(home.cache / "tool" / "uv" / "0.9.5" / "arm64" / "uv", "0.9.5", calls)
    # three older uvs merged after it: pruning the tool cache first would drop 0.9.5
    for n, version in enumerate(("0.9.1", "0.9.2", "0.9.3")):
        _fake_uv(home.cache / "tool" / "uv" / version / "arm64" / "uv", version, calls)
        mark = home.cache / "tool" / "uv" / version / "arm64.complete"
        mark.write_text("", encoding="utf-8")
        later = time.time() + 60 + n
        os.utime(mark, (later, later))
    unfinished = home.cache / "tool" / "uv" / "0.1.0"
    (unfinished / "arm64").mkdir(parents=True)
    for r in pool.live():  # a runner is alive: no pruning
        launcher.exit(r.slot, 0)
        break
    pool.pass_once()
    assert pool.live() and pool.next_prune == 0.0 and unfinished.is_dir() and not calls.exists()
    for r in pool.live():
        launcher.exit(r.slot, 0)
    for run_id in list(fake.jobs):
        fake.cancel(run_id)
    pool.pass_once()  # idle: pruned
    assert not pool.live() and pool.next_prune == clock.t + PRUNE_S and not unfinished.exists()
    kept = sorted(path.name for path in (home.cache / "tool" / "uv").iterdir())
    assert kept == ["0.9.1", "0.9.2", "0.9.3"], kept  # 0.9.5 went, but after it pruned uv's
    env = "UV_NO_CONFIG=1 UV_LOCK_TIMEOUT=30"
    pruned = f"{cached_uv} cache prune --cache-dir {home.cache / 'uv'} {env}\n"
    assert calls.read_text(encoding="utf-8") == pruned
    said = "pruned the shared uv cache with uv 0.9.5: Removed 3 files (1.0MiB)"
    assert sum(said in line for line in capture.lines) == 1
    (unfinished / "arm64").mkdir(parents=True)
    clock.t += PRUNE_S - 10
    pool.pass_once()  # not a day yet
    assert unfinished.is_dir()
    # the next day's newest, 0.9.3, fails: that's said, and the pass goes on
    _fake_uv(home.cache / "tool" / "uv" / "0.9.3" / "arm64" / "uv", "0.9.3", calls, "exit 2\n")
    clock.t += 10
    assert pool.pass_once()["version"] == POOL_VERSION
    assert not unfinished.exists()
    failed = "can't prune the shared uv cache: uv cache prune failed (exit 2): "
    assert sum(line.startswith(failed) for line in capture.lines) == 1, capture.lines[-3:]
    assert sum("can't read the memory pressure any more" in line for line in capture.lines) == 1


def _test_missed(root: Path, capture: _Capture) -> None:
    """A runner the scan misses, whose own process still leads its group in its slot, is kept, at
    startup and in each pass, and said once; once that process is gone, it has exited."""
    clock = _Clock(30000.0)
    home = Home(root)
    repo_add(home, "alice/app", None)
    _fake_base(home)
    name = f"factory-{load_config(home)['pool_id']}-1-aaaa"
    fake = FakeGitHub({"alice/app": "alice"})
    fake.add_run("alice/app", 1, "2026-01-01T00:00:00Z", jobs=1)
    reg = {"id": 1, "name": name, "busy": True, "labels": [{"name": "self-hosted"}]}
    fake.regs.setdefault("alice/app", {})[1] = reg
    fake.take(1, 0, name)
    saved = Runner(name, "alice/app", "alice", 1, 1, "s01", pid=4242, started=clock.t)
    write_json(home.state, {"runners": [asdict(saved)]})
    (home.slots / "s01" / "_diag").mkdir(parents=True)
    launcher = FakeLauncher(fake)
    launcher.leading[4242] = "s01"  # its run.sh exec'd another script: scan() misses it
    pool = _pool(home, fake, clock, launcher)
    pool.startup()
    (r,) = pool.live()
    assert r.name == name and pool.procs == {"s01": {4242}} and (home.slots / "s01").is_dir()
    pool.pass_once()
    pool.pass_once()
    assert pool.live() == [r] and (home.slots / "s01").is_dir() and not launcher.stops
    warned = [line for line in capture.lines if "though the scan missed it" in line]
    assert len(warned) == 1 and name in warned[0], warned
    launcher.leading.clear()  # its process is gone
    pool.pass_once()
    assert not pool.live() and not (home.slots / "s01").exists()


def _fake_uv(path: Path, version: str, calls: Path, prune: str = "") -> Path:
    """A uv that says its version, and logs each other call, with UV_NO_CONFIG and
    UV_LOCK_TIMEOUT, to `calls`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f'[ "$1" = --version ] && echo "uv {version} (abc123 2026-01-01)" && exit 0\n'
        f'echo "$0 $* UV_NO_CONFIG=$UV_NO_CONFIG UV_LOCK_TIMEOUT=$UV_LOCK_TIMEOUT" >> "{calls}"\n'
        + (prune or 'echo "Pruning cache at: x"\necho "Removed 3 files (1.0MiB)"\n'),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _test_uv(root: Path) -> None:
    """The shared uv cache is pruned by the newest uv the jobs use."""
    calls = root / "calls"
    tools = root / "tool"
    on_path = _fake_uv(root / "bin" / "uv", "0.9.5", calls)
    assert newest_uv(tools, str(root / "bin")) == ((0, 9, 5), str(on_path))
    assert newest_uv(tools, str(root / "no-bin")) is None
    # setup-uv's finished 0.13.10 is newer than 0.9.5, by number rather than as text
    cached = _fake_uv(tools / "uv" / "0.13.10" / "aarch64" / "uv", "0.13.10", calls)
    (tools / "uv" / "0.13.10" / "aarch64.complete").write_text("", encoding="utf-8")
    # left out, though named newer: an unfinished one, one that can't say its version, and one
    # through a link out of the tool cache
    _fake_uv(tools / "uv" / "0.14.0" / "aarch64" / "uv", "0.14.0", calls)
    _fake_uv(tools / "uv" / "0.15.0" / "aarch64" / "uv", "unknown", calls)
    failing = tools / "uv" / "0.17.0" / "aarch64" / "uv"  # says a version, but fails
    _fake_uv(failing, "0.17.0", calls)
    failing.write_text(
        '#!/bin/sh\necho "uv 0.17.0 (abc123 2026-01-01)"\nexit 1\n', encoding="utf-8"
    )
    (tools / "uv" / "0.17.0" / "aarch64.complete").write_text("", encoding="utf-8")
    away = _fake_uv(root / "away" / "uv", "0.16.0", calls)
    (tools / "uv" / "0.16.0").mkdir()
    (tools / "uv" / "0.16.0" / "aarch64").symlink_to(away.parent, target_is_directory=True)
    for version in ("0.15.0", "0.16.0"):
        (tools / "uv" / version / "aarch64.complete").write_text("", encoding="utf-8")
    assert newest_uv(tools, str(root / "bin")) == ((0, 13, 10), str(cached))
    hung = root / "hung" / "uv"  # never answers `--version`: left out after the timeout
    _fake_uv(hung, "0.30.0", calls)
    hung.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    began = time.monotonic()
    assert newest_uv(tools, str(root / "hung"), timeout=0.5) == ((0, 13, 10), str(cached))
    assert time.monotonic() - began < 5
    newer = _fake_uv(root / "bin2" / "uv", "0.20.0", calls)
    got = newest_uv(tools, f"{root / 'bin2'}{os.pathsep}{root / 'bin'}")
    assert got == ((0, 20, 0), str(newer)), got
    assert not calls.exists()  # only asked for their versions

    cache = root / "cache" / "uv"
    cache.mkdir(parents=True)
    env = {"PATH": "/usr/bin:/bin", "UV_NO_CONFIG": ""}
    assert prune_uv(str(cached), cache, env) == "Removed 3 files (1.0MiB)"
    pruned = f"{cached} cache prune --cache-dir {cache} UV_NO_CONFIG=1 UV_LOCK_TIMEOUT=30\n"
    assert calls.read_text(encoding="utf-8") == pruned
    failing = _fake_uv(root / "failing" / "uv", "1.0.0", calls, 'echo "error: no"\nexit 2\n')
    # a wrapper that doesn't exec uv: its whole session is stopped, and soon
    pids = root / "pids"
    wrapper = f'echo $$ >> "{pids}"\nsleep 30 &\necho $! >> "{pids}"\nwait\n'
    slow = _fake_uv(root / "slow" / "uv", "1.0.0", calls, wrapper)
    ticks: list = []
    began = time.monotonic()
    for uv, why, timeout in (
        (failing, "failed (exit 2): error: no", 60.0),
        (slow, "ran over 2 s", 2.0),
    ):
        try:
            prune_uv(str(uv), cache, env, lambda: ticks.append(1), timeout)
        except PoolError as exc:
            assert why in str(exc), exc
        else:
            raise AssertionError(f"{uv} didn't fail")
    assert ticks  # the heartbeat was kept alive while uv ran
    assert time.monotonic() - began < 30, "stopped, not waited out"
    deadline = time.monotonic() + 5
    alive = [int(pid) for pid in pids.read_text(encoding="utf-8").split()]
    while alive and time.monotonic() < deadline:
        alive = [pid for pid in alive if _alive(pid)]
        time.sleep(0.05)
    assert len(pids.read_text(encoding="utf-8").split()) == 2 and not alive, alive


def _test_make_launcher(root: Path) -> None:
    """Container mode swaps in containers.py's launcher, which wraps the host one. It gets
    this very module, even when this file runs as a script, so its PoolError is the pool's."""
    home = Home(root)
    update_config(home, lambda cfg: None)
    host = Launcher(home.slots)
    assert make_launcher(home, host) is host
    update_config(home, lambda cfg: cfg.update(mode="container"))
    launcher = make_launcher(home, host)
    assert type(launcher).__name__ == "ContainerLauncher" and launcher.host is host
    assert sys.modules["containers"].runners is sys.modules[__name__]
    update_config(home, lambda cfg: cfg.update(mode="Container"))  # a typo: fails closed
    assert type(make_launcher(home, host)).__name__ == "ContainerLauncher"
    saved = sys.modules.pop("containers")
    sys.modules["containers"] = None  # containers.py missing: no import, so no pool either
    try:
        assert "needs containers.py" in _refuses(make_launcher, home, host)
    finally:
        sys.modules["containers"] = saved


def _self_test() -> None:
    capture = _Capture()
    log.addHandler(capture)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    saved = dict(_MACHINE)
    _MACHINE.update(cores=6, memory_gb=36, slots=2)  # no test reads this Mac's sysctl
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            _test_config(root / "config")
            _test_client()
            _test_allocate()
            _test_simulation()
            _test_mac(root / "mac")
            _test_tools(root / "tools")
            _test_uv(root / "uv")
            _test_install(root / "install")
            _test_pool(root / "pool", capture)
            _test_fair_pool(root / "fair", capture)
            _test_cache_kept(root / "cache-kept")
            _test_unlisted(root / "unlisted", capture)
            _test_heartbeat(root / "heartbeat")
            _test_policy(root / "policy", capture)
            _test_ops(root / "ops", capture)
            _test_logging(root / "logging")
            _test_launch_failure(root / "launch")
            _test_recovery(root / "recovery")
            _test_missed(root / "missed", capture)
            _test_process(root / "process")
            _test_make_launcher(root / "launcher")
            _test_held(root / "held")  # last: it pauses a pool, which _test_ops counts
        leaked = [line for line in capture.lines if "TOKEN-" in line or "JITSECRET" in line]
        assert not leaked, leaked
    finally:
        _MACHINE.clear()
        _MACHINE.update(saved)
        log.removeHandler(capture)
        log.propagate = True
    print("runners.py self-test: ok")


if __name__ == "__main__":
    sys.exit(main())

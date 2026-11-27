"""Keep a CI job in container mode away from the Mac's own services.

Docker Desktop forwards host.docker.internal to the Mac's loopback, and its HTTP proxy (port 3128 on
the gateway, http.docker.internal) relays there too, so a job could reach any server bound to the
Mac's 127.0.0.1. The pool runs this guard first in each job's container, as root with NET_ADMIN:
`python3 guard.py RUN_SH`. It:
1. finds every address of host.docker.internal (the container's /etc/hosts has them, from
   `--add-host host.docker.internal:host-gateway`, so there's no DNS race), and the Docker Desktop
   subnet around each: the /24 of an IPv4 address, the /64 of an IPv6 one;
2. adds a prohibit route for each subnet (netlink, so the image needs no `ip`);
3. checks by outcome that a TCP connect fails at once, with EACCES (the route), ENETUNREACH or
   EHOSTUNREACH (no route at all) or EADDRNOTAVAIL (IPv6 off): to the host gateway, to each subnet's
   first address on the proxy's port, and to http.docker.internal's when that name resolves. It
   also checks that IPv6 is off (no interface but lo has an IPv6 address): a socket bound to the
   interface would get past an IPv6 route. A bound socket gets past the IPv4 route too, but Docker
   Desktop's host gateway isn't on the pool network's link, so it gets no answer;
4. checks that setpriv and `runner` are there, prints `amplifactory guard: blocked <subnets>`
   (which the pool waits for), then drops NET_ADMIN from the bounding set for good and runs RUN_SH
   as `runner` (setpriv), so no process of the job, `sudo` included, can take the routes away.
Any step that fails prints `amplifactory guard: <why>` and exits 1 before the runner starts: the
container stops, and the pool backs off. It fails closed.

It runs on Linux, in the container; its self-test runs anywhere. So Linux's values are written out
below, not taken from this platform's `socket`. Stdlib only, Python 3.9+.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import pwd
import socket
import struct
import sys
import threading

HOST = "host.docker.internal"
PROXY = "http.docker.internal"
PROXY_PORT = 3128  # Docker Desktop's HTTP proxy, on the subnet's first address too
HOST_PORT = 9  # any port shows the route: nothing need listen on it
PREFIX = {4: 24, 6: 64}  # the Docker Desktop subnet around an address of HOST
RUNNER = "runner"
SETPRIV = "/usr/bin/setpriv"
CHECK_S = 2.0  # a blocked connect fails at once; the first one that doesn't ends the guard
LOOKUP_S = 2.0  # for PROXY: so the guard's worst case stays well under the pool's RUNNING_S (15 s)
# how a blocked connect fails, by errno name: the route, no route, or (IPv6 off) no address to send from
BLOCKED = ("EACCES", "ENETUNREACH", "EHOSTUNREACH", "EADDRNOTAVAIL")
IF_INET6 = "/proc/net/if_inet6"  # the container's IPv6 addresses, one a line, the interface last
READY = "amplifactory guard: blocked "  # the line the pool waits for

# Linux's values (linux/socket.h, linux/netlink.h, linux/rtnetlink.h)
LINUX_FAMILY = {4: 2, 6: 10}  # AF_INET, AF_INET6
AF_NETLINK = 16
NETLINK_ROUTE = 0
NLMSG_ERROR = 2
RTM_NEWROUTE = 24
NLM_F_REQUEST, NLM_F_ACK, NLM_F_EXCL, NLM_F_CREATE = 0x1, 0x4, 0x200, 0x400
RT_TABLE_MAIN = 254
RTPROT_STATIC = 4
RT_SCOPE_UNIVERSE = 0
RTN_PROHIBIT = 8
RTA_DST = 1
NLMSG_HEADER = "=IHHII"  # length, type, flags, sequence, port id
RTMSG = "=BBBBBBBBI"  # family, dst_len, src_len, tos, table, protocol, scope, type, flags


class GuardError(Exception):
    """Why the job can't start: printed as `amplifactory guard: ...`."""


def addresses(name: str, resolve=socket.getaddrinfo) -> list:
    """Every IPv4 and IPv6 address of `name`, each once, in the resolver's order; [] when it
    doesn't resolve."""
    try:
        infos = resolve(name, None)
    except OSError:  # socket.gaierror is one
        return []
    found: list = []
    for _, _, _, _, sockaddr in infos:
        try:
            addr = str(ipaddress.ip_address(str(sockaddr[0]).split("%")[0]))
        except ValueError:
            continue
        if addr not in found:
            found.append(addr)
    return found


def within(seconds: float, fn, *args):
    """fn(*args), or None when it takes longer than `seconds`: it's left to finish in a daemon
    thread, which the exec ends."""
    box: list = []
    worker = threading.Thread(target=lambda: box.append(fn(*args)), daemon=True)
    worker.start()
    worker.join(seconds)
    return box[0] if box else None


def read_text(path: str) -> str:
    """A file's text, or "" when it's missing (a kernel without IPv6)."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def ipv6_interfaces(text: str) -> list:
    """The interfaces other than lo with an IPv6 address, from /proc/net/if_inet6's text. A socket
    bound to such an interface gets past an IPv6 prohibit route, which sits on lo, so the pool
    starts each container with IPv6 off (net.ipv6.conf.all.disable_ipv6=1), and this says when
    it isn't."""
    found: list = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 6 and parts[5] != "lo" and parts[5] not in found:
            found.append(parts[5])
    return found


def subnets(addrs: list) -> list:
    """The Docker Desktop subnet around each address: its /24, or its /64."""
    found: list = []
    for addr in addrs:
        ip = ipaddress.ip_address(addr)
        net = ipaddress.ip_network(f"{ip}/{PREFIX[ip.version]}", strict=False)
        if net not in found:
            found.append(net)
    return found


def route_request(net, seq: int = 1) -> bytes:
    """The netlink RTM_NEWROUTE request for a prohibit route to `net`, in the main table."""
    raw = net.network_address.packed
    rtmsg = struct.pack(
        RTMSG,
        LINUX_FAMILY[net.version],
        net.prefixlen,
        0,
        0,
        RT_TABLE_MAIN,
        RTPROT_STATIC,
        RT_SCOPE_UNIVERSE,
        RTN_PROHIBIT,
        0,
    )
    body = rtmsg + struct.pack("=HH", 4 + len(raw), RTA_DST) + raw
    flags = NLM_F_REQUEST | NLM_F_ACK | NLM_F_CREATE | NLM_F_EXCL
    return struct.pack(NLMSG_HEADER, 16 + len(body), RTM_NEWROUTE, flags, seq, 0) + body


def check_reply(reply: bytes) -> None:
    """Pass an ACK (an error message whose error is 0); raise the OSError of any other error,
    and GuardError for a reply that isn't one."""
    if len(reply) < 20:
        raise GuardError(f"netlink: a reply of {len(reply)} bytes")
    (kind,) = struct.unpack("=H", reply[4:6])
    if kind != NLMSG_ERROR:
        raise GuardError(f"netlink: a reply of type {kind}, not an acknowledgement")
    (code,) = struct.unpack("=i", reply[16:20])
    if code:
        raise OSError(-code, os.strerror(-code))


def add_route(net, sock=None) -> None:
    """Add the prohibit route to `net` (needs NET_ADMIN); OSError when the kernel refuses."""
    with sock or socket.socket(AF_NETLINK, socket.SOCK_RAW, NETLINK_ROUTE) as nl:
        nl.bind((0, 0))
        nl.send(route_request(net))
        check_reply(nl.recv(65536))


def blocked(addr: str, port: int, connect=socket.create_connection) -> bool:
    """Does a TCP connect to `addr` fail at once, as a prohibit route (or no route) makes it? A
    refused connection, a timeout or a connection means the address is reachable."""
    try:
        connect((addr, port), timeout=CHECK_S).close()
    except OSError as exc:
        return errno.errorcode.get(exc.errno or 0) in BLOCKED
    return False


def runner_command(run_sh: str, environ) -> tuple:
    """setpriv's argv and environment: RUN_SH as `runner`, NET_ADMIN gone from the bounding set
    (so `sudo` can't get it back), no inheritable capabilities. The rest of the environment, the
    JIT config and the hook among it, is kept."""
    argv = [
        SETPRIV,
        f"--reuid={RUNNER}",
        f"--regid={RUNNER}",
        "--init-groups",
        "--bounding-set=-net_admin",
        "--inh-caps=-all",
        "--",
        run_sh,
    ]
    env = dict(environ, HOME=f"/home/{RUNNER}", USER=RUNNER, LOGNAME=RUNNER)
    return argv, env


def guard(
    run_sh: str,
    environ,
    resolve=socket.getaddrinfo,
    add=add_route,
    connect=socket.create_connection,
    execve=os.execve,
    out=None,
    access=os.access,
    getpwnam=pwd.getpwnam,
    lookup_s: float = LOOKUP_S,
    inet6=lambda: read_text(IF_INET6),
) -> None:
    """Block the subnets, check them, say so, then become the runner. Returns only through an
    exception."""
    on = ipv6_interfaces(inet6())
    if on:
        why = "a socket bound to it would get past the block: start the container with IPv6 off"
        raise GuardError(f"IPv6 is on in the container ({', '.join(on)}): {why}")
    hosts = addresses(HOST, resolve)
    if not hosts:
        raise GuardError(f"{HOST} has no address")
    nets = subnets(hosts)
    refused: dict = {}
    for net in nets:
        try:
            add(net)
        except OSError as exc:  # its outcome is what counts: a subnet with no route is fine
            refused[net] = exc
    targets = [(addr, HOST_PORT) for addr in hosts]
    targets += [(str(net.network_address + 1), PROXY_PORT) for net in nets]
    targets += [(addr, PROXY_PORT) for addr in within(lookup_s, addresses, PROXY, resolve) or ()]
    for addr, port in targets:
        if not blocked(addr, port, connect):
            ip = ipaddress.ip_address(addr)
            failed = [exc for net, exc in refused.items() if ip in net]
            why = f": its route failed ({failed[0]})" if failed else ""
            raise GuardError(f"{addr} port {port} is still reachable{why}")
    if not access(SETPRIV, os.X_OK):  # so the line below is followed by the runner
        raise GuardError(f"no {SETPRIV} to run the runner with")
    try:
        getpwnam(RUNNER)
    except KeyError:
        raise GuardError(f"no user {RUNNER} to run the runner as") from None
    print(f"{READY}{', '.join(map(str, nets))}", file=out or sys.stderr, flush=True)
    argv, env = runner_command(run_sh, environ)
    execve(argv[0], argv, env)
    raise GuardError("execve returned")


def main(argv: list | None = None, err=None, **fakes) -> int:
    argv = sys.argv[1:] if argv is None else argv
    err = err or sys.stderr
    if argv == ["--self-test"]:
        _self_test()
        return 0
    if len(argv) != 1:
        print("usage: guard.py RUN_SH (as root with NET_ADMIN, in a job's container)", file=err)
        return 2
    try:
        guard(argv[0], fakes.pop("environ", os.environ), **fakes)
    except (GuardError, OSError) as exc:
        print(f"amplifactory guard: {exc}", file=err, flush=True)
    return 1


# ------------------------------------------------------------------------------------- self-test


def _infos(*addrs) -> list:
    """What getaddrinfo returns for these addresses: each twice, as it lists them."""
    out = []
    for addr in addrs:
        family = socket.AF_INET6 if ":" in addr else socket.AF_INET
        sockaddr = (addr, 0, 0, 0) if family == socket.AF_INET6 else (addr, 0)
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            out.append((family, kind, 0, "", sockaddr))
    return out


def _fails_with(code: int):
    def connect(address, timeout=None):
        raise OSError(code, os.strerror(code))

    return connect


class _Out:
    """A stream that keeps what's written to it, in `lines` (and in `log`, if given)."""

    def __init__(self, log: list | None = None) -> None:
        self.text = ""
        self.log = log

    def write(self, text: str) -> None:
        self.text += text
        if self.log is not None and text.strip():
            self.log.append(("say", text.strip()))

    def flush(self) -> None:
        pass


def _test_request() -> None:
    v4 = route_request(ipaddress.ip_network("192.168.65.0/24"))
    want = "24000000" + "1800" + "0506" + "01000000" + "00000000"  # the netlink header
    want += "02" + "18" + "0000" + "fe" + "04" + "00" + "08" + "00000000"  # AF_INET, /24, prohibit
    want += "0800" + "0100" + "c0a84100"  # RTA_DST 192.168.65.0
    assert v4.hex() == want, v4.hex()
    v6 = route_request(ipaddress.ip_network("fdc4:f303:9324::/64"), seq=2)
    want = "30000000" + "1800" + "0506" + "02000000" + "00000000"
    want += "0a" + "40" + "0000" + "fe" + "04" + "00" + "08" + "00000000"  # AF_INET6 (10), /64
    want += "1400" + "0100" + "fdc4f3039324" + "00" * 10
    assert v6.hex() == want, v6.hex()

    def reply(kind: int, code: int) -> bytes:
        return struct.pack(NLMSG_HEADER, 36, kind, 0, 1, 0) + struct.pack("=i", code) + bytes(16)

    check_reply(reply(NLMSG_ERROR, 0))  # an acknowledgement
    try:
        check_reply(reply(NLMSG_ERROR, -errno.EPERM))
        raise AssertionError("an error passed")
    except OSError as exc:
        assert exc.errno == errno.EPERM, exc
    for bad in (reply(RTM_NEWROUTE, 0), b"\0" * 12):
        try:
            check_reply(bad)
            raise AssertionError("a reply that isn't an acknowledgement passed")
        except GuardError:
            pass

    class _Socket:
        def __init__(self) -> None:
            self.sent: list = []

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            pass

        def bind(self, address) -> None:
            assert address == (0, 0)

        def send(self, data: bytes) -> None:
            self.sent.append(data)

        def recv(self, size: int) -> bytes:
            return reply(NLMSG_ERROR, -errno.EEXIST)

    sock = _Socket()
    try:
        add_route(ipaddress.ip_network("192.168.65.0/24"), sock)
        raise AssertionError("a refused route passed")
    except OSError as exc:
        assert exc.errno == errno.EEXIST and sock.sent == [v4], (exc, sock.sent)


def _test_addresses() -> None:
    both = _infos("192.168.65.254", "fdc4:f303:9324::254", "192.168.65.254", "fe80::1%eth0")
    got = addresses(HOST, lambda name, port: both)
    assert got == ["192.168.65.254", "fdc4:f303:9324::254", "fe80::1"], got
    assert addresses(HOST, lambda name, port: []) == []

    def unknown(name, port):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    assert addresses(PROXY, unknown) == []
    lo_only = "00000000000000000000000000000001 01 80 10 80       lo\n"
    assert ipv6_interfaces("") == [] and ipv6_interfaces(lo_only) == []
    eth = "fdea807a40b400000000000000000002 0b 40 00 00     eth0\n"
    assert ipv6_interfaces(lo_only + eth + eth.replace("fdea", "fe80")) == ["eth0"]
    assert read_text("/nonexistent/if_inet6") == ""
    nets = subnets(["192.168.65.254", "fdc4:f303:9324::254", "192.168.65.7"])
    assert list(map(str, nets)) == ["192.168.65.0/24", "fdc4:f303:9324::/64"], nets


def _test_blocked() -> None:
    for code in (errno.EACCES, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EADDRNOTAVAIL):
        assert blocked("192.168.65.254", 9, _fails_with(code)), code
    for code in (errno.ECONNREFUSED, errno.ETIMEDOUT):
        assert not blocked("192.168.65.254", 9, _fails_with(code)), code

    def timeout(address, timeout=None):
        raise TimeoutError("timed out")  # no errno

    class _Sock:
        def close(self) -> None:
            pass

    assert not blocked("192.168.65.254", 9, timeout)
    assert not blocked("192.168.65.254", 9, lambda address, timeout=None: _Sock())


def _test_guard() -> None:
    environ = {"PATH": "/usr/bin", "HOME": "/root", "ACTIONS_RUNNER_INPUT_JITCONFIG": "JIT"}
    names = {HOST: ["192.168.65.254", "fdc4:f303:9324::254"], PROXY: ["192.168.65.1"]}
    calls: list = []

    def resolve(name, port):
        calls.append(("resolve", name))
        return _infos(*names.get(name, ()))

    def add(net):
        calls.append(("add", str(net)))

    timeouts: list = []

    def connect(address, timeout=None):
        calls.append(("connect", address))
        timeouts.append(timeout)
        raise OSError(errno.EACCES, "Permission denied")

    def execve(path, argv, env):
        calls.append(("exec", path, argv, env))

    fakes = {"environ": environ, "resolve": resolve, "add": add, "connect": connect}
    fakes.update(execve=execve, access=lambda path, mode: True, getpwnam=lambda name: name)
    fakes.update(inet6=lambda: "")  # not this machine's /proc/net/if_inet6: Linux CI has IPv6
    err = _Out()
    assert main(["/home/runner/run.sh"], err, out=_Out(calls), **dict(fakes)) == 1  # a fake exec
    assert calls[:4] == [
        ("resolve", HOST),
        ("add", "192.168.65.0/24"),
        ("add", "fdc4:f303:9324::/64"),
        ("resolve", PROXY),
    ], calls
    checks = calls[4:9]
    assert [call[1] for call in checks] == [
        ("192.168.65.254", 9),  # the host gateway
        ("fdc4:f303:9324::254", 9),
        ("192.168.65.1", 3128),  # each subnet's first address, on the proxy's port
        ("fdc4:f303:9324::1", 3128),
        ("192.168.65.1", 3128),  # http.docker.internal
    ], calls
    assert calls[9] == ("say", f"{READY}192.168.65.0/24, fdc4:f303:9324::/64"), calls[9]
    _, path, argv, env = calls[10]
    assert path == SETPRIV and argv[0] == SETPRIV and argv[-2:] == ["--", "/home/runner/run.sh"]
    for option in ("--reuid=runner", "--regid=runner", "--init-groups"):
        assert option in argv, argv
    assert "--bounding-set=-net_admin" in argv and "--inh-caps=-all" in argv, argv
    assert env["HOME"] == "/home/runner" and env["USER"] == env["LOGNAME"] == "runner"
    assert env["ACTIONS_RUNNER_INPUT_JITCONFIG"] == "JIT" and env["PATH"] == "/usr/bin"
    assert environ["HOME"] == "/root", "the caller's environment is left alone"
    assert err.text == "amplifactory guard: execve returned\n", err.text
    assert timeouts and set(timeouts) == {CHECK_S}, "each check is bounded"

    # a route the kernel refuses is fine while its subnet is blocked anyway (no IPv6 route)...
    def no_v6(net):
        if net.version == 6:
            raise OSError(errno.EACCES, "Permission denied")

    def v6_unroutable(address, timeout=None):
        code = errno.ENETUNREACH if ":" in address[0] else errno.EACCES
        raise OSError(code, os.strerror(code))

    calls.clear()
    changed = dict(fakes, add=no_v6, connect=v6_unroutable)
    assert main(["/r"], _Out(), out=_Out(), **changed) == 1 and calls[-1][0] == "exec", calls

    # ...but not while it's reachable: each failure stops before the line and the exec
    def proxy_open(address, timeout=None):
        if address == ("192.168.65.1", 3128):
            raise OSError(errno.ECONNREFUSED, "Connection refused")
        raise OSError(errno.EACCES, "Permission denied")

    def refused(address, timeout=None):
        raise OSError(errno.ECONNREFUSED, "Connection refused")

    def denied(net):
        raise OSError(errno.EPERM, "Operation not permitted")

    def missing(path, argv, env):
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)

    def nobody(name):
        raise KeyError(name)

    inet6 = (
        "00000000000000000000000000000001 01 80 10 80       lo\n"
        "fe80000000000000948881fffe1e4137 0b 40 20 80     eth0\n"
    )
    for change, said in (
        (
            {"inet6": lambda: inet6},
            (
                "IPv6 is on in the container (eth0): a socket bound to it would get past the "
                "block: start the container with IPv6 off"
            ),
        ),
        ({"connect": refused}, "192.168.65.254 port 9 is still reachable"),
        ({"connect": proxy_open}, "192.168.65.1 port 3128 is still reachable"),
        (
            {"connect": refused, "add": denied},
            (
                "192.168.65.254 port 9 is still reachable: its route failed "
                "([Errno 1] Operation not permitted)"
            ),
        ),
        ({"resolve": lambda name, port: []}, f"{HOST} has no address"),
        ({"access": lambda path, mode: False}, "no /usr/bin/setpriv to run the runner with"),
        ({"getpwnam": nobody}, "no user runner to run the runner as"),
        ({"execve": missing}, "[Errno 2] No such file or directory: '/usr/bin/setpriv'"),
    ):
        calls.clear()
        err = _Out()
        out = _Out(calls)
        assert main(["/r"], err, out=out, **dict(fakes, **change)) == 1
        assert err.text == f"amplifactory guard: {said}\n", err.text
        kinds = [call[0] for call in calls]
        if "execve" not in change:
            assert "exec" not in kinds and "say" not in kinds and not out.text, calls
    assert main([], _Out()) == 2 and main(["a", "b"], _Out()) == 2

    # a lookup of PROXY that hangs is left behind: the guard still ends in time
    gate = threading.Event()

    def slow(name, port):
        if name == PROXY:
            gate.wait(10)
        return resolve(name, port)

    calls.clear()
    changed = dict(fakes, resolve=slow, lookup_s=0.2)
    began = threading.Event()  # a clock that doesn't need `time`: the gate would hold 10 s
    try:
        timer = threading.Timer(5, began.set)
        timer.start()
        assert main(["/r"], _Out(), out=_Out(calls), **changed) == 1 and calls[-1][0] == "exec"
        assert not began.is_set(), "the guard waited out the lookup"
        timer.cancel()
    finally:
        gate.set()
    assert ("192.168.65.1", 3128) in [call[1] for call in calls if call[0] == "connect"]
    assert within(0.05, gate.wait, 0) is True and within(0.05, threading.Event().wait, 1) is None


def _self_test() -> None:
    _test_request()
    _test_addresses()
    _test_blocked()
    _test_guard()
    print("guard.py self-test: ok")


if __name__ == "__main__":
    sys.exit(main())

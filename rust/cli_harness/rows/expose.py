"""CLI-EXPOSE: ``pseudolife-mcp expose`` against a stand-in ``tailscale``.

Canonical argv: ``expose -h``, ``expose {tailscale,off,status} -h``,
argparse's errors, ``expose status [--port N] [--json]``, ``expose off
[--port N] [--yes] [--json]`` and ``expose tailscale --port N [--yes]
[--json]`` up to the change. The ``tailscale`` program on every case's
``PATH`` is a scripted stand-in (``_STAND_IN``): it answers ``status
--json``, ``ip -4`` and ``serve status --json`` from the case's scenario,
applies ``serve --bg`` and ``serve --tcp=N off`` to its own state file, and
appends every argv it receives to ``ts/calls.jsonl``. Both files stay in
the observation, so the exact commands each arm ran and the serve state it
left are compared. The daemon is a fixture serving ``/health``.

Safety (checked when the row loads): every case declares ``tailscale`` in
``programs`` with ``PATH`` inside the home, so core's preflight proves no
real ``tailscale`` resolves; every case whose action is ``tailscale`` names
its port (the fixture's, or the closed port 9), so no arm reaches a daemon
on the default 8765; and every address the stand-in reports in such a case
is on loopback, so the tailnet probe never leaves the machine. Scope and
deferrals: ``rows/expose.md``.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import sys

from .. import core, normalize
from ..core import Case
from ..fixture_daemon import FixtureDaemon, json_body, redirect, text
from ..mutants import Mutant

DEFERRAL = "pseudolife-stdio: mode 'expose' is deferred in this candidate\n"
FILE = "shim/src/cli/expose/mod.rs"
TS_FILE = "shim/src/cli/expose/tailscale.rs"
ARGS_FILE = "shim/src/cli/expose/args.rs"
DEAD_PORT = "9"
TS_PATH = "{HOME}/bin"
# The lookup's inputs, pinned: PATH holds only the stand-in's directory, and
# PATHEXT (Windows) fixes the extension spelling shutil.which reports.
ENV = {"PATH": TS_PATH, "PATHEXT": ".COM;.EXE;.BAT;.CMD"}
PROGRAMS = ("tailscale",)


def _native(value: str) -> bytes:
    return (value.replace("\n", "\r\n") if core.WINDOWS else value).encode()


# --- the stand-in -----------------------------------------------------------------

_STAND_IN = r'''import base64, json, os, sys
from pathlib import Path
root = Path(os.environ["HOME"]) / "ts"
root.mkdir(exist_ok=True)
args = sys.argv[1:]
with (root / "calls.jsonl").open("a", encoding="utf-8", newline="\n") as log:
    log.write(json.dumps(args) + "\n")
scenario = json.loads((root / "scenario.json").read_text(encoding="utf-8"))
state = root / "serve.json"
port = scenario["port"]


def config():
    if state.exists():
        return state.read_text(encoding="utf-8")
    return scenario["serve"]


def answer(spec, default_out=""):
    spec = spec or {}
    out = spec.get("out", default_out)
    err = spec.get("err", "")
    out = base64.b64decode(spec["out_b64"]) if "out_b64" in spec else out.encode("utf-8")
    err = base64.b64decode(spec["err_b64"]) if "err_b64" in spec else err.encode("utf-8")
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(err)
    sys.stderr.buffer.flush()
    raise SystemExit(spec.get("code", 0))


def edit(change):
    try:
        current = json.loads(config())
    except ValueError:
        current = None
    if not isinstance(current, dict):
        current = {}
    tcp = current.get("TCP") if isinstance(current.get("TCP"), dict) else {}
    change(tcp)
    current["TCP"] = tcp
    if not tcp:
        del current["TCP"]
    state.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")


if args == ["status", "--json"]:
    answer(scenario.get("status"))
if args == ["ip", "-4"]:
    answer(scenario.get("ip4"))
if args == ["serve", "status", "--json"]:
    reads = scenario.get("reads", [])
    count = len([1 for line in (root / "calls.jsonl").read_text(encoding="utf-8").splitlines()
                 if json.loads(line) == args])
    spec = reads[count - 1] if count <= len(reads) and reads[count - 1] else {}
    answer(spec, config())
if args == ["serve", "--bg", f"--tcp={port}", f"tcp://127.0.0.1:{port}"]:
    spec = scenario.get("bg") or {}
    effect = spec.get("effect", "ours")
    if effect == "ours":
        edit(lambda tcp: tcp.__setitem__(str(port), {"TCPForward": f"127.0.0.1:{port}"}))
    elif effect == "tls":
        edit(lambda tcp: tcp.__setitem__(str(port), {"TCPForward": f"127.0.0.1:{port}",
                                                     "TerminateTLS": "node.example.com"}))
    answer(spec)
if args == ["serve", f"--tcp={port}", "off"]:
    spec = scenario.get("off") or {}
    if spec.get("effect", "removed") == "removed":
        edit(lambda tcp: tcp.pop(str(port), None))
    answer(spec)
sys.stderr.write("stand-in tailscale: unexpected arguments\n")
raise SystemExit(99)
'''


def _wrapper(script) -> tuple[str, bytes]:
    if core.WINDOWS:
        body = f'@echo off\r\n"{sys.executable}" -I "{script}" %*\r\nexit /b %errorlevel%\r\n'
        return "tailscale.cmd", body.encode()
    body = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I {shlex.quote(str(script))} "$@"\n'
    return "tailscale", body.encode()


def _port_of(arm, fixed: str | None) -> int:
    """The port the case's ``--port`` names: ``fixed``, else the fixture
    daemon's, else expose's default."""
    if fixed is not None:
        return int(fixed)
    if arm.daemon is not None:
        return int(arm.daemon.url.rsplit(":", 1)[1])
    return 8765


def _scenario_setup(scenario: dict | None, port: str | None, extra=None):
    """Install the stand-in (unless ``scenario`` is None) and ``extra`` files.
    ``{PORT}`` in the scenario's strings becomes the arm's port."""
    def setup(arm):
        inputs = {}
        bin_dir = arm.home / "bin"
        bin_dir.mkdir()
        if scenario is not None:
            number = _port_of(arm, port)
            ts = arm.home / "ts"
            ts.mkdir()
            script = ts / "stand_in.py"
            script.write_text(_STAND_IN, encoding="utf-8")
            data = json.loads(json.dumps(scenario).replace("{PORT}", str(number)))
            data["port"] = number
            (ts / "scenario.json").write_text(json.dumps(data), encoding="utf-8")
            name, body = _wrapper(script)
            wrapper = bin_dir / name
            wrapper.write_bytes(body)
            wrapper.chmod(0o700)
            for path in (script, ts / "scenario.json", wrapper):
                inputs[path] = path.read_bytes()
        for rel, data in (extra or {}).items():
            path = arm.home / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            if not core.WINDOWS:
                path.chmod(0o700)
            inputs[path] = data
        arm.state["inputs"] = inputs
    return setup


def _after(arm, obs):
    """The stand-in's inputs hold this machine's interpreter path: check
    each is unchanged, then drop it, leaving the call log and the serve
    state the arm produced."""
    obs["arm"] = arm.name
    for path, data in arm.state.get("inputs", {}).items():
        assert path.read_bytes() == data, f"{path.name} changed under the arm"
        path.unlink()


# --- rules --------------------------------------------------------------------------

@normalize.rule("expose-port")
def expose_port(obs: dict) -> None:
    """The fixture daemon's per-arm port is the CLI's ``--port``: it appears
    in the plan, the URLs, the stand-in's argv log and its serve state. Only
    a whole number equal to that arm's own fixture port becomes ``{PORT}``,
    after ``daemon_tokens`` has already replaced ``127.0.0.1:PORT``."""
    url = obs.get("daemon_url")
    if not url:
        return
    port = url.rsplit(":", 1)[1].encode()
    if not port.isdigit():
        return
    pattern = re.compile(rb"(?<![0-9])" + re.escape(port) + rb"(?![0-9])")
    for field in ("stdout", "stderr"):
        normalize._put(obs, field, pattern.sub(b"{PORT}", normalize._get(obs, field)))
    for rel in list(obs["files"]):
        data = normalize._file(obs, rel)
        if data is not None:
            normalize._set_file(obs, rel, pattern.sub(b"{PORT}", data))


# A fixture input (the arm's own port), so goldens hold it tokenized: a
# recording drops daemon_url, after which this rule could not find the port.
normalize.RECORD_RULES.add("expose-port")


def _mutating(argv: list) -> bool:
    """``serve --bg ...`` or ``serve --tcp=N off``: the calls that change
    the serve config."""
    return argv[:2] == ["serve", "--bg"] or (
        len(argv) == 3 and argv[0] == "serve" and argv[1].startswith("--tcp=")
        and argv[2] == "off")


@normalize.rule("expose-deferral-at-change")
def expose_deferral_at_change(obs: dict) -> None:
    """A declared deferral at the change: the candidate stops, printing the
    dispatcher's deferral line (exit 1), right before the first ``tailscale``
    call that changes the serve config, after the same reads. The oracle
    goes on to make the change. Its observation keeps every call up to that
    first mutating one; the mutating call and everything after it (later
    calls, the stand-in's serve state, any later daemon request) are
    removed only once validated as present, and its streams become that
    expectation. The candidate's observation is untouched."""
    if obs.get("arm") != "python":
        return
    log = normalize._file(obs, "ts/calls.jsonl")
    if log is None:
        return
    lines = log.splitlines(keepends=True)
    cut = next((i for i, line in enumerate(lines) if _mutating(json.loads(line))), None)
    if cut is None:
        return
    normalize._set_file(obs, "ts/calls.jsonl", b"".join(lines[:cut]))
    obs["files"].pop("ts/serve.json", None)
    obs.get("modes", {}).pop("ts/serve.json", None)
    if obs.get("requests"):
        obs["requests"] = obs["requests"][:1]
    obs["exit"] = 1
    normalize._put(obs, "stdout", b"")
    normalize._put(obs, "stderr", _native(DEFERRAL))


@normalize.rule("expose-deferral")
def expose_deferral(obs: dict) -> None:
    """A declared deferral before any effect the stand-in records as a
    change: the candidate prints the deferral line (exit 1). The oracle's
    streams become that expectation; its files and requests stay compared,
    so the candidate must have made exactly the oracle's reads (a case
    chosen so that the oracle changes nothing)."""
    if obs.get("arm") != "python":
        return
    obs["exit"] = 1
    normalize._put(obs, "stdout", b"")
    normalize._put(obs, "stderr", _native(DEFERRAL))


# --- scenarios ----------------------------------------------------------------------

LOOPBACK = "127.0.0.1"
OURS = '{"TCP": {"{PORT}": {"TCPForward": "127.0.0.1:{PORT}"}}}'
RUNNING = {"out": json.dumps({"BackendState": "Running",
                              "Self": {"TailscaleIPs": ["127.0.0.1", "fd7a:115c::1"]}})}
IP4 = {"out": LOOPBACK + "\n"}


def scenario(serve: str = "{}", status=None, ip4=None, reads=None, bg=None, off=None) -> dict:
    return {"serve": serve, "status": RUNNING if status is None else status,
            "ip4": IP4 if ip4 is None else ip4, "reads": reads or [], "bg": bg, "off": off}


def _health(value, status: int = 200):
    return lambda: FixtureDaemon({"/health": json_body(value, status)})


HEALTHY = {"status": "ok", "auth": True, "bank": "fixture-bank-1"}


# --- cases --------------------------------------------------------------------------

def _names_port(argv: list[str]) -> bool:
    """A ``tailscale`` action reaches ``127.0.0.1:<port>/health``: the case
    must name the fixture's port or the closed port, or stop in argparse."""
    words = argv[1:]
    if words[:1] != ["tailscale"]:
        return True
    if any(w in ("-h", "--help") for w in words):
        return True
    for index, word in enumerate(words):
        if word.startswith("--po"):
            value = (word.split("=", 1)[1] if "=" in word else
                     words[index + 1] if index + 1 < len(words) else "")
            if value in ("{DAEMON_PORT}", DEAD_PORT) or not value.isdigit() or \
                    not 1 <= int(value) <= 65535:
                return True
    return False


_ADDRESS = re.compile(r"(?<![0-9.])[0-9]{1,3}(?:\.[0-9]{1,3}){3}(?![0-9.])")


def _loopback_only(value) -> bool:
    return all(found.startswith("127.") for found in _ADDRESS.findall(json.dumps(value)))


def case(case_id: str, argv: list[str], *, scenario_=None, daemon=None, port=None,
         rules=(), env=None, extra=None, platforms=("windows", "linux"), timeout=40.0,
         expect_output=False) -> Case:
    full = ["expose", *argv]
    if not _names_port(full):
        raise AssertionError(f"{case_id}: a tailscale case must name its port")
    if full[1:2] == ["tailscale"] and scenario_ is not None and not _loopback_only(scenario_):
        raise AssertionError(f"{case_id}: a tailscale case reports only loopback addresses")
    if daemon is not None:
        rules = ("expose-port", *rules)
    return Case(case_id, full, env={**ENV, **(env or {})}, rules=rules,
                setup=_scenario_setup(scenario_, port, extra), after=_after,
                platforms=platforms, timeout=timeout, expect_output=expect_output,
                daemon=daemon, programs=PROGRAMS)


def at(port_value: str = "{DAEMON_PORT}") -> list[str]:
    return ["--port", port_value]


def cases() -> list[Case]:
    c: list[Case] = []
    add = c.append
    s = scenario

    # argparse: help, and usage errors at COLUMNS=80.
    add(case("help", ["-h"]))
    add(case("help-long", ["--help"]))
    add(case("help-before-word", ["-h", "bogus"]))
    add(case("help-tailscale", ["tailscale", "-h"]))
    add(case("help-off", ["off", "--help"]))
    add(case("help-status", ["status", "-h"]))
    add(case("help-after-extra", ["status", "zz", "-h"]))
    add(case("help-after-port", ["off", "--port", "70000", "-h"]))
    add(case("no-action", []))
    add(case("invalid-action", ["bogus"]))
    add(case("invalid-action-empty", [""]))
    add(case("invalid-action-space", ["a b", "--port", "x"]))
    add(case("invalid-action-then-help", ["bogus", "-h"]))
    add(case("port-missing", ["tailscale", "--port"]))
    add(case("port-text", ["tailscale", "--port", "x"]))
    add(case("port-zero", ["off", "--port", "0"]))
    add(case("port-high", ["status", "--port", "65536"]))
    add(case("port-empty-joined", ["tailscale", "--port="]))
    add(case("port-joined-text", ["off", "--port=8x"]))
    add(case("port-quote", ["status", "--port", "it's"]))
    add(case("port-other-digits", ["status", "--port", "８７６５"]))
    add(case("port-arabic-digits", ["off", "--port", "٨٧٦٥", "--yes"]))
    add(case("yes-explicit", ["off", "--yes=1"]))
    add(case("json-explicit", ["status", "--json="]))
    add(case("status-yes", ["status", "--yes"]))
    add(case("extras", ["off", "a", "b c"]))
    add(case("extras-then-bad-port", ["tailscale", "zz", "--port", "x"]))
    add(case("extras-tailscale", ["tailscale", "--port", DEAD_PORT, "zz"]))

    # Declared deferrals in argparse (before any effect).
    for case_id, argv in (("defer-abbrev-main", ["--he"]),
                          ("defer-abbrev-port", ["status", "--po", "8765"]),
                          ("defer-double-dash", ["status", "--"]),
                          ("defer-dash-value", ["tailscale", "--port", "-1"]),
                          ("defer-short-other", ["status", "-x"]),
                          ("defer-port-plus", ["off", "--port", "+8765"]),
                          ("defer-option-first", ["--port", "9", "status"])):
        # No stand-in: the candidate defers before any lookup, so the oracle
        # arm must make no tailscale call either.
        add(case(case_id, argv, rules=("expose-deferral",)))
    add(case("defer-help-width", ["-h"], env={"COLUMNS": "100"}, rules=("expose-deferral",)))
    add(case("defer-int-digits", ["status"], env={"PYTHONINTMAXSTRDIGITS": "640"},
             rules=("expose-deferral",)))

    # status: no tailscale CLI.
    add(case("status-not-installed", ["status"]))
    add(case("status-not-installed-json", ["status", "--json"]))
    # The lookup never searches the current directory or a relative PATH
    # entry: a decoy there (exit 99 if run) is never found.
    decoy = ("tailscale.cmd", b"@exit /b 99\r\n") if core.WINDOWS else (
        "tailscale", b"#!/bin/sh\nexit 99\n")
    add(case("status-cwd-not-searched", ["status"], extra={f"cwd/{decoy[0]}": decoy[1]}))
    add(case("status-relative-path-skipped", ["status", "--json"], scenario_=s(),
             env={"PATH": os.pathsep.join([".", "bin", "", TS_PATH])},
             extra={f"cwd/{decoy[0]}": decoy[1], f"cwd/bin/{decoy[0]}": decoy[1]}))
    add(case("off-relative-path-skipped", ["off"], scenario_=s(serve=OURS),
             env={"PATH": os.pathsep.join(["", ".", TS_PATH])},
             extra={f"cwd/{decoy[0]}": decoy[1]}))
    # status: the backend.
    add(case("status-not-running", ["status"], scenario_=s(status={
        "code": 1, "err": "\nfailed to connect to local tailscaled; it doesn't appear to be "
                          "running\n"})))
    add(case("status-not-running-blank-line", ["status"], scenario_=s(status={
        "code": 1, "err": " \t \n  the daemon is down  \n"})))
    add(case("status-not-running-exit", ["status", "--json"],
             scenario_=s(status={"out": "  \n", "code": 3})))
    add(case("status-not-json", ["status"], scenario_=s(status={"out": "{oops"})))
    add(case("status-json-list", ["status"], scenario_=s(status={"out": "[1]"})))
    for name, state in (("stopped", "Stopped"), ("needs-login", "NeedsLogin"),
                        ("number", 7), ("null", None), ("quote", "it's")):
        add(case(f"status-backend-{name}", ["status"],
                 scenario_=s(status={"out": json.dumps({"BackendState": state})})))
    add(case("status-backend-missing-json", ["status", "--json"],
             scenario_=s(status={"out": "{}"})))
    # status: the serve config.
    add(case("status-serve-fails", ["status"], scenario_=s(reads=[
        {"code": 1, "err": "serve config denied\nmore\n"}])))
    add(case("status-serve-fails-quiet", ["status", "--json"], scenario_=s(reads=[{"code": 2}])))
    add(case("status-serve-not-json", ["status"], scenario_=s(serve="<html>")))
    add(case("status-serve-list", ["status"], scenario_=s(serve="[]")))
    add(case("status-serve-null", ["status"], scenario_=s(serve="null")))
    add(case("status-serve-empty", ["status"], scenario_=s(serve="")))
    add(case("status-none", ["status"], scenario_=s()))
    add(case("status-none-json", ["status", "--json"], scenario_=s()))
    # status: someone else's serve on the port.
    for name, handler in (
            ("tls", {"TCPForward": "127.0.0.1:{PORT}", "TerminateTLS": "node.example.com"}),
            ("https", {"HTTPS": True}),
            ("http", {"HTTP": True, "TCPForward": "127.0.0.1:1"}),
            ("proxy", {"TCPForward": "127.0.0.1:{PORT}", "ProxyProtocol": 2}),
            ("elsewhere", {"TCPForward": "127.0.0.1:8000"}),
            ("number-forward", {"TCPForward": 5}),
            ("unknown", {}),
            ("falsy", {"TCPForward": "", "HTTPS": False})):
        add(case(f"status-foreign-{name}", ["status"],
                 scenario_=s(serve=json.dumps({"TCP": {"{PORT}": handler}}))))
    add(case("status-foreign-unreadable", ["status", "--json"],
             scenario_=s(serve='{"TCP": {"{PORT}": 5}}')))
    add(case("status-foreign-list", ["status"], scenario_=s(serve='{"TCP": {"{PORT}": []}}')))
    add(case("status-foreground", ["status"], scenario_=s(serve=json.dumps(
        {"Foreground": {"session-1": {"TCP": {"{PORT}": {"TCPForward": "127.0.0.1:{PORT}"}}}}}))))
    add(case("status-funnel", ["status"], scenario_=s(serve=json.dumps(
        {"TCP": {"{PORT}": {"TCPForward": "127.0.0.1:{PORT}"}},
         "AllowFunnel": {"node.example.com:{PORT}": True}}))))
    add(case("status-funnel-foreground", ["status", "--json"], scenario_=s(serve=json.dumps(
        {"Foreground": {"a": {"AllowFunnel": {"node.example.com:{PORT}": 1}}}}))))
    add(case("status-funnel-elsewhere", ["status"], scenario_=s(serve=json.dumps(
        {"TCP": {"{PORT}": {"TCPForward": "127.0.0.1:{PORT}"}},
         "AllowFunnel": {"node.example.com:443": True, "node.example.com:{PORT}": False}}))))
    add(case("status-null-handler", ["status"], scenario_=s(serve='{"TCP": {"{PORT}": null}}')))
    add(case("status-other-port", ["status", "--port", "8443"], scenario_=s(serve=OURS)))
    # status: ours.
    add(case("status-exposed", ["status"], scenario_=s(serve=OURS)))
    add(case("status-exposed-json", ["status", "--json"], scenario_=s(serve=OURS)))
    add(case("status-exposed-port", ["status", "--port", "08443"], port="8443",
             scenario_=s(serve=OURS)))
    add(case("status-ip-fallback", ["status"], scenario_=s(serve=OURS, ip4={"code": 1})))
    add(case("status-ip-unicode-space", ["status"],
             scenario_=s(serve=OURS, ip4={"out": "x 127.0.0.2\n"})))
    add(case("status-ip-none", ["status"], scenario_=s(serve=OURS, ip4={"out": "fd7a::1\n"},
                                                       status={"out": '{"BackendState": "Running"}'})))
    add(case("status-ip-none-json", ["status", "--json"], scenario_=s(
        serve=OURS, ip4={"out": "1.2.3\n"},
        status={"out": '{"BackendState": "Running", "Self": {"TailscaleIPs": "127.0.0.1"}}'})))
    add(case("status-ip-dict", ["status"], scenario_=s(
        serve=OURS, ip4={"code": 1},
        status={"out": '{"BackendState": "Running", "Self": {"TailscaleIPs": {"127.0.0.3": 1}}}'})))
    add(case("status-ip-self-list", ["status"], scenario_=s(
        serve=OURS, ip4={"code": 1},
        status={"out": '{"BackendState": "Running", "Self": []}'})))
    # status: TailscaleIPs that is not a list is ignored; only an ASCII
    # dotted quad with octets 0-255 and no leading zero is an address.
    for name, own in (("number", "7"), ("float", "2.5"), ("true", "true")):
        add(case(f"status-ip-{name}", ["status"], scenario_=s(
            serve=OURS, ip4={"code": 1},
            status={"out": '{"BackendState": "Running", "Self": {"TailscaleIPs": %s}}' % own})))
    add(case("status-ip-digits", ["status"], scenario_=s(
        serve=OURS, ip4={"out": "١٢٧.0.0.1\n"})))
    add(case("status-ip-fullwidth-json", ["status", "--json"], scenario_=s(
        serve=OURS, ip4={"out": "１２７.0.0.1\n"},
        status={"out": '{"BackendState": "Running", "Self": {"TailscaleIPs": ["١.2.3.4"]}}'})))
    add(case("status-ip-octet", ["status"], scenario_=s(
        serve=OURS, ip4={"out": "999.1.1.1 256.0.0.1\n"})))
    add(case("status-ip-leading-zero", ["status", "--json"], scenario_=s(
        serve=OURS, ip4={"out": "127.0.0.01 0127.0.0.1\n"},
        status={"out": '{"BackendState": "Running", "Self": {"TailscaleIPs": ["010.0.0.1", "127.0.0.0"]}}'})))
    add(case("status-ip-undecodable", ["status"], scenario_=s(
        serve=OURS, ip4={"out_b64": base64.b64encode(b"\xff 10.0.0.1\n").decode()})))
    # A lone surrogate is printed escaped, as on stderr.
    add(case("status-surrogate-detail", ["status"],
             scenario_=s(serve='{"TCP": {"{PORT}": {"TCPForward": "\\udc80"}}}')))
    add(case("status-surrogate-detail-json", ["status", "--json"],
             scenario_=s(serve='{"TCP": {"{PORT}": {"TCPForward": "\\udc80"}}}')))
    # Output that cannot be read: nested past 100 brackets, or not UTF-8.
    add(case("status-deep-at-bound", ["status"],
             scenario_=s(status={"out": "[" * 100 + "]" * 100})))
    add(case("status-deep", ["status"], scenario_=s(status={"out": "[" * 101 + "]" * 101})))
    add(case("status-deep-invalid-json", ["status", "--json"],
             scenario_=s(status={"out": '{"a": "[[[", ' + "{" * 150})))
    add(case("status-serve-deep", ["status"], scenario_=s(serve="[" * 100_000)))
    add(case("status-undecodable", ["status"],
             scenario_=s(status={"out_b64": base64.b64encode(b"\xff{}").decode()})))
    add(case("status-undecodable-stderr-json", ["status", "--json"],
             scenario_=s(status={"code": 1, "err_b64": base64.b64encode(b"down \xfe\n").decode()})))
    add(case("status-serve-undecodable", ["status"], scenario_=s(
        reads=[{"out_b64": base64.b64encode(b'{"TCP": "\xc3("}').decode()}])))

    # A tailscale program that cannot start (str(OSError) in the refusal).
    if core.WINDOWS:
        add(case("status-not-a-program", ["status"], extra={
            "Program Files/Tailscale/tailscale.exe": b"not a program\n"}))
        add(case("off-not-a-program-json", ["off", "--json"], extra={
            "Program Files/Tailscale/tailscale.exe": b"not a program\n"}))
    else:
        add(case("status-not-a-program", ["status"], extra={"bin/tailscale": b"not a program\n"}))
        add(case("off-not-a-program-json", ["off", "--json"],
                 extra={"bin/tailscale": b"not a program\n"}))

    # off.
    add(case("off-not-installed", ["off"]))
    add(case("off-not-installed-json", ["off", "--json"]))
    add(case("off-not-running", ["off"], scenario_=s(status={"code": 1, "err": "down\n"})))
    add(case("off-serve-fails", ["off", "--json"], scenario_=s(reads=[{"code": 1}])))
    add(case("off-not-exposed", ["off"], scenario_=s()))
    add(case("off-not-exposed-json", ["off", "--json"], scenario_=s()))
    add(case("off-foreign", ["off"], scenario_=s(serve='{"TCP": {"{PORT}": {"HTTPS": true}}}')))
    add(case("off-foreign-json", ["off", "--json"], scenario_=s(serve=json.dumps(
        {"Foreground": {"x": {"TCP": {"{PORT}": {"TCPForward": "127.0.0.1:{PORT}"}}}}}))))
    add(case("off-no-terminal", ["off"], scenario_=s(serve=OURS)))
    add(case("off-no-terminal-json", ["off", "--json", "--port", "8443"], port="8443",
             scenario_=s(serve=OURS)))
    add(case("defer-off-yes", ["off", "--yes"], scenario_=s(serve=OURS),
             rules=("expose-deferral-at-change",)))

    # tailscale: the local daemon.
    add(case("tailscale-no-daemon", ["tailscale", *at(DEAD_PORT)], scenario_=s()))
    add(case("tailscale-no-daemon-json", ["tailscale", *at(DEAD_PORT), "--json", "--yes"],
             scenario_=s()))
    daemon_cases = [
        ("redirect", lambda: FixtureDaemon({"/health": redirect("/elsewhere")}), False),
        ("not-json", lambda: FixtureDaemon({"/health": text("ok")}), True),
        ("json-list", _health([1]), False),
        ("not-found", lambda: FixtureDaemon({}), False),
        ("degraded", _health({"status": "degraded", "auth": True}, 503), True),
        ("status-missing", _health({"auth": True}), False),
        ("status-object", _health({"status": {"a": [1, None]}, "auth": True}), True),
        ("no-auth", _health({"status": "ok", "auth": False, "bank": "b"}), True),
        ("auth-missing", _health({"status": "ok"}), False),
        ("auth-one", _health({"status": "ok", "auth": 1}), False),
        ("bad-utf8", lambda: FixtureDaemon({"/health": text(b"\xff\xfe", 200,
                                                             "application/json")}), False),
    ]
    for name, daemon, json_too in daemon_cases:
        add(case(f"tailscale-daemon-{name}", ["tailscale", *at()], daemon=daemon,
                 scenario_=s()))
        if json_too:
            add(case(f"tailscale-daemon-{name}-json", ["tailscale", *at(), "--json"],
                     daemon=daemon, scenario_=s()))
    deep = lambda: FixtureDaemon({"/health": text(
        '{"status": "ok", "auth": true, "bank": ' + "[" * 200 + "]" * 200 + "}", 200,
        "application/json")})
    add(case("tailscale-daemon-deep", ["tailscale", *at()], daemon=deep, scenario_=s()))
    add(case("tailscale-undecodable-serve", ["tailscale", *at(), "--yes"], daemon=_health(HEALTHY),
             scenario_=s(reads=[{"out_b64": base64.b64encode(b"\xff").decode()}])))
    add(case("tailscale-deep-status-json", ["tailscale", *at(), "--json"], daemon=_health(HEALTHY),
             scenario_=s(status={"out": "{" * 101})))
    add(case("off-undecodable-status", ["off", "--yes"],
             scenario_=s(serve=OURS, status={"out_b64": base64.b64encode(b"\x80").decode()})))
    healthy = _health(HEALTHY)
    # tailscale: the CLI and its state.
    add(case("tailscale-not-installed", ["tailscale", *at()], daemon=healthy))
    add(case("tailscale-not-installed-json", ["tailscale", *at(), "--json"], daemon=healthy))
    add(case("tailscale-not-running", ["tailscale", *at()], daemon=healthy,
             scenario_=s(status={"out": json.dumps({"BackendState": "NeedsLogin"})})))
    add(case("tailscale-serve-fails", ["tailscale", *at()], daemon=healthy,
             scenario_=s(reads=[{"code": 1, "err": "Access denied: serve config denied\n"}])))
    add(case("tailscale-foreign", ["tailscale", *at()], daemon=healthy,
             scenario_=s(serve='{"TCP": {"{PORT}": {"TCPForward": "127.0.0.1:1"}}}')))
    add(case("tailscale-foreign-json", ["tailscale", *at(), "--json", "--yes"], daemon=healthy,
             scenario_=s(serve='{"TCP": {"{PORT}": {"HTTP": true}}}')))
    add(case("tailscale-funnel", ["tailscale", *at(), "--yes"], daemon=healthy,
             scenario_=s(serve=json.dumps({"AllowFunnel": {"n.example.com:{PORT}": True}}))))
    add(case("tailscale-no-ipv4", ["tailscale", *at()], daemon=healthy,
             scenario_=s(ip4={"out": "\n"}, status={"out": '{"BackendState": "Running"}'})))
    add(case("tailscale-no-ipv4-json", ["tailscale", *at(), "--json"], daemon=healthy,
             scenario_=s(ip4={"code": 1, "out": "127.0.0.1"},
                         status={"out": '{"BackendState": "Running", "Self": null}'})))
    add(case("tailscale-already", ["tailscale", *at()], daemon=healthy,
             scenario_=s(serve=OURS)))
    add(case("tailscale-already-json", ["tailscale", *at(), "--json", "--yes"], daemon=healthy,
             scenario_=s(serve=OURS)))
    add(case("tailscale-no-terminal", ["tailscale", *at()], daemon=healthy, scenario_=s()))
    add(case("tailscale-no-terminal-json", ["tailscale", "--json", *at()], daemon=_health(
        {"status": "ok", "auth": True, "bank": {"id": "fixture", "n": [1, 2.5, None]}}),
        scenario_=s()))
    add(case("tailscale-no-terminal-bankless", ["tailscale", *at(), "--json"],
             daemon=_health({"status": "ok", "auth": True}), scenario_=s()))
    add(case("defer-tailscale-yes", ["tailscale", *at(), "--yes"], daemon=healthy,
             scenario_=s(), rules=("expose-deferral-at-change",)))
    add(case("defer-tailscale-yes-json", ["tailscale", *at(), "--yes", "--json"],
             daemon=healthy, scenario_=s(), rules=("expose-deferral-at-change",)))
    return c


MUTANTS = [
    Mutant("expose-funnel-ignored", "expose", TS_FILE,
           "if funnelled(Some(config), port) ||", "if false ||", ("status-funnel",)),
    Mutant("expose-relative-path-searched", "expose", TS_FILE,
           "if known == Some(false) {", "if known.is_none() {", ("status-relative-path-skipped",)),
    Mutant("expose-yes-skips-tty-check", "expose", FILE,
           "if !args.yes {", "if false {", ("off-no-terminal",)),
    Mutant("expose-first-line-unstripped", "expose", TS_FILE,
           ".find(|line| !pyjson::py_strip(line).is_empty())",
           ".find(|line| !line.is_empty())", ("status-not-running-blank-line",)),
    Mutant("expose-port-range-off-by-one", "expose", ARGS_FILE,
           "(1..=65535).contains(port)", "(0..=65535).contains(port)", ("port-zero",)),
    Mutant("expose-auth-truthy", "expose", FILE,
           'if pyjson::get(&health, "auth") != Some(&J::Bool(true)) {',
           'if !pyjson::get(&health, "auth").is_some_and(J::truthy) {',
           ("tailscale-daemon-auth-one",)),
    # Only Windows' message text ends in a full stop.
    Mutant("expose-winerror-full-stop", "expose", TS_FILE,
           "message.trim_end_matches(|c: char| c <= ' ' || c == '.')",
           "message.trim_end_matches(|c: char| c <= ' ')", ("status-not-a-program",),
           platforms=("windows",)),
    # Only Windows has PATHEXT, whose spelling the lookup reports.
    Mutant("expose-pathext-lowercased", "expose", TS_FILE,
           '.map(|ext| format!("tailscale{ext}"))',
           '.map(|ext| format!("tailscale{}", ext.to_lowercase()))', ("status-none-json",),
           platforms=("windows",)),
]

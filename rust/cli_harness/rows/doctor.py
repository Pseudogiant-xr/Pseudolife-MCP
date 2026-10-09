"""CLI-DOCTOR: ``pseudolife-mcp doctor``, read-only diagnostics.

Canonical argv (README, docs/guide/configuration.md "Coordination
diagnostic quickstart", docs/guide/providers.md, ops/install.*): ``doctor``
and ``doctor --host codex|claude-code|claude-desktop|generic``, with
``--timeout SECONDS`` and ``--agent-state FILE``. A nonempty
``--disposable-proof`` database defers (Rust tests cover deferrals).

Each case runs against the stdlib fixture daemon below, bound to
one loopback port for both arms, so the daemon URL, the shim's handshake
cache name and every request line agree. The daemon reports each arm's own
package version unless a case says otherwise: the oracle's version comes
from its interpreter's installed metadata and the candidate's from Cargo,
and the four interpreter identity fields differ by construction (declared
substitution ``doctor-runtime-identity``).
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import core, normalize, producers
from ..core import Case
from ..mutants import Mutant

HERE = Path(__file__).resolve().parent
RUST = HERE.parents[1]
CARGO_VERSION = re.search(r'(?m)^version = "([^"]+)"',
                          (RUST / "shim" / "Cargo.toml").read_text(encoding="utf-8")).group(1)
RULES = ("doctor-runtime-identity", "shim-handshake-cache-semantic", "doctor-context-nonce")


@functools.lru_cache(maxsize=None)
def oracle_version() -> str:
    """``importlib.metadata.version("pseudolife-mcp")`` as the oracle arm
    sees it: the default oracle (this interpreter, ``--oracle-python``'s
    default) under the harness's isolated home."""
    source = producers._SOURCE or RUST.parent
    target = core.python_target(sys.executable, source)
    scratch = core._home_root().with_name(core._home_root().name + "-version")
    core._reset(scratch)
    try:
        arm = core.Arm("python", scratch, scratch)
        env = core._environment(Case("version", []), arm, target)
        result = subprocess.run(
            [sys.executable, "-P", "-c",
             "import importlib.metadata as m; print(m.version('pseudolife-mcp'))"],
            env=env, cwd=scratch, capture_output=True, text=True, timeout=60)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if result.returncode != 0:
        raise RuntimeError(f"oracle version probe failed: {result.stderr[-400:]}")
    return result.stdout.strip()


def own_version(arm) -> str:
    return oracle_version() if arm.name == "python" else CARGO_VERSION


# --- the fixture daemon -------------------------------------------------------
#
# A stdlib daemon answering the routes doctor reads (/health, the board
# probe, /api/maintainer) and the streamable-HTTP MCP route the handshake's
# shim proxies to, with bodies shaped like the Python daemon's
# (daemon._build_health_payload, web/api.py _send_json). Each route's answer
# is set per case; every request is recorded.

# web/api.py ``_send_json``: json.dumps(payload, default=str) as UTF-8.
def body(payload: Any) -> bytes:
    return json.dumps(payload, default=str).encode("utf-8")


WAKE_CAPS = {"per_recipient_per_hour": 20, "urgent_per_sender_per_hour": 6,
             "nightly_total": 200, "fan_out_stagger_seconds": 30, "active_seconds": 300,
             "authority_per_sender_per_hour": 4}


def health(version: str | None = None, *, auth: bool = False, status: str = "ok",
           coordination: Any = "default", hooks_digest: str | None = None, **extra) -> dict:
    """``daemon._build_health_payload``'s key order for a PostgreSQL bank."""
    payload: dict = {"status": status}
    if version is not None:
        payload["version"] = version
    payload.update({"schema": 63, "storage": "postgres", "auth": auth,
                    "bank": "0f1e2d3c4b5a69788796a5b4c3d2e1f0", "persist_errors": 0})
    if coordination == "default":
        coordination = {"enabled": True, "wake": dict(WAKE_CAPS, nudge_interval_seconds=3600)}
    if coordination is not None:
        payload["coordination"] = coordination
    if hooks_digest is not None:
        payload["hooks_digest"] = hooks_digest
    payload.update(extra)
    return payload


def tool(name: str, annotations: dict | None = None) -> dict:
    value = {"name": name, "description": f"{name} fixture",
             "inputSchema": {"type": "object", "properties": {}}}
    if annotations is not None:
        value["annotations"] = annotations
    return value


READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
             "openWorldHint": False}
WRITES = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
          "openWorldHint": False}
TOOLS = [tool("memory_search", READ_ONLY), tool("memory_store", WRITES),
         tool("memory_agents", READ_ONLY), tool("memory_message", WRITES)]
INSTRUCTIONS = "Shared durable memory. At task start: memory_search."


class Route:
    """One route's answer: status, headers and body bytes; ``drop`` closes
    the connection without an answer."""

    def __init__(self, status: int = 200, payload: Any = None, *, raw: bytes | None = None,
                 headers: dict | None = None, drop: bool = False, stall: float = 0.0):
        self.status = status
        self.raw = raw if raw is not None else (b"" if payload is None else body(payload))
        self.headers = headers or {}
        self.drop = drop
        # ``stall``: announce more bytes than are sent, send the body, then
        # hold the connection open this long (a reader bounded below the
        # body's length returns; one that reads to the end waits).
        self.stall = stall


class FixtureDaemon:
    """``routes`` maps a path to a callable taking this daemon and returning
    a :class:`Route`, evaluated per request (``version`` is set per arm)."""

    def __init__(self, routes: dict | None = None, *, port: int = 0, tools: list | None = None,
                 instructions: str | None = INSTRUCTIONS, mcp_status: int = 200,
                 initialize_delay: float = 0.0):
        self.routes = dict(routes or {})
        self.version: str | None = None
        self.initialize_delay = initialize_delay
        self.tools = TOOLS if tools is None else tools
        self.instructions = instructions
        self.mcp_status = mcp_status
        self._seen: list = []
        self._lock = threading.Lock()
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _record(self, entry):
                with daemon._lock:
                    daemon._seen.append(entry)

            def _answer(self, route: Route):
                if route.drop:
                    self.close_connection = True
                    try:
                        self.connection.shutdown(2)
                    except OSError:
                        pass
                    return
                self.send_response(route.status)
                for key, value in route.headers.items():
                    self.send_header(key, value)
                if "Content-Type" not in route.headers:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length",
                                 str(len(route.raw) + (4096 if route.stall else 0)))
                self.end_headers()
                self.wfile.write(route.raw)
                if route.stall:
                    self.wfile.flush()
                    time.sleep(route.stall)
                    self.close_connection = True

            def _entry(self, body: bytes = b"") -> dict:
                """The request in the harness's wire shape (core.py)."""
                return {"method": self.command, "target": self.path,
                        "headers": [[k, v] for k, v in self.headers.items()],
                        "body": body.decode("utf-8", "backslashreplace")}

            def _shim(self, path: str) -> bool:
                """Positively the handshake shim's traffic: the MCP route,
                its episode posts, and every /health after doctor's one."""
                if path == "/mcp" or path.startswith("/api/episode/"):
                    return True
                if path == "/health":
                    with daemon._lock:
                        return any(e["target"].split("?", 1)[0] == "/health"
                                   for e in daemon._seen if not e.get("via"))
                return False

            def do_GET(self):  # noqa: N802 - http.server naming
                path = self.path.split("?", 1)[0]
                entry = self._entry()
                if self._shim(path):
                    entry["via"] = "shim"
                self._record(entry)
                if path == "/mcp":
                    self._answer(Route(405, {"error": "method_not_allowed"}))
                    return
                route = daemon.routes.get(path)
                self._answer(route(daemon) if route else Route(404, {"error": "not_found"}))

            def do_DELETE(self):  # noqa: N802
                entry = self._entry()
                if self._shim(self.path.split("?", 1)[0]):
                    entry["via"] = "shim"
                self._record(entry)
                self._answer(Route(200, {}))

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                path = self.path.split("?", 1)[0]
                entry = self._entry(raw)
                if self._shim(path):
                    entry["via"] = "shim"
                if path != "/mcp":
                    self._record(entry)
                    route = daemon.routes.get("POST " + path)
                    if route is not None:
                        try:
                            request = json.loads(raw)
                        except ValueError:
                            request = None
                        self._answer(route(daemon, request))
                    elif entry.get("via"):
                        self._answer(Route(200, {"ok": True}))
                    else:
                        self._answer(Route(404, {"error": "not_found"}))
                    return
                try:
                    request = json.loads(raw)
                except ValueError:
                    self._answer(Route(400, {"error": "bad_json"}))
                    return
                method = request.get("method") if isinstance(request, dict) else None
                entry["rpc"] = method
                self._record(entry)
                if daemon.mcp_status != 200:
                    self._answer(Route(daemon.mcp_status, {"error": "unauthorized"}))
                    return
                if "id" not in request:
                    self._answer(Route(202, raw=b""))
                    return
                if method == "initialize":
                    if daemon.initialize_delay:
                        time.sleep(daemon.initialize_delay)
                    result = {"protocolVersion": request["params"]["protocolVersion"],
                              "capabilities": {"tools": {"listChanged": False}},
                              "serverInfo": {"name": "pseudolife-memory", "version": "fixture"}}
                    if daemon.instructions is not None:
                        result["instructions"] = daemon.instructions
                elif method == "tools/list":
                    result = {"tools": daemon.tools}
                elif method == "ping":
                    result = {}
                else:
                    payload = {"jsonrpc": "2.0", "id": request["id"],
                               "error": {"code": -32601, "message": "Method not found"}}
                    self._answer(Route(200, payload, headers={"Mcp-Session-Id": "fixture-session"}))
                    return
                payload = {"jsonrpc": "2.0", "id": request["id"], "result": result}
                self._answer(Route(200, payload, headers={"Mcp-Session-Id": "fixture-session"}))

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        # A client closing a kept-alive connection is not a fixture failure.
        self.server.handle_error = lambda request, address: None
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def requests(self) -> list:
        """Doctor's own requests, in order and in full. The handshake shim's
        traffic (positively tagged ``via: shim``) is CLI-SHIM's contract:
        it is summarized as one entry naming the JSON-RPC methods it
        proxied. Anything untagged is compared request by request."""
        with self._lock:
            seen = list(self._seen)
        own = [entry for entry in seen if not entry.get("via")]
        proxied = sorted({entry["rpc"] for entry in seen
                          if entry.get("via") and entry.get("rpc")})
        shim = [entry for entry in seen if entry.get("via")]
        if shim:
            own.append({"method": "SHIM", "target": "rpc:" + ",".join(proxied),
                        "headers": [], "body": ""})
        return own

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# --- the identity substitution ------------------------------------------------

_TOP = re.compile(rb'(\n  "(interpreter|source|pseudolife-mcp|mcp|daemon_version)": )'
                  rb'"((?:[^"\\\r\n]|\\.)*)"')
_VERSION = re.compile(r"^[0-9]+(\.[0-9]+)*([.+-][0-9A-Za-z.+-]*)?$")
# The oracle's MCP SDK line, with its line break and the comma after it (the
# key is never last: credential_source follows it).
_MCP_LINE = re.compile(rb'\r?\n  "mcp": "((?:[^"\\\r\n]|\\.)*)",')


def _arm_of(fields: dict) -> str | None:
    interpreter = fields.get("interpreter", "")
    source = fields.get("source", "")
    own = fields.get("pseudolife-mcp", "")
    mcp = fields.get("mcp")
    name = os.path.basename(interpreter).lower()
    if name in ("pseudolife-stdio", "pseudolife-stdio.exe"):
        # The candidate: its own executable, the directory it resolves
        # into, the Cargo version, and no Python MCP SDK.
        resolved = os.path.dirname(os.path.realpath(interpreter))
        # Maintainer decision 2026-10-09: the native report has no `mcp`.
        if (os.path.normcase(resolved) == os.path.normcase(source) and own == CARGO_VERSION
                and mcp is None):
            return "rust"
        return None
    if re.match(r"^python[0-9.]*(\.exe)?$", name) and os.path.isfile(interpreter):
        if (os.path.basename(source) == "pseudolife_memory" and os.path.isdir(source)
                and _VERSION.match(own) and mcp is not None and _VERSION.match(mcp)):
            return "python"
    return None


@normalize.rule("doctor-runtime-identity")
def runtime_identity(obs: dict) -> None:
    """Declared substitution (maintainer decision 2026-10-09): the oracle
    reports its Python interpreter, package directory, installed package and
    MCP SDK versions; the native doctor reports its own executable, the
    directory it resolves into and its Cargo version, and no ``mcp`` key.
    Each arm's values are validated against what that arm must report
    (the oracle's ``mcp`` must be a real version string; the native report
    must not carry the key). The oracle's ``mcp`` line is then deleted, so
    the native report must equal the rest of Python's byte for byte, and
    the three shared fields are tokenized, together with that arm's own
    version where the report or the shim's stderr repeats it
    (``daemon_version`` when the fixture echoes it, the version-mismatch
    recovery and the shim's mismatch warning)."""
    out = normalize._get(obs, "stdout")
    fields = {m.group(2).decode(): json.loads(b'"' + m.group(3) + b'"')
              for m in _TOP.finditer(out)}
    # Each arm must carry its OWN identity; another arm's is a difference.
    if obs.get("arm") not in ("python", "rust") or _arm_of(fields) != obs["arm"]:
        return
    own = fields["pseudolife-mcp"]
    if obs["arm"] == "python":
        lines = _MCP_LINE.findall(out)
        if len(lines) != 1 or json.loads(b'"' + lines[0] + b'"') != fields["mcp"]:
            return
        out = _MCP_LINE.sub(b"", out)

    def swap(match: re.Match) -> bytes:
        key = match.group(2).decode()
        if key == "daemon_version" and fields[key] != own:
            return match.group(0)
        token = "pseudolife-mcp" if key == "daemon_version" else key
        return match.group(1) + b'"<runtime:' + token.encode() + b'>"'

    out = _TOP.sub(swap, out)
    mine = own.encode()
    out = out.replace(b"The shim is pseudolife-mcp " + mine + b" but",
                      b"The shim is pseudolife-mcp <runtime:pseudolife-mcp> but")
    out = out.replace(b"--tag " + mine + b" ", b"--tag <runtime:pseudolife-mcp> ")
    normalize._put(obs, "stdout", out)
    err = normalize._get(obs, "stderr")
    err = err.replace(b"this shim is pseudolife-mcp " + mine + b" but",
                      b"this shim is pseudolife-mcp <runtime:pseudolife-mcp> but")
    normalize._put(obs, "stderr", err)


_CACHE = re.compile(r"^\.pseudolife-mcp/handshake-cache/[0-9a-f]{16}\.json$")


@normalize.rule("shim-handshake-cache-semantic")
def handshake_cache_semantic(obs: dict) -> None:
    """Inherited from CLI-SHIM, not doctor's own output: the handshake's
    shim writes ``~/.pseudolife-mcp/handshake-cache/<sha256(url)[:16]>.json``.
    The Python shim writes ``json.dumps({"url": url, **cached})`` (spaced
    separators, ASCII escapes, ``url`` first); the native shim
    (``cache.rs``) writes compact UTF-8 with ``url`` last. Its only reader
    parses it. Only that path, only when the content is a JSON object
    whose ``url`` is the fixture origin (already ``{DAEMON}``) and whose
    other keys are ``instructions``/``tools``, is re-serialized with sorted
    keys; any value difference still shows. Temporary: the connect leaf's
    cache.rs fix writes Python's bytes, and this rule goes once it merges."""
    for rel in list(obs["files"]):
        if not _CACHE.match(rel):
            continue
        raw = normalize._file(obs, rel)
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if (not isinstance(value, dict) or value.get("url") != "{DAEMON}"
                or not set(value) <= {"url", "instructions", "tools"}):
            continue
        normalize._set_file(obs, rel, json.dumps(value, sort_keys=True).encode())


_NONCE = re.compile(r'"nonce":"([0-9a-f]{32})"')


@normalize.rule("doctor-context-nonce")
def context_nonce(obs: dict) -> None:
    """``probe_registration`` sends ``uuid.uuid4().hex`` as the context
    request's nonce: random per run. Only the request to
    ``/api/coordination/context`` whose body is a JSON object with exactly
    agent_id, nonce and read_only, and only those 32 hex digits, become
    ``<nonce>``; every other byte of the body stays compared."""
    for request in obs.get("requests", []):
        if request.get("target") != "/api/coordination/context":
            continue
        try:
            value = json.loads(request["body"])
        except ValueError:
            continue
        if not isinstance(value, dict) or set(value) != {"agent_id", "nonce", "read_only"}:
            continue
        match = _NONCE.search(request["body"])
        if match and match.group(1) == value["nonce"] and len(_NONCE.findall(request["body"])) == 1:
            request["body"] = request["body"].replace(match.group(1), "<nonce>")


# --- fixtures -----------------------------------------------------------------

def fixture(routes=None, **options):
    """A daemon factory: both arms bind the same loopback port."""
    port = [0]

    def make():
        daemon = FixtureDaemon(dict(routes or {}), port=port[0], **options)
        port[0] = daemon.port
        return daemon
    return make


def healthy(**fields):
    """``/health`` answering the arm's own version unless ``version`` is given."""
    def answer(daemon):
        version = fields.get("version", daemon.version)
        rest = {k: v for k, v in fields.items() if k != "version"}
        return Route(200, health(version, **rest))
    return answer


def setup(*steps):
    def run(arm):
        if arm.daemon is not None:
            arm.daemon.version = own_version(arm)
            arm.daemon.home = arm.home
        for step in steps:
            step(arm)
    return run


def write(rel: str, text: str | bytes):
    def step(arm):
        path = arm.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text.replace("{HOME}", str(arm.home)), encoding="utf-8")
    return step


def _fill(value, arm):
    if isinstance(value, str):
        value = value.replace("{HOME}", str(arm.home))
        return value.replace("{URL}", arm.daemon.url) if arm.daemon else value
    if isinstance(value, dict):
        return {key: _fill(item, arm) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill(item, arm) for item in value]
    return value


def write_json(rel: str, value):
    """A JSON file whose strings name the arm's home ({HOME}) or daemon ({URL})."""
    def step(arm):
        path = arm.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_fill(value, arm)), encoding="utf-8")
    return step


def mkdir(rel: str):
    return lambda arm: (arm.home / rel).mkdir(parents=True, exist_ok=True)


def token_file(rel: str, token: str = "fixture-token"):
    def step(arm):
        from pseudolife_memory.credentials import _write_token_file  # noqa: PLC0415 (oracle)
        _write_token_file(arm.home / rel, token)
    return step


AGENT = "a" * 32
CREDENTIAL = "fixture-instance-credential"
BANK = "00000000-0000-4000-8000-000000000001"
PRINCIPAL = "default"
STATE = "state/instance.json"


def saved_instance(private: bool = True, raw: str | None = None, **fields):
    """A saved instance file in the adapter's legacy-state shape, written by
    the adapter's own private writer (or plainly, for a non-private file)."""
    def step(arm):
        url = arm.daemon.url if arm.daemon else core.DEAD_DAEMON_URL
        value = {"bank_url": url, "agent_id": AGENT, "credential": CREDENTIAL, "version": 2,
                 "bank_id": BANK, "principal": PRINCIPAL}
        for key, item in fields.items():
            if item is None:
                value.pop(key, None)
            else:
                value[key] = item
        text = raw if raw is not None else json.dumps(value)
        path = arm.home / STATE
        if private:
            producers.write_private(path, text)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    return step


def redirected_dir(rel: str):
    """``rel`` as a redirect to an empty directory: a junction on Windows
    (no privilege needed), a symlink elsewhere."""
    def step(arm):
        target = arm.home / (rel + "-target")
        target.mkdir(parents=True)
        link = arm.home / rel
        if core.WINDOWS:
            import _winapi  # noqa: PLC0415
            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)
    return step


def context_proof(bank: str = BANK, principal: str = PRINCIPAL, proof: str | None = None,
                  extra: str | None = None):
    """``/api/coordination/context``'s read-only answer: the daemon's nonce
    proof over the request's agent and nonce (``web/coordination``)."""
    import hashlib
    import hmac

    def answer(_daemon, request):
        message = json.dumps(["pseudolife-context-v1", bank, principal, request["agent_id"],
                              request["nonce"]], separators=(",", ":"),
                             ensure_ascii=True).encode("ascii")
        good = hmac.new(hashlib.sha256(CREDENTIAL.encode()).digest(), message,
                        hashlib.sha256).hexdigest()
        value = {"bank_id": bank, "principal": principal, "proof": proof or good}
        if extra is None:
            return Route(200, value)
        # One more member, spelled raw (deep nesting, long integers).
        return Route(200, raw=(json.dumps(value)[:-1] + ', "x": ' + extra + "}").encode())
    return answer


def registration(env: dict) -> dict:
    return {"mcpServers": {"pseudolife-memory": {"command": "pseudolife-mcp", "args": [],
                                                 "env": env}}}


def claude_registration(env: dict) -> str:
    return json.dumps(registration(env))


BOARD_ON = Route(200, raw=b"## Agent board\n", headers={"X-PL-Board": "on"})
MAINTAINER_UNSET = Route(409, {"error": "maintainer_https_required", "config_problem": "unset"})
TOKEN = {"PSEUDOLIFE_MCP_TOKEN": "fixture-token"}


def case(id: str, argv=(), *, routes=None, steps=(), env=None, rules=RULES, daemon=True,
         timeout=60.0, **options) -> Case:
    return Case(id, ["doctor", *argv], env=dict(env or {}),
                setup=setup(*steps), rules=rules, timeout=timeout, after=tag_arm,
                daemon=fixture(routes, **options) if daemon else None)


def tag_arm(arm, observation: dict) -> None:
    """Which arm produced the observation, for the identity rule only (the
    comparison never reads this key)."""
    observation["arm"] = arm.name


DEPTH_CASES = ("agent-state-deep-parsed", "agent-state-too-deep", "agent-state-too-deep-refusal")


def cases() -> list[Case]:
    c: list[Case] = []
    add = c.append
    ok = {"/health": healthy()}

    # No daemon at the URL: no request answers, no handshake.
    add(case("unreachable", daemon=False))
    for host in ("codex", "claude-code", "claude-desktop", "generic"):
        add(case(f"unreachable-host-{host}", ["--host", host], daemon=False))
    add(case("unreachable-timeout", ["--timeout", "5", "--host", "codex"], daemon=False))
    add(case("unreachable-with-token", env=TOKEN, daemon=False))

    # The daemon answers, but not as healthy.
    add(case("health-degraded-503", routes={"/health": lambda _: Route(503, health(
        status="degraded", init_refusal="dimension mismatch"))}))
    add(case("health-not-json", routes={"/health": lambda _: Route(200, raw=b"<html>hi</html>")}))
    add(case("health-empty-object", routes={"/health": lambda _: Route(200, {})}))
    add(case("health-no-status", routes={"/health": lambda _: Route(200, {"version": "1"})}))
    # Python 3.11's int() refuses more than 4300 digits (a ValueError).
    for digits, name in ((4301, "long-integer"), (4300, "integer-at-limit")):
        add(case(f"health-{name}", routes={"/health": (lambda n: lambda daemon: Route(
            200, raw=json.dumps(health(daemon.version))[:-1].encode() + b', "n": '
            + b"9" * n + b"}"))(digits)}))
    add(case("health-stalled-body", routes={"/health": lambda daemon: Route(
        200, health(daemon.version), stall=6.0)}))
    add(case("bearer-rejected", env=TOKEN, routes={
        "/health": lambda _: Route(401, {"error": "unauthorized"}),
        "/api/hook/coordination-start": lambda _: Route(401, {"error": "unauthorized"})}))
    add(case("bearer-missing", routes={"/health": healthy(auth=True)}))

    # Healthy daemon: the handshake runs through the arm's own shim.
    add(case("healthy", routes=ok))
    for host in ("codex", "claude-code", "claude-desktop"):
        add(case(f"healthy-host-{host}", ["--host", host], routes=ok))
    add(case("healthy-timeout-option", ["--timeout", "30"], routes=ok))
    add(case("healthy-version-mismatch", routes={"/health": healthy(version="9.9.9")}))
    add(case("healthy-version-absent", routes={"/health": healthy(version=None)}))
    add(case("healthy-version-empty", routes={"/health": healthy(version="")}))
    add(case("healthy-no-coordination-report", routes={"/health": healthy(coordination=None)}))
    add(case("healthy-coordination-disabled", ["--host", "claude-code"], routes={
        "/health": healthy(coordination={"enabled": False, "wake": dict(WAKE_CAPS)})}))
    add(case("healthy-caps-filtered", routes={"/health": healthy(coordination={
        "enabled": True, "wake": {"nightly_total": 5, "per_recipient_per_hour": 2.0,
                                  "active_seconds": -1, "urgent_per_sender_per_hour": True,
                                  "nudge_interval_seconds": 9, "fan_out_stagger_seconds": 0,
                                  "nightly_total_extra": 1}})}))
    add(case("healthy-no-instructions", routes=ok, instructions=None))
    add(case("healthy-missing-annotations", routes=ok,
             tools=[tool("memory_search", READ_ONLY), tool("memory_store"),
                    tool("memory_agents", READ_ONLY), tool("memory_message")]))
    add(case("healthy-no-coordination-tools", routes=ok,
             tools=[tool("memory_search", READ_ONLY)]))
    add(case("healthy-no-tools", routes=ok, tools=[]))
    add(case("handshake-timeout", ["--timeout", "3"], routes=ok, initialize_delay=8.0))
    add(case("mcp-refused", routes=ok, mcp_status=401))
    add(case("auth-with-token", env=TOKEN, routes={"/health": healthy(auth=True)}))

    # The board and the maintainer passkeys, with a bearer.
    board = "/api/hook/coordination-start"
    for name, route in [
        ("on", BOARD_ON),
        ("served-without-header", Route(200, raw=b"briefing")),
        ("empty-without-header", Route(200, raw=b" \n")),
        ("disabled", Route(200, raw=b"", headers={"X-PL-Board": "off; reason=disabled"})),
        ("principal-not-allowed", Route(200, raw=b"",
                                          headers={"X-PL-Board": "off; reason=principal_not_allowed"})),
        ("unknown-reason", Route(200, raw=b"", headers={"X-PL-Board": "off; reason=elsewhere"})),
        ("not-found", Route(404, {"error": "not_found"})),
        ("server-error", Route(500, {"error": "boom"})),
    ]:
        add(case(f"board-{name}", env=TOKEN, routes={
            "/health": healthy(), board: (lambda r: lambda _: r)(route),
            "/api/maintainer": lambda _: MAINTAINER_UNSET}))
    keys = [{"credential_id": "k1", "state": "active", "active_from": 1.0},
            {"credential_id": "k2", "state": "revoked"}, "junk"]
    for name, route in [
        ("on", Route(200, {"available": True, "rp_id": "box.example",
                             "origin": "https://box.example:8443", "passkeys": keys})),
        ("on-unset-fields", Route(200, {"available": True})),
        ("invalid", Route(409, {"error": "maintainer_https_required",
                                  "config_problem": "plain http is allowed for rp_id localhost only"})),
        ("older-daemon", Route(409, {"error": "maintainer_https_required"})),
        ("unsupported", Route(404, {"error": "not_found"})),
        ("unavailable", Route(503, {"error": "coordination_unavailable"})),
        ("not-json", Route(500, raw=b"oops")),
    ]:
        add(case(f"maintainer-{name}", env=TOKEN, routes={
            "/health": healthy(), board: lambda _: BOARD_ON,
            "/api/maintainer": (lambda r: lambda _: r)(route)}))
    # Python reads at most 65536 (board, maintainer refusal) or 1 MiB
    # (maintainer success) bytes and returns while the rest never comes.
    add(case("board-stalled-body", env=TOKEN, routes={
        "/health": healthy(), board: lambda _: Route(200, raw=b"x" * 70000,
                                                     headers={"X-PL-Board": "on"}, stall=6.0),
        "/api/maintainer": lambda _: MAINTAINER_UNSET}))
    add(case("maintainer-stalled-refusal", env=TOKEN, routes={
        "/health": healthy(), board: lambda _: BOARD_ON,
        "/api/maintainer": lambda _: Route(409, raw=MAINTAINER_UNSET.raw + b" " * 70000,
                                           stall=6.0)}))
    add(case("maintainer-stalled-success", env=TOKEN, routes={
        "/health": healthy(), board: lambda _: BOARD_ON,
        "/api/maintainer": lambda _: Route(200, raw=body({"rp_id": "box.example",
                                                          "origin": "https://box.example"})
                                           + b" " * (1 << 20), stall=6.0)}))
    add(case("maintainer-invalid-unreachable-daemon", env=TOKEN, routes={
        "/health": lambda _: Route(503, {"status": "degraded"}), board: lambda _: BOARD_ON,
        "/api/maintainer": lambda _: Route(409, {"error": "maintainer_https_required",
                                                   "config_problem": "origin must be https"})}))
    add(case("token-file", env={"PSEUDOLIFE_MCP_TOKEN_FILE": "{HOME}/secrets/token"},
             steps=[token_file("secrets/token")], routes={
                 "/health": healthy(), board: lambda _: BOARD_ON,
                 "/api/maintainer": lambda _: MAINTAINER_UNSET}))

    # A saved instance verified by nonce proof (--agent-state).
    state_arg = ["--agent-state", "{HOME}/" + STATE]
    context = "POST /api/coordination/context"

    def agent(id, *, answer=None, steps=None, argv=(), env=TOKEN, daemon=True, path=STATE):
        routes = {"/health": healthy(), board: lambda _: BOARD_ON,
                  "/api/maintainer": lambda _: MAINTAINER_UNSET}
        if answer is not None:
            routes[context] = answer
        state = ["--agent-state", "{HOME}/" + path] if path != STATE else state_arg
        add(case(f"agent-state-{id}", [*state, *argv], env=env, routes=routes,
                 steps=steps if steps is not None else [saved_instance()], daemon=daemon))

    agent("authenticated", answer=context_proof())
    agent("authenticated-host-codex", answer=context_proof(), argv=["--host", "codex"])
    agent("legacy-unversioned", answer=context_proof(),
          steps=[saved_instance(version=None, bank_id=None, principal=None)])
    agent("version-float", answer=context_proof(), steps=[saved_instance(version=2.0)])
    agent("version-3", answer=context_proof(), steps=[saved_instance(version=3)])
    agent("bank-mismatch", answer=context_proof(bank="00000000-0000-4000-8000-000000000002"))
    agent("principal-mismatch", answer=context_proof(principal="other"))
    agent("bad-proof", answer=context_proof(proof="0" * 64))
    agent("proof-missing", answer=lambda _d, _r: Route(200, {"bank_id": BANK,
                                                             "principal": PRINCIPAL}))
    agent("not-json", answer=lambda _d, _r: Route(200, raw=b"<html>"))
    for status, payload, name in [
        (401, {"error": "unauthorized"}, "unauthorized"),
        (404, {"error": "not_found"}, "not-found"),
        (405, {"error": "method_not_allowed"}, "method-not-allowed"),
        (400, {"error": "instance_not_found"}, "instance-not-found"),
        (403, {"error": "invalid_credential"}, "invalid-credential"),
        (403, {"error": "principal_not_allowed"}, "principal-not-allowed"),
        (400, {"error": "unexpected_parameter"}, "unexpected-parameter"),
        (400, {"error": "something_else"}, "other-refusal"),
        (400, ["not", "a", "dict"], "refusal-not-dict"),
        (503, {"error": "coordination_unavailable"}, "unavailable"),
        (418, {"error": "teapot"}, "other-status"),
    ]:
        agent(name, answer=(lambda s, p: lambda _d, _r: Route(s, p))(status, payload))
    agent("refusal-not-json", answer=lambda _d, _r: Route(400, raw=b"oops"))
    agent("redirect", answer=lambda _d, _r: Route(302, {}, headers={"Location": "/elsewhere"}))
    agent("missing-file", answer=context_proof(), steps=[])
    agent("invalid-json", answer=context_proof(), steps=[saved_instance(raw="{oops")])
    agent("not-an-object", answer=context_proof(), steps=[saved_instance(raw="[1]")])
    agent("wrong-url", answer=context_proof(),
          steps=[saved_instance(bank_url="http://127.0.0.1:1")])
    agent("empty-credential", answer=context_proof(), steps=[saved_instance(credential="")])
    agent("not-private", answer=context_proof(), steps=[saved_instance(private=False)])
    agent("board-off", answer=context_proof(), env={})
    agent("claude-desktop", answer=context_proof(), argv=["--host", "claude-desktop"])
    agent("unreachable-daemon", daemon=False)
    # Review round 1: bearer encoding, unhashable codes, nesting, long
    # integers, path spelling, and a missing file under a redirected dir.
    agent("error-list", answer=lambda _d, _r: Route(400, {"error": []}))
    agent("error-object", answer=lambda _d, _r: Route(403, {"error": {}}))
    agent("error-number", answer=lambda _d, _r: Route(400, {"error": 7}))
    agent("deep-parsed", answer=context_proof(extra="[" * 977 + "]" * 977))
    agent("too-deep", answer=context_proof(extra="[" * 978 + "]" * 978))
    agent("too-deep-refusal", answer=lambda _d, _r: Route(
        400, raw=b'{"error": ' + b"[" * 1200 + b"]" * 1200 + b"}"))
    agent("long-integer", answer=context_proof(extra="9" * 4301))
    agent("integer-at-limit", answer=context_proof(extra="9" * 4300))
    agent("trailing-separator", answer=context_proof(), path=STATE + "/")
    # The bearer is read again for each probe (doctor_cli.py:434, :650):
    # the board answer rotates the token file before the next ones.
    def rotate(daemon):
        from pseudolife_memory.credentials import _write_token_file  # noqa: PLC0415 (oracle)
        _write_token_file(daemon.home / "secrets/token", "rotated-token")
        return BOARD_ON
    add(case("token-rotated-between-probes", state_arg,
             env={"PSEUDOLIFE_MCP_TOKEN_FILE": "{HOME}/secrets/token"},
             steps=[token_file("secrets/token"), saved_instance()], routes={
                 "/health": healthy(), board: rotate,
                 "/api/maintainer": lambda _: MAINTAINER_UNSET, context: context_proof()}))
    agent("missing-under-redirect", answer=context_proof(), steps=[redirected_dir("state")])

    # Client registrations lend the credential a plain shell lacks.
    reg_routes = {"/health": healthy(auth=True), board: lambda _: BOARD_ON,
                  "/api/maintainer": lambda _: MAINTAINER_UNSET}
    add(case("claude-registration-token", steps=[
        write(".claude.json", claude_registration({"PSEUDOLIFE_MCP_TOKEN": "fixture-token",
                                                   "PSEUDOLIFE_WRITER_ID": "claude-code"}))],
        routes=reg_routes))
    add(case("claude-registration-token-file", steps=[
        token_file("secrets/claude-token"),
        write_json(".claude.json", registration(
            {"PSEUDOLIFE_MCP_TOKEN_FILE": "{HOME}/secrets/claude-token"}))],
        routes=reg_routes))
    add(case("claude-registration-url", env={"PSEUDOLIFE_MCP_DAEMON_URL": None}, steps=[
        write_json(".claude.json", registration(
            {"PSEUDOLIFE_MCP_TOKEN": "fixture-token", "PSEUDOLIFE_MCP_DAEMON_URL": "{URL}"}))],
        routes=reg_routes))
    add(case("empty-shell-token", env={"PSEUDOLIFE_MCP_TOKEN": ""}, steps=[
        write(".claude.json", claude_registration({"PSEUDOLIFE_MCP_TOKEN": "fixture-token"}))],
        routes=reg_routes))
    add(case("claude-config-dir-registration", env={"CLAUDE_CONFIG_DIR": "{HOME}/claude-config"},
             steps=[write("claude-config/.claude.json",
                          claude_registration({"PSEUDOLIFE_MCP_TOKEN": "fixture-token"}))],
             routes=reg_routes))
    add(case("claude-config-dir-dot-parts",
             env={"CLAUDE_CONFIG_DIR": "{HOME}/./claude-config//"},
             steps=[write("claude-config/.claude.json",
                          claude_registration({"PSEUDOLIFE_MCP_TOKEN": "fixture-token"}))],
             routes=reg_routes))
    add(case("claude-config-dir-non-ascii", env={"CLAUDE_CONFIG_DIR": "{HOME}/claud\u00e9"},
             steps=[write("claud\u00e9/.claude.json",
                          claude_registration({"PSEUDOLIFE_MCP_TOKEN": "fixture-token"}))],
             routes=reg_routes))
    codex_toml = ('[mcp_servers.pseudolife-memory]\ncommand = "pseudolife-mcp"\n'
                  '[mcp_servers.pseudolife-memory.env]\nPSEUDOLIFE_MCP_TOKEN = "fixture-token"\n'
                  'PSEUDOLIFE_WRITER_ID = "codex"\n')
    add(case("codex-registration-token", ["--host", "codex"], steps=[
        write(".claude.json", claude_registration({"PSEUDOLIFE_WRITER_ID": "claude-code"})),
        write(".codex/config.toml", codex_toml)], routes=reg_routes))
    add(case("codex-home-registration", ["--host", "codex"], env={"CODEX_HOME": "{HOME}/codex-home"},
             steps=[write("codex-home/config.toml", codex_toml)], routes=reg_routes))
    add(case("shell-token-wins", env=TOKEN, steps=[
        write(".claude.json", claude_registration({"PSEUDOLIFE_MCP_TOKEN": "other-token"}))],
        routes=reg_routes))
    add(case("registration-without-token", steps=[
        write(".claude.json", claude_registration({"PSEUDOLIFE_WRITER_ID": "claude-code"}))],
        routes={"/health": healthy(auth=True)}))
    add(case("claude-json-invalid", steps=[write(".claude.json", "{not json")], routes=ok))
    add(case("claude-json-bom", steps=[write(".claude.json", "﻿" + claude_registration(
        {"PSEUDOLIFE_MCP_TOKEN": "fixture-token"}))], routes=reg_routes))

    # Claude Code's wake path: the plugin's Stop hook.
    plugin = json.dumps({"version": 2, "plugins": {"pseudolife-memory@pseudolife-mcp": [
        {"scope": "user", "version": "0.17.0"}]}})
    for name, settings in [
        ("plugin-on", None),
        ("plugin-disabled", {"enabledPlugins": {"pseudolife-memory@pseudolife-mcp": False}}),
        ("hook-off", {"env": {"PSEUDOLIFE_AGENT_WAKE_HOOK": " OFF "}}),
        ("hook-invalid", {"env": {"PSEUDOLIFE_AGENT_WAKE_HOOK": "sometimes"}}),
        ("coordination-off", {"env": {"PSEUDOLIFE_AGENT_COORDINATION": "no"}}),
        ("coordination-invalid", {"env": {"PSEUDOLIFE_AGENT_COORDINATION": "maybe"}}),
        ("coordination-bool", {"env": {"PSEUDOLIFE_AGENT_COORDINATION": True}}),
        ("coordination-zero", {"env": {"PSEUDOLIFE_AGENT_COORDINATION": 0}}),
        ("settings-invalid", "{oops"),
    ]:
        steps = [write(".claude/plugins/installed_plugins.json", plugin)]
        if settings is not None:
            steps.append(write(".claude/settings.json",
                               settings if isinstance(settings, str) else json.dumps(settings)))
        add(case(f"claude-wake-{name}", ["--host", "claude-code"], steps=steps, routes=ok))
    add(case("claude-wake-registered-only", ["--host", "claude-code"], steps=[
        write(".claude.json", claude_registration({}))], routes=ok))
    add(case("claude-wake-env-coordination-off", ["--host", "claude-code"],
             env={"PSEUDOLIFE_AGENT_COORDINATION": "off"},
             steps=[write(".claude/plugins/installed_plugins.json", plugin)], routes=ok))

    # Codex's wake path: the shim's doorbell.
    def codex(env_block: str, extra: str = "") -> str:
        return ('[mcp_servers.pseudolife-memory]\ncommand = "pseudolife-mcp"\n' + extra
                + '[mcp_servers.pseudolife-memory.env]\n' + env_block)
    cli = "PSEUDOLIFE_CODEX_BIN = '{HOME}/bin/codex.exe'\n"
    for name, config in [
        ("doorbell-on", codex('PSEUDOLIFE_WRITER_ID = "codex"\nPSEUDOLIFE_MCP_TOKEN = "t"\n' + cli)),
        ("not-codex-writer", codex('PSEUDOLIFE_WRITER_ID = "claude-code"\nPSEUDOLIFE_MCP_TOKEN = "t"\n')),
        ("no-bearer", codex('PSEUDOLIFE_WRITER_ID = "codex"\n' + cli)),
        ("no-cli", codex('PSEUDOLIFE_WRITER_ID = "codex"\nPSEUDOLIFE_MCP_TOKEN = "t"\n'
                         "PSEUDOLIFE_CODEX_BIN = '{HOME}/bin/absent.exe'\nPATH = ''\n")),
        ("doorbell-off", codex('PSEUDOLIFE_CODEX_DOORBELL = "0"\n')),
        ("doorbell-invalid", codex('PSEUDOLIFE_CODEX_DOORBELL = "ring"\n')),
        ("forwarded-writer", codex('PSEUDOLIFE_MCP_TOKEN = "t"\n' + cli,
                                   'env_vars = ["PSEUDOLIFE_WRITER_ID"]\n')),
        ("not-registered", '[mcp_servers.other]\ncommand = "x"\n'),
        ("servers-not-table", 'mcp_servers = "x"\n'),
        ("env-int", codex('PSEUDOLIFE_AGENT_COORDINATION = 1\nPSEUDOLIFE_WRITER_ID = "codex"\n')),
    ]:
        add(case(f"codex-wake-{name}", ["--host", "codex"],
                 env={"PSEUDOLIFE_WRITER_ID": "codex"} if name == "forwarded-writer" else None,
                 steps=[write("bin/codex.exe", b""),
                        write(".codex/config.toml", config.replace("{HOME}", "{HOME}"))],
                 routes=ok))
    add(case("codex-configured-admitted", ["--host", "codex"], steps=[
        write("bin/codex.exe", b""),
        write(".codex/config.toml", codex('PSEUDOLIFE_WRITER_ID = "codex"\n'
                                          'PSEUDOLIFE_MCP_TOKEN = "fixture-token"\n' + cli))],
        routes=reg_routes))
    add(case("codex-config-undecodable", ["--host", "codex"],
             steps=[write(".codex/config.toml", b"\xff\xfe[x]\n")], routes=ok))
    # tomllib refuses a leading U+FEFF; utf-8-sig removes one first.
    add(case("codex-wake-bom", ["--host", "codex"], steps=[
        write("bin/codex.exe", b""),
        write(".codex/config.toml", "\ufeff" + codex('PSEUDOLIFE_WRITER_ID = "codex"\n'
                                                     'PSEUDOLIFE_MCP_TOKEN = "t"\n' + cli))],
        routes=ok))
    add(case("codex-registration-bom", ["--host", "codex"], steps=[
        write(".codex/config.toml", "\ufeff" + codex_toml)], routes=reg_routes))
    add(case("codex-registration-double-bom", ["--host", "codex"], steps=[
        write(".codex/config.toml", "\ufeff\ufeff" + codex_toml)], routes=reg_routes))

    # Codex's hook copy.
    add(case("codex-hooks-bundle-present", steps=[mkdir(".codex/pseudolife/hooks")], routes=ok))
    add(case("codex-hooks-plugin-no-digest", steps=[write(
        ".codex/config.toml", '[plugins."pseudolife-memory@pseudolife-mcp"]\nenabled = true\n')],
        routes=ok))

    # Which `pseudolife-mcp` a terminal runs.
    launcher_name = "pseudolife-mcp.exe" if core.WINDOWS else "pseudolife-mcp"
    add(case("launcher-elsewhere", env={"PSEUDOLIFE_SHIM_RUNTIMES": "{HOME}/runtimes",
                                        "PSEUDOLIFE_SHIM_LAUNCHER": "{HOME}/bin/" + launcher_name},
             steps=[write("bin/" + launcher_name, "launcher")], daemon=False))
    add(case("launcher-on-path", env={"PSEUDOLIFE_SHIM_RUNTIMES": "{HOME}/runtimes",
                                      "PSEUDOLIFE_SHIM_LAUNCHER": "{HOME}/bin/" + launcher_name,
                                      "PATH": "{HOME}/bin" + os.pathsep + os.environ.get("PATH", "")},
             steps=[write("bin/" + launcher_name, "launcher")], daemon=False))
    add(case("default-launcher", steps=[write(
        ("AppData/Local/pseudolife-mcp/bin/" if core.WINDOWS else ".local/share/pseudolife-mcp/bin/")
        + launcher_name, "launcher")], daemon=False))
    add(case("launcher-wrong-suffix", env={"PSEUDOLIFE_SHIM_RUNTIMES": "{HOME}/runtimes",
                                           "PSEUDOLIFE_SHIM_LAUNCHER": "{HOME}/bin/launcher"
                                           + ("" if core.WINDOWS else ".exe")},
             steps=[write("bin/launcher" + ("" if core.WINDOWS else ".exe"), "x")], daemon=False))
    add(case("no-path", env={"PATH": ""}, daemon=False))
    if core.WINDOWS:
        # The working directory comes before PATH (CPython 3.11 on Windows).
        add(case("which-cwd-first", steps=[write("cwd/" + launcher_name, "x")], daemon=False))

    # Git Bash (Windows): what Claude Code would run the plugin hooks with.
    if core.WINDOWS:
        add(case("git-bash-from-settings", steps=[
            write("git/bin/bash.exe", b""),
            write_json(".claude/settings.json", {"env": {
                "CLAUDE_CODE_GIT_BASH_PATH": "{HOME}\\git\\bin\\bash.exe"}})], daemon=False))
        add(case("git-bash-settings-wins-over-env",
                 env={"CLAUDE_CODE_GIT_BASH_PATH": "{HOME}\\missing\\bash.exe"}, steps=[
                     write("git/bin/sh.exe", b""),
                     write_json(".claude/settings.json", {"env": {
                         "CLAUDE_CODE_GIT_BASH_PATH": "{HOME}/git/bin/sh.exe"}})], daemon=False))
        add(case("git-bash-env", env={"CLAUDE_CODE_GIT_BASH_PATH": "{HOME}\\git\\bin\\BASH.EXE"},
                 steps=[write("git/bin/bash.exe", b"")], daemon=False))
        # PATH names only a Git cmd directory: `bash` is not on PATH. (Where
        # Git for Windows sits in its default directory, that still wins.)
        add(case("git-bash-git-on-path", env={"PATH": "{HOME}\\Git\\cmd"},
                 steps=[write("Git/cmd/git.exe", b""), write("Git/bin/bash.exe", b"")],
                 daemon=False))
        add(case("git-bash-env-not-bash", env={"CLAUDE_CODE_GIT_BASH_PATH": "{HOME}\\git\\git.exe"},
                 steps=[write("git/git.exe", b"")], daemon=False))
    # The JSON depth where CPython's parser raises depends on the interpreter's
    # call depth at that point: 978 was measured on Windows; Linux CPython
    # parses 978. The threshold cases run where it was measured (declared
    # in doctor.md).
    for item in c:
        if item.id in DEPTH_CASES:
            item.platforms = ("windows",)
    return c


# --- mutants ------------------------------------------------------------------

def _python_identity_mutant() -> Mutant:
    """The native report claiming the oracle interpreter's identity (this
    interpreter, the oracle package directory and an MCP SDK version,
    resolved at run time):
    the identity rule must not tokenize another arm's identity."""
    source = (producers._SOURCE or RUST.parent) / "pseudolife_memory"
    return Mutant(
        "doctor-identity-claims-python", "doctor", "shim/src/cli/doctor/mod.rs",
        '        ("interpreter", interpreter),\n        ("source", source),\n'
        '        ("pseudolife-mcp", env!("CARGO_PKG_VERSION").to_owned()),\n    ])',
        f'        ("interpreter", r"{sys.executable}".to_owned()),\n'
        f'        ("source", {{ let _ = (interpreter, source); r"{source}".to_owned() }}),\n'
        '        ("pseudolife-mcp", env!("CARGO_PKG_VERSION").to_owned()),\n'
        '        ("mcp", "2.1.1".to_owned()),\n    ])',
        ("unreachable",))


MUTANTS = [
    _python_identity_mutant(),
    Mutant("doctor-version-mismatch-ok", "doctor", "shim/src/cli/doctor/mod.rs",
           'put(&mut report, "ok", json!(false));\n                    put(&mut report, "version_mismatch"',
           'put(&mut report, "ok", json!(true));\n                    put(&mut report, "version_mismatch"',
           ("healthy-version-mismatch",)),
    Mutant("doctor-exit-code-ok", "doctor", "shim/src/cli/doctor/mod.rs",
           "if ok { 0 } else { 1 }", "if ok { 0 } else { 2 }", ("unreachable",)),
    Mutant("doctor-caps-order", "doctor", "shim/src/cli/doctor/mod.rs",
           "for (key, value) in wake {", "for (key, value) in wake.iter().rev() {", ("healthy",)),
    Mutant("doctor-board-token", "doctor", "shim/src/cli/doctor/probes.rs",
           '"on - token present, principal allowed"', '"on - token present, principal admitted"',
           ("board-on",)),
    Mutant("doctor-skip-maintainer", "doctor", "shim/src/cli/doctor/mod.rs",
           "probes::maintainer(&url, &token, short).await?",
           "{ let _ = &token; Map::new() }", ("maintainer-on",)),
    Mutant("doctor-registration-label", "doctor", "shim/src/cli/doctor/clients.rs",
           '"Claude Code registration ({claude})"', '"Claude registration ({claude})"',
           ("claude-registration-token",)),
    Mutant("doctor-wake-hook-flag", "doctor", "shim/src/cli/doctor/clients.rs",
           '&& (flag != "PSEUDOLIFE_AGENT_WAKE_HOOK" || off)', "",
           ("claude-wake-hook-invalid",)),
    Mutant("doctor-tool-count", "doctor", "shim/src/cli/doctor/handshake.rs",
           "tool_count: tools.len(),", "tool_count: tools.len() + 1,", ("healthy",)),
    Mutant("doctor-agent-proof-inverted", "doctor", "shim/src/cli/doctor/agent.rs",
           "if answer == expected {", "if answer != expected {",
           ("agent-state-authenticated", "agent-state-bad-proof")),
    Mutant("doctor-maintainer-frozen-bearer", "doctor", "shim/src/cli/doctor/mod.rs",
           "        match fresh() {\n", "        match Ok::<_, ()>(token.clone()) {\n",
           ("token-rotated-between-probes",)),
    Mutant("doctor-unbounded-body-read", "doctor", "shim/src/cli/doctor/probes.rs",
           "let limits = (Some(1 << 20), Some(65536));",
           "let limits = (None, None);",
           ("maintainer-stalled-refusal", "maintainer-stalled-success")),
    Mutant("doctor-unhashable-error-refused", "doctor", "shim/src/cli/doctor/agent.rs",
           'Some(Value::Array(_) | Value::Object(_)) => "unsupported_capability",', "",
           ("agent-state-error-list", "agent-state-error-object")),
    Mutant("doctor-int-digits-at-limit", "doctor", "shim/src/cli/doctor/pyenv.rs",
           "len() > INT_MAX_STR_DIGITS", "len() >= INT_MAX_STR_DIGITS",
           ("health-integer-at-limit", "agent-state-integer-at-limit")),
    Mutant("doctor-toml-bom-accepted", "doctor", "shim/src/cli/doctor/pyenv.rs",
           "if text.starts_with('\\u{feff}') {", "if text.starts_with('\\u{0}') {",
           ("codex-wake-bom", "codex-registration-double-bom")),
    Mutant("doctor-agent-path-unnormalized", "doctor", "shim/src/cli/doctor/agent.rs",
           "let normalized = super::pyenv::path_str(value)?;", "let normalized = value.to_owned();",
           ("agent-state-trailing-separator",)),
    Mutant("doctor-which-skips-cwd", "doctor", "shim/src/cli/doctor/pyenv.rs",
           'directories.insert(0, ".");', "", ("which-cwd-first",)),
    Mutant("doctor-pathlib-keeps-dot-parts", "doctor", "shim/src/cli/doctor/pyenv.rs",
           ".split('\\\\')\n            .filter(|p| !p.is_empty() && *p != \".\")",
           ".split('\\\\')\n            .filter(|p| !p.is_empty())",
           ("claude-config-dir-dot-parts",)),
    Mutant("doctor-escape-upper-hex", "doctor", "shim/src/cli/doctor/pyjson.rs",
           'format!("\\\\u{unit:04x}")', 'format!("\\\\u{unit:04X}")',
           ("claude-config-dir-non-ascii",)),
    Mutant("doctor-wire-keep-alive", "doctor", "shim/src/cli/hook_http.rs",
           '.header("Connection", "close")', '.header("Connection", "keep-alive")',
           ("healthy", "board-on")),
    Mutant("doctor-context-no-encodings", "doctor", "shim/src/cli/doctor/agent.rs",
           '.header("Accept-Encoding", "gzip, deflate")', "",
           ("agent-state-authenticated",)),
    Mutant("doctor-drop-final-newline", "doctor", "shim/src/cli/doctor/mod.rs",
           '&(pyjson::dumps(&report) + "\\n"),', "&pyjson::dumps(&report),", ("unreachable",)),
]
if core.WINDOWS:
    # Its cases run only where the depth threshold was measured.
    MUTANTS.append(Mutant("doctor-json-depth-off-by-one", "doctor", "shim/src/cli/doctor/agent.rs",
                          "> PYTHON_JSON_DEPTH {", "> PYTHON_JSON_DEPTH + 1 {",
                          ("agent-state-too-deep", "agent-state-deep-parsed")))

"""CLI-CONNECT: ``pseudolife-mcp connect`` for the JSON-file clients.

Canonical argv (the producers: ``ops/install.sh`` / ``ops/install.ps1``'s
client-only connect block, ``docs/guide/remote-bank.md``, ``move_cli``):
``connect <url> [--token-file P] [--read-token] [--client NAMES] [--dry-run]
[--yes] [--json]``, the URL first, each option at most once with its value
as the next argument. Pairing (``--code`` / ``--read-code``), a Codex
``config.toml``, an interactive question or token prompt, a Windows
User-scope daemon URL and an unattended-update schedule defer before any
effect; the Rust tests (``rust/shim/tests/cli_connect_contract.rs``) pin
those deferrals. This row compares answered cases only.

Contract per arm: exit code, both streams byte-exact, every file under the
home (written configs, backups, created token files), and the requests the
stand-in daemon received: connect's own requests (attributed by the client
process holding each connection) raw and in order, every header field and
the body, and of its handshake child, whose transport is the shim's own
row, the ``Authorization`` values its MCP posts carried. The fixture
credentials are generated per process and replaced by placeholders in
each arm's observation (``redact_credentials``). Named rules:
``connect-backup-stamp`` (the UTC stamp in a backup's name, validated
against the arm's own clock window), ``connect-restart-pid`` (the pid of
the process a case starts inside the runtimes root) and
``shim-handshake-cache-name`` (the cache file's per-port name). The
handshake shim's cache content is compared byte for byte.

Safety: every client-locating variable is set to the disposable home or
removed (HOME, USERPROFILE, APPDATA, LOCALAPPDATA by the harness; CODEX_HOME,
XDG_*, CLAUDE_CONFIG_DIR, the shim layout overrides, HOMEDRIVE/HOMEPATH
here), the arm's cwd is inside the home, the daemon is a fixture, and every
case's ``after`` hook fails the run if this user's real client
configuration (or a backup or temporary file beside it, or a token file in
the real ``~/.pseudolife-mcp``) changed, or if a path the arm reported lies
outside its home.

A daemon "on another machine" is reached as ``http://localhost.:<port>``:
``_is_loopback_url`` (and the candidate) treat the trailing-dot name as
remote, and the resolver still answers it with this machine's loopback
addresses, where the fixture listens.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil  # attributes each request to connect or to its handshake child

from .. import normalize
from ..core import WINDOWS, Case
from ..mutants import Mutant
from . import _handshake_cache  # noqa: F401 (registers shim-handshake-cache-name)

# Fixture credentials, generated per harness process so that neither the
# source nor a golden ever holds one. Each observation replaces exactly
# these values by their placeholders before it is compared or recorded
# (redact_credentials); any other credential stays as sent.
GOOD = "good-" + secrets.token_hex(16)
OTHER = "other-" + secrets.token_hex(16)
CREDENTIALS = {GOOD: "<credential:good>", OTHER: "<credential:other>"}
SERVER = "pseudolife-memory"
DESKTOP = "pseudolife-desktop"
URL_KEY = "PSEUDOLIFE_MCP_DAEMON_URL"
FILE_KEY = "PSEUDOLIFE_MCP_TOKEN_FILE"
TOKEN_KEY = "PSEUDOLIFE_MCP_TOKEN"
NO_SPAWN = "PSEUDOLIFE_MCP_NO_SPAWN"
OLD_URL = "http://100.64.0.1:8765"
SEP = os.sep

TOOLS = [
    {"name": "memory_search", "description": "Search memory.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "memory_store", "description": "Store a memory.",
     "inputSchema": {"type": "object", "properties": {"content": {"type": "string"}}},
     "annotations": {"readOnlyHint": False}},
    {"name": "memory_stats", "description": "Bank statistics.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},
]


# ── the stand-in daemon ────────────────────────────────────────────────────

class Daemon:
    """``/health``, the bearer-checked ``/api/episodes`` probe, the board
    check-in, streamable-HTTP ``/mcp`` (JSON answers) and the episode posts,
    on 127.0.0.1 and ::1 at one port."""

    def __init__(self, port: int, *, url: str, auth=True, accept=(GOOD,), board="on",
                 health=None, health_after_board=None, tools=None,
                 instructions="Fixture daemon instructions.", health_status=200,
                 health_redirect=False, board_body=None):
        self.url = url
        self.health_status = health_status
        self.health_redirect = health_redirect
        self.board_body = board_body
        self.health = health if health is not None else {
            "status": "ok", "auth": auth, "version": "0.0.0-fixture"}
        self.auth = auth
        self.accept = set(accept)
        self.board = board
        self.health_after_board = health_after_board
        self.tools = TOOLS if tools is None else tools
        self.instructions = instructions
        self.board_seen = False
        self.port = port
        self.arm_pid: int | None = None
        self._seen: list[tuple[int | None, dict]] = []
        self._lock = threading.Lock()
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _token(self) -> str:
                return (self.headers.get("Authorization") or "").removeprefix("Bearer ")

            def _allowed(self) -> bool:
                return not daemon.auth or self._token() in daemon.accept

            def _record(self, body: bytes = b"") -> None:
                """The request as it arrived, in the harness's wire shape:
                every header field in order (values as http.server decodes
                them, Latin-1) and the body."""
                owner = daemon._owner(self.client_address[1])
                entry = {"method": self.command, "target": self.path,
                         "headers": [[name, value] for name, value in self.headers.items()],
                         "body": body.decode("utf-8", "backslashreplace")}
                with daemon._lock:
                    daemon._seen.append((owner, entry))

            def _send(self, status, body=b"", headers=()):
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _json(self, status, value, headers=()):
                self._send(status, json.dumps(value).encode(),
                           (("Content-Type", "application/json"), *headers))

            def do_GET(self):
                self._record()
                if self.path == "/health" and daemon.health_redirect:
                    self._send(302, b"", (("Location", "/health/live"),))
                elif self.path in ("/health", "/health/live"):
                    health = daemon.health
                    if daemon.health_after_board is not None and daemon.board_seen:
                        health = daemon.health_after_board
                    self._json(daemon.health_status, health)
                elif self.path.startswith("/api/episodes"):
                    if self._allowed():
                        self._json(200, {"episodes": []})
                    else:
                        self._json(401, {"error": "unauthorized"})
                elif self.path.startswith("/api/hook/coordination-start"):
                    daemon.board_seen = True
                    body = daemon.board_body
                    if body is None:
                        body = b"check-in" if daemon.board == "on" else b""
                    headers = (("X-PL-Board", daemon.board),) if daemon.board is not None else ()
                    self._send(200, body, headers)
                elif self.path.startswith("/mcp"):
                    self._send(405, b"", (("Allow", "POST, DELETE"),))
                else:
                    self._json(404, {"error": "not_found"})

            def do_DELETE(self):
                self._record()
                self._send(200 if self.path.startswith("/mcp") else 404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._record(body)
                if self.path.startswith("/api/episode"):
                    self._json(200, {"ok": True})
                    return
                if not self.path.startswith("/mcp"):
                    self._json(404, {"error": "not_found"})
                    return
                if not self._allowed():
                    self._json(401, {"error": "unauthorized"})
                    return
                try:
                    message = json.loads(body)
                except ValueError:
                    self._json(400, {"error": "bad json"})
                    return
                if not isinstance(message, dict) or "id" not in message:
                    self._send(202)
                    return
                ident, method = message["id"], message.get("method")
                if method == "initialize":
                    params = message.get("params") or {}
                    result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                              "capabilities": {"tools": {"listChanged": False}},
                              "serverInfo": {"name": "pseudolife-memory",
                                             "version": "0.0.0-fixture"}}
                    if daemon.instructions is not None:
                        result["instructions"] = daemon.instructions
                    self._json(200, {"jsonrpc": "2.0", "id": ident, "result": result},
                               (("Mcp-Session-Id", "fixture-session"),))
                elif method == "tools/list":
                    self._json(200, {"jsonrpc": "2.0", "id": ident,
                                     "result": {"tools": daemon.tools}})
                elif method == "ping":
                    self._json(200, {"jsonrpc": "2.0", "id": ident, "result": {}})
                else:
                    self._json(200, {"jsonrpc": "2.0", "id": ident,
                                     "error": {"code": -32601, "message": "Method not found"}})

        class V4Server(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # a client closing a kept-alive connection is not a finding

        class V6Server(V4Server):
            address_family = socket.AF_INET6

        self.servers = [V4Server(("127.0.0.1", port), Handler)]
        try:
            self.servers.append(V6Server(("::1", port), Handler))
        except OSError:
            pass
        self.threads = []
        for server in self.servers:
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)

    def _owner(self, client_port: int) -> int | None:
        """The pid that holds the client end of this connection (looked up
        per request: an ephemeral port may later belong to another process)."""
        for conn in psutil.net_connections(kind="tcp"):
            if (conn.laddr and conn.laddr.port == client_port and conn.raddr
                    and conn.raddr.port == self.port):
                return conn.pid
        return None

    def requests(self) -> list:
        """Connect's own requests in order, each raw: method, target, every
        header field in order and the body (the harness's wire projection
        compares every field). Of the handshake child (the shim, whose
        transport is its own row) only the ``Authorization`` values its MCP
        posts carried, as one summary entry whose headers are already
        projected. Credentials are tokenized later, per arm, by
        redact_credentials."""
        with self._lock:
            seen = list(self._seen)

        def mcp(entry: dict) -> bool:
            return entry["target"].startswith("/mcp")

        def authorization(entry: dict) -> str:
            values = [v for k, v in entry["headers"] if k.lower() == "authorization"]
            return "|".join(values) if values else "none"

        # Connect never calls /mcp (connect_cli.py, codex_connection.py), so
        # an unattributed /mcp request is the handshake child's: on Linux
        # psutil can miss the child's short-lived sockets. A /mcp request
        # traced to connect's own pid stays in `own` and fails the comparison.
        own = [entry for owner, entry in seen
               if owner == self.arm_pid or (owner is None and not mcp(entry))]
        child = sorted({authorization(entry) for owner, entry in seen
                        if owner != self.arm_pid and mcp(entry) and entry["method"] == "POST"})
        return own + [{"method": "handshake-credentials", "target": "/mcp",
                       "headers": {"authorization": ",".join(child)}, "body": ""}]

    def close(self) -> None:
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=5)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _target(remote: bool = False) -> tuple[int, str]:
    port = _free_port()
    return port, (f"http://localhost.:{port}" if remote else f"http://127.0.0.1:{port}")


def _fixture(port: int, url: str, **options):
    return lambda: Daemon(port, url=url, **options)


# ── seeds ───────────────────────────────────────────────────────────────────

def _oracle_token_writer():
    from pseudolife_memory.credentials import _write_token_file  # noqa: PLC0415 (oracle producer)
    return _write_token_file


def token_file(home: Path, name: str = "claude-code.token", token: str = GOOD) -> Path:
    path = home / ".pseudolife-mcp" / name
    _oracle_token_writer()(path, token)
    return path


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def launcher(home: Path) -> Path:
    if WINDOWS:
        return home / "AppData" / "Local" / "pseudolife-mcp" / "bin" / "pseudolife-mcp.exe"
    return home / ".local" / "share" / "pseudolife-mcp" / "bin" / "pseudolife-mcp"


def make_launcher(home: Path) -> Path:
    path = launcher(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"launcher stand-in")
    return path


def runtimes_root(home: Path) -> Path:
    return launcher(home).parent.parent / "runtimes"


def desktop_config(home: Path, msix: str | None = None) -> Path:
    if not WINDOWS:
        return home / ".config" / "Claude" / "claude_desktop_config.json"
    if msix:
        return (home / "AppData" / "Local" / "Packages" / msix / "LocalCache" / "Roaming"
                / "Claude" / "claude_desktop_config.json")
    return home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"


def entry(command, env=None, **extra) -> dict:
    data = {"type": "stdio", "command": str(command), "args": []}
    data.update(extra)
    if env is not None:
        data["env"] = env
    return data


# Binary64 values whose shortest repr is an exact decimal tie: CPython keeps
# the even digit, Rust's shortest formatting may not (the ties in
# rust/shim/tests/cli_audit_float_repr.tsv, then 2**49 + 0.25).
FLOAT_TIES = (float.fromhex("0x1.526daef896bc8p+46"),   # 93026504287663.12
              float.fromhex("0x1.c6bf526340002p+49"),   # 1000000000000000.2
              float.fromhex("-0x1.8130f222a4572p+49"),  # -847044394961070.2
              2.0 ** 49 + 0.25)                         # 562949953421312.2


def claude_json(home: Path, server: dict | None = None, **extra) -> Path:
    """``~/.claude.json`` as Claude Code keeps it: unrelated state around
    the user-scope ``mcpServers`` entry."""
    data = {"numStartups": 42, "theme": "dark", "tipsHistory": {"memory": 3},
            "lastCost": 1.25, "userName": "Zoë 雪"}
    if server is not None:
        data["mcpServers"] = {SERVER: server, "other-server": {"command": "node", "args": ["x.js"]}}
    data.update(extra)
    path = home / ".claude.json"
    write_json(path, data)
    return path


def claude_env(home: Path, *, url: str = OLD_URL, token: Path | None = None, **extra) -> dict:
    env = {"PSEUDOLIFE_WRITER_ID": "claude-code", URL_KEY: url}
    if token is not None:
        env[FILE_KEY] = str(token)
    env.update(extra)
    return env


def settings_json(home: Path, env: dict | None, base: Path | None = None, **extra) -> Path:
    data = {"enabledPlugins": {"pseudolife-memory@pseudolife-mcp": True}}
    if env is not None:
        data["env"] = env
    data.update(extra)
    path = (base or home / ".claude") / "settings.json"
    write_json(path, data)
    return path


# ── named rules ─────────────────────────────────────────────────────────────

_STAMP = re.compile(rb"\.bak-pseudolife-(\d{8}-\d{6}-\d{6})")


def _stamp_in_window(stamp: bytes, window: list[float]) -> bool:
    moment = datetime.datetime.strptime(stamp.decode(), "%Y%m%d-%H%M%S-%f")
    seconds = moment.replace(tzinfo=datetime.timezone.utc).timestamp()
    return window[0] - 1 <= seconds <= window[1] + 1


@normalize.rule("connect-backup-stamp")
def backup_stamp(obs: dict) -> None:
    """The UTC stamp in ``<file>.bak-pseudolife-<stamp>``, only where it
    names a moment inside the arm's own run. In file names (and the
    ``modes`` and ACL records keyed by them) two names that normalize alike
    stay distinct (`` <normalized-collision>``), so an extra backup shows.
    In the streams a stamp is replaced only when this arm's snapshot holds a
    backup with that exact stamp, so a reported backup must exist."""
    window = obs["window"]

    def swap(match: re.Match) -> bytes:
        if _stamp_in_window(match.group(1), window):
            return b".bak-pseudolife-<stamp>"
        return match.group(0)

    on_disk = {match.group(1) for rel in obs["files"]
               for match in _STAMP.finditer(rel.encode())}

    def reported(match: re.Match) -> bytes:
        return swap(match) if match.group(1) in on_disk else match.group(0)

    for field in ("stdout", "stderr"):
        normalize._put(obs, field, _STAMP.sub(reported, normalize._get(obs, field)))
    names: dict[str, str] = {}
    files = {}
    for rel, value in obs["files"].items():
        new_rel = _STAMP.sub(swap, rel.encode()).decode()
        while new_rel in files:
            new_rel += " <normalized-collision>"
        names[rel] = new_rel
        files[new_rel] = value
    obs["files"] = files
    for record in (obs.get("modes"), (obs.get("db") or {}).get("windows_acl")):
        if isinstance(record, dict):
            renamed = {names.get(rel, rel): value for rel, value in record.items()}
            record.clear()
            record.update(renamed)


@normalize.rule("connect-target-url")
def target_url(obs: dict) -> None:
    """The case's own target URL (``fixture_url``, a loopback port the
    harness picked) as ``{DAEMON}`` in the streams and files, where no
    fixture daemon answered it (``health-unreachable``): the harness's
    daemon token then never ran, and a golden recorded on one free port
    replays on another. Only that exact URL is replaced."""
    url = obs.get("fixture_url")
    if not url or obs.get("daemon_url"):
        return
    raw = url.encode()
    for field in ("stdout", "stderr"):
        normalize._put(obs, field, normalize._get(obs, field).replace(raw, b"{DAEMON}"))
    for rel, value in list(obs["files"].items()):
        if value.startswith("file:"):
            normalize._set_file(obs, rel, base64.b64decode(value[5:]).replace(raw, b"{DAEMON}"))


@normalize.rule("connect-restart-pid")
def restart_pid(obs: dict) -> None:
    """The pid of the process this case started inside the runtimes root."""
    pid = obs.get("restart_pid")
    if not pid:
        return
    pattern = re.compile(rb"(?<![0-9])" + str(pid).encode() + rb"(?![0-9])")
    for field in ("stdout",):
        normalize._put(obs, field, pattern.sub(b"<pid>", normalize._get(obs, field)))


# ── the fixture credentials ─────────────────────────────────────────────────

def _redact(data: bytes) -> bytes:
    for value, token in CREDENTIALS.items():
        data = data.replace(value.encode(), token.encode())
    return data


def _redact_text(text: str) -> str:
    for value, token in CREDENTIALS.items():
        text = text.replace(value, token)
    return text


def redact_credentials(arm, obs: dict) -> None:
    """Each generated fixture credential replaced by its placeholder in
    everything this arm's observation holds: both streams, every request
    field (``Authorization`` included, so a different credential, or the
    same one encoded differently, still shows around the placeholder) and
    every file under the home, rewritten in place before the harness
    snapshots it (same file, same mode and ACL). Only these exact values
    are replaced: they are this process's, so a match identifies them, and
    nothing else is touched. Run last, after the checks that read the raw
    observation."""
    for field in ("stdout", "stderr"):
        normalize._put(obs, field, _redact(normalize._get(obs, field)))
    for request in obs.get("requests", []):
        headers = request["headers"]
        if isinstance(headers, dict):
            request["headers"] = {k: _redact_text(v) for k, v in headers.items()}
        else:
            request["headers"] = [[k, _redact_text(v)] for k, v in headers]
        request["target"] = _redact_text(request["target"])
        request["body"] = _redact_text(request["body"])
    for dirpath, _dirnames, filenames in os.walk(arm.home, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue  # the snapshot records it as unreadable
            redacted = _redact(data)
            if redacted != data:
                with open(path, "r+b") as stream:
                    stream.write(redacted)
                    stream.truncate()


# ── the real-configuration guard ────────────────────────────────────────────

def _real_targets() -> list[Path]:
    env = os.environ
    home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
    found = [home / ".claude.json", home / ".claude" / "settings.json",
             home / ".codex" / "config.toml", home / ".codex" / "pseudolife" / "connection.json",
             home / ".gemini" / "settings.json", Path.cwd() / ".mcp.json"]
    if env.get("CLAUDE_CONFIG_DIR"):
        base = Path(env["CLAUDE_CONFIG_DIR"])
        found += [base / ".claude.json", base / "settings.json"]
    if env.get("CODEX_HOME"):
        found.append(Path(env["CODEX_HOME"]) / "config.toml")
    if WINDOWS:
        appdata = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
        found.append(appdata / "Claude" / "claude_desktop_config.json")
        local = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
        for package in sorted((local / "Packages").glob("Claude_*")):
            found.append(package / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
    else:
        config = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
        found.append(config / "Claude" / "claude_desktop_config.json")
    return found


def _real_state() -> dict:
    """What a connect write to a real file would change. The clients rewrite
    their own configs at any time (Claude Code its ``.claude.json``, Claude
    Desktop its config, measured during the first run of this row), so
    mtimes prove nothing; a connect write always creates the file or leaves
    a ``.bak-pseudolife-<stamp>`` backup beside it (and briefly a
    ``.pseudolife-*`` temporary), and puts the target URL in it, which
    ``_check_real_configuration`` looks for."""
    state = {}
    for path in _real_targets():
        state[str(path)] = path.exists()
        try:
            names = sorted(name for name in os.listdir(path.parent)
                           if ".bak-pseudolife" in name or name.startswith(".pseudolife-"))
        except OSError:
            names = None
        state[f"{path.parent}|written-beside"] = names
    tokens = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())
    try:
        state["token-files"] = sorted(p.name for p in (tokens / ".pseudolife-mcp").glob("*.token"))
    except OSError:
        state["token-files"] = None
    return state


_BASELINE: dict | None = None


def _check_real_configuration(arm, url: str) -> None:
    now = _real_state()
    changed = sorted(key for key in set(now) | set(_BASELINE or {})
                     if (_BASELINE or {}).get(key) != now.get(key))
    markers = [str(arm.home.parent.name)] + ([url] if url else [])
    for path in _real_targets():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        changed += [f"{path} names {marker}" for marker in markers if marker in text]
    if changed:
        raise AssertionError(f"connect row: real client configuration changed: {changed}")


def _check_reported_paths(arm, obs: dict) -> None:
    home = str(arm.home)
    reported = []
    stdout = base64.b64decode(obs["stdout"]).decode("utf-8", "replace")
    reported += re.findall(r"backup: (.+?)\r?$", stdout, re.M)
    try:
        report = json.loads(stdout)
    except ValueError:
        report = None
    if isinstance(report, dict):
        for row in report.get("rows") or []:
            reported += [row.get("file"), row.get("backup")]
        for item in report.get("rollback") or []:
            reported += [item.get("file"), item.get("backup")]
    for path in filter(None, reported):
        if not path.startswith(home):
            raise AssertionError(f"connect row: {arm.name} reported a path outside its home: {path}")


def _windows_acl(home: Path) -> dict:
    """Whether each file under the home is owner-only, by the oracle's own
    test (``credentials._windows_owner_only``). Master's core compares POSIX
    modes and no Windows ACLs; this record rides in the compared ``db``
    field. The arms' temporary directory is the handshake child's, not
    connect's."""
    from pseudolife_memory.credentials import _windows_owner_only  # noqa: PLC0415
    found = {}
    for path in sorted(home.rglob("*")):
        rel = path.relative_to(home).as_posix()
        if not path.is_file() or rel.startswith("tmp/"):
            continue
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            found[rel] = "owner-only" if _windows_owner_only(fd) else "inherited"
        finally:
            os.close(fd)
    return found


def _after(url: str, extra=None):
    def after(arm, obs):
        try:
            if extra:
                extra(arm, obs)
        finally:
            obs["fixture_url"] = url
            if WINDOWS:
                obs["db"] = {"windows_acl": _windows_acl(arm.home)}
            _check_reported_paths(arm, obs)
            _check_real_configuration(arm, url)
            redact_credentials(arm, obs)
    return after


# Every variable a client-locating path reads, pinned inside the home.
def _env(**extra) -> dict:
    env = {"CODEX_HOME": f"{{HOME}}{SEP}.codex",
           "XDG_CONFIG_HOME": f"{{HOME}}{SEP}.config",
           "XDG_DATA_HOME": f"{{HOME}}{SEP}.local{SEP}share",
           "XDG_STATE_HOME": f"{{HOME}}{SEP}.local{SEP}state",
           "XDG_CACHE_HOME": f"{{HOME}}{SEP}.cache",
           "CLAUDE_CONFIG_DIR": None, "PSEUDOLIFE_SHIM_RUNTIMES": None,
           "PSEUDOLIFE_SHIM_LAUNCHER": None, "PSEUDOLIFE_SHIM_USER_BIN": None,
           "PSEUDOLIFE_AGENT_STATE_DIR": None, "HOMEDRIVE": None, "HOMEPATH": None,
           # The oracle runs its handshake child, and the shim under it, from
           # tempfile.gettempdir() with that directory first on sys.path. On
           # the shared Windows host the default TEMP held ~115,000 entries
           # churned by other sessions, and the child missed its 20 s budget
           # in 6 of 46 cases (2026-10-09; 1 of 6 standalone runs failed from
           # TEMP, 0 of 6 from a small directory). A directory inside the
           # home keeps the default root's ACLs and the arms' cwd neutral.
           "TEMP": f"{{HOME}}{SEP}tmp", "TMP": f"{{HOME}}{SEP}tmp",
           "TMPDIR": f"{{HOME}}{SEP}tmp"}
    env.update(extra)
    return env


RULES = ("connect-backup-stamp", "connect-restart-pid", "shim-handshake-cache-name",
         "connect-target-url")


def _arm_pid(arm, proc) -> None:
    if arm.daemon is not None:
        arm.daemon.arm_pid = proc.pid


def _with_temp(setup):
    def run(arm):
        (arm.home / "tmp").mkdir(exist_ok=True)
        if setup:
            setup(arm)
    return run


def case(case_id: str, argv: list[str], *, url: str, port: int | None, setup=None, after=None,
         env=None, stdin: bytes = b"", platforms=("windows", "linux"), timeout: float = 120,
         stderr_closed: bool = False, stdout_closed: bool = False, golden: bool = True,
         **daemon) -> Case:
    # With every client selected (no --client), the oracle reads the
    # unattended-update schedule: on Windows it runs this host's System32
    # schtasks /Query, which no PATH inside the home can hide.
    schedule = ("schtasks",) if WINDOWS and "--client" not in argv else ()
    return Case(case_id, ["connect", *argv], env=_env(**(env or {})), stdin=stdin,
                setup=_with_temp(setup), during=_arm_pid, real_programs=schedule,
                after=_after(url, after), timeout=timeout,
                rules=RULES + (("python-stdout-closed-trailer",) if stdout_closed else ()),
                platforms=platforms, stderr_closed=stderr_closed, stdout_closed=stdout_closed,
                golden=golden,
                daemon=_fixture(port, url, **daemon) if port is not None else None)


# ── setups ──────────────────────────────────────────────────────────────────

def claude_registered(*, url=OLD_URL, settings=None, launcher_command=False, token=True,
                      **extra_env):
    def setup(arm):
        home = arm.home
        tf = token_file(home) if token else None
        command = make_launcher(home) if launcher_command else home / "tools" / "pseudolife-mcp.exe"
        claude_json(home, entry(command, claude_env(home, url=url, token=tf, **extra_env)))
        if settings is not None:
            settings_json(home, settings(home, tf))
    return setup


def desktop_and_gemini(*, msix=True, legacy=True, gemini_env=None, desktop_url=OLD_URL):
    def setup(arm):
        home = arm.home
        tf = token_file(home, "desktop.token")
        shim = make_launcher(home)
        servers = {DESKTOP: entry(shim, {"PSEUDOLIFE_WRITER_ID": "claude-desktop",
                                         URL_KEY: desktop_url, FILE_KEY: str(tf)})}
        if legacy:
            servers[SERVER] = entry(shim, {"PSEUDOLIFE_WRITER_ID": "claude-desktop"})
        write_json(desktop_config(home, "Claude_pzs8sxrjxfjjc" if msix else None),
                   {"mcpServers": servers, "globalShortcut": ""})
        if msix:
            write_json(desktop_config(home), {"mcpServers": {"filesystem": {"command": "npx"}}})
        genv = gemini_env if gemini_env is not None else {
            "PSEUDOLIFE_WRITER_ID": "gemini", URL_KEY: OLD_URL, TOKEN_KEY: GOOD}
        write_json(home / ".gemini" / "settings.json",
                   {"theme": "Default", "mcpServers": {SERVER: entry(shim, genv)}})
    return setup


def both(*setups):
    def setup(arm):
        for item in setups:
            item(arm)
    return setup


# ── cases ───────────────────────────────────────────────────────────────────

def cases() -> list[Case]:
    global _BASELINE
    if _BASELINE is None:
        _BASELINE = _real_state()
    out: list[Case] = []
    add = out.append

    # Refusals before any daemon contact.
    add(case("invalid-url-path", ["http://127.0.0.1:8765/mcp", "--dry-run"], url="", port=None))
    add(case("invalid-url-no-scheme", ["100.64.0.2:8765"], url="", port=None))
    add(case("invalid-url-scheme", ["ftp://Host.Example:21"], url="", port=None))
    add(case("invalid-url-json", ["http://user:pw@h:1/x", "--json"], url="", port=None))
    port, url = _target()
    add(case("bad-client", [url, "--client", "claude-code,cursor"], url=url, port=port))
    port, url = _target()
    add(case("read-token-needs-file", [url, "--read-token", "--yes"], url=url, port=port))
    port, url = _target()
    add(case("read-token-file-exists",
             [url, "--client", "claude-code", "--token-file", "~/.pseudolife-mcp/claude-code.token",
              "--read-token", "--yes"], url=url, port=port, setup=claude_registered()))

    # The health probe.
    port, url = _target()
    add(case("health-unreachable", [url, "--dry-run"], url=url, port=None,
             setup=claude_registered()))
    port, url = _target()
    add(case("health-degraded", [url, "--dry-run"], url=url, port=port,
             health={"status": "degraded", "auth": True, "version": "0.0.0-fixture"},
             setup=claude_registered()))
    port, url = _target()
    add(case("health-status-null-json", [url, "--yes", "--json"], url=url, port=port,
             health={"status": None}, setup=claude_registered()))
    port, url = _target(remote=True)
    add(case("remote-open-daemon", [url, "--dry-run"], url=url, port=port, auth=False,
             setup=claude_registered()))

    # Plans.
    port, url = _target()
    add(case("dry-run-docs", [url, "--dry-run"], url=url, port=port,
             setup=claude_registered(launcher_command=True,
                                     settings=lambda home, tf: {"OTHER": "kept"})))
    port, url = _target()

    def installer_home(arm):
        home = arm.home
        claude_registered(settings=lambda home, tf: {FILE_KEY: str(tf), URL_KEY: OLD_URL})(arm)
        desktop_and_gemini()(arm)
        data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
        data["projects"] = {str(home / "proj"): {"mcpServers": {SERVER: {"command": "x"}}},
                            str(home / "other"): {"mcpServers": {}}}
        write_json(home / ".claude.json", data)
        write_json(arm.cwd / ".mcp.json", {"mcpServers": {SERVER: {"command": "y"}}})
    add(case("dry-run-installer-json",
             [url, "--token-file", f"{{HOME}}{SEP}.pseudolife-mcp{SEP}installer.token", "--client",
              "claude-code,claude-desktop,gemini", "--dry-run", "--json"],
             url=url, port=port, setup=installer_home))
    port, url = _target(remote=True)
    add(case("dry-run-remote", [url, "--dry-run"], url=url, port=port,
             setup=both(claude_registered(settings=lambda home, tf: None),
                        desktop_and_gemini(msix=False, legacy=False, gemini_env={
                            "PSEUDOLIFE_WRITER_ID": "gemini", URL_KEY: OLD_URL,
                            NO_SPAWN: " TRUE ", TOKEN_KEY: GOOD}))))
    port, url = _target()
    add(case("dry-run-environment", [url, "--client", "gemini", "--dry-run"], url=url, port=port,
             env={URL_KEY: "http://User:pw@100.64.0.9:8765/"},
             setup=desktop_and_gemini(msix=False, legacy=False)))
    port, url = _target()

    def config_dir(arm):
        base = arm.home / "claude-dir"
        tf = token_file(arm.home)
        write_json(base / ".claude.json", {"mcpServers": {SERVER: entry(
            arm.home / "tools" / "shim.exe", claude_env(arm.home, token=tf))}})
        settings_json(arm.home, {URL_KEY: OLD_URL}, base=base)
    add(case("dry-run-claude-config-dir", [url, "--client", "claude-code", "--dry-run"], url=url,
             port=port, env={"CLAUDE_CONFIG_DIR": f"{{HOME}}{SEP}claude-dir"}, setup=config_dir))
    port, url = _target()
    add(case("dry-run-notes", [url, "--dry-run", "--client", "claude-code"], url=url, port=port,
             setup=claude_registered(token=False, **{TOKEN_KEY: GOOD,
                                                     "PSEUDOLIFE_AGENT_STATE": "x.json",
                                                     NO_SPAWN: 0}),
             ))
    port, url = _target()

    def relative_token(arm):
        # The input pinned by rust/shim/tests/cli_connect_contract.rs.
        text = ("{\n  \"mcpServers\": {\n    \"pseudolife-memory\": {\n      \"command\": "
                + json.dumps(str(arm.home / "shim-bin")) + ",\n      \"env\": {\n        "
                "\"PSEUDOLIFE_MCP_DAEMON_URL\": \"http://100.64.0.1:8765\",\n        "
                "\"PSEUDOLIFE_MCP_TOKEN_FILE\": \"t.token\"\n      }\n    }\n  }\n}\n")
        (arm.home / ".claude.json").write_bytes(text.encode())
    add(case("dry-run-relative-token", [url, "--client", "claude-code", "--dry-run"], url=url,
             port=port, setup=relative_token))
    port, url = _target()
    add(case("dry-run-settings-literal", [url, "--dry-run", "--client", "claude-code"], url=url,
             port=port, setup=claude_registered(settings=lambda home, tf: {TOKEN_KEY: GOOD})))

    # Nothing to write, or nothing connect can write.
    port, url = _target()
    add(case("none-found", [url, "--yes"], url=url, port=port))
    port, url = _target()
    add(case("none-found-json", [url, "--client", "gemini", "--dry-run", "--json"], url=url,
             port=port))
    port, url = _target()

    def manual_only(arm):
        claude_json(arm.home, {"type": "http", "url": "https://example.com/mcp"})
        path = desktop_config(arm.home)
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{"mcpServers": {')
        write_json(arm.home / ".gemini" / "settings.json", {"mcpServers": {SERVER: [1, 2]}})
    add(case("manual-only", [url, "--yes"], url=url, port=port, setup=manual_only))
    port, url = _target()
    add(case("env-not-object", [url, "--client", "claude-code", "--yes", "--json"], url=url,
             port=port, setup=lambda arm: claude_json(arm.home, entry("shim", ["x"]))))
    port, url = _target()
    add(case("nothing-to-write", [url, "--yes"], url=url, port=port,
             setup=claude_registered(url=url, settings=lambda home, tf, url=url: {
                 FILE_KEY: str(tf), URL_KEY: url})))
    port, url = _target()

    def current_and_project(arm, url=url):
        claude_registered(url=url, settings=lambda home, tf: {FILE_KEY: str(tf), URL_KEY: url})(arm)
        write_json(arm.cwd / ".mcp.json", {"mcpServers": {SERVER: {"command": "y"}}})
    add(case("nothing-but-manual", [url, "--client", "claude-code", "--yes"], url=url, port=port,
             setup=current_and_project))

    # Confirmation.
    port, url = _target()
    add(case("not-interactive", [url], url=url, port=port, setup=claude_registered()))
    port, url = _target()
    add(case("docs-token-file-not-interactive",
             [url, "--client", "claude-code", "--token-file", "~/.pseudolife-mcp/claude-code.token"],
             url=url, port=port, setup=claude_registered()))

    # Apply.
    port, url = _target()
    add(case("apply-move-yes", [url, "--yes"], url=url, port=port, setup=claude_registered()))
    port, url = _target()

    def float_ties(arm):
        # Float state Claude Code keeps beside the entry, at exact repr ties:
        # the rewrite must keep CPython's even digit.
        claude_registered()(arm)
        path = arm.home / ".claude.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["lastCost"] = FLOAT_TIES[0]
        data["costHistory"] = list(FLOAT_TIES)
        write_json(path, data)

    add(case("apply-float-ties", [url, "--yes"], url=url, port=port, setup=float_ties))
    port, url = _target()
    add(case("apply-move-json", [url, "--yes", "--json"], url=url, port=port,
             setup=desktop_and_gemini()))
    # A stdout that refuses the report (review of #678 at 6447829f): the
    # oracle's block-buffered prints succeed, every request and write runs,
    # and CPython's shutdown flush then exits 120. Text and JSON apply, beside
    # their healthy controls apply-move-yes and apply-move-json (exit 0).
    port, url = _target()
    add(case("apply-move-yes-stdout-closed", [url, "--yes"], url=url, port=port,
             setup=claude_registered(), stdout_closed=True))
    port, url = _target()
    add(case("apply-move-json-stdout-closed", [url, "--yes", "--json"], url=url, port=port,
             setup=desktop_and_gemini(), stdout_closed=True))
    port, url = _target()

    def installer_apply(arm):
        claude_registered(settings=lambda home, tf: {FILE_KEY: str(tf), URL_KEY: OLD_URL,
                                                     TOKEN_KEY: GOOD})(arm)
        desktop_and_gemini(legacy=False)(arm)
        token_file(arm.home, "installer.token")
    add(case("apply-installer",
             [url, "--token-file", f"{{HOME}}{SEP}.pseudolife-mcp{SEP}installer.token", "--client",
              "claude-code,claude-desktop,gemini", "--yes"], url=url, port=port,
             setup=installer_apply))
    port, url = _target(remote=True)
    add(case("apply-remote", [url, "--yes"], url=url, port=port,
             setup=both(claude_registered(), desktop_and_gemini(msix=False, legacy=False))))
    # A stderr that refuses a warning (review of #678, 2026-10-10): the
    # oracle's line-buffered stderr raises there, before anything is written,
    # and CPython exits 120. The plain-HTTP warning comes before the plan; the
    # board warning after the plan and the verification, before the writes.
    port, url = _target(remote=True)
    add(case("apply-remote-stderr-closed", [url, "--yes"], url=url, port=port,
             setup=claude_registered(), stderr_closed=True))
    port, url = _target()
    add(case("apply-board-off-stderr-closed", [url, "--yes", "--client", "claude-code"],
             url=url, port=port, board="off; reason=principal_not_allowed",
             setup=claude_registered(), stderr_closed=True))
    port, url = _target()
    add(case("apply-read-token",
             [url, "--client", "claude-code", "--token-file", "~/.pseudolife-mcp/new.token",
              "--read-token", "--yes"], url=url, port=port, stdin=GOOD.encode() + b"\r\n",
             setup=claude_registered()))
    port, url = _target()
    add(case("read-token-empty-stdin",
             [url, "--client", "claude-code", "--token-file", "~/.pseudolife-mcp/new.token",
              "--read-token", "--yes"], url=url, port=port, stdin=b"",
             setup=claude_registered()))
    port, url = _target()
    add(case("apply-literal-token", [url, "--yes", "--client", "gemini,claude-desktop"], url=url,
             port=port, setup=desktop_and_gemini(msix=False, legacy=False)))
    port, url = _target()
    add(case("apply-open-loopback", [url, "--yes", "--client", "claude-code"], url=url, port=port,
             auth=False, setup=claude_registered(token=False)))
    port, url = _target()
    add(case("apply-board-off", [url, "--yes", "--client", "claude-code"], url=url, port=port,
             board="off; reason=principal_not_allowed", setup=claude_registered()))
    port, url = _target()

    def runtime_process(arm):
        claude_registered(launcher_command=True)(arm)
        if WINDOWS:
            source = Path(os.environ["SYSTEMROOT"]) / "System32" / "PING.EXE"
            image = runtimes_root(arm.home) / "000001" / "Scripts" / "ping.exe"
            argv = ["-n", "60", "127.0.0.1"]
        else:
            source = Path(shutil.which("sleep") or "/bin/sleep")
            image = runtimes_root(arm.home) / "000001" / "bin" / "sleep"
            argv = ["60"]
        image.parent.mkdir(parents=True)
        shutil.copy2(source, image)
        process = subprocess.Popen([str(image), *argv], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        arm.state["process"] = process

    def stop_runtime_process(arm, obs):
        process = arm.state.get("process")
        if process is not None:
            obs["restart_pid"] = process.pid
            process.kill()
            process.wait(10)
    # Live-only off Windows: the stand-in process is a copy of the host's `sleep`,
    # whose bytes the snapshot keeps (a multicall coreutils binary is ~15 MB).
    add(case("apply-restart-pids", [url, "--yes", "--client", "claude-code"], url=url, port=port,
             setup=runtime_process, after=stop_runtime_process, golden=WINDOWS))

    # Verification refusals.
    port, url = _target()
    add(case("verify-refused", [url, "--yes"], url=url, port=port, accept=(OTHER,),
             setup=claude_registered()))
    port, url = _target()
    add(case("verify-refused-json", [url, "--yes", "--json"], url=url, port=port,
             accept=(OTHER,), setup=claude_registered()))
    port, url = _target()

    def missing_token(arm):
        claude_json(arm.home, entry("shim", claude_env(
            arm.home, token=arm.home / ".pseudolife-mcp" / "gone.token")))
    add(case("verify-missing-token-file", [url, "--yes"], url=url, port=port,
             setup=missing_token))
    port, url = _target()
    add(case("verify-no-credential", [url, "--yes", "--client", "claude-code"], url=url,
             port=port, setup=claude_registered(token=False)))
    port, url = _target()
    add(case("verify-no-tools", [url, "--yes", "--client", "claude-code"], url=url, port=port,
             tools=[], setup=claude_registered()))
    port, url = _target()
    add(case("verify-bad-literal", [url, "--yes", "--client", "gemini"], url=url, port=port,
             setup=desktop_and_gemini(msix=False, legacy=False, gemini_env={
                 URL_KEY: OLD_URL, TOKEN_KEY: "two words"})))
    port, url = _target()
    add(case("verify-refused-read-token",
             [url, "--client", "claude-code", "--token-file", "~/.pseudolife-mcp/new.token",
              "--read-token", "--yes"], url=url, port=port, accept=(OTHER,),
             stdin=GOOD.encode() + b"\n", setup=claude_registered()))

    # Inputs the first candidate deferred on, and coverage the reviews asked for.
    port, url = _target()

    def bom_token(arm):
        path = arm.home / ".pseudolife-mcp" / "bom.token"
        _oracle_token_writer()(path, GOOD)
        # The oracle's writer refuses a BOM; rewrite the owner-only file's bytes.
        with open(path, "r+b") as stream:
            stream.write(("﻿" + GOOD).encode())
        claude_json(arm.home, entry("shim", claude_env(arm.home, token=path)))
    add(case("bom-token-dry-run", [url, "--client", "claude-code", "--dry-run"], url=url,
             port=port, setup=bom_token))
    port, url = _target()
    add(case("bom-token-refused", [url, "--client", "claude-code", "--yes"], url=url,
             port=port, setup=bom_token))
    port, url = _target()
    add(case("health-status-container", [url, "--dry-run"], url=url, port=port,
             health={"status": {"phase": "booting", "ok": False}, "auth": True},
             setup=claude_registered()))
    port, url = _target()
    add(case("health-version-container-nan", [url, "--dry-run", "--json"], url=url, port=port,
             health={"status": "ok", "auth": True, "version": [1, float("nan")]},
             setup=claude_registered()))
    port, url = _target()
    add(case("health-503", [url, "--dry-run"], url=url, port=port, health_status=503,
             health={"status": "stopping", "auth": True}, setup=claude_registered()))
    port, url = _target()
    add(case("health-redirect", [url, "--client", "claude-code", "--dry-run"], url=url,
             port=port, health_redirect=True, setup=claude_registered()))
    for reason in ("disabled", "authentication_required", "coordination_requires_postgres",
                   "something-new"):
        port, url = _target()
        add(case(f"board-{reason}", [url, "--client", "claude-code", "--yes"], url=url,
                 port=port, board=f"off; reason={reason}", setup=claude_registered()))
    port, url = _target()
    add(case("board-no-header-empty-body", [url, "--client", "claude-code", "--yes"], url=url,
             port=port, board=None, board_body=b" \x0b\n", setup=claude_registered()))
    port, url = _target()

    def literal(token):
        def setup(arm):
            write_json(arm.home / ".gemini" / "settings.json", {"mcpServers": {SERVER: entry(
                "shim", {URL_KEY: OLD_URL, TOKEN_KEY: token})}})
        return setup
    add(case("literal-trailing-newline", [url, "--client", "gemini", "--yes"], url=url,
             port=port, setup=literal(GOOD + "\n")))
    port, url = _target()
    add(case("literal-beyond-latin-1", [url, "--client", "gemini", "--yes"], url=url, port=port,
             setup=literal(GOOD + "€")))
    port, url = _target()
    add(case("literal-latin-1", [url, "--client", "gemini", "--yes"], url=url, port=port,
             setup=literal(GOOD + "é")))
    port, url = _target()

    def second_refused(arm):
        claude_registered()(arm)
        write_json(arm.home / ".gemini" / "settings.json", {"mcpServers": {SERVER: entry(
            "shim", {URL_KEY: OLD_URL, TOKEN_KEY: OTHER})}})
    add(case("verify-second-credential-refused", [url, "--yes"], url=url, port=port,
             setup=second_refused))
    port, url = _target()

    def respelled(arm):
        tf = token_file(arm.home)
        text = ('﻿{"numStartups": 1E2, "ratio": 1.50, "neg": -0, "tiny": 1e-7,\r\n'
                '"big": 123456789012345678901234567890, "odd": NaN, "inf": -Infinity,\r\n'
                '"mcpServers": {"x": 1},\r\n'
                '"mcpServers": {"pseudolife-memory": {"command": "shim", "env": {'
                f'"{URL_KEY}": "{OLD_URL}", "{FILE_KEY}": ' + json.dumps(str(tf)) + "}}},\r\n"
                '"escaped": "\\u00e9\\ud83d\\ude00\\/\\t"}\r\n')
        (arm.home / ".claude.json").write_bytes(text.encode("utf-8"))
    add(case("apply-json-respelling", [url, "--client", "claude-code", "--yes"], url=url,
             port=port, setup=respelled))
    port, url = _target()

    def two_packages(arm):
        tf = token_file(arm.home, "desktop.token")
        for package in ("Claude_bbb", "claude_AAA"):
            write_json(desktop_config(arm.home, package), {"mcpServers": {DESKTOP: entry(
                "shim", {URL_KEY: OLD_URL, FILE_KEY: str(tf)})}})
    add(case("two-msix-packages", [url, "--client", "claude-desktop", "--yes"], url=url,
             port=port, setup=two_packages, platforms=("windows",)))

    # After the writes.
    port, url = _target()
    add(case("post-apply-health", [url, "--yes", "--client", "claude-code"], url=url, port=port,
             health_after_board={"status": "stopping"}, setup=claude_registered()))
    port, url = _target()

    def hold_gemini(arm):
        claude_registered()(arm)
        desktop_and_gemini(msix=False, legacy=False)(arm)
        arm.state["held"] = open(arm.home / ".gemini" / "settings.json", "rb")  # noqa: SIM115

    def release_gemini(arm, obs):
        held = arm.state.pop("held", None)
        if held is not None:
            held.close()
    add(case("rollback-held-file", [url, "--yes"], url=url, port=port, setup=hold_gemini,
             after=release_gemini, platforms=("windows",)))
    port, url = _target()
    add(case("rollback-held-file-json", [url, "--yes", "--json"], url=url, port=port,
             setup=hold_gemini, after=release_gemini, platforms=("windows",)))
    port, url = _target()

    def read_only_gemini(arm):
        claude_registered()(arm)
        desktop_and_gemini(msix=False, legacy=False)(arm)
        os.chmod(arm.home / ".gemini", 0o555)

    def writable_gemini(arm, obs):
        os.chmod(arm.home / ".gemini", 0o755)
    add(case("rollback-read-only-dir", [url, "--yes"], url=url, port=port,
             setup=read_only_gemini, after=writable_gemini, platforms=("linux",)))
    return out


# ── mutant control ──────────────────────────────────────────────────────────

MUTANTS = [
    Mutant("connect-exit-nothing-4", "connect", "shim/src/cli/connect/mod.rs",
           "const EXIT_NOTHING: u8 = 3;", "const EXIT_NOTHING: u8 = 4;",
           ("none-found", "manual-only")),
    Mutant("connect-skip-settings-write", "connect", "shim/src/cli/connect/mod.rs",
           'if row.state != "change" || row.json.is_none() {',
           'if row.state != "change" || row.json.is_none() || row.place == "settings" {',
           ("apply-move-yes",)),
    Mutant("connect-changes-reverse-sort", "connect", "shim/src/cli/connect/discover.rs",
           "sorted.sort_by(|a, b| a.0.cmp(&b.0));", "sorted.sort_by(|a, b| b.0.cmp(&a.0));",
           ("dry-run-docs", "dry-run-installer-json")),
    Mutant("connect-message-token", "connect", "shim/src/cli/connect/mod.rs",
           "already names {url}; nothing to write", "already names {url}; nothing to change",
           ("nothing-to-write",)),
    Mutant("connect-no-credential-check", "connect", "shim/src/cli/connect/mod.rs",
           "&& !net::credential_valid(&ctx.url, token).await", "&& false",
           ("verify-refused",)),
    Mutant("connect-no-backup", "connect", "shim/src/cli/connect/mod.rs",
           "backup = Some(files::backup(&path)?);", "backup = None;",
           ("apply-move-yes",)),
    Mutant("connect-rollback-keeps-writes", "connect", "shim/src/cli/connect/mod.rs",
           "let outcome = rollback(&steps, &created);", "let outcome = rollback(&[], &created);",
           ("rollback-held-file", "rollback-read-only-dir")),
    Mutant("connect-cache-url-last", "connect", "shim/src/cache.rs",
           'let mut data = Map::from_iter([("url".to_owned(), Value::String(self.url.clone()))]);\n'
           "            data.extend(cached);",
           "let mut data = cached;\n"
           '            data.insert("url".to_owned(), Value::String(self.url.clone()));',
           ("apply-move-yes",)),
    Mutant("connect-json-indent-4", "connect", "shim/src/cli/connect/pyjson.rs",
           "out.extend(std::iter::repeat_n(' ', level * 2));",
           "out.extend(std::iter::repeat_n(' ', level * 4));",
           ("apply-move-yes",)),
    # Review of #678 (2026-10-10): Rust's shortest digits on an exact tie.
    Mutant("connect-stderr-warning-ignored", "connect", "shim/src/cli/connect/mod.rs",
           "if !self.json && !emit(&mut std::io::stderr().lock(), &line) {",
           "if !self.json && !emit(&mut std::io::stderr().lock(), &line) && false {",
           ("apply-remote-stderr-closed", "apply-board-off-stderr-closed")),
    # Review of #678 at 6447829f: a refused stdout must change the status.
    Mutant("connect-stdout-refusal-ignored", "connect", "shim/src/cli/connect/mod.rs",
           "        if self.stdout_refused.get() {\n            EXIT_STDOUT_REFUSED\n",
           "        if false && self.stdout_refused.get() {\n            EXIT_STDOUT_REFUSED\n",
           ("apply-move-yes-stdout-closed", "apply-move-json-stdout-closed")),
    Mutant("connect-float-ties", "connect", "shim/src/cli/connect/pyjson.rs",
           "    let (digits, exponent) = shortest_digits(value.abs());\n"
           "    let significant = digits.as_str();\n",
           "    let (digits, exponent) = scientific(&format!(\"{:e}\", value.abs()));\n"
           "    let significant = digits.trim_end_matches('0');\n",
           ("apply-float-ties",)),
    Mutant("connect-backup-stamp-millis", "connect", "shim/src/cli/connect/files.rs",
           '"%Y%m%d-%H%M%S-%6f"', '"%Y%m%d-%H%M%S-%3f"', ("apply-move-yes",)),
    Mutant("connect-config-not-private", "connect", "shim/src/cli/connect/files.rs",
           "        make_private(&file)?;\n", "        let _ = &file;\n",
           ("apply-move-yes", "rollback-read-only-dir")),
    Mutant("connect-verify-reverse-order", "connect", "shim/src/cli/connect/mod.rs",
           "for (credential, label) in credentials {",
           "for (credential, label) in credentials.into_iter().rev() {",
           ("verify-second-credential-refused",)),
    Mutant("connect-header-utf8", "connect", "shim/src/cli/connect/net.rs",
           "value.push(u8::try_from(u32::from(c)).ok()?);",
           "value.extend(c.to_string().as_bytes());",
           ("literal-beyond-latin-1",)),
    Mutant("connect-remote-keeps-spawn", "connect", "shim/src/cli/connect/discover.rs",
           'if !matches!(current.as_str(), "1" | "true" | "yes" | "on") {',
           'if !matches!(current.as_str(), "1" | "true" | "yes" | "on" | "") {',
           ("dry-run-remote",)),
    # Same-outcome controls (review of #670): each request is still refused
    # or answered exactly as before, so only the raw request fields show it.
    # A Latin-1 credential sent as UTF-8: still rejected with the same 401,
    # refusal and files, but a different credential on the wire.
    Mutant("connect-latin-1-credential-as-utf8", "connect", "shim/src/cli/connect/net.rs",
           "    for c in token.chars() {\n"
           "        value.push(u8::try_from(u32::from(c)).ok()?);\n"
           "    }\n",
           "    for c in token.chars() {\n"
           "        let byte = u8::try_from(u32::from(c)).ok()?;\n"
           "        if byte < 0x80 {\n"
           "            value.push(byte);\n"
           "        } else {\n"
           "            value.extend(c.to_string().as_bytes());\n"
           "        }\n"
           "    }\n",
           ("literal-latin-1",)),
    # A changed request field the daemon ignores.
    Mutant("connect-user-agent-changed", "connect", "shim/src/cli/hook_http.rs",
           '.header("User-Agent", "Python-urllib/3.11");',
           '.header("User-Agent", "Python-urllib/3.12");', ("health-degraded",)),
]

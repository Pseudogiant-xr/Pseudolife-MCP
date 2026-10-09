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
process holding each connection) in order with the credential each carried,
and of its handshake child, whose transport is the shim's own row, the
credentials its MCP posts carried. Named rules:
``connect-backup-stamp`` (the UTC stamp in a backup's name, validated
against the arm's own clock window) and ``connect-restart-pid`` (the pid of
the process a case starts inside the runtimes root). The handshake shim's
cache file is compared byte for byte.

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

GOOD = "good-" + "g" * 32
OTHER = "other-" + "o" * 32
_LABELS = {GOOD: "good", OTHER: "other"}
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
                 instructions="Fixture daemon instructions."):
        self.url = url
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

            def _label(self) -> str:
                if not self.headers.get("Authorization"):
                    return "none"
                return _LABELS.get(self._token(), "unknown")

            def _allowed(self) -> bool:
                return not daemon.auth or self._token() in daemon.accept

            def _record(self) -> None:
                owner = daemon._owner(self.client_address[1])
                with daemon._lock:
                    daemon._seen.append((owner, {"method": self.command, "path": self.path,
                                                 "auth": self._label()}))

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
                if self.path == "/health":
                    health = daemon.health
                    if daemon.health_after_board is not None and daemon.board_seen:
                        health = daemon.health_after_board
                    self._json(200, health)
                elif self.path.startswith("/api/episodes"):
                    if self._allowed():
                        self._json(200, {"episodes": []})
                    else:
                        self._json(401, {"error": "unauthorized"})
                elif self.path.startswith("/api/hook/coordination-start"):
                    daemon.board_seen = True
                    body = b"check-in" if daemon.board == "on" else b""
                    self._send(200, body, (("X-PL-Board", daemon.board),))
                elif self.path.startswith("/mcp"):
                    self._send(405, b"", (("Allow", "POST, DELETE"),))
                else:
                    self._json(404, {"error": "not_found"})

            def do_DELETE(self):
                self._record()
                self._send(200 if self.path.startswith("/mcp") else 404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._record()
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

    def requests(self) -> dict:
        """Connect's own requests in order; of the handshake child (the shim,
        whose transport is its own row) only the credentials its MCP
        requests carried."""
        with self._lock:
            seen = list(self._seen)
        own = [entry for owner, entry in seen if owner == self.arm_pid or owner is None]
        child = sorted({entry["auth"] for owner, entry in seen
                        if owner not in (self.arm_pid, None) and entry["path"].startswith("/mcp")
                        and entry["method"] == "POST"})
        return {"connect": own, "handshake_credentials": child}

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
    """The UTC stamp in ``<file>.bak-pseudolife-<stamp>``: in streams and in
    file names, only where it names a moment inside the arm's own run."""
    window = obs["window"]

    def swap(match: re.Match) -> bytes:
        if _stamp_in_window(match.group(1), window):
            return b".bak-pseudolife-<stamp>"
        return match.group(0)

    for field in ("stdout", "stderr"):
        normalize._put(obs, field, _STAMP.sub(swap, normalize._get(obs, field)))
    renamed = {}
    for rel, value in obs["files"].items():
        renamed[_STAMP.sub(swap, rel.encode()).decode()] = value
    obs["files"] = renamed


@normalize.rule("connect-restart-pid")
def restart_pid(obs: dict) -> None:
    """The pid of the process this case started inside the runtimes root."""
    pid = obs.get("restart_pid")
    if not pid:
        return
    pattern = re.compile(rb"(?<![0-9])" + str(pid).encode() + rb"(?![0-9])")
    for field in ("stdout",):
        normalize._put(obs, field, pattern.sub(b"<pid>", normalize._get(obs, field)))


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


def _after(url: str, extra=None):
    def after(arm, obs):
        try:
            if extra:
                extra(arm, obs)
        finally:
            obs["fixture_url"] = url
            _check_reported_paths(arm, obs)
            _check_real_configuration(arm, url)
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


RULES = ("connect-backup-stamp", "connect-restart-pid")


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
         **daemon) -> Case:
    return Case(case_id, ["connect", *argv], env=_env(**(env or {})), stdin=stdin,
                setup=_with_temp(setup), during=_arm_pid,
                after=_after(url, after), timeout=timeout, rules=RULES, platforms=platforms,
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
    add(case("apply-move-json", [url, "--yes", "--json"], url=url, port=port,
             setup=desktop_and_gemini()))
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
    add(case("apply-restart-pids", [url, "--yes", "--client", "claude-code"], url=url, port=port,
             setup=runtime_process, after=stop_runtime_process))

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
    Mutant("connect-remote-keeps-spawn", "connect", "shim/src/cli/connect/discover.rs",
           'if !matches!(current.as_str(), "1" | "true" | "yes" | "on") {',
           'if !matches!(current.as_str(), "1" | "true" | "yes" | "on" | "") {',
           ("dry-run-remote",)),
]

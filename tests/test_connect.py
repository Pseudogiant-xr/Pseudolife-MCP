"""``pseudolife-mcp connect``: re-point every client on a machine at a daemon.

Every case runs against a fake home (HOME / USERPROFILE / CODEX_HOME /
APPDATA / LOCALAPPDATA / XDG_CONFIG_HOME under ``tmp_path``) and an
in-process HTTP stub for the daemon, never a real client config or daemon.
Codex's app-server is faked at its JSON-RPC surface (``config/read`` and
``config/batchWrite`` with ``expectedVersion``), writing a real
``config.toml`` so discovery reads what the writer wrote. The MCP handshake
(a shim subprocess speaking MCP) and the process table are seams.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import io
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

from pseudolife_memory import client_config, codex_connection, connect_cli, runtimes
from pseudolife_memory.credentials import CredentialProvider, _write_token_file

ROOT = Path(__file__).resolve().parents[1]
GOOD = "good-" + "g" * 32
OTHER = "other-" + "o" * 32
OLD_URL = "http://127.0.0.1:1"
URL_KEY = "PSEUDOLIFE_MCP_DAEMON_URL"
FILE_KEY = "PSEUDOLIFE_MCP_TOKEN_FILE"
TOKEN_KEY = "PSEUDOLIFE_MCP_TOKEN"
NO_SPAWN = "PSEUDOLIFE_MCP_NO_SPAWN"

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = pytest.importorskip("tomli")


# -- the stub daemon ------------------------------------------------------------

class Daemon:
    """``/health``, a bearer-checked ``/api/episodes`` and the board check-in;
    every other GET answers 200. ``requests`` records (path, bearer sent)."""

    def __init__(self, *, auth=True, tokens=(GOOD,), board="on"):
        self.health = {"status": "ok", "auth": auth, "version": "0.0.0-fixture"}
        self.tokens = set(tokens)
        self.board = board
        self.requests: list[tuple[str, str]] = []
        # POST /api/pair: each body, the status to answer, and the hashes it
        # paired (a bearer whose SHA-256 is one of them authenticates).
        self.pairs: list[bytes] = []
        self.pair_status = 200
        self.principal = "laptop"
        self.hashes: set[str] = set()
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                daemon.requests.append((self.path, bearer))
                allowed = not daemon.health["auth"] or any(
                    hmac.compare_digest(bearer, token) for token in daemon.tokens) or (
                    hashlib.sha256(bearer.encode()).hexdigest() in daemon.hashes)
                if self.path == "/health":
                    body, status = json.dumps(daemon.health).encode(), 200
                elif self.path.startswith("/api/episodes"):
                    body, status = b'{"episodes": []}', 200 if allowed else 401
                elif self.path.startswith("/api/hook/coordination-start"):
                    self.send_response(200)
                    self.send_header("X-PL-Board", daemon.board)
                    self.end_headers()
                    self.wfile.write(b"check-in" if daemon.board == "on" else b"")
                    return
                else:
                    body, status = b"fixture", 200 if allowed else 401
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                daemon.requests.append((self.path, ""))
                if self.path != "/api/pair":
                    self.send_response(404)
                    self.end_headers()
                    return
                daemon.pairs.append(body)
                if daemon.pair_status == 200:
                    daemon.hashes.add(json.loads(body)["token_sha256"])
                    answer = {"principal": daemon.principal, "tier": None, "bank": None}
                else:
                    answer = {"error": "pairing_refused"}
                data = json.dumps(answer).encode()
                self.send_response(daemon.pair_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def bearers(self):
        return [bearer for _path, bearer in self.requests if bearer]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def daemon():
    stub = Daemon()
    yield stub
    stub.close()


# -- the fake machine -----------------------------------------------------------

def dump_toml(data: dict, prefix: tuple = ()) -> str:
    """A minimal TOML writer for the fake app-server: tables of strings,
    numbers, booleans and string lists (JSON literals are valid TOML there)."""
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    out = []
    if prefix and (scalars or not tables):
        out.append("[" + ".".join(json.dumps(part) for part in prefix) + "]")
    out += [f"{json.dumps(key)} = {json.dumps(value)}" for key, value in scalars.items()]
    text = "\n".join(out) + ("\n" if out else "")
    for key, value in tables.items():
        text += ("\n" if text else "") + dump_toml(value, prefix + (key,))
    return text


class FakeCodex:
    """Codex's app-server at its JSON-RPC surface, over a real config.toml.
    ``project`` is an effective-only overlay (another configuration layer)."""

    def __init__(self, home: Path, project: dict | None = None):
        self.home = home
        self.path = home / "config.toml"
        self.project = project or {}
        self.calls: list[str] = []

    def _data(self):
        return tomllib.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def version(self):
        raw = self.path.read_bytes() if self.path.exists() else b""
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    def rpc(self, method, params, timeout=20):
        self.calls.append(method)
        if method == "config/read":
            user = self._data()
            effective = json.loads(json.dumps(user))
            server = effective.get("mcp_servers", {}).get("pseudolife-memory")
            if server is not None and self.project:
                server.setdefault("env", {}).update(self.project)
            return {"config": effective,
                    "layers": [{"name": {"type": "user", "file": str(self.path)},
                                "version": self.version(), "config": user}]}
        if method == "config/batchWrite":
            if params["expectedVersion"] != self.version():
                raise codex_connection.SetupError("Codex rejected config/batchWrite")
            data = self._data()
            for edit in params["edits"]:
                assert edit["keyPath"] == codex_connection.dotted("mcp_servers", "pseudolife-memory", "env")
                data["mcp_servers"]["pseudolife-memory"]["env"] = edit["value"]
            self.path.write_text(dump_toml(data), encoding="utf-8")
            return {}
        raise AssertionError(method)


class Machine:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.work = tmp_path / "work"
        for path in (self.home, self.work):
            path.mkdir(parents=True)
        self.codex_home = self.home / ".codex"
        for key in ("CLAUDE_CONFIG_DIR", URL_KEY, FILE_KEY, TOKEN_KEY, "PSEUDOLIFE_MCP_TOKENS",
                    "PSEUDOLIFE_SHIM_RUNTIMES", "PSEUDOLIFE_SHIM_LAUNCHER", "XDG_DATA_HOME"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("USERPROFILE", str(self.home))
        monkeypatch.setenv("CODEX_HOME", str(self.codex_home))
        monkeypatch.setenv("APPDATA", str(self.home / "AppData" / "Roaming"))
        monkeypatch.setenv("LOCALAPPDATA", str(self.home / "AppData" / "Local"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(self.home / ".config"))
        monkeypatch.chdir(self.work)
        self.codex = FakeCodex(self.codex_home.resolve())
        self.processes: list[tuple[int, int, str]] = []
        self.handshakes: list[tuple[str, tuple]] = []
        self.handshake_ok = True
        self.schedule = None
        self.user_url = None
        self.launcher = str(runtimes.default_layout(dict(os.environ)).launcher)

        @contextlib.contextmanager
        def open_codex(home):
            assert Path(home).resolve() == self.codex_home.resolve()
            yield self.codex, self.work

        def handshake(url, credential, timeout=20.0):
            self.handshakes.append((url, credential))
            return ({"ok": True, "tool_count": 30} if self.handshake_ok
                    else {"ok": False, "error": "handshake failed"})

        monkeypatch.setattr(connect_cli, "open_codex", open_codex)
        monkeypatch.setattr(connect_cli, "handshake", handshake)
        monkeypatch.setattr(connect_cli, "list_processes", lambda: self.processes)
        monkeypatch.setattr(connect_cli, "scheduled_update", lambda: self.schedule)
        monkeypatch.setattr(connect_cli, "user_environment_url", lambda: self.user_url)
        monkeypatch.setattr(connect_cli, "interactive", lambda: False)

    # paths
    @property
    def claude_json(self):
        return self.home / ".claude.json"

    @property
    def settings(self):
        return self.home / ".claude" / "settings.json"

    @property
    def gemini(self):
        return self.home / ".gemini" / "settings.json"

    @property
    def desktop(self):
        return runtimes.desktop_config_files(dict(os.environ))[-1]

    @property
    def connection(self):
        return self.codex_home / "pseudolife" / "connection.json"

    def token(self, name="claude-code.token", value=GOOD) -> str:
        path = self.home / ".pseudolife-mcp" / name
        _write_token_file(path, value)
        return str(path)

    def write_json(self, path: Path, data: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def entry(self, env, **extra):
        return {"type": "stdio", "command": self.launcher, "args": [], "env": dict(env), **extra}

    def register_claude(self, env, **extra):
        self.write_json(self.claude_json, {"numStartups": 3,
                                           "mcpServers": {"pseudolife-memory": self.entry(env, **extra),
                                                          "other": {"command": "x"}}})

    def register_codex(self, env):
        self.codex_home.mkdir(parents=True, exist_ok=True)
        self.codex.path.write_text(dump_toml({
            "model": "fixture",
            "mcp_servers": {"pseudolife-memory": {"command": self.launcher, "args": [],
                                                  "startup_timeout_sec": 240.0, "env": dict(env)}}}),
            encoding="utf-8")

    def register_desktop(self, env, key="pseudolife-desktop", path=None):
        self.write_json(path or self.desktop, {"mcpServers": {key: {"command": self.launcher, "args": [],
                                                                   "env": dict(env)}},
                                               "preferences": {"x": 1}})

    def register_gemini(self, env):
        self.write_json(self.gemini, {"mcpServers": {"pseudolife-memory": self.entry(env)}, "theme": "t"})

    def snapshot(self) -> dict:
        found = {}
        for path in sorted(self.home.rglob("*")):
            if path.is_file():
                found[str(path)] = path.read_bytes()
        return found


@pytest.fixture
def machine(tmp_path, monkeypatch):
    return Machine(tmp_path, monkeypatch)


def assert_restored(before: dict, after: dict):
    """Every file that existed is byte-identical; anything new is a backup."""
    for path, data in before.items():
        assert after.get(path) == data, path
    assert all(".bak-" in Path(path).name for path in set(after) - set(before)), sorted(set(after) - set(before))


def run(capsys, *argv):
    code = connect_cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out + out.err


def run_json(capsys, *argv):
    code = connect_cli.main([*argv, "--json"])
    out = capsys.readouterr()
    return code, json.loads(out.out), out.out + out.err


def rows(report, **match):
    return [row for row in report["rows"] if all(row.get(k) == v for k, v in match.items())]


def one(report, **match):
    found = rows(report, **match)
    assert len(found) == 1, (match, report["rows"])
    return found[0]


def full_machine(machine, token_file, url=OLD_URL):
    env = {"PSEUDOLIFE_WRITER_ID": "claude-code", NO_SPAWN: "1", URL_KEY: url, FILE_KEY: token_file,
           "PSEUDOLIFE_AGENT_STATE_DIR": str(machine.home / "agents"), "USER_ADDED": "keep"}
    machine.register_claude(env)
    machine.write_json(machine.settings, {"env": {URL_KEY: url, FILE_KEY: token_file, "OTHER": "keep"},
                                          "theme": "dark"})
    machine.register_codex({"PSEUDOLIFE_WRITER_ID": "codex", NO_SPAWN: "1", URL_KEY: url,
                            FILE_KEY: token_file, "CODEX_ONLY": "keep"})
    machine.register_desktop({"PSEUDOLIFE_WRITER_ID": "claude-desktop", NO_SPAWN: "1", URL_KEY: url,
                              FILE_KEY: token_file, "PROXY": "keep"})
    machine.register_gemini({"PSEUDOLIFE_WRITER_ID": "gemini", URL_KEY: url, FILE_KEY: token_file})


# -- plan and apply ----------------------------------------------------------------

def test_every_client_moves_and_only_managed_keys_change(machine, daemon, capsys):
    token = machine.token()
    full_machine(machine, token)
    before_claude = json.loads(machine.claude_json.read_text())
    code, report, out = run_json(capsys, daemon.url, "--yes")
    assert code == 0, out
    for client in ("claude-code", "codex", "claude-desktop", "gemini"):
        assert one(report, client=client, place="registration")["state"] == "change"
    assert one(report, client="claude-code", place="settings")["state"] == "change"

    claude = json.loads(machine.claude_json.read_text())
    expected = json.loads(json.dumps(before_claude))
    expected["mcpServers"]["pseudolife-memory"]["env"][URL_KEY] = daemon.url
    assert claude == expected  # command, args, writer id, state dir, other servers: untouched
    settings = json.loads(machine.settings.read_text())
    assert settings == {"env": {URL_KEY: daemon.url, FILE_KEY: token, "OTHER": "keep"}, "theme": "dark"}
    desktop = json.loads(machine.desktop.read_text())
    assert desktop["mcpServers"]["pseudolife-desktop"] == {
        "command": machine.launcher, "args": [],
        "env": {"PSEUDOLIFE_WRITER_ID": "claude-desktop", NO_SPAWN: "1", URL_KEY: daemon.url,
                FILE_KEY: token, "PROXY": "keep"}}
    assert desktop["preferences"] == {"x": 1}
    gemini = json.loads(machine.gemini.read_text())
    assert gemini["mcpServers"]["pseudolife-memory"]["env"] == {
        "PSEUDOLIFE_WRITER_ID": "gemini", URL_KEY: daemon.url, FILE_KEY: token}
    codex = tomllib.loads(machine.codex.path.read_text())
    assert codex["model"] == "fixture"
    server = codex["mcp_servers"]["pseudolife-memory"]
    assert server["command"] == machine.launcher and server["startup_timeout_sec"] == 240.0
    assert server["env"] == {"PSEUDOLIFE_WRITER_ID": "codex", NO_SPAWN: "1", URL_KEY: daemon.url,
                             FILE_KEY: token, "CODEX_ONLY": "keep"}
    assert codex_connection.read_connection(machine.connection) == (daemon.url, token)
    # Every file written was backed up first.
    changed = [row for row in report["rows"] if row["state"] == "change"]
    assert all(row["backup"] or row["created"] for row in changed)
    assert all(Path(row["backup"]).is_file() for row in changed if row["backup"])
    assert one(report, client="codex", place="connection")["created"] is True
    assert GOOD not in out


def test_a_second_run_is_all_current_and_writes_nothing(machine, daemon, capsys):
    full_machine(machine, machine.token())
    assert run(capsys, daemon.url, "--yes")[0] == 0
    before = machine.snapshot()
    code, report, _ = run_json(capsys, daemon.url, "--yes")
    assert code == 0
    states = {row["state"] for row in report["rows"] if row["place"] in ("registration", "settings",
                                                                          "connection")}
    assert states == {"current"}
    assert machine.snapshot() == before
    assert "config/batchWrite" not in machine.codex.calls[-3:]


def test_absent_clients_are_reported_with_the_command_that_registers_them(machine, daemon, capsys):
    machine.register_claude({URL_KEY: OLD_URL, FILE_KEY: machine.token()})
    code, report, _ = run_json(capsys, daemon.url, "--yes")
    assert code == 0
    for client, verb in (("codex", "codex mcp add"), ("claude-desktop", "register_claude_desktop"),
                         ("gemini", "gemini mcp add")):
        row = one(report, client=client)
        assert row["state"] == "absent" and verb in row["detail"]
    assert not machine.codex.path.exists() and not machine.desktop.exists()
    assert not machine.gemini.exists() and not machine.connection.exists()


def test_no_registration_at_all_exits_3(machine, daemon, capsys):
    before = machine.snapshot()
    code, out = run(capsys, daemon.url, "--yes")
    assert code == 3, out
    assert machine.snapshot() == before


def test_client_narrows_the_set(machine, daemon, capsys):
    full_machine(machine, machine.token())
    before_desktop = machine.desktop.read_bytes()
    code, report, _ = run_json(capsys, daemon.url, "--yes", "--client", "claude-code,gemini")
    assert code == 0
    assert {row["client"] for row in report["rows"]} <= {"claude-code", "gemini", "environment",
                                                         "unattended-update"}
    assert machine.desktop.read_bytes() == before_desktop
    assert json.loads(machine.gemini.read_text())["mcpServers"]["pseudolife-memory"]["env"][URL_KEY] == daemon.url


def test_unknown_client_is_a_usage_error(machine, daemon, capsys):
    assert run(capsys, daemon.url, "--client", "desktop")[0] == 2


@pytest.mark.parametrize("url", ["ftp://example.com", "http://example.com/mcp", "http://u:p@example.com",
                                 "not a url"])
def test_an_invalid_url_is_a_usage_error_with_its_own_message(machine, capsys, url):
    code, out = run(capsys, url)
    assert code == 2
    assert "connect:" in out and "origin" in out


# -- manual rows -----------------------------------------------------------------------

def test_registrations_it_cannot_write_are_manual_and_untouched(machine, daemon, capsys):
    token = machine.token()
    machine.write_json(machine.claude_json, {
        "mcpServers": {"pseudolife-memory": {"type": "http", "url": "http://127.0.0.1:8765/mcp"}},
        "projects": {str(machine.work): {"mcpServers": {"pseudolife-memory": machine.entry({URL_KEY: OLD_URL})}}}})
    machine.write_json(machine.work / ".mcp.json", {"mcpServers": {"pseudolife-memory": machine.entry({})}})
    machine.register_desktop({"PSEUDOLIFE_WRITER_ID": "claude-desktop", URL_KEY: OLD_URL},
                             key="pseudolife-memory")
    machine.register_gemini({URL_KEY: OLD_URL, FILE_KEY: token})
    before = machine.snapshot()
    before_mcp = (machine.work / ".mcp.json").read_bytes()
    code, report, _ = run_json(capsys, daemon.url, "--yes")
    assert code == 0
    manual = rows(report, state="manual")
    places = {(row["client"], row["place"]) for row in manual}
    assert ("claude-code", "registration") in places   # type: http
    assert ("claude-code", "project") in places        # projects[...] and .mcp.json
    assert len(rows(report, client="claude-code", place="project")) == 2
    assert ("claude-desktop", "registration") in places  # the legacy name: the registrar migrates it
    assert one(report, client="gemini", place="registration")["state"] == "change"
    after = machine.snapshot()
    for path in (machine.claude_json, machine.desktop):
        assert after[str(path)] == before[str(path)]
    assert (machine.work / ".mcp.json").read_bytes() == before_mcp


def test_codex_without_its_app_server_is_manual(machine, daemon, capsys, monkeypatch):
    machine.register_claude({URL_KEY: OLD_URL, FILE_KEY: machine.token()})
    machine.register_codex({URL_KEY: OLD_URL})
    before = machine.codex.path.read_bytes()

    def missing(home):
        raise codex_connection.SetupError("Codex CLI is unavailable; install or update Codex, then rerun setup.")

    monkeypatch.setattr(connect_cli, "open_codex", missing)
    code, report, _ = run_json(capsys, daemon.url, "--yes")
    assert code == 0
    row = one(report, client="codex", place="registration")
    assert row["state"] == "manual" and "Codex CLI is unavailable" in row["detail"]
    assert machine.codex.path.read_bytes() == before and not machine.connection.exists()


def test_codex_settings_from_another_layer_are_manual(machine, daemon, capsys):
    machine.register_claude({URL_KEY: OLD_URL, FILE_KEY: machine.token()})
    machine.register_codex({URL_KEY: OLD_URL})
    machine.codex.project = {URL_KEY: "http://127.0.0.1:2"}
    before = machine.codex.path.read_bytes()
    code, report, _ = run_json(capsys, daemon.url, "--yes")
    assert code == 0
    row = one(report, client="codex", place="registration")
    assert row["state"] == "manual" and "another Codex configuration layer" in row["detail"]
    assert machine.codex.path.read_bytes() == before


def test_an_ambient_daemon_url_and_a_fixed_agent_state_are_reported(machine, daemon, capsys, monkeypatch):
    monkeypatch.setenv(URL_KEY, OLD_URL)
    machine.user_url = "http://127.0.0.1:3"
    machine.register_claude({URL_KEY: OLD_URL, FILE_KEY: machine.token(),
                             "PSEUDOLIFE_AGENT_STATE": str(machine.home / "state.json")})
    code, report, _ = run_json(capsys, daemon.url, "--yes")
    assert code == 0
    environment = rows(report, client="environment")
    assert {row["state"] for row in environment} == {"manual"} and len(environment) == 2
    assert any(OLD_URL in row["detail"] for row in environment)
    registration = one(report, client="claude-code", place="registration")
    assert any("PSEUDOLIFE_AGENT_STATE" in note for note in registration["notes"])


def test_a_scheduled_unattended_update_is_reported_with_the_reschedule_command(machine, daemon, capsys):
    machine.register_claude({URL_KEY: OLD_URL, FILE_KEY: machine.token()})
    machine.schedule = {"where": "fixture task", "time": "03:30", "url": OLD_URL}
    code, report, _ = run_json(capsys, daemon.url, "--yes")
    assert code == 0
    row = one(report, client="unattended-update")
    assert row["state"] == "manual"
    assert f"pseudolife-mcp update --schedule 03:30 --daemon-url {daemon.url}" in row["detail"]
    machine.schedule = {"where": "fixture task", "time": "03:30", "url": daemon.url}
    code, report, _ = run_json(capsys, daemon.url, "--yes")
    assert one(report, client="unattended-update")["state"] == "current"


# -- credentials ---------------------------------------------------------------------------

def test_a_given_token_file_replaces_a_literal_token(machine, daemon, capsys):
    machine.register_claude({URL_KEY: OLD_URL, TOKEN_KEY: GOOD, "PSEUDOLIFE_WRITER_ID": "claude-code"})
    token = machine.token()
    code, report, out = run_json(capsys, daemon.url, "--yes", "--token-file", token)
    assert code == 0, out
    env = json.loads(machine.claude_json.read_text())["mcpServers"]["pseudolife-memory"]["env"]
    assert env == {URL_KEY: daemon.url, FILE_KEY: token, "PSEUDOLIFE_WRITER_ID": "claude-code"}
    assert GOOD not in out


def test_a_literal_token_is_kept_with_a_warning_without_a_file(machine, daemon, capsys):
    machine.register_claude({URL_KEY: OLD_URL, TOKEN_KEY: GOOD})
    code, report, out = run_json(capsys, daemon.url, "--yes")
    assert code == 0, out
    env = json.loads(machine.claude_json.read_text())["mcpServers"]["pseudolife-memory"]["env"]
    assert env == {URL_KEY: daemon.url, TOKEN_KEY: GOOD}
    row = one(report, client="claude-code", place="registration")
    assert any("literal" in note for note in row["notes"])
    assert GOOD in daemon.bearers()  # the literal was verified against the target
    assert GOOD not in out


def test_no_spawn_is_forced_for_a_remote_target_and_kept_for_loopback(machine, daemon, capsys, monkeypatch):
    token = machine.token()
    machine.register_claude({URL_KEY: OLD_URL, FILE_KEY: token})
    assert run(capsys, daemon.url, "--yes")[0] == 0
    env = json.loads(machine.claude_json.read_text())["mcpServers"]["pseudolife-memory"]["env"]
    assert NO_SPAWN not in env  # loopback: the install's choice stands

    remote = "http://100.64.0.2:8765"
    real_probe, real_valid, real_board = connect_cli.probe_health, connect_cli.credential_valid, connect_cli.board_line
    monkeypatch.setattr(connect_cli, "probe_health",
                        lambda url, timeout: real_probe(daemon.url if url == remote else url, timeout))
    monkeypatch.setattr(connect_cli, "credential_valid",
                        lambda url, tok: real_valid(daemon.url if url == remote else url, tok))
    monkeypatch.setattr(connect_cli, "board_line",
                        lambda url, tok: real_board(daemon.url if url == remote else url, tok))
    code, out = run(capsys, remote, "--yes")
    assert code == 0, out
    env = json.loads(machine.claude_json.read_text())["mcpServers"]["pseudolife-memory"]["env"]
    assert env == {URL_KEY: remote, FILE_KEY: token, NO_SPAWN: "1"}
    assert "plain HTTP" in out


# -- verify before writing --------------------------------------------------------------------

def _refused(machine, capsys, *argv):
    before = machine.snapshot()
    code, out = run(capsys, *argv)
    assert code == 4, out
    assert machine.snapshot() == before
    return out


def test_an_unreachable_daemon_is_refused_before_any_write(machine, capsys):
    full_machine(machine, machine.token())
    out = _refused(machine, capsys, "http://127.0.0.1:9", "--yes")
    assert "did not answer" in out


def test_a_remote_daemon_without_auth_is_refused(machine, capsys, monkeypatch):
    full_machine(machine, machine.token())
    monkeypatch.setattr(connect_cli, "probe_health",
                        lambda url, timeout: {"status": "ok", "auth": False})
    out = _refused(machine, capsys, "http://100.64.0.2:8765", "--yes")
    assert "auth: false" in out


def test_a_rejected_token_is_refused_before_any_write(machine, capsys):
    stub = Daemon(tokens=(OTHER,))
    try:
        full_machine(machine, machine.token())
        out = _refused(machine, capsys, stub.url, "--yes")
        assert "refused" in out and GOOD not in out
    finally:
        stub.close()


def test_a_failed_handshake_is_refused_before_any_write(machine, daemon, capsys):
    full_machine(machine, machine.token())
    machine.handshake_ok = False
    out = _refused(machine, capsys, daemon.url, "--yes")
    assert "handshake" in out


def test_a_registration_without_a_credential_is_refused_by_a_token_gated_daemon(machine, daemon, capsys):
    machine.register_claude({URL_KEY: OLD_URL})
    out = _refused(machine, capsys, daemon.url, "--yes")
    assert "--token-file" in out


def test_a_token_file_with_a_byte_order_mark_is_refused(machine, daemon, capsys):
    path = Path(machine.token())
    path.write_bytes(b"\xef\xbb\xbf" + GOOD.encode())
    machine.register_claude({URL_KEY: OLD_URL, FILE_KEY: str(path)})
    out = _refused(machine, capsys, daemon.url, "--yes")
    assert "byte-order mark" in out


def test_a_token_file_others_can_read_is_refused(machine, daemon, capsys):
    path = Path(machine.token())
    if os.name == "nt":
        subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:R"], check=True, capture_output=True)
    else:
        path.chmod(0o644)
    machine.register_claude({URL_KEY: OLD_URL, FILE_KEY: str(path)})
    _refused(machine, capsys, daemon.url, "--yes")


def test_the_handshake_gets_the_new_url_and_file_and_the_board_check_in_off(machine, daemon, capsys,
                                                                            monkeypatch):
    token = machine.token()
    machine.register_claude({URL_KEY: OLD_URL, FILE_KEY: token})
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, json.dumps(
            {"instructions_present": True, "tool_count": 30, "tools_missing_annotations": []}), "")

    monkeypatch.setattr(connect_cli.subprocess, "run", fake_run)
    monkeypatch.setenv(TOKEN_KEY, "ambient-" + "a" * 20)
    result = connect_cli.subprocess_handshake(daemon.url, ("file", token))
    assert result["ok"] is True
    assert seen[URL_KEY] == daemon.url and seen[FILE_KEY] == token
    assert TOKEN_KEY not in seen
    assert seen["PSEUDOLIFE_AGENT_COORDINATION"] == "0" and seen[NO_SPAWN] == "1"


def test_the_handshake_ignores_a_pseudolife_memory_package_in_the_working_directory(
        tmp_path, monkeypatch):
    # `python -c` and `-m` put the working directory first on sys.path, so
    # a connect run from a source checkout would otherwise validate the
    # checkout's package instead of the installed one: in the handshake
    # child and in the shim it starts.
    marker = tmp_path / "shadow-imported"
    shadow = tmp_path / "checkout" / "pseudolife_memory"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text(
        f"open({str(marker)!r}, 'a').write(__name__ + '\\n')\n", encoding="utf-8")
    (shadow / "doctor_cli.py").write_text(
        "async def _handshake():\n"
        "    return {'instructions_present': True, 'tool_count': 999}\n", encoding="utf-8")
    (shadow / "cli.py").write_text("", encoding="utf-8")
    monkeypatch.chdir(shadow.parent)
    # The tree under test, not whatever copy site-packages holds; the
    # working directory still precedes PYTHONPATH on sys.path.
    monkeypatch.setenv("PYTHONPATH", str(Path(connect_cli.__file__).resolve().parents[1]))
    result = connect_cli.subprocess_handshake("http://127.0.0.1:9", ("literal", GOOD), timeout=10)
    assert not marker.exists(), marker.read_text(encoding="utf-8")
    assert result.get("tool_count") != 999


def test_a_relative_token_file_reaches_the_neutral_directory_handshake_resolved(tmp_path,
                                                                                monkeypatch):
    # The checks before the handshake resolve a registration's relative
    # token file against the caller's directory; the child, run elsewhere,
    # must read that same file.
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, json.dumps(
            {"instructions_present": True, "tool_count": 30}), "")

    monkeypatch.setattr(connect_cli.subprocess, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    connect_cli.subprocess_handshake("http://127.0.0.1:9", ("file", os.path.join("tokens", "me.token")))
    assert seen[FILE_KEY] == os.path.join(os.getcwd(), "tokens", "me.token")


# -- confirmation, dry run ------------------------------------------------------------------------

def test_dry_run_writes_nothing_and_sends_no_token(machine, daemon, capsys):
    full_machine(machine, machine.token())
    before = machine.snapshot()
    code, report, out = run_json(capsys, daemon.url, "--dry-run")
    assert code == 0, out
    assert machine.snapshot() == before
    assert daemon.bearers() == [] and machine.handshakes == []
    assert "no token was sent" in out
    assert one(report, client="claude-code", place="registration")["state"] == "change"


def test_dry_run_exits_4_when_the_daemon_does_not_answer(machine, capsys):
    full_machine(machine, machine.token())
    assert run(capsys, "http://127.0.0.1:9", "--dry-run")[0] == 4


def test_a_non_tty_run_without_yes_prints_the_plan_and_exits_2(machine, daemon, capsys):
    full_machine(machine, machine.token())
    before = machine.snapshot()
    code, out = run(capsys, daemon.url)
    assert code == 2
    assert "change" in out and "--yes" in out
    assert machine.snapshot() == before
    assert daemon.bearers() == []


def test_an_interactive_no_writes_nothing(machine, daemon, capsys, monkeypatch):
    full_machine(machine, machine.token())
    monkeypatch.setattr(connect_cli, "interactive", lambda: True)
    monkeypatch.setattr("sys.stdin", io.StringIO("n\n"))
    before = machine.snapshot()
    code, _ = run(capsys, daemon.url)
    assert code == 2 and machine.snapshot() == before and daemon.bearers() == []


# -- all or nothing ----------------------------------------------------------------------------------

def _failing_third_write(monkeypatch, *, corrupt=False, meddle=None):
    real = connect_cli._write_json
    written = []

    def write(path, data):
        written.append(Path(path))
        if len(written) == 3:
            if meddle:  # another writer changes the first file after connect wrote it
                meddle(written[0])
            if corrupt:
                real(path, {**data, "corrupted": True})
                return
            raise OSError("injected failure")
        real(path, data)

    monkeypatch.setattr(connect_cli, "_write_json", write)
    return written


@pytest.mark.parametrize("corrupt", [False, True], ids=["write-fails", "read-back-differs"])
def test_a_failed_third_write_restores_the_first_two(machine, daemon, capsys, monkeypatch, corrupt):
    token = machine.token()
    machine.register_claude({URL_KEY: OLD_URL, FILE_KEY: token})
    machine.write_json(machine.settings, {"env": {URL_KEY: OLD_URL}})
    machine.register_desktop({URL_KEY: OLD_URL, FILE_KEY: token})
    machine.register_gemini({URL_KEY: OLD_URL, FILE_KEY: token})
    before = machine.snapshot()
    written = _failing_third_write(monkeypatch, corrupt=corrupt)
    code, out = run(capsys, daemon.url, "--yes")
    assert code == 1, out
    assert len(written) == 3
    assert str(written[2]) in out
    after = machine.snapshot()
    for path in (machine.claude_json, machine.settings, machine.desktop, machine.gemini):
        assert after[str(path)] == before[str(path)], path


def test_rollback_leaves_a_file_someone_else_changed_and_keeps_its_backup(machine, daemon, capsys,
                                                                          monkeypatch):
    token = machine.token()
    machine.register_claude({URL_KEY: OLD_URL, FILE_KEY: token})
    machine.write_json(machine.settings, {"env": {URL_KEY: OLD_URL}})
    machine.register_gemini({URL_KEY: OLD_URL, FILE_KEY: token})

    def meddle(path):
        path.write_text(path.read_text() + " ", encoding="utf-8")

    _failing_third_write(monkeypatch, meddle=meddle)
    code, report, out = run_json(capsys, daemon.url, "--yes")
    assert code == 1
    assert machine.claude_json.read_text().endswith(" ")  # the other writer's content stays
    assert "changed under the edit" in out
    assert json.loads(machine.settings.read_text()) == {"env": {URL_KEY: OLD_URL}}  # restored


def test_a_codex_write_failure_rolls_back_the_json_files(machine, daemon, capsys, monkeypatch):
    token = machine.token()
    machine.register_claude({URL_KEY: OLD_URL, FILE_KEY: token})
    machine.register_codex({URL_KEY: OLD_URL, FILE_KEY: token})
    before = machine.snapshot()
    real = machine.codex.rpc

    def failing(method, params, timeout=20):
        if method == "config/batchWrite":
            raise codex_connection.SetupError("Codex rejected config/batchWrite")
        return real(method, params, timeout)

    monkeypatch.setattr(machine.codex, "rpc", failing)
    code, out = run(capsys, daemon.url, "--yes")
    assert code == 1, out
    assert_restored(before, machine.snapshot())


def test_a_failed_post_apply_check_exits_5(machine, daemon, capsys, monkeypatch):
    machine.register_claude({URL_KEY: OLD_URL, FILE_KEY: machine.token()})
    real = connect_cli.probe_health
    calls = []

    def probe(url, timeout):
        calls.append(url)
        return real(url, timeout) if len(calls) == 1 else None

    monkeypatch.setattr(connect_cli, "probe_health", probe)
    code, out = run(capsys, daemon.url, "--yes")
    assert code == 5, out
    assert "backup" in out


# -- Codex replace mode --------------------------------------------------------------------------------

def test_codex_replace_mode_moves_a_literal_into_the_token_copy(machine, daemon, capsys):
    machine.register_claude({URL_KEY: OLD_URL, FILE_KEY: machine.token()})
    machine.register_codex({URL_KEY: OLD_URL, TOKEN_KEY: GOOD, "PSEUDOLIFE_WRITER_ID": "codex"})
    code, report, out = run_json(capsys, daemon.url, "--yes")
    assert code == 0, out
    copy = (machine.codex_home / "pseudolife" / "token").resolve()
    env = tomllib.loads(machine.codex.path.read_text())["mcp_servers"]["pseudolife-memory"]["env"]
    assert env == {URL_KEY: daemon.url, FILE_KEY: str(copy), "PSEUDOLIFE_WRITER_ID": "codex"}
    assert CredentialProvider(path=copy).snapshot().token == GOOD
    assert codex_connection.read_connection(machine.connection) == (daemon.url, str(copy))
    assert GOOD not in out


def test_codex_replace_mode_rolls_back_a_token_copy_it_created(machine, daemon, capsys, monkeypatch):
    machine.register_claude({URL_KEY: OLD_URL, FILE_KEY: machine.token()})
    machine.register_codex({URL_KEY: OLD_URL, TOKEN_KEY: GOOD})
    machine.register_gemini({URL_KEY: OLD_URL, FILE_KEY: machine.token("g.token")})
    before = machine.snapshot()
    real = connect_cli._write_json

    def fail_gemini(path, data):
        if Path(path) == machine.gemini:
            raise OSError("injected failure")
        real(path, data)

    monkeypatch.setattr(connect_cli, "_write_json", fail_gemini)
    assert run(capsys, daemon.url, "--yes")[0] == 1
    assert_restored(before, machine.snapshot())


def test_the_codex_hooks_follow_connection_json_to_the_new_url(machine, daemon, capsys):
    """The plugin's SessionStart hook in Codex context reads connection.json,
    not the MCP server env: after connect it reaches the new daemon with the
    token file connect recorded there."""
    from tests.test_codex_hooks import bash_exe
    token = machine.token()
    machine.register_claude({URL_KEY: OLD_URL, FILE_KEY: token})
    machine.register_codex({URL_KEY: OLD_URL, FILE_KEY: token})
    assert run(capsys, daemon.url, "--yes")[0] == 0
    daemon.requests.clear()
    env = {key: value for key, value in os.environ.items()
           if key not in (URL_KEY, FILE_KEY, TOKEN_KEY, "PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT")}
    env.update({"CODEX_HOME": machine.codex_home.as_posix(), "PSEUDOLIFE_CODEX_HOOK": "1"})
    result = subprocess.run([bash_exe(), str(ROOT / "plugin/hooks/session-start.sh")],
                            input='{"session_id":"fixture","source":"startup"}', env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert daemon.requests and all(bearer == GOOD for _path, bearer in daemon.requests)


# -- --read-token --------------------------------------------------------------------------------------

def test_read_token_creates_an_owner_only_file_without_a_bom(machine, daemon, capsys, monkeypatch):
    target = machine.home / ".pseudolife-mcp" / "new.token"
    machine.register_claude({URL_KEY: OLD_URL})
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(("﻿" + GOOD + "\r\n").encode())))
    code, out = run(capsys, daemon.url, "--yes", "--token-file", str(target), "--read-token")
    assert code == 0, out
    assert client_config.check_token_file(target) == {"status": "ready"}
    assert target.read_bytes() == GOOD.encode()
    env = json.loads(machine.claude_json.read_text())["mcpServers"]["pseudolife-memory"]["env"]
    assert env[FILE_KEY] == str(target)
    assert GOOD not in out


def test_read_token_refuses_an_existing_file(machine, daemon, capsys):
    machine.register_claude({URL_KEY: OLD_URL})
    existing = machine.token()
    assert run(capsys, daemon.url, "--yes", "--token-file", existing, "--read-token")[0] == 2


def test_read_token_removes_the_file_when_the_daemon_refuses_it(machine, daemon, capsys, monkeypatch):
    target = machine.home / ".pseudolife-mcp" / "new.token"
    machine.register_claude({URL_KEY: OLD_URL})
    before = machine.claude_json.read_bytes()
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(OTHER.encode())))
    code, out = run(capsys, daemon.url, "--yes", "--token-file", str(target), "--read-token")
    assert code == 4, out
    assert not target.exists() and machine.claude_json.read_bytes() == before


def test_read_token_needs_a_token_file(machine, daemon, capsys):
    assert run(capsys, daemon.url, "--yes", "--read-token")[0] == 2


# -- the report -----------------------------------------------------------------------------------------

def test_the_report_names_the_sessions_to_restart(machine, daemon, capsys):
    token = machine.token()
    machine.register_claude({URL_KEY: OLD_URL, FILE_KEY: token})
    machine.register_desktop({URL_KEY: OLD_URL, FILE_KEY: token})
    layout = runtimes.default_layout(dict(os.environ))
    machine.processes = [(4242, 1, str(layout.root / "000001" / "Scripts" / "pseudolife-mcp.exe")),
                         (4343, 1, str(machine.home / "elsewhere.exe"))]
    code, report, out = run_json(capsys, daemon.url, "--yes")
    assert code == 0
    assert report["restart"]["pids"] == [4242]
    assert report["restart"]["claude_desktop"] is True
    assert any("board" in note for note in report["notes"])


@pytest.mark.skipif(os.name != "nt", reason="the MSIX package cache is a Windows path")
def test_the_msix_desktop_config_is_found_and_rewritten(machine, daemon, capsys):
    token = machine.token()
    cache = (machine.home / "AppData" / "Local" / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache"
             / "Roaming" / "Claude" / "claude_desktop_config.json")
    machine.register_desktop({URL_KEY: OLD_URL, FILE_KEY: token}, path=cache)
    code, report, _ = run_json(capsys, daemon.url, "--yes")
    assert code == 0
    assert one(report, client="claude-desktop", place="registration")["file"] == str(cache)
    assert json.loads(cache.read_text())["mcpServers"]["pseudolife-desktop"]["env"][URL_KEY] == daemon.url


def test_the_cli_dispatches_connect_and_usage_names_it():
    from pseudolife_memory.cli import _USAGE
    assert "connect" in _USAGE and "PSEUDOLIFE_MCP_TOKEN_FILE" in _USAGE
    proc = subprocess.run([sys.executable, "-m", "pseudolife_memory.cli", "connect", "--help"],
                          capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0 and "--dry-run" in proc.stdout


# -- Codex replace mode, at the writer ------------------------------------------------------------------

def test_replace_connection_rewrites_env_and_connection_json(tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    fake = FakeCodex(home)
    fake.path.write_text(dump_toml({"mcp_servers": {"pseudolife-memory": {
        "command": "x", "env": {URL_KEY: OLD_URL, "KEEP": "1"}}}}), encoding="utf-8")
    token = tmp_path / "t.token"
    _write_token_file(token, GOOD)
    result = codex_connection.replace_connection(fake, home, tmp_path, "http://100.64.0.2:8765",
                                                 token_file=str(token), no_spawn=True)
    env = tomllib.loads(fake.path.read_text())["mcp_servers"]["pseudolife-memory"]["env"]
    assert env == {URL_KEY: "http://100.64.0.2:8765", "KEEP": "1", FILE_KEY: str(token), NO_SPAWN: "1"}
    assert codex_connection.read_connection(home / "pseudolife" / "connection.json") == (
        "http://100.64.0.2:8765", str(token))
    assert result["changed"] == sorted([URL_KEY, FILE_KEY, NO_SPAWN])
    assert result["backup"] and Path(result["backup"]).is_file()
    # The file the hooks parse keeps its fixed five-line layout.
    lines = (home / "pseudolife" / "connection.json").read_text().splitlines()
    assert len(lines) == 5 and lines[1] == '  "version": 1,'
    assert base64.b64decode(lines[2].split('"')[3]).decode() == "http://100.64.0.2:8765"


# -- review findings (2026-09-30) -------------------------------------------------------------------------

def test_only_manual_registrations_exit_3(machine, daemon, capsys):
    machine.write_json(machine.claude_json, {
        "mcpServers": {"pseudolife-memory": {"type": "http", "url": "http://127.0.0.1:8765/mcp"}}})
    before = machine.snapshot()
    code, out = run(capsys, daemon.url, "--yes")
    assert code == 3, out
    assert "manual" in out and machine.snapshot() == before


def test_current_beside_manual_names_the_manual_places(machine, daemon, capsys):
    token = machine.token()
    machine.register_claude({URL_KEY: daemon.url, FILE_KEY: token})
    machine.write_json(machine.settings, {"env": {URL_KEY: daemon.url, FILE_KEY: token}})
    machine.register_desktop({"PSEUDOLIFE_WRITER_ID": "claude-desktop", URL_KEY: OLD_URL},
                             key="pseudolife-memory")
    code, out = run(capsys, daemon.url, "--yes")
    assert code == 0, out
    assert "1 place needs manual action" in out
    assert "every place already names" not in out


def test_a_codex_failure_after_its_backup_names_the_backup(machine, daemon, capsys, monkeypatch):
    token = machine.token()
    machine.register_claude({URL_KEY: OLD_URL, FILE_KEY: token})
    machine.register_codex({URL_KEY: OLD_URL, FILE_KEY: token})
    original = machine.codex.path.read_bytes()

    def broken(path, updates):
        raise codex_connection.SetupError("The managed Codex connection file is unsafe or malformed; "
                                          "repair or remove it before retrying.")

    monkeypatch.setattr(codex_connection, "_private_json", broken)
    code, report, out = run_json(capsys, daemon.url, "--yes")
    assert code == 1, out
    assert machine.codex.path.read_bytes() == original  # config.toml was written, then restored
    entry = next(item for item in report["rollback"] if Path(item["file"]) == machine.codex.path)
    assert entry["state"] == "restored"
    assert entry["backup"] and Path(entry["backup"]).read_bytes() == original


SECRET = "s3cr3t-" + "z" * 16


def test_a_credential_bearing_url_is_never_echoed(machine, daemon, capsys, monkeypatch):
    code, out = run(capsys, f"http://user:{SECRET}@example.com")
    assert code == 2 and SECRET not in out and "user" not in out
    code, _report, out = run_json(capsys, f"http://user:{SECRET}@example.com")
    assert code == 2 and SECRET not in out

    monkeypatch.setenv(URL_KEY, f"http://user:{SECRET}@127.0.0.1:1")
    machine.user_url = f"https://user:{SECRET}@example.com/path"
    machine.schedule = {"where": "fixture task", "time": "03:30", "url": f"http://u:{SECRET}@127.0.0.1:2"}
    machine.register_claude({URL_KEY: f"http://user:{SECRET}@127.0.0.1:1", FILE_KEY: machine.token()})
    for argv in ((daemon.url, "--yes"), (daemon.url, "--dry-run", "--json")):
        code, out = run(capsys, *argv)
        assert code == 0, out
        assert SECRET not in out
    assert "127.0.0.1:1" in out  # the host is still named


# -- --code: pair, then connect with the new token file ----------------------------------------------

CODE = "ABCD-EFGH-JKMN"


def paired_files(machine) -> list[Path]:
    directory = machine.home / ".pseudolife-mcp"
    return sorted(directory.glob("*.token")) if directory.exists() else []


def test_code_dry_run_never_redeems_or_mints(machine, daemon, capsys):
    machine.register_claude({URL_KEY: OLD_URL})
    code, report, out = run_json(capsys, daemon.url, "--code", CODE, "--dry-run")
    assert code == 0, out
    assert daemon.pairs == [] and paired_files(machine) == []
    change = one(report, client="claude-code", place="registration")["changes"][FILE_KEY]
    assert change[1] == "~/.pseudolife-mcp/<principal>.token (name from the daemon)"
    assert CODE not in out


def test_code_with_nothing_to_repoint_stops_before_redeeming(machine, daemon, capsys):
    code, report, out = run_json(capsys, daemon.url, "--code", CODE, "--yes")
    assert code == 3
    assert daemon.pairs == [] and paired_files(machine) == []
    assert "--pairing-code" in report["error"] and "installer" in report["error"]


def test_code_redeems_once_then_repoints_at_the_real_file(machine, daemon, capsys):
    machine.register_claude({URL_KEY: OLD_URL})
    code, report, out = run_json(capsys, daemon.url, "--code", CODE, "--yes")
    assert code == 0, out
    assert len(daemon.pairs) == 1
    target = machine.home / ".pseudolife-mcp" / "laptop.token"
    assert paired_files(machine) == [target]
    token = target.read_text(encoding="utf-8")
    assert json.loads(daemon.pairs[0])["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    env = json.loads(machine.claude_json.read_text())["mcpServers"]["pseudolife-memory"]["env"]
    assert env[FILE_KEY] == str(target)
    settings = json.loads(machine.settings.read_text())["env"]
    assert settings[FILE_KEY] == str(target)
    assert machine.handshakes == [(daemon.url, ("file", str(target)))]
    assert token not in out and CODE not in out


def test_code_with_a_token_file_pairs_into_that_path(machine, daemon, capsys):
    machine.register_claude({URL_KEY: OLD_URL})
    target = machine.home / "chosen.token"
    code, report, out = run_json(capsys, daemon.url, "--code", CODE, "--token-file", str(target), "--yes")
    assert code == 0, out
    assert target.exists() and paired_files(machine) == []
    env = json.loads(machine.claude_json.read_text())["mcpServers"]["pseudolife-memory"]["env"]
    assert env[FILE_KEY] == str(target)


def test_code_warns_when_several_clients_would_share_one_principal(machine, daemon, capsys):
    machine.register_claude({URL_KEY: OLD_URL})
    machine.register_gemini({URL_KEY: OLD_URL})
    code, report, out = run_json(capsys, daemon.url, "--code", CODE, "--dry-run")
    assert code == 0, out
    assert any("share one principal" in warning and "invite" in warning for warning in report["warnings"])


def test_code_on_one_client_does_not_warn_about_sharing(machine, daemon, capsys):
    machine.register_claude({URL_KEY: OLD_URL})
    code, report, out = run_json(capsys, daemon.url, "--code", CODE, "--dry-run")
    assert code == 0, out
    assert not any("share one principal" in warning for warning in report["warnings"])


def test_a_refused_code_is_exit_4_and_writes_nothing(machine, daemon, capsys):
    machine.register_claude({URL_KEY: OLD_URL})
    before = machine.snapshot()
    daemon.pair_status = 400
    code, report, out = run_json(capsys, daemon.url, "--code", CODE, "--yes")
    assert code == 4, out
    assert len(daemon.pairs) == 1
    assert machine.snapshot() == before


def test_an_unverified_pairing_is_exit_4_and_names_the_kept_file(machine, daemon, capsys, monkeypatch):
    machine.register_claude({URL_KEY: OLD_URL})
    before = machine.claude_json.read_bytes()
    monkeypatch.setattr(connect_cli, "credential_valid", lambda url, token: False)
    code, report, out = run_json(capsys, daemon.url, "--code", CODE, "--yes")
    assert code == 4, out
    target = machine.home / ".pseudolife-mcp" / "laptop.token"
    assert target.exists() and str(target) in report["error"]
    assert machine.claude_json.read_bytes() == before


def test_code_needs_a_daemon_with_auth(machine, capsys):
    stub = Daemon(auth=False)
    machine.register_claude({URL_KEY: OLD_URL})
    try:
        code, report, out = run_json(capsys, stub.url, "--code", CODE, "--yes")
    finally:
        stub.close()
    assert code == 4 and stub.pairs == []


@pytest.mark.parametrize("argv", [["--code", CODE, "--read-token", "--token-file", "x.token"],
                                  ["--code", CODE, "--read-code"],
                                  ["--code", "not-a-code"]])
def test_code_usage_errors_exit_2(machine, daemon, capsys, monkeypatch, argv):
    machine.register_claude({URL_KEY: OLD_URL})
    monkeypatch.setattr("sys.stdin", io.StringIO(CODE + "\n"))
    code, report, out = run_json(capsys, daemon.url, *argv, "--yes")
    assert code == 2, out
    assert daemon.pairs == []


def test_read_code_takes_the_code_from_stdin(machine, daemon, capsys, monkeypatch):
    machine.register_claude({URL_KEY: OLD_URL})
    monkeypatch.setattr("sys.stdin", io.StringIO(CODE + "\n"))
    code, report, out = run_json(capsys, daemon.url, "--read-code", "--yes")
    assert code == 0, out
    assert json.loads(daemon.pairs[0])["code"] == CODE.replace("-", "")


def test_an_existing_token_file_with_code_is_refused_before_redeeming(machine, daemon, capsys):
    machine.register_claude({URL_KEY: OLD_URL})
    existing = machine.token()
    code, report, out = run_json(capsys, daemon.url, "--code", CODE, "--token-file", existing, "--yes")
    assert code == 2 and daemon.pairs == []


def test_the_board_hint_names_invite():
    assert "pseudolife-mcp invite" in connect_cli._BOARD_HINT

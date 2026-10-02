"""Codex's connection to the daemon: its MCP server env, ``connection.json``
and the token copy under ``$CODEX_HOME/pseudolife``.

The Codex config is written only through Codex's own app-server
(``config/batchWrite`` with ``expectedVersion``), never by editing
``config.toml`` directly. ``ops/setup-codex-hooks.py`` imports this from a
checkout under whatever ``python3`` the installer finds, and ``pseudolife-mcp
connect`` imports it from a shim runtime, so it stays standard library only
(plus the package's own standard-library credential and private-file
modules).

``connection.json`` is what the plugin hooks read in Codex context
(``plugin/hooks/session-start.sh``, ``lifecycle.ps1``): the daemon URL and
the token-file path, each base64-encoded, in a fixed five-line layout.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pseudolife_memory.credentials import (
    CredentialError,
    CredentialProvider,
    _write_token_file,
)
from pseudolife_memory.private_state import open_private as _open_state

SERVER = "pseudolife-memory"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


urlopen = build_opener(_NoRedirect).open


class SetupError(Exception):
    """A safe, user-facing error (never a raw transport error)."""


def private_backup(path: Path) -> str | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target = path.with_name(path.name + ".bak-pseudolife-credentials-" + stamp)
    fd = _open_state(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "wb") as stream:
        stream.write(path.read_bytes())
    return str(target)


def _private_json(path: Path, updates):
    current = {}
    if path.exists() or path.is_symlink():
        try:
            fd = _open_state(path, os.O_RDONLY)
            with os.fdopen(fd, "rb") as stream:
                data = stream.read(16385)
            if len(data) > 16384:
                raise ValueError
            current = json.loads(data)
            if not isinstance(current, dict):
                raise ValueError
        except (OSError, ValueError, UnicodeError) as error:
            raise SetupError(
                "The managed Codex connection file is unsafe or malformed; "
                "repair or remove it before retrying.") from error
    updated = dict(updates)
    data = (json.dumps(updated, indent=2) + "\n").encode()
    if updated == current:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    saved = private_backup(path)
    temporary = path.with_name(".connection-" + uuid.uuid4().hex + ".tmp")
    fd = _open_state(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return saved


def _validated_daemon_url(url):
    if not isinstance(url, str):
        raise SetupError("The configured memory daemon URL is invalid.")
    try:
        parsed = urlsplit(url)
        parsed.port
    except (TypeError, ValueError) as error:
        raise SetupError("The configured memory daemon URL is invalid.") from error
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}
            or any(character.isspace() or ord(character) < 0x20 for character in url)):
        raise SetupError("The configured memory daemon URL is invalid.")
    return urlunsplit((parsed.scheme, parsed.netloc.rstrip("/"), "", "", ""))


def _daemon_url(server):
    env = server.get("env") or {}
    forwarded = set(server.get("env_vars") or ())
    if "PSEUDOLIFE_MCP_DAEMON_URL" in env:
        url = env["PSEUDOLIFE_MCP_DAEMON_URL"]
    elif ("PSEUDOLIFE_MCP_DAEMON_URL" in forwarded
          and "PSEUDOLIFE_MCP_DAEMON_URL" in os.environ):
        url = os.environ["PSEUDOLIFE_MCP_DAEMON_URL"]
    elif not server:
        url = os.environ.get("PSEUDOLIFE_MCP_DAEMON_URL")
    else:
        url = None
    if url is None:
        url = "http://127.0.0.1:8765"
    return _validated_daemon_url(url)


def installer_credential_valid(url, token):
    """Authenticate a one-shot installer credential without exposing details."""
    try:
        token = CredentialProvider(token=token).snapshot().token
        request = Request(
            url + "/api/episodes?limit=1",
            headers={"Authorization": "Bearer " + token})
        with urlopen(request, timeout=3) as response:
            response.read(1)
            return getattr(response, "status", 200) == 200
    except Exception:
        return False


def _connection_values(url, target=None):
    def encoded(value):
        text = "" if value is None else str(value)
        return base64.b64encode(text.encode("utf-8")).decode("ascii")
    return {"version": 1, "daemon_url": encoded(url), "token_file": encoded(target)}


def dotted(*parts: str) -> str:
    return ".".join(json.dumps(p) for p in parts)


def toml_value(value):
    """CLI -c values are TOML, while config RPC values are JSON.

    Override entire tables for hook/plugin keys containing dots: the CLI's
    dotted-path splitter does not implement the config RPC's quoted keys.
    """
    if isinstance(value, dict):
        return "{" + ", ".join(json.dumps(k, ensure_ascii=False) + " = " + toml_value(v)
                                for k, v in value.items()) + "}"
    return json.dumps(value, ensure_ascii=False)


def resolve_codex() -> str:
    explicit = os.environ.get("CODEX_CLI_PATH")
    if explicit and Path(explicit).is_file():
        return explicit
    found = shutil.which("codex")
    if found:
        return found
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/bin"
        candidates = list(root.glob("*/codex.exe"))
        if candidates:
            return str(max(candidates, key=lambda p: p.stat().st_mtime))
    raise SetupError("Codex CLI is unavailable; install or update Codex, then rerun setup.")


class Codex:
    """Bounded stdio JSON-RPC client; child stderr never enters user reports."""

    def __init__(self, executable: str, home: Path, cwd: Path, overrides=None):
        args = [executable, "app-server", "--stdio"]
        for key, value in (overrides or {}).items():
            args += ["-c", key + "=" + toml_value(value)]
        env = dict(os.environ, CODEX_HOME=str(home))
        self.proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding="utf-8")
        self.messages = queue.Queue()
        self.events = []
        self.counter = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.rpc("initialize", {"clientInfo": {"name": "pseudolife_hook_setup", "version": "1"},
                                    "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def _read(self):
        for line in self.proc.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                continue
        self.messages.put(None)

    def send(self, value):
        self.proc.stdin.write(json.dumps(value) + "\n")
        self.proc.stdin.flush()

    def receive(self, timeout):
        try:
            item = self.messages.get(timeout=max(timeout, 0.01))
        except queue.Empty:
            raise SetupError("Codex did not respond within the setup timeout.") from None
        if item is None:
            raise SetupError("Codex exited before hook setup completed.")
        self.events.append(item)
        return item

    def rpc(self, method, params, timeout=20):
        self.counter += 1
        ident = self.counter
        self.send({"id": ident, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            item = self.receive(deadline - time.monotonic())
            if item.get("id") == ident:
                if "error" in item:
                    raise SetupError(f"Codex rejected {method}; update Codex or use /hooks for manual review.")
                return item["result"]
        raise SetupError("Codex did not respond within the setup timeout.")

    def close(self):
        if self.proc.stdin and not self.proc.stdin.closed:
            # A request a dead Codex never read stays buffered, and close()
            # flushes it again; subprocess.communicate() ignores this too.
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        self.reader.join(timeout=2)
        # A hook Codex killed can outlive it holding an inherited copy of its
        # stdout (seen on Windows under load: a PowerShell hook stuck mid-exit
        # for minutes), so the reader never sees EOF. Closing the pipe under
        # that blocked read would wait on the hook; the daemon reader ends
        # with this process instead.
        if self.proc.stdout and not self.reader.is_alive():
            self.proc.stdout.close()


@contextmanager
def codex(executable, home, cwd, overrides=None):
    client = Codex(executable, home, cwd, overrides)
    try:
        yield client
    finally:
        client.close()


def _user_config_layer(config, home):
    path = (home / "config.toml").resolve()
    layers = [layer for layer in config.get("layers", [])
              if layer.get("name", {}).get("type") == "user"
              and not layer["name"].get("profile")
              and Path(layer["name"].get("file", "")).resolve() == path]
    if len(layers) != 1 or not layers[0].get("version"):
        raise SetupError("Codex did not expose a versioned user configuration; credential settings were not changed.")
    return path, layers[0]["version"], layers[0].get("config") or {}


def configure_credential_file(client, home, cwd, config=None,
                              installer_connection=None, *, credential_valid=None):
    """Bootstrap a private token file and migrate an existing Codex MCP env.

    ``credential_valid(url, token)`` checks an installer credential against
    the daemon (default: :func:`installer_credential_valid`)."""
    credential_valid = credential_valid or installer_credential_valid
    config = config or client.rpc("config/read", {"includeLayers": True, "cwd": str(cwd)})
    effective_server = config.get("config", {}).get("mcp_servers", {}).get(
        "pseudolife-memory") or {}
    user_layers = [layer for layer in config.get("layers", [])
                   if layer.get("name", {}).get("type") == "user"
                   and not layer["name"].get("profile")
                   and Path(layer["name"].get("file", "")).resolve()
                   == (home / "config.toml").resolve()]
    user_config = user_layers[0].get("config") if len(user_layers) == 1 else {}
    server = (user_config or {}).get("mcp_servers", {}).get(
        "pseudolife-memory") or {}
    env = dict(server.get("env") or {})
    forwarded = set(server.get("env_vars") or ())
    effective_env = dict(effective_server.get("env") or {})
    effective_forwarded = set(effective_server.get("env_vars") or ())
    source_path = None
    literal = None
    selected_daemon_url = None
    if "PSEUDOLIFE_MCP_TOKEN_FILE" in env:
        source_path = env["PSEUDOLIFE_MCP_TOKEN_FILE"]
    elif "PSEUDOLIFE_MCP_TOKEN" in env:
        literal = env["PSEUDOLIFE_MCP_TOKEN"]
    elif ("PSEUDOLIFE_MCP_TOKEN_FILE" in forwarded
          and "PSEUDOLIFE_MCP_TOKEN_FILE" in os.environ):
        source_path = os.environ["PSEUDOLIFE_MCP_TOKEN_FILE"]
    elif ("PSEUDOLIFE_MCP_TOKEN" in forwarded
          and "PSEUDOLIFE_MCP_TOKEN" in os.environ):
        literal = os.environ["PSEUDOLIFE_MCP_TOKEN"]
    if source_path is None and not literal and installer_connection is not None:
        if (any(key in effective_env for key in (
                "PSEUDOLIFE_MCP_TOKEN_FILE", "PSEUDOLIFE_MCP_TOKEN"))
                or any(key in effective_forwarded for key in (
                    "PSEUDOLIFE_MCP_TOKEN_FILE", "PSEUDOLIFE_MCP_TOKEN"))):
            raise SetupError(
                "Credential settings come from another Codex configuration layer; "
                "the installer did not replace them.")
        try:
            installer_url, installer_token = installer_connection
            installer_url = _validated_daemon_url(installer_url)
            if installer_token is not None:
                CredentialProvider(token=installer_token).snapshot()
        except (CredentialError, SetupError, TypeError, ValueError) as error:
            raise SetupError(
                "The installer-provided daemon credential is invalid.") from error
        if effective_server:
            user_url = _daemon_url(server)
            effective_url = _daemon_url(effective_server)
            if installer_url != user_url or installer_url != effective_url:
                raise SetupError(
                    "The installer daemon URL does not match the configured Codex server.")
        if (installer_token is not None
                and not credential_valid(installer_url, installer_token)):
            raise SetupError(
                "The installer credential was not accepted by the configured daemon.")
        literal = installer_token or None
        selected_daemon_url = installer_url
    try:
        if source_path is not None:
            snapshot = CredentialProvider(path=source_path).snapshot()
            target = Path(os.path.abspath(Path(source_path).expanduser()))
        else:
            token = literal or None
            if token is None:
                result = {"credential_file_configured": False,
                          "credential_file_path": "",
                          "daemon_url": selected_daemon_url,
                          "connection_configured": selected_daemon_url is not None,
                          "migrated_literal": False, "backup": None,
                          "connection_backup": None}
                if selected_daemon_url is not None:
                    result["connection_backup"] = _private_json(
                        home / "pseudolife" / "connection.json",
                        _connection_values(selected_daemon_url))
                return result
            target = (home / "pseudolife" / "token").resolve()
            if target.exists() or target.is_symlink():
                current = CredentialProvider(path=target).snapshot()
                if current.token != token:
                    _write_token_file(target, token)
            else:
                _write_token_file(target, token)
            snapshot = CredentialProvider(path=target).snapshot()
            if snapshot.token != token:
                raise CredentialError("credential file validation failed")
    except (CredentialError, OSError, UnicodeError) as error:
        raise SetupError(
            "The configured credential file is missing, unsafe, or malformed; "
            "repair it or remove PSEUDOLIFE_MCP_TOKEN_FILE before retrying.") from error

    result = {"credential_file_configured": True,
              "credential_file_path": str(target),
              "daemon_url": selected_daemon_url or _daemon_url(
                  server if effective_server else {}),
              "connection_configured": True,
              "migrated_literal": ("PSEUDOLIFE_MCP_TOKEN" in env
                                    or selected_daemon_url is not None),
              "backup": None,
              "connection_backup": None}
    if not effective_server:
        result["connection_backup"] = _private_json(
            home / "pseudolife" / "connection.json",
            _connection_values(result["daemon_url"], target))
        return result
    new_env = dict(env)
    new_env["PSEUDOLIFE_MCP_TOKEN_FILE"] = str(target)
    new_env.pop("PSEUDOLIFE_MCP_TOKEN", None)
    if new_env == env:
        result["connection_backup"] = _private_json(
            home / "pseudolife" / "connection.json",
            _connection_values(result["daemon_url"], target))
        return result
    path, version, _ = _user_config_layer(config, home)
    result["backup"] = private_backup(path)
    client.rpc("config/batchWrite", {
        "edits": [{"keyPath": dotted("mcp_servers", "pseudolife-memory", "env"),
                   "value": new_env, "mergeStrategy": "replace"}],
        "filePath": str(path),
        "expectedVersion": version,
    })
    current = client.rpc("config/read", {"includeLayers": True, "cwd": str(cwd)})
    actual = current.get("config", {}).get("mcp_servers", {}).get(
        "pseudolife-memory", {}).get("env", {})
    if (actual.get("PSEUDOLIFE_MCP_TOKEN_FILE") != str(target)
            or "PSEUDOLIFE_MCP_TOKEN" in actual
            or any(actual.get(key) != value for key, value in new_env.items())):
        raise SetupError(
            "Codex saved credential settings but its effective configuration differs; "
            "check project or managed overrides before reconnecting.")
    result["connection_backup"] = _private_json(
        home / "pseudolife" / "connection.json",
        _connection_values(result["daemon_url"], target))
    return result


# -- replace mode -------------------------------------------------------------
#
# configure_credential_file fills in what is missing and refuses an installer
# URL that differs from the configured server's. Replace mode is
# `pseudolife-mcp connect`'s: it points the user-level server at a new
# daemon whatever URL it names now, and rewrites connection.json to match,
# so the plugin hooks in Codex context follow the MCP server.

URL_KEY = "PSEUDOLIFE_MCP_DAEMON_URL"
TOKEN_FILE_KEY = "PSEUDOLIFE_MCP_TOKEN_FILE"
TOKEN_KEY = "PSEUDOLIFE_MCP_TOKEN"
NO_SPAWN_KEY = "PSEUDOLIFE_MCP_NO_SPAWN"
_CONNECTION_KEYS = (URL_KEY, TOKEN_FILE_KEY, TOKEN_KEY)
_TRUTHY = {"1", "true", "yes", "on"}


def connection_path(home: Path) -> Path:
    return home / "pseudolife" / "connection.json"


def token_copy_path(home: Path) -> Path:
    return (home / "pseudolife" / "token").resolve()


def read_connection(path: Path) -> tuple[str, str] | None:
    """``(daemon_url, token_file)`` recorded in ``connection.json`` (the
    token file ``""`` for a tokenless connection), ``None`` when there is
    none. Raises :class:`SetupError` for an unsafe or malformed file."""
    if not (path.exists() or path.is_symlink()):
        return None
    try:
        fd = _open_state(path, os.O_RDONLY)
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(16385)
        if len(data) > 16384:
            raise ValueError
        record = json.loads(data)
        return (base64.b64decode(record["daemon_url"]).decode("utf-8"),
                base64.b64decode(record["token_file"]).decode("utf-8"))
    except (OSError, ValueError, UnicodeError, KeyError, TypeError) as error:
        raise SetupError(
            "The managed Codex connection file is unsafe or malformed; "
            "repair or remove it before retrying.") from error


def plan_replace(config, home, daemon_url, *, token_file=None, no_spawn=False):
    """What replace mode writes, from a ``config/read`` result: ``(env,
    new_env, literal)``, the user-level server env before and after, and
    the literal token that goes to the token copy (``None`` when none does).

    Only the daemon URL, the token-file path (when ``token_file`` is given)
    and, with ``no_spawn``, ``PSEUDOLIFE_MCP_NO_SPAWN`` change. As the
    credential writer does, a literal ``PSEUDOLIFE_MCP_TOKEN`` with no token
    file moves into the Codex token copy, and a literal beside a token file
    is dropped. Raises :class:`SetupError` when the user configuration has no
    server, or when any of these settings comes from another layer (a
    project or managed config, or the environment through ``env_vars``),
    which replace mode leaves alone."""
    _, _, user_config = _user_config_layer(config, home)
    server = (user_config.get("mcp_servers") or {}).get(SERVER)
    if not isinstance(server, dict) or not server:
        raise SetupError("Codex's user configuration has no pseudolife-memory server.")
    effective = (config.get("config", {}).get("mcp_servers") or {}).get(SERVER) or {}
    env = dict(server.get("env") or {})
    effective_env = dict(effective.get("env") or {})
    forwarded = set(effective.get("env_vars") or ())
    if (any(effective_env.get(key) != env.get(key) for key in _CONNECTION_KEYS)
            or any(key in forwarded and key not in env for key in _CONNECTION_KEYS)):
        raise SetupError(
            "Connection settings come from another Codex configuration layer; "
            "replace mode did not change them.")
    new_env = dict(env)
    new_env[URL_KEY] = _validated_daemon_url(daemon_url)
    if no_spawn and str(env.get(NO_SPAWN_KEY, "")).strip().lower() not in _TRUTHY:
        new_env[NO_SPAWN_KEY] = "1"
    literal = None
    if token_file:
        new_env[TOKEN_FILE_KEY] = str(token_file)
    elif TOKEN_FILE_KEY not in env and env.get(TOKEN_KEY):
        literal = env[TOKEN_KEY]
        new_env[TOKEN_FILE_KEY] = str(token_copy_path(home))
    if new_env.get(TOKEN_FILE_KEY):
        new_env.pop(TOKEN_KEY, None)
    return env, new_env, literal


def replace_connection(client, home, cwd, daemon_url, *, token_file=None,
                       no_spawn=False, config=None, backups=None):
    """Replace mode: point Codex's user-level ``pseudolife-memory`` server
    at ``daemon_url`` (see :func:`plan_replace` for what changes), then
    rewrite ``connection.json`` to match. Writes, in the credential
    writer's order: the token copy (literal case only; backed up before it
    is replaced), the server env (``config/batchWrite`` against the layer's
    version, then read back from the effective config), ``connection.json``.

    Returns the changed keys, the token-file path, each backup (``None``
    when that file was not replaced) and the files this call created. Each
    backup is also recorded in the caller's ``backups`` dict (``config``,
    ``connection``, ``token``) as it is made, so a caller can name it when a
    later step raises."""
    backups = {} if backups is None else backups
    daemon_url = _validated_daemon_url(daemon_url)
    config = config or client.rpc("config/read", {"includeLayers": True, "cwd": str(cwd)})
    env, new_env, literal = plan_replace(config, home, daemon_url,
                                         token_file=token_file, no_spawn=no_spawn)
    path, version, _ = _user_config_layer(config, home)
    target = new_env.get(TOKEN_FILE_KEY) or None
    result = {"daemon_url": daemon_url, "credential_file_path": target or "",
              "changed": sorted(key for key in set(env) | set(new_env)
                                if env.get(key) != new_env.get(key)),
              "backup": None, "connection_backup": None, "token_backup": None,
              "created": []}
    if literal is not None:
        copy = Path(target)
        try:
            if copy.exists() or copy.is_symlink():
                if CredentialProvider(path=copy).snapshot().token != literal:
                    result["token_backup"] = backups["token"] = private_backup(copy)
                    _write_token_file(copy, literal)
            else:
                _write_token_file(copy, literal)
                result["created"].append(str(copy))
            if CredentialProvider(path=copy).snapshot().token != literal:
                raise CredentialError("credential file validation failed")
        except (CredentialError, OSError, UnicodeError) as error:
            raise SetupError(
                "The Codex token copy is unsafe or could not be written; "
                "repair or remove it before retrying.") from error
    if new_env != env:
        result["backup"] = backups["config"] = private_backup(path)
        client.rpc("config/batchWrite", {
            "edits": [{"keyPath": dotted("mcp_servers", SERVER, "env"),
                       "value": new_env, "mergeStrategy": "replace"}],
            "filePath": str(path),
            "expectedVersion": version,
        })
        current = client.rpc("config/read", {"includeLayers": True, "cwd": str(cwd)})
        actual = (current.get("config", {}).get("mcp_servers") or {}).get(
            SERVER, {}).get("env", {})
        if (any(actual.get(key) != value for key, value in new_env.items())
                or any(key in actual for key in env if key not in new_env)):
            raise SetupError(
                "Codex saved the connection settings but its effective configuration "
                "differs; check project or managed overrides before reconnecting.")
    connection = connection_path(home)
    existed = connection.exists() or connection.is_symlink()
    result["connection_backup"] = _private_json(
        connection, _connection_values(daemon_url, target))
    if result["connection_backup"]:
        backups["connection"] = result["connection_backup"]
    if not existed:
        result["created"].append(str(connection))
    return result

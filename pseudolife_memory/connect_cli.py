"""``pseudolife-mcp connect``: point this machine's clients at a daemon.

    pseudolife-mcp connect <daemon-url> [--token-file PATH [--read-token]]
        [--code CODE | --read-code]
        [--client claude-code,codex,claude-desktop,gemini | --client all]
        [--dry-run] [--yes] [--json]

When a bank's daemon changes address, every client registration on every
machine has to follow it, and each registrar fills in what is missing but
leaves an existing daemon URL alone. This command re-points the
registrations it finds, and the copies of the URL and token file the plugin
hooks read, in five steps:

1. **Plan.** Validate the URL, probe ``/health`` without a token, find every
   registration and report each place as ``current``, ``change``,
   ``manual`` (found but not safely writable; the detail says what to do) or
   ``absent`` (with the command that registers it: connect never creates a
   registration). ``--dry-run`` stops here and sends no token.
2. **Confirm.** Show the plan and ask once; ``--yes`` skips the question, and
   a non-interactive run without it exits 2. No authenticated request is
   sent before this point.
3. **Verify.** Every distinct credential the plan points at the target
   (token files and literal ``PSEUDOLIFE_MCP_TOKEN`` values) must pass the
   shim's token-file check, an authenticated request that follows no
   redirects, and an MCP handshake with the board check-in off. Nothing is
   written until all pass.
4. **Apply, all or nothing.** Each file is backed up, written and read
   back. If any write or read-back fails, every file this run wrote is
   restored, unless something else changed it since: that file is left as
   it is, with its backup, and named. A file this run created is removed.
5. **Report.** One line per place, the backups, the sessions to restart,
   and a post-apply check: the plan again (every place ``current``) and
   ``/health`` again.

Only the managed keys change: the daemon URL, the token-file path (and a
literal token a given file replaces), and ``PSEUDOLIFE_MCP_NO_SPAWN=1`` for a
daemon on another machine (a loopback target keeps the registration's own
value: whether a local shim may start a daemon is the install's choice).
Commands, writer ids, state directories and user-added env stay as they
are. Codex is written through its own app-server (``codex_connection``'s
replace mode). Tokens are never printed.

``--code CODE`` (or ``--read-code``, the code on stdin) joins a bank with a
pairing code from ``pseudolife-mcp invite`` on the daemon host: it runs
``pair`` and then connects with the new token file. The plan names that file
as ``~/.pseudolife-mcp/<principal>.token (name from the daemon)``, or the
``--token-file`` path, which must not exist. The code is redeemed only after
confirmation, at the point where ``--read-token`` writes its file, so
``--dry-run`` never consumes it, and a plan with nothing to re-point stops
before redeeming. After redemption the plan is rebuilt with the real path.
When several clients would be re-pointed, the plan warns that they will
share one principal. The code is never printed.

Exit codes: 0 done or already current; 1 a write failed and was rolled
back; 2 usage, an invalid URL, or no confirmation; 3 no registration it can
write (none found, or only ``manual`` ones); 4 the target refused
verification or the pairing code, nothing written (a pairing whose outcome
is unknown keeps, and names, its token file); 5 applied, but the post-apply
check failed.

Standard library only at import time, like the other client modes: the MCP
handshake runs in a subprocess, and the shim (httpx) is imported only to
probe ``/health``.
"""
from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass, field
import getpass
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from pseudolife_memory import client_config, codex_connection, pair_cli, runtimes
from pseudolife_memory.credentials import CredentialError, CredentialProvider
from pseudolife_memory.daemon_url import DEFAULT_URL, _is_loopback_url
from pseudolife_memory.principals import normalize_pairing_code

CLIENTS = ("claude-code", "codex", "claude-desktop", "gemini")
SERVER = runtimes.SERVER
DESKTOP_SERVER = runtimes.DESKTOP_SERVER
URL_KEY = "PSEUDOLIFE_MCP_DAEMON_URL"
FILE_KEY = "PSEUDOLIFE_MCP_TOKEN_FILE"
TOKEN_KEY = "PSEUDOLIFE_MCP_TOKEN"
NO_SPAWN_KEY = "PSEUDOLIFE_MCP_NO_SPAWN"
STATE_KEY = "PSEUDOLIFE_AGENT_STATE"
_TRUTHY = {"1", "true", "yes", "on"}

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NOTHING = 3
EXIT_REFUSED = 4
EXIT_UNVERIFIED = 5

HANDSHAKE_TIMEOUT_S = 20.0

#: The plan's name for the token file a pairing code will create.
PAIRED_TOKEN_FILE = "~/.pseudolife-mcp/<principal>.token (name from the daemon)"

_BOARD_HINT = ("to admit this principal, list it under coordination.allowed_principals in the "
               "daemon's config.yaml, or invite this machine with `pseudolife-mcp invite <machine>` "
               "on the daemon host (an invited principal is admitted to the board)")

_PLAIN_HTTP = ("WARNING: {url} is plain HTTP: the link itself is unencrypted, so it must be a "
               "private network such as a tailnet, or a TLS reverse proxy must front the daemon.")


# ── seams (the tests replace these) ──────────────────────────────────────────

def health_timeout() -> float:
    """The shim's own budget for a remote daemon's ``/health``."""
    from pseudolife_memory.shim import _REMOTE_PROBE_TIMEOUT_S
    return _REMOTE_PROBE_TIMEOUT_S


def probe_health(url: str, timeout: float) -> dict | None:
    from pseudolife_memory.shim import probe_health as probe
    return probe(url, timeout=timeout)


def credential_valid(url: str, token: str) -> bool:
    """An authenticated request that follows no redirects (the installers'
    check of a client credential)."""
    return codex_connection.installer_credential_valid(url, token)


def board_line(url: str, token: str | None) -> str:
    from pseudolife_memory.board_status import board_status
    return board_status(url, token)[1]


_HANDSHAKE_SCRIPT = (
    "import asyncio, json, sys\n"
    "from pseudolife_memory.doctor_cli import _handshake\n"
    "print(json.dumps(asyncio.run(asyncio.wait_for(_handshake(), float(sys.argv[1])))))\n")


def subprocess_handshake(url: str, credential: tuple, timeout: float = HANDSHAKE_TIMEOUT_S) -> dict:
    """``doctor``'s MCP handshake (initialize + tools/list through a real
    shim) in a child whose environment names only the target: this URL,
    this credential, ``PSEUDOLIFE_MCP_NO_SPAWN=1``, and
    ``PSEUDOLIFE_AGENT_COORDINATION=0`` so verifying registers no agent
    address on the target's board. Any other credential in this
    environment is scrubbed. The child's output is never relayed: it can
    carry transport details."""
    kind, value = credential
    env = {key: item for key, item in os.environ.items()
           if key not in (URL_KEY, FILE_KEY, TOKEN_KEY, "PSEUDOLIFE_MCP_TOKENS", STATE_KEY)}
    env.update({URL_KEY: url, NO_SPAWN_KEY: "1", "PSEUDOLIFE_AGENT_COORDINATION": "0",
                "PYTHONIOENCODING": "utf-8"})
    if kind == "file":
        env[FILE_KEY] = value
    elif kind == "literal":
        env[TOKEN_KEY] = value
    try:
        proc = subprocess.run([sys.executable, "-c", _HANDSHAKE_SCRIPT, str(timeout)], env=env,
                              capture_output=True, text=True, timeout=timeout + 15,
                              stdin=subprocess.DEVNULL, errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": type(exc).__name__}
    if proc.returncode != 0:
        return {"ok": False, "error": f"the handshake exited {proc.returncode}"}
    try:
        result = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": "no handshake report"}
    if not (result.get("instructions_present") and result.get("tool_count")):
        return {"ok": False, "error": "the daemon listed no tools"}
    return {"ok": True, "tool_count": result["tool_count"]}


handshake = subprocess_handshake


@contextlib.contextmanager
def open_codex(home: Path):
    """Codex's app-server for ``home``, run from an empty directory so that
    no project layer applies; yields ``(client, cwd)``."""
    executable = codex_connection.resolve_codex()
    with tempfile.TemporaryDirectory(prefix="pseudolife-connect-") as temporary:
        cwd = Path(temporary)
        with codex_connection.codex(executable, home, cwd) as client:
            yield client, cwd


def list_processes():
    return runtimes.list_processes()


def user_environment_url() -> str | None:
    """``PSEUDOLIFE_MCP_DAEMON_URL`` in the Windows User-scope environment."""
    if os.name != "nt":
        return None
    from pseudolife_memory.unattended_update import _user_environment
    return _user_environment(URL_KEY)


def _flag_url(text: str) -> str | None:
    found = re.search(r"--daemon-url[= ]+\"?([^\s\"<]+)", text or "")
    return found.group(1) if found else None


def scheduled_update() -> dict | None:
    """The unattended-update schedule on this machine: where it is, its time
    of day (``HH:MM`` or ``None``) and the daemon URL its run uses; ``None``
    when there is none."""
    from pseudolife_memory import unattended_update as unattended
    if sys.platform.startswith("win"):
        code, out = runtimes.run_cli(["schtasks", "/Query", "/TN", unattended.TASK_NAME, "/XML"],
                                     timeout=30)
        if code != 0:
            return None
        command = " ".join(re.findall(r"<(?:Command|Arguments)>(.*?)</", out, re.S))
        start = re.search(r"<StartBoundary>[^<T]*T(\d\d:\d\d)", out)
        return {"where": f"the scheduled task '{unattended.TASK_NAME}'",
                "time": start.group(1) if start else None,
                "url": _flag_url(command) or unattended._user_environment(URL_KEY) or DEFAULT_URL}
    if sys.platform.startswith("linux"):
        unit = unattended._systemd_dir() / f"{unattended.UNIT_NAME}.service"
        try:
            service = unit.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            timer = unit.with_suffix(".timer").read_text(encoding="utf-8")
        except OSError:
            timer = ""
        exec_start = re.search(r"(?m)^ExecStart=(.*)$", service)
        environment = re.search(rf"(?m)^Environment={URL_KEY}=(.*)$", service)
        at = re.search(r"(?m)^OnCalendar=\S+ (\d\d:\d\d)", timer)
        return {"where": str(unit), "time": at.group(1) if at else None,
                "url": (_flag_url(exec_start.group(1) if exec_start else "")
                        or (environment.group(1).strip() if environment else None) or DEFAULT_URL)}
    return None


def interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _write_json(path: Path, data: dict) -> None:
    client_config._write_json(path, data)


# ── the plan ────────────────────────────────────────────────────────────────

@dataclass
class Context:
    url: str
    remote: bool
    token_file: str | None
    clients: tuple
    env: dict
    layout: runtimes.Layout | None = None
    registrations: list = field(default_factory=list)

    @property
    def shim(self) -> str:
        if self.layout is not None and self.layout.launcher.is_file():
            return str(self.layout.launcher)
        return "<shim path>"


def _row(client, place, file, key, state, *, detail="", changes=None, notes=None) -> dict:
    return {"client": client, "place": place, "file": str(file) if file else None, "key": key,
            "state": state, "changes": changes or {}, "detail": detail, "notes": notes or [],
            "backup": None, "created": False}


def _manual(client, place, file, key, detail) -> dict:
    return _row(client, place, file, key, "manual", detail=detail)


def _shown_url(value) -> str:
    """A URL for display: scheme and host[:port] only, so credentials, a
    path or a query in a rejected or foreign URL are never echoed."""
    try:
        parsed = urlsplit(str(value))
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return "<unreadable URL>"
    if not parsed.scheme or not host:
        return "<unreadable URL>"
    host = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme}://{host}" + (f":{port}" if port else "")


def _normalised(url: str) -> str:
    try:
        return codex_connection._validated_daemon_url(url)
    except codex_connection.SetupError:
        return url


def _changes(env: dict, edits: dict) -> dict:
    """``{key: [old, new]}`` for display: a literal token is never shown."""
    shown = {}
    for key, new in sorted(edits.items()):
        old = env.get(key)
        if key == TOKEN_KEY:
            shown[key] = ["<literal token>" if old else None, None if new is None else "<literal token>"]
        elif key == URL_KEY:
            shown[key] = [None if old is None else _shown_url(old), new]
        else:
            shown[key] = [None if old is None else str(old), new]
    return shown


def _after(env: dict, edits: dict) -> dict:
    result = dict(env)
    for key, value in edits.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def _credential(env: dict) -> tuple | None:
    """The credential the shim would send with this env (the file wins)."""
    if env.get(FILE_KEY):
        return ("file", str(env[FILE_KEY]))
    if env.get(TOKEN_KEY):
        return ("literal", str(env[TOKEN_KEY]))
    return None


def _registration_edits(env: dict, ctx: Context) -> dict:
    edits = {}
    if env.get(URL_KEY) != ctx.url:
        edits[URL_KEY] = ctx.url
    if ctx.token_file:
        if env.get(FILE_KEY) != ctx.token_file:
            edits[FILE_KEY] = ctx.token_file
        if TOKEN_KEY in env:
            edits[TOKEN_KEY] = None
    if ctx.remote and str(env.get(NO_SPAWN_KEY, "")).strip().lower() not in _TRUTHY:
        edits[NO_SPAWN_KEY] = "1"
    return edits


def _settings_edits(env: dict, ctx: Context, fallback_file: str | None) -> dict:
    """The plugin hooks' copy in ``settings.json``: the URL, and the token
    file (the given one; else the one it names; else, when it names no
    credential at all, the Claude Code registration's)."""
    edits = {}
    has_credential = bool(env.get(FILE_KEY) or env.get(TOKEN_KEY))
    wanted_file = ctx.token_file or (None if has_credential else fallback_file)
    if wanted_file and env.get(FILE_KEY) != wanted_file:
        edits[FILE_KEY] = wanted_file
    if ctx.token_file and TOKEN_KEY in env:
        edits[TOKEN_KEY] = None
    # The hooks default to DEFAULT_URL, so an absent key already names it,
    # unless a token file is added (beside a managed Codex connection the
    # hooks refuse an explicit token file without the matching URL).
    if env.get(URL_KEY) != ctx.url and (URL_KEY in env or ctx.url != DEFAULT_URL or FILE_KEY in edits):
        edits[URL_KEY] = ctx.url
    return edits


def _notes(env: dict, ctx: Context, edits: dict, registration=None) -> list[str]:
    notes = []
    after = _after(env, edits)
    if after.get(TOKEN_KEY):
        notes.append(f"keeps its literal {TOKEN_KEY}: pass --token-file to replace it with an "
                     "owner-only token file")
    if env.get(STATE_KEY):
        notes.append(f"sets a fixed {STATE_KEY}, which is not keyed by the daemon URL: the "
                     "coordination adapter refuses a state bound to another bank; remove it and "
                     "use PSEUDOLIFE_AGENT_STATE_DIR")
    if (registration is not None and ctx.layout is not None
            and not runtimes.registers_launcher(registration, ctx.layout)):
        notes.append(f"runs {registration.command}, not the shim launcher; connect leaves the "
                     "command alone (`pseudolife-mcp update --clients-only`, or `python "
                     "ops/shim_runtime.py migrate` from a checkout, moves it)")
    return notes


def _is_shim(entry) -> bool:
    return (isinstance(entry, dict) and isinstance(entry.get("command"), str)
            and entry.get("type") in (None, "stdio"))


def _read_json(path: Path) -> tuple[dict | None, str | None]:
    if not path.exists():
        return None, None
    try:
        return client_config._load_json(path), None
    except client_config.HelperError as exc:
        return None, str(exc)


def _register_command(client: str, ctx: Context) -> str:
    token = ctx.token_file or "<token file>"
    shim = ctx.shim
    no_spawn = [f"{NO_SPAWN_KEY}=1"] if ctx.remote else []
    pairs = no_spawn + [f"{FILE_KEY}={token}", f"{URL_KEY}={ctx.url}"]
    if client == "claude-code":
        flags = " ".join(f"-e {pair}" for pair in ["PSEUDOLIFE_WRITER_ID=claude-code", *pairs])
        return f"claude mcp add --scope user {SERVER} {flags} -- {shim}"
    if client == "codex":
        flags = " ".join(f"--env {pair}" for pair in ["PSEUDOLIFE_WRITER_ID=codex", *pairs])
        return f"codex mcp add {SERVER} {flags} -- {shim}"
    if client == "gemini":
        flags = " ".join(f"-e {pair}" for pair in ["PSEUDOLIFE_WRITER_ID=gemini", *pairs])
        return f"gemini mcp add -s user {flags} {SERVER} {shim}"
    return (f"python ops/register_claude_desktop.py --command {shim} --daemon-url {ctx.url} "
            f"--token-file {token} (from a checkout; the installers run it for --client "
            "claude-desktop)")


def _absent(client: str, ctx: Context) -> dict:
    return _row(client, "registration", None, None, "absent",
                detail=f"no registration; connect never creates one. To register: "
                       f"{_register_command(client, ctx)}")


def _json_registration(ctx: Context, client: str, path: Path, key: str, entry: dict) -> dict:
    env = entry.get("env")
    if env is None:
        env = {}
    if not isinstance(env, dict):
        return _manual(client, "registration", path, key,
                       f"mcpServers.{key}.env is not a JSON object; fix it by hand, then re-run")
    edits = _registration_edits(env, ctx)
    registration = runtimes.Registration(client, path, key, entry["command"])
    row = _row(client, "registration", path, key, "change" if edits else "current",
               changes=_changes(env, edits), notes=_notes(env, ctx, edits, registration))
    row["_json"] = {"path": path, "pointer": ("mcpServers", key, "env"), "edits": edits}
    row["_credential"] = _credential(_after(env, edits))
    return row


def _non_shim(client: str, path: Path, key: str, entry) -> dict:
    kind = entry.get("type") if isinstance(entry, dict) else None
    return _manual(client, "registration", path, key,
                   f"not a stdio registration of the shim (type: {kind or 'unknown'}); connect "
                   f"rewrites only the shim's own. Re-register it: {SERVER} over stdio")


def _claude_rows(ctx: Context) -> list[dict]:
    path = runtimes._claude_config_file(ctx.env)
    data, error = _read_json(path)
    if error:
        return [_manual("claude-code", "registration", path, SERVER, error)]
    rows = []
    servers = (data or {}).get("mcpServers")
    entry = servers.get(SERVER) if isinstance(servers, dict) else None
    if entry is not None:
        found = [r for r in ctx.registrations if r.client == "claude-code" and r.key == SERVER]
        rows.append(_json_registration(ctx, "claude-code", path, SERVER, entry) if found and _is_shim(entry)
                    else _non_shim("claude-code", path, SERVER, entry))
    projects = (data or {}).get("projects")
    for project, record in (projects.items() if isinstance(projects, dict) else ()):
        scoped = record.get("mcpServers") if isinstance(record, dict) else None
        if isinstance(scoped, dict) and SERVER in scoped:
            rows.append(_manual("claude-code", "project", path,
                                f'projects["{project}"].mcpServers.{SERVER}',
                                "a project-scoped registration: connect does not rewrite project "
                                "scopes. Edit its env by hand, or remove it so the user-scope "
                                "registration applies"))
    local = Path.cwd() / ".mcp.json"
    local_data, local_error = _read_json(local)
    scoped = (local_data or {}).get("mcpServers")
    if local_error or (isinstance(scoped, dict) and SERVER in scoped):
        rows.append(_manual("claude-code", "project", local, f"mcpServers.{SERVER}",
                            local_error or "a project-scoped registration in this directory's "
                                           ".mcp.json: connect does not rewrite project scopes; "
                                           "edit it by hand"))
    registration = next((r for r in rows if r["place"] == "registration"), None)
    if registration is None:
        rows.insert(0, _absent("claude-code", ctx))
    elif registration["state"] in ("current", "change"):
        rows.insert(rows.index(registration) + 1, _settings_row(ctx, registration))
    return rows


def _settings_row(ctx: Context, registration: dict) -> dict:
    base = Path(ctx.env["CLAUDE_CONFIG_DIR"]) if ctx.env.get("CLAUDE_CONFIG_DIR") else runtimes.home(ctx.env) / ".claude"
    path = base / "settings.json"
    data, error = _read_json(path)
    if error:
        return _manual("claude-code", "settings", path, "env", error)
    env = (data or {}).get("env")
    if env is None:
        env = {}
    if not isinstance(env, dict):
        return _manual("claude-code", "settings", path, "env",
                       "settings.json 'env' is not an object; fix it by hand, then re-run")
    credential = registration.get("_credential")
    fallback = credential[1] if credential and credential[0] == "file" else None
    edits = _settings_edits(env, ctx, fallback)
    row = _row("claude-code", "settings", path, "env", "change" if edits else "current",
               changes=_changes(env, edits),
               notes=[f"keeps its literal {TOKEN_KEY}: pass --token-file to replace it"]
               if _after(env, edits).get(TOKEN_KEY) else [])
    row["_json"] = {"path": path, "pointer": ("env",), "edits": edits}
    row["_credential"] = _credential(_after(env, edits))
    return row


def _desktop_rows(ctx: Context) -> list[dict]:
    rows = []
    for path in runtimes.desktop_config_files(ctx.env):
        data, error = _read_json(path)
        if error:
            rows.append(_manual("claude-desktop", "registration", path, DESKTOP_SERVER, error))
            continue
        servers = (data or {}).get("mcpServers")
        if not isinstance(servers, dict):
            continue
        entry = servers.get(DESKTOP_SERVER)
        if entry is not None:
            rows.append(_json_registration(ctx, "claude-desktop", path, DESKTOP_SERVER, entry)
                        if _is_shim(entry) else _non_shim("claude-desktop", path, DESKTOP_SERVER, entry))
        legacy = servers.get(SERVER)
        if legacy is not None:
            env = legacy.get("env") if isinstance(legacy, dict) else None
            own = isinstance(env, dict) and env.get("PSEUDOLIFE_WRITER_ID") == "claude-desktop"
            rows.append(_manual(
                "claude-desktop", "registration", path, SERVER,
                (f"the entry's old name {SERVER}: the Desktop registrar moves it to {DESKTOP_SERVER} "
                 "(ops/install.* --client claude-desktop, or python ops/register_claude_desktop.py); "
                 "re-run connect after that") if own else
                (f"a {SERVER} entry the Desktop registrar did not write (its env does not set "
                 "PSEUDOLIFE_WRITER_ID=claude-desktop); left as it is")))
    return rows or [_absent("claude-desktop", ctx)]


def _gemini_rows(ctx: Context) -> list[dict]:
    path = runtimes.home(ctx.env) / ".gemini" / "settings.json"
    data, error = _read_json(path)
    if error:
        return [_manual("gemini", "registration", path, SERVER, error)]
    servers = (data or {}).get("mcpServers")
    entry = servers.get(SERVER) if isinstance(servers, dict) else None
    if entry is None:
        return [_absent("gemini", ctx)]
    if not _is_shim(entry):
        return [_non_shim("gemini", path, SERVER, entry)]
    return [_json_registration(ctx, "gemini", path, SERVER, entry)]


def _codex_rows(ctx: Context) -> list[dict]:
    path = runtimes._codex_config_file(ctx.env)
    home = path.parent.expanduser().resolve()
    registration = next((r for r in ctx.registrations if r.client == "codex"), None)
    if registration is None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        data = runtimes._toml_loads(text) if text else None
        server = ((data.get("mcp_servers") or {}).get(SERVER)
                  if isinstance(data, dict) and isinstance(data.get("mcp_servers"), dict) else None)
        if isinstance(server, dict):
            return [_manual("codex", "registration", path, SERVER,
                            "not a stdio registration of the shim (a url server); connect rewrites "
                            "only the shim's own. Re-register it with codex mcp add")]
        if text and data is None and f"mcp_servers.{SERVER}" in text:
            return [_manual("codex", "registration", path, SERVER,
                            "config.toml could not be read here (TOML needs Python 3.11, or tomli); "
                            "edit [mcp_servers.pseudolife-memory.env] by hand")]
        return [_absent("codex", ctx)]

    def manual(reason: str) -> list[dict]:
        return [_manual("codex", "registration", path, SERVER,
                        f"{reason} Edit [mcp_servers.{SERVER}.env] where it is set, and "
                        f"{codex_connection.connection_path(home)} to match")]

    try:
        with open_codex(home) as (client, cwd):
            config = client.rpc("config/read", {"includeLayers": True, "cwd": str(cwd)})
    except codex_connection.SetupError as exc:
        return manual(str(exc))
    except Exception as exc:  # noqa: BLE001 - never serialize the app-server's errors
        return manual(f"Codex's app-server did not answer ({type(exc).__name__}).")
    try:
        env, new_env, literal = codex_connection.plan_replace(
            config, home, ctx.url, token_file=ctx.token_file, no_spawn=ctx.remote)
        recorded = codex_connection.read_connection(codex_connection.connection_path(home))
    except codex_connection.SetupError as exc:
        return manual(str(exc))
    edits = {key: new_env.get(key) for key in set(env) | set(new_env) if env.get(key) != new_env.get(key)}
    notes = _notes(env, ctx, edits, registration)
    if literal is not None:
        notes.append(f"its literal {TOKEN_KEY} moves into Codex's token copy "
                     f"{codex_connection.token_copy_path(home)}, as the Codex credential writer does")
    row = _row("codex", "registration", path, SERVER, "change" if edits else "current",
               changes=_changes(env, edits), notes=notes)
    row["_codex"] = {"home": home}
    row["_credential"] = ("literal", literal) if literal is not None else _credential(new_env)
    target = new_env.get(FILE_KEY) or ""
    wanted = (ctx.url, target)
    connection = codex_connection.connection_path(home)
    old_url, old_file = recorded or (None, None)
    conn_changes = {}
    if old_url != ctx.url:
        conn_changes["daemon_url"] = [None if old_url is None else _shown_url(old_url), ctx.url]
    if old_file != target:
        conn_changes["token_file"] = [old_file, target]
    conn = _row("codex", "connection", connection, None, "current" if recorded == wanted else "change",
                changes=conn_changes,
                notes=["read by the plugin hooks in Codex context"])
    conn["_codex"] = {"home": home}
    return [row, conn]


def _environment_rows(ctx: Context) -> list[dict]:
    rows = []
    ambient = os.environ.get(URL_KEY)
    if ambient and _normalised(ambient) != ctx.url:
        rows.append(_manual("environment", "process", None, URL_KEY,
                            f"this shell sets {URL_KEY}={_shown_url(ambient)}: sessions started from it, and the "
                            f"plugin hooks, may take it over the registrations. Unset it or set it to "
                            f"{ctx.url}"))
    user = user_environment_url()
    if user and _normalised(user) != ctx.url:
        rows.append(_manual("environment", "user", None, URL_KEY,
                            f"the Windows User-scope environment sets {URL_KEY}={_shown_url(user)}, which every new "
                            f"process inherits. Change it in PowerShell: [Environment]::"
                            f"SetEnvironmentVariable(\"{URL_KEY}\", \"{ctx.url}\", \"User\")"))
    return rows


def _schedule_rows(ctx: Context) -> list[dict]:
    try:
        found = scheduled_update()
    except Exception:  # noqa: BLE001 - a schedule that cannot be read is not one to report
        found = None
    if not found:
        return []
    if _normalised(found["url"]) == ctx.url:
        return [_row("unattended-update", "schedule", None, None, "current", detail=found["where"])]
    when = found.get("time") or "HH:MM"
    if ctx.remote:
        fix = (f"the daemon now runs on another machine, and an unattended update only updates a daemon "
               f"on this one: remove it here with `pseudolife-mcp update --unschedule`, and schedule it "
               f"on the daemon's host")
    else:
        flag = f" --daemon-url {ctx.url}" if ctx.url != DEFAULT_URL else ""
        fix = f"re-schedule it: pseudolife-mcp update --schedule {when}{flag}"
    return [_manual("unattended-update", "schedule", None, None,
                    f"{found['where']} runs against {_shown_url(found['url'])}; connect does not re-run the "
                    f"schedule step: {fix}")]


def discover(ctx: Context) -> list[dict]:
    ctx.registrations = runtimes.find_registrations(ctx.env)
    rows = []
    for client, finder in (("claude-code", _claude_rows), ("codex", _codex_rows),
                           ("claude-desktop", _desktop_rows), ("gemini", _gemini_rows)):
        if client in ctx.clients:
            rows += finder(ctx)
    rows += _environment_rows(ctx)
    if set(ctx.clients) == set(CLIENTS):
        rows += _schedule_rows(ctx)
    return rows


# ── verify ──────────────────────────────────────────────────────────────────

def _shown(credential: tuple, label: str) -> str:
    kind, value = credential
    if kind == "file":
        return f"token file {value}"
    if kind == "literal":
        return f"the literal {TOKEN_KEY} of {label}"
    return "no token"


def verify(ctx: Context, rows: list[dict], health: dict) -> tuple[list[dict], list[str], str | None]:
    """``(results, warnings, failure)`` for every distinct credential the plan
    points at the target; ``failure`` names the first one refused."""
    auth = health.get("auth") is not False
    credentials: dict[tuple, str] = {}
    for row in rows:
        if row["state"] not in ("current", "change") or "_credential" not in row:
            continue
        credential = row["_credential"]
        if credential is None:
            if auth and row["place"] == "registration":
                return [], [], (f"{row['client']}: the registration in {row['file']} has no credential and "
                                f"the daemon requires one; pass --token-file <owner-only file holding "
                                f"its token>")
            continue
        credentials.setdefault(credential, f"{row['client']} {row['place']} ({row['file']})")
    if not credentials:
        credentials[("none", None)] = "no credential"
    results, warnings = [], []
    for credential, label in credentials.items():
        kind, value = credential
        shown = _shown(credential, label)
        token = None
        if kind == "file":
            check = client_config.check_token_file(Path(value))
            if check["status"] != "ready":
                return [], [], f"{shown}: {check['recovery']}"
            token = CredentialProvider(path=value).snapshot().token
        elif kind == "literal":
            try:
                token = CredentialProvider(token=value).snapshot().token
            except CredentialError:
                return [], [], f"{shown} is not a well-formed token"
        if token is not None and not credential_valid(ctx.url, token):
            return [], [], (f"the daemon at {ctx.url} refused {shown} (an authenticated request that "
                            f"follows no redirects did not succeed)")
        answer = handshake(ctx.url, credential)
        if not answer.get("ok"):
            return [], [], (f"the MCP handshake with {shown} failed ({answer.get('error', 'no tools')}); "
                            f"check the daemon's MCP access and compare daemon and shim versions")
        board = board_line(ctx.url, token)
        results.append({"credential": shown, "tools": answer.get("tool_count"), "board": board})
        if not board.startswith("on"):
            warnings.append(f"the agent board for {shown}: {board}. Memory works without the board; "
                            + _BOARD_HINT)
    return results, warnings, None


# ── apply ───────────────────────────────────────────────────────────────────

class _WriteFailed(Exception):
    def __init__(self, path: Path, reason: str):
        super().__init__(reason)
        self.path = path
        self.reason = reason


def _bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _apply_json(row: dict, steps: list[dict]) -> None:
    spec = row["_json"]
    path = spec["path"]
    record = {"path": path, "original": _bytes(path), "written": None, "backup": None}
    steps.append(record)
    try:
        data = client_config._load_json(path)
        *parents, last = spec["pointer"]
        node = data
        for part in parents:
            node = node[part]
        env = node.get(last)
        if env is None:
            env = {}
        if not isinstance(env, dict):
            raise client_config.HelperError(f"{'.'.join(spec['pointer'])} is not a JSON object")
        node[last] = _after(env, spec["edits"])
        if record["original"] is not None:
            record["backup"] = row["backup"] = client_config._backup(path)
        else:
            row["created"] = True
        _write_json(path, data)
        record["written"] = _bytes(path)
        if client_config._load_json(path) != data:
            raise _WriteFailed(path, "the read-back differs from what was written")
    except _WriteFailed:
        raise
    except (OSError, KeyError, TypeError, client_config.HelperError, CredentialError) as exc:
        record["written"] = _bytes(path)
        detail = str(exc) if isinstance(exc, client_config.HelperError) else type(exc).__name__
        raise _WriteFailed(path, detail) from exc


def _apply_codex(ctx: Context, rows: list[dict], steps: list[dict]) -> None:
    home = rows[0]["_codex"]["home"]
    paths = [home / "config.toml", codex_connection.connection_path(home),
             codex_connection.token_copy_path(home)]
    records = [{"path": p, "original": _bytes(p), "written": None, "backup": None} for p in paths]
    steps.extend(records)
    made: dict[str, str] = {}
    try:
        with open_codex(home) as (client, cwd):
            result = codex_connection.replace_connection(
                client, home, cwd, ctx.url, token_file=ctx.token_file, no_spawn=ctx.remote,
                backups=made)
    except Exception as exc:  # noqa: BLE001 - the SetupError text is safe; nothing else is shown
        for record, key in zip(records, ("config", "connection", "token")):
            record["written"] = _bytes(record["path"])
            record["backup"] = made.get(key)
        reason = str(exc) if isinstance(exc, codex_connection.SetupError) else type(exc).__name__
        raise _WriteFailed(paths[0], reason) from exc
    for record in records:
        record["written"] = _bytes(record["path"])
    records[0]["backup"] = result["backup"]
    records[1]["backup"] = result["connection_backup"]
    records[2]["backup"] = result["token_backup"]
    created = set(result["created"])
    for row in rows:
        if row["place"] == "registration":
            row["backup"] = result["backup"]
        else:
            row["backup"] = result["connection_backup"]
            row["created"] = str(paths[1]) in created
        if result["token_backup"] and row["place"] == "registration":
            row["notes"].append(f"token copy backup: {result['token_backup']}")


def _rollback(steps: list[dict], created: list[Path]) -> list[dict]:
    """Put back every file this run wrote and still holds what it wrote;
    leave (and name) one that changed since; remove the files it created."""
    outcome = []
    for record in reversed(steps):
        path, original, written = record["path"], record["original"], record["written"]
        if written == original:
            if record["backup"]:
                outcome.append({"file": str(path), "state": "unchanged", "backup": record["backup"],
                                "detail": "not changed by this run; its backup can be deleted"})
            continue
        if _bytes(path) != written:
            outcome.append({"file": str(path), "state": "left", "backup": record["backup"],
                            "detail": "changed under the edit since this run wrote it; left as it is "
                                      "now, backup kept"})
            continue
        try:
            if original is None:
                path.unlink()
                outcome.append({"file": str(path), "state": "removed"})
            else:
                client_config._write_private(path, original)
                outcome.append({"file": str(path), "state": "restored", "backup": record["backup"]})
        except OSError as exc:
            outcome.append({"file": str(path), "state": "failed", "backup": record["backup"],
                            "detail": f"could not be restored ({type(exc).__name__}); restore it from "
                                      f"its backup"})
    for path in created:
        try:
            path.unlink()
            outcome.append({"file": str(path), "state": "removed"})
        except FileNotFoundError:
            pass
        except OSError as exc:
            outcome.append({"file": str(path), "state": "failed",
                            "detail": f"could not be removed ({type(exc).__name__})"})
    return outcome


def apply(ctx: Context, rows: list[dict], created: list[Path]) -> tuple[list[dict], dict | None]:
    """Write every ``change`` row; ``(steps, failure)``, failure carrying the
    file that failed and what the rollback did."""
    steps: list[dict] = []
    try:
        codex_rows = [row for row in rows if "_codex" in row]
        codex_done = False
        for row in rows:
            if row["state"] != "change":
                continue
            if "_json" in row:
                _apply_json(row, steps)
            elif "_codex" in row and not codex_done:
                _apply_codex(ctx, codex_rows, steps)
                codex_done = True
    except _WriteFailed as failure:
        return steps, {"file": str(failure.path), "reason": failure.reason,
                       "rollback": _rollback(steps, created)}
    return steps, None


# ── the command ─────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pseudolife-mcp connect",
        description="Point every client registration on this machine at a daemon URL, replacing old "
                    "values, after proving the daemon accepts them. With --code, first join the bank "
                    "with a pairing code from `pseudolife-mcp invite`. Exit codes: 0 done or current, "
                    "1 a write failed (rolled back), 2 usage or not confirmed, 3 no registration it "
                    "can write, 4 verification or the pairing code refused (nothing written), 5 "
                    "applied but the post-apply check failed.")
    parser.add_argument("daemon_url", metavar="daemon-url",
                        help="the daemon's origin, e.g. http://100.64.0.2:8765 (no path)")
    parser.add_argument("--token-file", default=None,
                        help="owner-only token file every selected client uses from now on "
                             "(default: each keeps the one it names)")
    parser.add_argument("--read-token", action="store_true",
                        help="read the token without echo (or from stdin) and create --token-file "
                             "owner-only; an existing file is refused")
    parser.add_argument("--code", default=None,
                        help="a pairing code from `pseudolife-mcp invite` on the daemon host: after "
                             "confirmation, pair (a new owner-only token file, "
                             "~/.pseudolife-mcp/<principal>.token or --token-file) and connect "
                             "with it; --dry-run never redeems it")
    parser.add_argument("--read-code", action="store_true",
                        help="as --code, reading the code from stdin (without echo on a terminal)")
    parser.add_argument("--client", default="all",
                        help="comma-separated: claude-code, codex, claude-desktop, gemini, or all "
                             "(default)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the plan; write nothing and send no token")
    parser.add_argument("--yes", action="store_true", help="apply without asking")
    parser.add_argument("--json", action="store_true", help="one JSON report on stdout")
    return parser


def _clients(value: str) -> tuple | None:
    names = [name.strip() for name in value.split(",") if name.strip()]
    if not names or names == ["all"]:
        return CLIENTS
    if any(name not in CLIENTS for name in names):
        return None
    return tuple(client for client in CLIENTS if client in names)


def _public(row: dict) -> dict:
    return {key: value for key, value in row.items() if not key.startswith("_")}


class _Report:
    def __init__(self, as_json: bool):
        self.as_json = as_json
        self.data = {"url": None, "remote": None, "dry_run": False, "daemon": None, "rows": [],
                     "verification": [], "warnings": [], "notes": [], "restart": None,
                     "rollback": [], "error": None, "exit": None}

    def say(self, line: str = "") -> None:
        if not self.as_json:
            print(line)

    def warn(self, line: str) -> None:
        self.data["warnings"].append(line)
        if not self.as_json:
            print(line, file=sys.stderr)

    def note(self, line: str) -> None:
        self.data["notes"].append(line)
        self.say(line)

    def fail(self, code: int, line: str) -> int:
        self.data["error"] = line
        if not self.as_json:
            print(f"connect: {line}", file=sys.stderr)
        return self.finish(code)

    def finish(self, code: int) -> int:
        self.data["exit"] = code
        if self.as_json:
            print(json.dumps(self.data, indent=2, default=str))
        return code

    def plan(self, rows: list[dict]) -> None:
        self.data["rows"] = [_public(row) for row in rows]
        for row in rows:
            where = " ".join(part for part in (row["file"], f"[{row['key']}]" if row["key"] else "") if part)
            self.say(f"  {row['state']:<8} {row['client']} {row['place']}" + (f": {where}" if where else ""))
            for key, (old, new) in row["changes"].items():
                self.say(f"             {key}: {old if old is not None else '(unset)'} -> "
                         f"{new if new is not None else '(removed)'}")
            if row["detail"]:
                self.say(f"             {row['detail']}")
            for note in row["notes"]:
                self.say(f"             note: {note}")


def _places(count: int) -> str:
    return f"{count} place{'s' if count != 1 else ''} need{'s' if count == 1 else ''}"


def _ask(question: str) -> bool:
    print(question, end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline()
    return answer.strip().lower() in ("y", "yes")


def _code_line() -> str:
    """One line holding the pairing code (without echo on a terminal)."""
    if interactive():
        return getpass.getpass("pairing code: ")
    return sys.stdin.readline()


def _token_stream():
    if interactive():
        return io.BytesIO(getpass.getpass("token: ").encode("utf-8"))
    return sys.stdin.buffer


def _restart(ctx: Context, rows: list[dict]) -> dict:
    desktop = any(row["client"] == "claude-desktop" and row["state"] == "change" for row in rows)
    try:
        table = list_processes()
    except OSError:
        table = None
    if table is None or ctx.layout is None:
        pids, detail = [], ("the process table could not be read: restart every running Claude Code, "
                            "Codex and Gemini CLI session")
    else:
        pids = sorted(runtimes.processes_inside(table, ctx.layout.root))
        detail = (f"{len(pids)} process{'es' if len(pids) != 1 else ''} run a shim runtime "
                  f"({', '.join(map(str, pids))}): restart those sessions; each keeps the old daemon "
                  f"until it restarts" if pids else "no process runs a shim runtime")
    return {"pids": pids, "claude_desktop": desktop, "detail": detail}


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(sys.argv[2:] if argv is None else argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    report = _Report(args.json)
    try:
        url = codex_connection._validated_daemon_url(args.daemon_url)
    except codex_connection.SetupError:
        return report.fail(EXIT_USAGE, f"the daemon URL given ({_shown_url(args.daemon_url)}) is not "
                                       "an http(s) origin: give one such as http://100.64.0.2:8765, "
                                       "with no path, query, fragment or credentials")
    clients = _clients(args.client)
    if clients is None:
        return report.fail(EXIT_USAGE, f"--client takes {', '.join(CLIENTS)} or all (got {args.client!r})")
    pairing = args.code is not None or args.read_code
    if pairing and args.read_token:
        return report.fail(EXIT_USAGE, "--code/--read-code pair a new token and --read-token stores one "
                                       "you already have; pass one of them")
    if args.code is not None and args.read_code:
        return report.fail(EXIT_USAGE, "give the pairing code with --code or --read-code, not both")
    if args.read_token and not args.token_file:
        return report.fail(EXIT_USAGE, "--read-token creates the file --token-file names; pass both")
    token_file = os.path.abspath(os.path.expanduser(args.token_file)) if args.token_file else None
    if (args.read_token or pairing) and token_file and (os.path.exists(token_file)
                                                         or os.path.islink(token_file)):
        flag = "--read-token" if args.read_token else "pairing"
        return report.fail(EXIT_USAGE, f"{token_file} already exists; {flag} creates a token file "
                                       "but never replaces one")
    code = None
    if pairing:
        text = _code_line() if args.read_code else args.code
        code = normalize_pairing_code(text)
        if code is None:
            return report.fail(EXIT_USAGE, "that is not a pairing code: it has 12 letters and digits, "
                                           "shown as XXXX-XXXX-XXXX (nothing was changed)")
    remote = not _is_loopback_url(url)
    report.data.update(url=url, remote=remote, dry_run=args.dry_run)

    # 1. The plan: an unauthenticated health probe, discovery.
    health = probe_health(url, health_timeout())
    if not isinstance(health, dict) or health.get("status") != "ok":
        status = f" (status: {health.get('status')})" if isinstance(health, dict) else ""
        return report.fail(EXIT_REFUSED, f"the daemon at {url} did not answer /health with status ok"
                                         f"{status}; nothing was changed")
    if remote and health.get("auth") is False:
        return report.fail(EXIT_REFUSED, f"the daemon at {url} runs without a bearer token (auth: false). "
                                         "An unauthenticated bank must never be reached over a network: "
                                         "set PSEUDOLIFE_MCP_TOKEN for the daemon on its host, restart it, "
                                         "and re-run. Nothing was changed")
    if pairing and health.get("auth") is not True:
        return report.fail(EXIT_REFUSED, f"the daemon at {url} does not report authentication, and "
                                         "pairing needs a daemon with a bearer token. Nothing was "
                                         "changed")
    report.data["daemon"] = {"version": health.get("version"), "auth": health.get("auth")}
    if remote and url.startswith("http://"):
        report.warn(_PLAIN_HTTP.format(url=url))
    env = dict(os.environ)
    try:
        layout = runtimes.default_layout(env)
    except ValueError:
        layout = None
    ctx = Context(url, remote, token_file or (PAIRED_TOKEN_FILE if pairing else None), clients, env,
                  layout)
    rows = discover(ctx)
    report.say(f"connect: {url} ({'another machine' if remote else 'this machine'}; daemon "
               f"{health.get('version') or 'unknown'}, auth {'on' if health.get('auth') else 'off'})")
    report.plan(rows)
    if pairing:
        sharing = sorted({row["client"] for row in rows
                          if row["client"] in CLIENTS and row["place"] == "registration"
                          and row["state"] in ("current", "change")})
        if len(sharing) > 1:
            report.warn(f"{len(sharing)} clients ({', '.join(sharing)}) will share one principal "
                        "through this pairing code; invite one name per client (`pseudolife-mcp "
                        "invite <name>` on the daemon host, then one `connect --code` per client "
                        "with --client) to keep their writes apart")
    unpaired = (" The pairing code was not redeemed: run the installer with it instead (option 2, "
                "or --pairing-code / -PairingCode)") if pairing else ""
    manual = [row for row in rows if row["state"] == "manual"]
    if not any(row["client"] in CLIENTS and row["state"] in ("current", "change") for row in rows):
        if any(row["client"] in CLIENTS and row["state"] == "manual" for row in rows):
            return report.fail(EXIT_NOTHING, f"no registration connect can write was found for "
                                             f"{', '.join(clients)}: {_places(len(manual))} manual "
                                             "action (listed above)." + unpaired)
        return report.fail(EXIT_NOTHING, "no registration of the shim was found for "
                                         f"{', '.join(clients)}; register a client first (the commands "
                                         "are above), or run the installer." + unpaired)
    changes = [row for row in rows if row["state"] == "change"]
    if args.dry_run:
        report.note("dry run: nothing was written and no token was sent; the credentials are "
                    "verified only on a real run" + ("; the pairing code was not redeemed"
                                                     if pairing else ""))
        return report.finish(EXIT_OK)
    if not changes:
        report.note(f"{_places(len(manual))} manual action (listed above); nothing else to write"
                    if manual else f"every place already names {url}; nothing to write")
        return report.finish(EXIT_OK)

    # 2. Confirm, before any authenticated request.
    if not args.yes:
        if not interactive():
            return report.fail(EXIT_USAGE, "this run is not interactive: re-run with --yes to apply the "
                                           "plan above (nothing was changed)")
        if not _ask("Apply these changes? [y/N] "):
            return report.fail(EXIT_USAGE, "not confirmed; nothing was changed")
    created: list[Path] = []
    if args.read_token:
        try:
            client_config.write_token_file(Path(token_file), _token_stream())
        except client_config.HelperError as exc:
            return report.fail(EXIT_USAGE, f"{exc}; nothing was changed")
        except (OSError, CredentialError) as exc:
            return report.fail(EXIT_USAGE, f"{type(exc).__name__} while writing {token_file}; check its "
                                           "directory's permissions. Nothing was changed")
        created.append(Path(token_file))
    kept = ""
    if pairing:
        # The code is spent from here on: the paired file is never in
        # ``created``, so no failure below removes the only copy of the token.
        paired = pair_cli.redeem(url, code, token_file)
        report.data["pairing"] = {key: paired[key] for key in
                                  ("state", "principal", "tier", "bank", "token_file")}
        for line in paired["warnings"]:
            report.warn(line)
        if paired["exit"] != pair_cli.EXIT_OK:
            return report.fail(EXIT_REFUSED, f"pairing did not complete: {paired['error']}. No client "
                                             "configuration was written")
        token_file = ctx.token_file = paired["token_file"]
        kept = (f" The paired token file {token_file} is kept: the code is spent, and the daemon "
                "accepts this token")
        report.say(f"paired: {paired['principal'] or 'a principal whose name could not be used'} "
                   f"on {url}; token file {token_file}")
        rows = discover(ctx)
        report.data["rows"] = [_public(row) for row in rows]

    # 3. Verify every credential against the target.
    results, warnings, failure = verify(ctx, rows, health)
    if failure:
        for path in created:
            path.unlink(missing_ok=True)
        return report.fail(EXIT_REFUSED, f"{failure}. Nothing was written." + kept)
    report.data["verification"] = results
    for line in warnings:
        report.warn(line)

    # 4. Apply, all or nothing.
    steps, failure = apply(ctx, rows, created)
    if failure:
        report.data["rollback"] = failure["rollback"]
        for item in failure["rollback"]:
            report.say(f"  {item['state']:<8} {item['file']}" + (f": {item['detail']}" if item.get("detail") else "")
                       + (f" (backup {item['backup']})" if item.get("backup") and item["state"] != "restored" else ""))
        return report.fail(EXIT_FAILED, f"writing {failure['file']} failed ({failure['reason']}); every file "
                                        "this run wrote was rolled back as listed above." + kept)

    # 5. Report and check.
    report.data["rows"] = [_public(row) for row in rows]
    for row in rows:
        if row["backup"]:
            report.say(f"  backup: {row['backup']}")
    for result in results:
        report.say(f"  verified: {result['credential']}: {result['tools']} tools; board "
                   f"{result['board']}")
    restart = _restart(ctx, rows)
    report.data["restart"] = restart
    report.note(f"restart: {restart['detail']}")
    if restart["claude_desktop"]:
        report.note("restart Claude Desktop: fully quit it (tray/menu-bar icon, not just the window) "
                    "and relaunch it")
    report.note(f"the agent board keys each session's address by the daemon URL, so sessions get new "
                f"addresses on {url}; the old state stays in place, and peers see this machine's "
                f"sessions under the new ones")
    pending = [row for row in discover(ctx)
               if row["client"] in CLIENTS and row["state"] == "change"]
    after = probe_health(url, health_timeout())
    if pending or not isinstance(after, dict) or after.get("status") != "ok":
        what = (", ".join(f"{row['client']} {row['place']} ({row['file']})" for row in pending)
                if pending else f"the daemon at {url} stopped answering /health")
        return report.fail(EXIT_UNVERIFIED, f"applied, but the post-apply check failed: {what}. The "
                                            "backups are listed above; restore them or re-run connect")
    report.note("post-apply check: every place names the daemon, and it answers /health; run "
                "`pseudolife-mcp doctor` from a client's environment for the full check")
    return report.finish(EXIT_OK)

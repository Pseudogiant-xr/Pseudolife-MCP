"""Client-side config edits: token files and the JSON env blocks.

Standard library only (plus the package's own standard-library credential
module): ``ops/client_credentials.py`` runs this from a checkout under
whatever ``python3`` the installer finds, before any package is installed,
and ``pseudolife-mcp connect`` runs it from a shim runtime.

  registration_env     add missing keys to a Claude Code registration's env
  claude_settings_env  point the Claude Code plugin's hooks at a token file
  check_token_file     the shim's own check of a token file
  write_token_file     create an owner-only token file from one token

Token values are never returned or printed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from pseudolife_memory import credentials

SERVER = "pseudolife-memory"
TOKEN_KEY = "PSEUDOLIFE_MCP_TOKEN"
TOKEN_FILE_KEY = "PSEUDOLIFE_MCP_TOKEN_FILE"
DAEMON_URL_KEY = "PSEUDOLIFE_MCP_DAEMON_URL"
BOM = "﻿"


class HelperError(Exception):
    pass


# -- private file io ----------------------------------------------------------

def _write_private(path: Path, data: bytes) -> None:
    """Atomically replace ``path`` with an owner-only file holding ``data``.

    A symlinked config (a dotfiles repo, say) is written through, so the
    link survives."""
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pseudolife-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        if os.name == "nt":
            credentials._secure_windows_file(temporary_path)
        else:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if fd != -1:
            os.close(fd)
        if temporary_path.exists():
            temporary_path.unlink()


def _backup(path: Path) -> str | None:
    """An owner-only copy beside ``path``: these configs can hold credentials."""
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target = path.with_name(f"{path.name}.bak-pseudolife-{stamp}")
    _write_private(target, path.read_bytes())
    return str(target)


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HelperError(f"{path.name} is not readable JSON; fix it, then re-run") from error
    if not isinstance(data, dict):
        raise HelperError(f"{path.name} is not a JSON object; fix it, then re-run")
    return data


def _write_json(path: Path, data: dict) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _write_private(path, text.encode("utf-8"))


# -- registration-env ---------------------------------------------------------

def registration_env(config: Path, wanted: dict[str, str]) -> dict:
    """Add missing keys to the user-scope stdio registration in ``config``
    (~/.claude.json); never replace a value, and leave a credential the
    registration already has."""
    data = _load_json(config)
    entry = (data.get("mcpServers") or {}).get(SERVER)
    if not isinstance(entry, dict):
        return {"status": "absent", "backup": None}
    if entry.get("type", "stdio") != "stdio" or not entry.get("command"):
        return {"status": "unsupported", "backup": None}
    env = entry.get("env") or {}
    if not isinstance(env, dict):
        return {"status": "unsupported", "backup": None}
    has_credential = bool(env.get(TOKEN_KEY) or env.get(TOKEN_FILE_KEY))
    additions = {key: value for key, value in wanted.items()
                 if key not in env and not (key == TOKEN_FILE_KEY and has_credential)}
    if not additions:
        return {"status": "unchanged", "backup": None}
    backup = _backup(config)
    entry["env"] = {**env, **additions}
    _write_json(config, data)
    return {"status": "updated", "backup": backup}


# -- claude-settings-env ------------------------------------------------------

def claude_settings_env(settings: Path, token_path: str, daemon_url: str,
                        replace: bool = False) -> dict:
    """The plugin's hooks read the Claude Code process environment, not the
    MCP registration's env block, so they need the token file there too.
    The daemon URL rides with it: beside a managed Codex connection the
    hooks refuse an explicit token file without the matching URL.

    ``replace`` is a client-only install's: the hooks must reach the remote
    daemon with the operator's file, so both keys are set whatever the
    installer's environment or an earlier local install left there."""
    if replace:
        data = _load_json(settings)
        env = data.get("env") or {}
        if not isinstance(env, dict):
            raise HelperError("settings.json 'env' is not an object; fix it, then re-run")
        wanted = {TOKEN_FILE_KEY: token_path, DAEMON_URL_KEY: daemon_url}
        changed = sorted(key for key, value in wanted.items() if env.get(key) != value)
        if not changed:
            return {"status": "unchanged", "backup": None, "changed": []}
        backup = _backup(settings)
        data["env"] = {**env, **wanted}
        _write_json(settings, data)
        return {"status": "updated", "backup": backup, "changed": changed}
    if os.environ.get(TOKEN_KEY) or os.environ.get(TOKEN_FILE_KEY):
        return {"status": "kept", "backup": None}
    data = _load_json(settings)
    env = data.get("env") or {}
    if not isinstance(env, dict):
        raise HelperError("settings.json 'env' is not an object; fix it, then re-run")
    if env.get(TOKEN_KEY) or env.get(TOKEN_FILE_KEY):
        return {"status": "kept", "backup": None}
    backup = _backup(settings)
    additions = {TOKEN_FILE_KEY: token_path}
    if not env.get(DAEMON_URL_KEY):
        additions[DAEMON_URL_KEY] = daemon_url
    data["env"] = {**env, **additions}
    _write_json(settings, data)
    return {"status": "updated", "backup": backup}


# -- check-token-file / write-token-file --------------------------------------

def check_token_file(path: Path) -> dict:
    """The shim's own check, so a file the installer accepts is one the
    shim will read. The reason names what is wrong, never the value."""
    try:
        token = credentials.CredentialProvider(path=path).snapshot().token
    except credentials.CredentialError as error:
        return {"status": "failed", "recovery": str(error)}
    # U+FEFF is neither whitespace nor a control character, so the shim
    # would send it as the start of the bearer and every call would fail.
    if token.startswith(BOM):
        return {"status": "failed",
                "recovery": "the file starts with a UTF-8 byte-order mark, which the shim "
                            "would send as part of the token; write it without one"}
    return {"status": "ready"}


def _already_exists(target: Path) -> HelperError:
    return HelperError(f"{target} already exists; the installer creates a token file "
                       "but never replaces one")


def write_token_file(path: Path, stream) -> dict:
    """Create ``path`` owner-only from one token read on ``stream``.

    The final path is created exclusively (O_CREAT | O_EXCL, no link
    followed), so a file that appears meanwhile, even in a race, is never
    replaced: this run reports that it exists and leaves it alone."""
    target = Path(os.path.abspath(path.expanduser()))
    if target.exists() or target.is_symlink():
        raise _already_exists(target)
    data = stream.read(credentials.MAX_TOKEN_BYTES + 3)
    try:
        # A BOM is never part of a token (a PowerShell 5.1 pipe adds one).
        token = data.decode("utf-8").removeprefix(BOM).rstrip("\r\n")
        credentials._decode_token(token.encode("utf-8"))
    except UnicodeDecodeError as error:
        raise HelperError("the token read on stdin is not UTF-8 text") from error
    except credentials.CredentialError as error:
        raise HelperError("the token read on stdin is empty, too long, or holds "
                          "whitespace or control characters") from error
    credentials._reject_ancestor_redirects(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError as error:
        raise _already_exists(target) from error
    try:
        try:
            # Owner-only before a byte is written: the ACL on Windows, the
            # mode bits elsewhere (the umask may have narrowed 0o600 only).
            if os.name == "nt":
                credentials._secure_windows_file(target)
            else:
                os.fchmod(fd, 0o600)
            credentials._validate_file(fd)
            os.write(fd, token.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        if credentials.CredentialProvider(path=target).snapshot().token != token:
            raise credentials.CredentialError("credential file validation failed")
    except BaseException:
        # This run created the file, so removing a half-written one is safe.
        target.unlink(missing_ok=True)
        raise
    return {"status": "written", "path": str(target)}

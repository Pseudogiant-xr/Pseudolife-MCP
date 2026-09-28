#!/usr/bin/env python3
"""Board-token plumbing shared by ops/install.sh and ops/install.ps1.

The agent board needs a bearer token, and once the daemon holds one every
client must present it. Both installers call this helper so each platform
behaves the same:

  mint                 give ops/.env a random PSEUDOLIFE_MCP_TOKEN when it has none
  token-file           write one client's owner-only token file
  registration-env     add the token file, daemon URL and agent state directory
                       to an existing Claude Code registration, in place
  claude-settings-env  point the Claude Code plugin's hooks at the token file
                       (--replace: a client-only install's file and URL, whatever
                       settings.json or the environment held before)
  check-token-file     the shim's own check of a token file: owner-only, a
                       regular file (no link), one well-formed token
  write-token-file     create an owner-only token file from one token read on
                       stdin; an existing file is never replaced
  board                print one line: whether the board is on, or why it is off

Token values arrive through the environment (PSEUDOLIFE_INSTALLER_TOKEN and
PSEUDOLIFE_INSTALLER_TOKENS) or stdin, never the command line, and are never
printed.
Results are JSON on stdout (``board`` prints its line); exit 1 means failed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
# Run from a checkout before the shim is installed: prefer this checkout's
# standard-library helpers over an older installed copy.
sys.path.insert(0, str(REPO_ROOT))

from pseudolife_memory import credentials  # noqa: E402

SERVER = "pseudolife-memory"
TOKEN_KEY = "PSEUDOLIFE_MCP_TOKEN"
TOKENS_KEY = "PSEUDOLIFE_MCP_TOKENS"
TOKEN_FILE_KEY = "PSEUDOLIFE_MCP_TOKEN_FILE"
DAEMON_URL_KEY = "PSEUDOLIFE_MCP_DAEMON_URL"
BOM = "﻿"
SOURCE_ENV = "PSEUDOLIFE_INSTALLER_TOKEN"
MAP_SOURCE_ENV = "PSEUDOLIFE_INSTALLER_TOKENS"
MINTED_COMMENT = ("# Bearer token minted by the installer: the agent board needs one. "
                  "See 'Turning the board on' in docs/guide/configuration.md.")


class HelperError(Exception):
    pass


def _registrar():
    """ops/register_claude_desktop.py, whose credential planning this reuses."""
    spec = importlib.util.spec_from_file_location(
        "pseudolife_register_claude_desktop",
        Path(__file__).with_name("register_claude_desktop.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


# -- mint ---------------------------------------------------------------------

def _active_value(text: str, key: str) -> str:
    values = re.findall(rf"(?m)^{key}=(.*?)\r?$", text)
    return values[-1].strip() if values else ""


def mint(env_file: Path) -> dict:
    # Compose takes a shell value over ops/.env, so a token in the
    # installer's environment already is the daemon's token.
    if os.environ.get(TOKEN_KEY) or os.environ.get(TOKENS_KEY):
        return {"status": "present"}
    # Bytes, not read_text: keep the file's own line endings.
    text = env_file.read_bytes().decode("utf-8") if env_file.exists() else ""
    if _active_value(text, TOKEN_KEY) or _active_value(text, TOKENS_KEY):
        return {"status": "present"}
    token = secrets.token_urlsafe(32)
    empty = re.compile(rf"(?m)^{TOKEN_KEY}=[ \t]*(\r?)$")
    if empty.search(text):
        text = empty.sub(lambda m: f"{TOKEN_KEY}={token}{m.group(1)}", text, count=1)
    else:
        eol = "\r\n" if "\r\n" in text else "\n"
        if text and not text.endswith("\n"):
            text += eol
        text += f"{MINTED_COMMENT}{eol}{TOKEN_KEY}={token}{eol}"
    _write_private(env_file, text.encode("utf-8"))
    return {"status": "minted"}


# -- token-file ---------------------------------------------------------------

def _principal_token(principal: str) -> str | None:
    """This client's own map entry, else the singular token."""
    raw_map = os.environ.get(MAP_SOURCE_ENV) or ""
    if raw_map.strip():
        from pseudolife_memory.principals import parse_token_map
        matches = [token for token, name in parse_token_map(raw_map).items()
                   if name == principal]
        if len(matches) > 1:
            raise HelperError(
                f"the daemon token map holds {len(matches)} tokens for principal "
                f"{principal!r}; keep exactly one")
        if matches:
            return matches[0]
    return os.environ.get(SOURCE_ENV) or None


def token_file(principal: str, path: Path) -> dict:
    registrar = _registrar()
    try:
        target, _, _ = registrar.plan_credential({}, str(path), _principal_token(principal),
                                                 dry_run=False)
    except registrar.ConfigError as error:
        raise HelperError(str(error)) from error
    if not target:
        return {"status": "tokenless"}
    return {"status": "ready", "path": str(path)}


# -- registration-env ---------------------------------------------------------

def _pairs(values: list[str]) -> dict[str, str]:
    pairs = {}
    for item in values:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise HelperError("--set takes KEY=VALUE")
        pairs[key] = value
    return pairs


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


# -- board --------------------------------------------------------------------

def board(daemon_url: str, token_path: str | None) -> str:
    from pseudolife_memory.board_status import board_status
    if token_path:
        try:
            token = credentials.CredentialProvider(path=token_path).snapshot().token
        except (credentials.CredentialError, OSError, UnicodeError):
            return "off - the client token file is missing, unsafe or malformed"
    else:
        token = os.environ.get(SOURCE_ENV) or None
    return board_status(daemon_url, token)[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("mint")
    p.add_argument("--env-file", required=True, type=Path)
    p = commands.add_parser("token-file")
    p.add_argument("--principal", required=True)
    p.add_argument("--path", required=True, type=Path)
    p = commands.add_parser("registration-env")
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--set", action="append", default=[], dest="pairs")
    p = commands.add_parser("claude-settings-env")
    p.add_argument("--settings", required=True, type=Path)
    p.add_argument("--token-file", required=True)
    p.add_argument("--daemon-url", required=True)
    p.add_argument("--replace", action="store_true")
    p = commands.add_parser("check-token-file")
    p.add_argument("--path", required=True, type=Path)
    p = commands.add_parser("write-token-file")
    p.add_argument("--path", required=True, type=Path)
    p = commands.add_parser("board")
    p.add_argument("--daemon-url", required=True)
    p.add_argument("--token-file")
    args = parser.parse_args(argv)
    if args.command == "board":
        print(board(args.daemon_url, args.token_file))
        return 0
    try:
        if args.command == "mint":
            report = mint(args.env_file)
        elif args.command == "token-file":
            report = token_file(args.principal, args.path)
        elif args.command == "registration-env":
            report = registration_env(args.config, _pairs(args.pairs))
        elif args.command == "check-token-file":
            report = check_token_file(args.path)
        elif args.command == "write-token-file":
            report = write_token_file(args.path, sys.stdin.buffer)
        else:
            report = claude_settings_env(args.settings, args.token_file, args.daemon_url,
                                         replace=args.replace)
    except HelperError as error:
        report = {"status": "failed", "recovery": str(error)}
    except (OSError, credentials.CredentialError) as error:
        # Name the failure class only: messages can carry paths, never values.
        report = {"status": "failed", "recovery": f"{type(error).__name__} while writing; "
                                                  "check the target's permissions, then re-run"}
    print(json.dumps(report))
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())

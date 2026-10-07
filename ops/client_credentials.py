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
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
# Run from a checkout before the shim is installed: prefer this checkout's
# standard-library helpers over an older installed copy.
sys.path.insert(0, str(REPO_ROOT))

from pseudolife_memory import credentials  # noqa: E402
# The file edits and token-file helpers live in the package, so that
# `pseudolife-mcp connect` can run them from a release install too.
from pseudolife_memory.client_config import (  # noqa: E402,F401
    BOM,
    DAEMON_URL_KEY,
    SERVER,
    TOKEN_FILE_KEY,
    TOKEN_KEY,
    HelperError,
    _backup,
    _load_json,
    _write_json,
    _write_private,
    check_token_file,
    claude_settings_env,
    registration_env,
    write_token_file,
)

TOKENS_KEY = "PSEUDOLIFE_MCP_TOKENS"
SOURCE_ENV = "PSEUDOLIFE_INSTALLER_TOKEN"
MAP_SOURCE_ENV = "PSEUDOLIFE_INSTALLER_TOKENS"
MINTED_COMMENT = ("# Bearer token minted by the installer: the agent board needs one. "
                  "See 'Turning the board on' in docs/guide/configuration.md.")


def _registrar():
    """ops/register_claude_desktop.py, whose credential planning this reuses."""
    spec = importlib.util.spec_from_file_location(
        "pseudolife_register_claude_desktop",
        Path(__file__).with_name("register_claude_desktop.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

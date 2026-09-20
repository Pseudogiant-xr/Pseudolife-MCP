#!/usr/bin/env python3
"""Register the pseudolife-memory stdio shim in Claude Desktop's config.

Claude Desktop has no ``mcp add`` CLI; its servers live in
``claude_desktop_config.json``. Both installers (``ops/install.sh`` and
``ops/install.ps1``, ``--client claude-desktop``) call this script so the
merge exists exactly once. It loads under any python >= 3.10 with no
third-party packages; the one package import (the credential-file writer
in ``pseudolife_memory/credentials.py``, itself standard-library only) is
taken lazily from this checkout when a token file has to be written.

Four things make Desktop different from the CLI clients, and each shapes
the entry this writes:

* Desktop launches MCP servers with a SANITIZED environment (PATH plus a
  few system variables). A bearer token exported in the OS environment
  never reaches the shim, so a token-protected daemon needs
  ``PSEUDOLIFE_MCP_TOKEN_FILE`` in the entry's ``env`` — a path to a
  private file, never the token value (2026-09-19 incident: every Desktop
  session 401'd for four days behind "unhandled errors in a TaskGroup").
  The shim reads that file first and unconditionally, so this script
  never points the entry at a file it did not write or validate: a token
  from ``--token-from-env`` or a literal already in the entry is written
  to the file (owner-only, atomically, via the package's own writer) and
  the literal is removed from the config; with no token to write and no
  usable file, the entry is written WITHOUT a credential and the exit code
  says so (3), rather than silently disabling a token that worked.
* The shim must be able to READ that file. The installers take the shim
  from PyPI, and every release through 0.15.0 reads only the literal
  ``PSEUDOLIFE_MCP_TOKEN`` — which Desktop never delivers — so an entry
  pointing such a shim at a token file fails exactly like the incident
  above, with no hint. Before writing anything this script runs
  ``<command> --help`` and looks for the ``PSEUDOLIFE_MCP_TOKEN_FILE``
  marker the capable shim prints; a help text that answers without it is
  refused (exit 4, nothing written, the upgrade named), and so is a shim
  with no help mode at all (releases before 2026-07-16 answer "unknown
  mode"). A probe that yields no evidence (missing, not executable,
  timeout, any other non-zero exit, or exit 0 with no output) is not
  blocking: the run proceeds and says the check did not happen. The probe
  gets no stdin and not the token variable, so a wrapper that drops its
  arguments cannot start a real shim holding the bearer.
  ``--skip-shim-check`` bypasses it for a wrapper the probe cannot see
  through.
* That sanitized PATH omits pipx/venv bin dirs, so ``command`` must be the
  shim's absolute path.
* On Windows the Desktop app is an MSIX package: its Roaming AppData lives
  under ``%LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude``,
  which an unpackaged shell's ``%APPDATA%\\Claude`` does not show. The
  package cache wins when it exists; if both files exist the other one is
  named in the output.

Usage:
    python ops/register_claude_desktop.py --command /abs/path/pseudolife-mcp
        [--writer-id claude-desktop] [--daemon-url http://127.0.0.1:8765]
        [--token-file /abs/path/claude-desktop.token]
        [--token-from-env VAR_NAME] [--config PATH] [--dry-run]
        [--skip-shim-check]

Exit codes: 0 registered · 2 refused (bad input, unreadable or malformed
config, unusable token file) · 3 registered but WITHOUT a credential the
daemon needs · 4 refused because the shim at ``--command`` cannot read a
token file (upgrade it, then re-run).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Mapping

SERVER = "pseudolife-memory"
DEFAULT_WRITER_ID = "claude-desktop"
DEFAULT_DAEMON_URL = "http://127.0.0.1:8765"
# The env keys this script owns on the entry. Anything else a user added by
# hand (a proxy setting, a literal token it could not migrate) is preserved.
MANAGED_ENV = ("PSEUDOLIFE_WRITER_ID", "PSEUDOLIFE_MCP_NO_SPAWN",
               "PSEUDOLIFE_MCP_DAEMON_URL", "PSEUDOLIFE_MCP_TOKEN_FILE")
LITERAL_TOKEN_KEY = "PSEUDOLIFE_MCP_TOKEN"
REPO_ROOT = Path(__file__).resolve().parents[1]

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_NO_CREDENTIAL = 3
EXIT_OLD_SHIM = 4
# The capable shim names the file form in its --help; releases through
# 0.15.0 mention it nowhere. tests/test_register_claude_desktop.py pins the
# checkout's help text to this marker.
TOKEN_FILE_MARKER = "PSEUDOLIFE_MCP_TOKEN_FILE"
SHIM_PROBE_TIMEOUT = 30.0


class ConfigError(RuntimeError):
    """The existing config or the token file cannot be used safely."""


def _platform() -> str:
    return sys.platform


def _is_absolute(path: str) -> bool:
    # Either OS's absolute form counts (the installers pass the host's);
    # only a bare name or a relative path is rejected.
    return (PurePosixPath(path).is_absolute()
            or PureWindowsPath(path).is_absolute())


# -- config path --------------------------------------------------------------

def windows_config_candidates(env: Mapping[str, str], home: Path) -> list[Path]:
    """Every location Desktop's config may live in on Windows, most
    authoritative first: MSIX package caches, then unpackaged ``%APPDATA%``."""
    found: list[Path] = []
    local = env.get("LOCALAPPDATA")
    if local:
        packages = Path(local) / "Packages"
        if packages.is_dir():
            for family in sorted(packages.glob("Claude_*")):
                cache = family / "LocalCache" / "Roaming" / "Claude"
                if cache.is_dir():
                    found.append(cache / "claude_desktop_config.json")
    appdata = env.get("APPDATA") or str(home / "AppData" / "Roaming")
    found.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
    return found


def resolve_config_path(system: str, env: Mapping[str, str], home: Path) -> Path:
    """Where Claude Desktop reads ``claude_desktop_config.json`` on this OS."""
    if system == "win32":
        return windows_config_candidates(env, home)[0]
    if system == "darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    base = env.get("XDG_CONFIG_HOME")
    root = Path(base) if base else home / ".config"
    return root / "Claude" / "claude_desktop_config.json"


# -- the entry ----------------------------------------------------------------

def build_entry(*, command: str, writer_id: str, daemon_url: str,
                token_file: str | None) -> dict:
    if not _is_absolute(command):
        raise ValueError(
            f"command must be an absolute path (got {command!r}): Claude "
            "Desktop launches servers with a sanitized PATH that omits "
            "pipx/venv bin directories")
    if token_file and not _is_absolute(token_file):
        raise ValueError(
            f"token file must be an absolute path (got {token_file!r}): "
            "Desktop launches servers from an arbitrary working directory")
    env = {
        "PSEUDOLIFE_WRITER_ID": writer_id,
        "PSEUDOLIFE_MCP_NO_SPAWN": "1",
        "PSEUDOLIFE_MCP_DAEMON_URL": daemon_url,
    }
    if token_file:
        env["PSEUDOLIFE_MCP_TOKEN_FILE"] = token_file
    return {"command": command, "env": env}


def merge_config(existing: dict, entry: dict, *,
                 drop_literal_token: bool = False) -> tuple[dict, bool]:
    """Return ``(merged, changed)``. Only the managed keys of our entry are
    replaced; every other key, server and user-added env var is kept.
    ``drop_literal_token`` removes ``PSEUDOLIFE_MCP_TOKEN`` from the entry —
    only after its value has been written to the token file."""
    merged = json.loads(json.dumps(existing))  # deep copy, JSON-shaped
    servers = merged.get("mcpServers")
    if servers is None:
        servers = merged["mcpServers"] = {}
    if not isinstance(servers, dict):
        raise ConfigError("mcpServers is not a JSON object; refusing to rewrite it")
    current = servers.get(SERVER)
    if current is not None and not isinstance(current, dict):
        raise ConfigError(f"mcpServers.{SERVER} is not a JSON object; refusing to rewrite it")
    new = dict(current or {})
    new["command"] = entry["command"]
    new.pop("args", None)  # the shim takes none; a stale list would break launch
    env = dict(new.get("env") or {})
    for key in MANAGED_ENV:
        env.pop(key, None)
    if drop_literal_token:
        env.pop(LITERAL_TOKEN_KEY, None)
    env.update(entry["env"])
    new["env"] = env
    servers[SERVER] = new
    return merged, merged != existing


def current_entry(existing: dict) -> dict:
    servers = existing.get("mcpServers")
    entry = servers.get(SERVER) if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else {}


# -- can the shim read a token file? ------------------------------------------

def help_shows_token_file_support(help_text: str) -> bool:
    return TOKEN_FILE_MARKER in help_text


def shim_reads_token_file(command: str, *, timeout: float = SHIM_PROBE_TIMEOUT,
                          scrub_env: tuple[str, ...] = ()) -> bool | None:
    """``True``/``False`` when ``<command> --help`` gave evidence: exit 0
    with output that does / does not name the token file, or the "unknown
    mode" answer of a shim from before help existed (``False``). ``None``
    when it could not be asked (missing, not executable, timed out, any
    other non-zero exit, or exit 0 with no output at all) — no evidence.

    The child gets no stdin, so a wrapper that drops its arguments and
    starts the real stdio shim exits on EOF instead of holding the
    installer's terminal; ``scrub_env`` names variables (the token source)
    it must not inherit; the UTF-8 pin keeps the em dashes in the usage
    text from failing the child on a narrow console code page."""
    env = {key: value for key, value in os.environ.items() if key not in scrub_env}
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        done = subprocess.run([command, "--help"], capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    output = (done.stdout or "") + (done.stderr or "")
    if done.returncode != 0:
        return False if "unknown mode" in output else None
    if not output.strip():
        return None
    return help_shows_token_file_support(output)


def _refuse_old_shim(command: str) -> int:
    print(
        f"error: the shim at {command} cannot read a token file: its --help "
        f"does not list {TOKEN_FILE_MARKER} (no PyPI release through 0.15.0 "
        f"does, and one that answers \"unknown mode\" predates --help itself). "
        f"Claude Desktop cannot pass the literal PSEUDOLIFE_MCP_TOKEN, "
        f"so registering this shim against a token-gated daemon would leave "
        f"every Desktop session refused (401) behind \"unhandled errors in a "
        f"TaskGroup\". Nothing was written. Upgrade the shim first — "
        f"`pipx upgrade pseudolife-mcp` / `pip install -U pseudolife-mcp` "
        f"once a release carries it, or install this checkout: "
        f"`pipx install {REPO_ROOT}` (or `pip install {REPO_ROOT}`) — then "
        f"re-run. --skip-shim-check bypasses this check for a wrapper the "
        f"probe cannot see through.",
        file=sys.stderr,
    )
    return EXIT_OLD_SHIM


# -- the credential file ------------------------------------------------------

def _credentials():
    """The package's owner-only token-file writer, imported from this
    checkout on demand (standard-library only itself)."""
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from pseudolife_memory import credentials
    except ImportError as exc:  # pragma: no cover - broken checkout
        raise ConfigError(
            f"cannot import pseudolife_memory.credentials from {REPO_ROOT}; "
            "run this script from a Pseudolife-MCP checkout") from exc
    return credentials


def plan_credential(existing_env: Mapping[str, str], token_file: str | None,
                    token: str | None, *, dry_run: bool) -> tuple[str | None, bool, list[str]]:
    """Decide the entry's credential settings and, unless ``dry_run``, write
    the token file. Returns ``(token_file_for_entry, drop_literal, notes)``.

    A token from the environment wins; failing that, a literal already on
    the entry is migrated into the file (and dropped from the config); with
    neither, an existing valid file is used as-is; with nothing usable the
    entry gets no credential (the caller exits 3 and says so)."""
    if not token_file:
        return None, False, []
    literal = existing_env.get(LITERAL_TOKEN_KEY) or None
    source = token or literal
    notes: list[str] = []
    if source:
        if dry_run:
            notes.append(f"token file: would write {token_file} (owner-only)")
        else:
            creds = _credentials()
            try:
                creds._write_token_file(token_file, source)
                if creds.CredentialProvider(path=token_file).snapshot().token != source:
                    raise creds.CredentialError("credential file validation failed")
            except (creds.CredentialError, OSError, UnicodeError) as exc:
                raise ConfigError(f"could not write the token file {token_file}: {exc}") from exc
            notes.append(f"token file: written {token_file} (owner-only; the shim reloads it per call)")
        if literal and not token:
            notes.append(f"migrated the literal {LITERAL_TOKEN_KEY} out of the config into that file")
        elif literal:
            notes.append(f"dropped the literal {LITERAL_TOKEN_KEY} from the config (the file supersedes it)")
        return token_file, bool(literal), notes
    if Path(token_file).exists():
        if dry_run:
            notes.append(f"token file: would validate the existing {token_file}")
            return token_file, False, notes
        creds = _credentials()
        try:
            creds.CredentialProvider(path=token_file).snapshot()
        except (creds.CredentialError, OSError, UnicodeError) as exc:
            raise ConfigError(
                f"the existing token file {token_file} cannot be used ({exc}); "
                "fix it, or re-run with the token in the environment so it is rewritten") from exc
        notes.append(f"token file: using the existing {token_file}")
        return token_file, False, notes
    return None, False, notes


# -- file io ------------------------------------------------------------------

def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (UnicodeDecodeError, OSError) as exc:
        raise ConfigError(f"{path} could not be read as UTF-8 JSON ({exc}); fix or move it first") from exc
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON ({exc}); fix or move it first") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} is not a JSON object; refusing to rewrite it")
    return data


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    # UTF-8 without a BOM: the app's JSON parser rejects one.
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


# -- main ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--command", required=True,
                        help="absolute path of the pseudolife-mcp shim executable")
    parser.add_argument("--writer-id", default=DEFAULT_WRITER_ID)
    parser.add_argument("--daemon-url", default=DEFAULT_DAEMON_URL)
    parser.add_argument("--token-file", default=None,
                        help="absolute path of the private file holding the daemon "
                             "bearer (the path goes in the config; the value never does)")
    parser.add_argument("--token-from-env", default=None, metavar="VAR",
                        help="name of an environment variable holding the token to "
                             "write into --token-file (the installer sets one; the "
                             "value is never printed)")
    parser.add_argument("--config", default=None,
                        help="claude_desktop_config.json to edit (default: this OS's)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the resolved path and entry; write nothing")
    parser.add_argument("--skip-shim-check", action="store_true",
                        help="do not probe --command --help for token-file support "
                             "(only for a wrapper the probe cannot see through)")
    args = parser.parse_args(argv)

    if args.token_file and not _is_absolute(args.token_file):
        print(f"error: token file must be an absolute path (got {args.token_file!r})",
              file=sys.stderr)
        return EXIT_REFUSED
    try:
        build_entry(command=args.command, writer_id=args.writer_id,
                    daemon_url=args.daemon_url, token_file=None)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    token = None
    if args.token_from_env:
        token = (os.environ.get(args.token_from_env) or "").strip() or None
        if token and not args.token_file:
            print("error: --token-from-env needs --token-file to write into",
                  file=sys.stderr)
            return EXIT_REFUSED

    # Only a token-gated daemon needs the shim to read a file; an open one
    # gets no probe (and no extra process spawn per registration).
    if args.token_file:
        if args.skip_shim_check:
            print("shim check: skipped (--skip-shim-check); the entry assumes "
                  f"{args.command} reads {TOKEN_FILE_MARKER}")
        else:
            scrub = (args.token_from_env,) if args.token_from_env else ()
            capable = shim_reads_token_file(args.command, scrub_env=scrub)
            if capable is False:
                return _refuse_old_shim(args.command)
            if capable is None:
                print(f"note: could not verify that {args.command} reads "
                      f"{TOKEN_FILE_MARKER} (its --help did not answer); "
                      f"proceeding. If Desktop sessions fail with a 401, the "
                      f"shim predates token-file support — upgrade it and re-run.")
            else:
                print(f"shim check: {args.command} reads {TOKEN_FILE_MARKER}")

    if args.config:
        path = Path(args.config)
    else:
        path = resolve_config_path(_platform(), os.environ, Path.home())
        if _platform() == "win32":
            others = [p for p in windows_config_candidates(os.environ, Path.home())
                      if p != path and p.exists()]
            for other in others:
                print(f"note: another Desktop config also exists at {other}; "
                      f"the Store/MSIX build reads the package-cache copy, an "
                      f"unpackaged install reads %APPDATA%. Writing to the "
                      f"package cache; pass --config to choose the other.")
    print(f"config: {path}")

    try:
        existing = _load(path)
        entry_before = current_entry(existing)
        token_file, drop_literal, notes = plan_credential(
            entry_before.get("env") or {}, args.token_file, token, dry_run=args.dry_run)
        entry = build_entry(command=args.command, writer_id=args.writer_id,
                            daemon_url=args.daemon_url, token_file=token_file)
        merged, changed = merge_config(existing, entry, drop_literal_token=drop_literal)
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    no_credential = bool(args.token_file) and token_file is None
    if "args" in entry_before:
        notes.append("dropped the entry's stale 'args' list (the shim takes none)")

    if args.dry_run:
        print("dry run: nothing written. Entry that would be merged:")
        print(json.dumps({SERVER: entry}, indent=2))
        for note in notes:
            print(note)
        return _finish(no_credential, args.token_file)
    if not changed:
        print(f"entry: unchanged ({SERVER} already registered this way)")
        for note in notes:
            print(note)
        return _finish(no_credential, args.token_file)
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        backup.write_bytes(path.read_bytes())
        print(f"backup: {backup}")
    _write(path, merged)
    print(f"entry: written ({SERVER} -> {args.command})")
    for note in notes:
        print(note)
    print("next: fully quit Claude Desktop (tray/menu-bar icon, not just the "
          "window) and relaunch it so the new entry loads")
    return _finish(no_credential, args.token_file)


def _finish(no_credential: bool, wanted_file: str | None) -> int:
    if not no_credential:
        return EXIT_OK
    print(
        f"WARNING: the daemon is token-gated but no token was available to write "
        f"{wanted_file}, and no usable file exists there — the entry was written "
        f"WITHOUT a credential, so Desktop sessions will be refused (401) until "
        f"one is configured. Re-run with the token in the environment "
        f"({LITERAL_TOKEN_KEY}=<token> ops/install.* --client claude-desktop) so "
        f"the installer writes the owner-only file, or set "
        f"PSEUDOLIFE_MCP_TOKEN_FILE to an existing private file.",
        file=sys.stderr,
    )
    return EXIT_NO_CREDENTIAL


if __name__ == "__main__":
    sys.exit(main())

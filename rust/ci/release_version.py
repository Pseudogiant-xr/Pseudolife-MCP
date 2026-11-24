"""The release version: one native authority, one reader, one drift check.

The authority is ``[package] version`` in ``rust/shim/Cargo.toml``: the shim
crate is the native client the release archives ship, and it has carried the
release version since 0.18.0, so the reader also works on older checkouts
and at older revisions. Every other place a release cut writes the version
is a *field* that must equal it; ``check`` names the first that differs.

``pyproject.toml`` is one of those fields while the Python package is still
published: the release cut keeps writing it, and ``check`` keeps it equal.
The pull request that deletes the package removes that field from
``FIELDS`` (with the file), so the drift guard never goes quiet by accident.

Standard library only and no ``tomllib``, so it runs on Python 3.10 (the
oldest interpreter ``ops/integrate.py`` supports) and reads text taken from
a git revision as easily as a file.

    python rust/ci/release_version.py print            # the release version
    python rust/ci/release_version.py check [--tag vX]  # every field equal (and the tag)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
AUTHORITY = "rust/shim/Cargo.toml"

_HEADER = re.compile(r"^\s*(\[\[?)\s*([^\]\s]+)\s*(\]\]?)\s*(?:#.*)?$")
_STRING_KEY = r'^\s*{key}\s*=\s*"([^"\\]*)"\s*(?:#.*)?$'


class VersionError(ValueError):
    """A version that cannot be read, or two that differ."""


def table_string(text: str, table: str, key: str) -> str | None:
    """The basic-string value of ``key`` in TOML table ``[table]`` (not an
    array of tables), read line by line: the first assignment wins, and a
    value that is not a plain one-line string (``version.workspace = true``,
    a literal string, a multi-line one) reads as absent."""
    current = None
    pattern = re.compile(_STRING_KEY.format(key=re.escape(key)))
    for line in text.splitlines():
        header = _HEADER.match(line)
        if header:
            current = header[2] if header[1] == "[" and header[3] == "]" else None
            continue
        if current == table:
            match = pattern.match(line)
            if match:
                return match[1]
    return None


def lock_version(text: str, package: str) -> str | None:
    """The version of ``package`` in a Cargo.lock: the ``version`` of the
    ``[[package]]`` entry whose ``name`` is ``package``."""
    name_re, version_re = (re.compile(_STRING_KEY.format(key=k)) for k in ("name", "version"))
    name = version = None
    in_package = False
    for line in [*text.splitlines(), "[[end]]"]:
        header = _HEADER.match(line)
        if header:
            if in_package and name == package:
                return version
            in_package = header[1] == "[[" and header[2] == "package"
            name = version = None
            continue
        if in_package:
            if name is None and (match := name_re.match(line)):
                name = match[1]
            elif version is None and (match := version_re.match(line)):
                version = match[1]
    return None


def from_manifest(text: str) -> str:
    """The release version in the text of the authority manifest."""
    value = table_string(text, "package", "version")
    if not value:
        raise VersionError(f"{AUTHORITY} names no [package] version; restore the literal "
                           '`version = "<x.y.z>"` line in its [package] table')
    return value


def read(root: Path = ROOT) -> str:
    """The release version of the checkout at ``root``."""
    try:
        text = (Path(root) / AUTHORITY).read_text(encoding="utf-8")
    except OSError as error:
        raise VersionError(f"cannot read {AUTHORITY} ({error.strerror or error})") from None
    return from_manifest(text)


def at_rev(rev: str, root: Path = ROOT) -> str | None:
    """The release version at git revision ``rev``, or None when the
    revision has no readable authority."""
    shown = subprocess.run(["git", "-C", str(root), "show", f"{rev}:{AUTHORITY}"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    if shown.returncode != 0:
        return None
    try:
        return from_manifest(shown.stdout)
    except VersionError:
        return None


def _json(*keys) -> Callable[[str], str | None]:
    def get(text: str) -> str | None:
        value = json.loads(text)
        for key in keys:
            value = value[key]
        return value if isinstance(value, str) else None
    return get


def _regex(pattern: str) -> Callable[[str], str | None]:
    compiled = re.compile(pattern, re.M)
    def get(text: str) -> str | None:
        match = compiled.search(text)
        return match[1] if match else None
    return get


# (label, path, reader of the file's text, required). A field that is not
# required is skipped while its file is absent; every one that exists must
# equal the authority. Order is the order `check` reports in.
FIELDS: tuple[tuple[str, str, Callable[[str], str | None], bool], ...] = (
    # The Python package's metadata, while the package is still published
    # (removed with the package; see the module docstring).
    ("pyproject.toml [project] version", "pyproject.toml",
     lambda text: table_string(text, "project", "version"), False),
    ("rust/daemon/Cargo.toml [package] version", "rust/daemon/Cargo.toml",
     lambda text: table_string(text, "package", "version"), True),
    ("rust/Cargo.lock pseudolife-stdio", "rust/Cargo.lock",
     lambda text: lock_version(text, "pseudolife-stdio"), True),
    ("rust/Cargo.lock pseudolife-daemon", "rust/Cargo.lock",
     lambda text: lock_version(text, "pseudolife-daemon"), True),
    ("rust/shim/src/lifecycle.rs PACKAGE_VERSION", "rust/shim/src/lifecycle.rs",
     _regex(r'^pub const PACKAGE_VERSION: &str = "([^"]*)";'), True),
    ("server.json version", "server.json", _json("version"), True),
    ("server.json packages[0].version", "server.json", _json("packages", 0, "version"), True),
    ("plugin/release.json version", "plugin/release.json", _json("version"), True),
    ("ops/docker-compose.yml daemon image tag", "ops/docker-compose.yml",
     _regex(r"^\s*image:\s*pseudolife-daemon:(\S+)$"), True),
    ("docs/atlas/atlas.json meta.version", "docs/atlas/atlas.json",
     _json("meta", "version"), True),
)


def fields(root: Path = ROOT) -> list[tuple[str, str | None]]:
    """Every version field present at ``root``, as (label, value); a value
    that cannot be read is None."""
    found = []
    for label, path, get, required in FIELDS:
        file = Path(root) / path
        if not file.is_file():
            if required:
                found.append((label, None))
            continue
        try:
            value = get(file.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            value = None
        found.append((label, value))
    return found


def check(root: Path = ROOT, tag: str | None = None) -> str:
    """The release version, after checking every field (and ``tag``, a
    release tag ``v<version>``) equals it; raises VersionError naming the
    first that differs."""
    value = read(root)
    if tag is not None:
        bare = tag[1:] if tag.startswith("v") else None
        if bare != value:
            raise VersionError(f"release tag {tag} does not match the release version {value} "
                               f"({AUTHORITY}); fix the tag or the version cut before publishing")
    for label, found in fields(root):
        if found != value:
            shown = "unreadable or missing" if found is None else found
            raise VersionError(f"{label} is {shown} but the release version is {value} "
                               f"({AUTHORITY}); the version cut must write it too")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("print", help="print the release version")
    checked = sub.add_parser("check", help="check every version field (and --tag) equals it")
    checked.add_argument("--tag", help="a release tag, v<version>, that must match")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        value = read(args.root) if args.command == "print" else check(args.root, args.tag)
    except VersionError as error:
        print(f"release version: {error}", file=sys.stderr)
        return 1
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

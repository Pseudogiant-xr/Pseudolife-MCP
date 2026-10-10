"""CLI-BACKUP-DSN: ``pseudolife-mcp backup`` (explicit DSN and file mode).

Canonical argv: ``backup [--data-dir P] [--out P] [--keep-days N]`` with
space-separated values, each option at most once, P a canonical path
(``str(Path(P)) == P``, no ``..``) and N a plain decimal of at least a
minute; plus ``--help`` at COLUMNS=80. The bank is the explicit
``PSEUDOLIFE_MCP_DATABASE_URL``; without one, a data dir holding no embedded
bank is archived in file mode. Every case that would need the lite tier's
embedded instance defers (Rust tests cover the deferrals; this row compares
only answered cases).

Contract per arm: exit code, both streams byte-exact (the timestamp in the
two new file names is normalized against the arm's own clock window), every
file under the home, the state archive read back member by member (name,
type, mode, uid, gid, mtime, size, link target, content hash; uname and gname
on Windows), the dump decompressed (pg_dump's per-run ``\\restrict`` key
normalized only when the ``\\restrict`` and ``\\unrestrict`` keys agree),
and the source bank unchanged. Container bytes (tar header layout, gzip
headers, compression) are free.
"""

from __future__ import annotations

import atexit
import base64
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile
import time
from pathlib import Path

from .. import normalize
from ..core import WINDOWS, Case
from ..mutants import Mutant
from . import _bank

DB = _bank.name("pl_cf_w1c_backup_src")
ABSENT_DB = _bank.name("pl_cf_w1c_backup_absent")
PG0 = Path.home() / ".pg0" / "installation"
_SEEDED = False
_CREATED = False
_BASELINE: dict | None = None
FIXED = 1_790_000_000.123456  # seeded mtimes, identical in both arms
OLD = time.time() - 30 * 86400


def _source() -> str:
    global _SEEDED, _BASELINE, _CREATED
    if not _SEEDED:
        _CREATED = True
        _bank.create(DB)
        from tests.test_transfer_cli import _seed_bank  # noqa: PLC0415 (oracle seeder)
        with _bank.connect(DB) as conn:
            _seed_bank(conn)
            conn.commit()
        _BASELINE = _bank.dump(DB)
        _SEEDED = True
    return _bank.url(DB)


@atexit.register
def _drop_source() -> None:
    if _CREATED:
        _bank.drop(DB)


def _pg0_version() -> Path:
    versions = sorted(p for p in PG0.iterdir() if (p / "bin").is_dir())
    return versions[-1]


def _write(path: Path, data: bytes | str, mtime: float = FIXED) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    os.utime(path, (mtime, mtime))


def tree(root_rel: str = "bank", old_backups: bool = True, bdir_rel: str | None = None):
    """A data dir with the shapes a real one holds, plus old own and foreign
    files in its backups dir for rotation."""
    def setup(arm):
        root = arm.home / root_rel
        _write(root / "config.yaml", "storage: postgres\nname: ünïcödé ✓\n")
        _write(root / "memory_state" / "state.json", json.dumps({"cursor": 7}))
        _write(root / "chromadb" / "chroma.sqlite3", bytes(range(256)) * 64)
        _write(root / "notes-ü" / "日本語.txt", "text\r\nwith crlf\n")
        _write(root / "notes-ü" / ("long-" + "x" * 120 + ".txt"), "long name")
        # Case-folded order at the top level on Windows, code points below it.
        _write(root / "B-top.txt", "upper")
        _write(root / "a-top.txt", "lower")
        _write(root / "notes-ü" / "B.txt", "upper")
        _write(root / "notes-ü" / "a.txt", "lower")
        _write(root / "run.cmd", "@echo off\n")
        _write(root / ".cmd", "dotfile named like an executable suffix\n")
        _write(root / "locked" / "inner.txt", "inside a read-only dir")
        _write(root / "locked.txt", "read-only file")
        os.chmod(root / "locked.txt", stat.S_IREAD)
        (root / "empty").mkdir()
        _write(root / "embedded_pg" / "postmaster.opts", "excluded, no PG_VERSION")
        if bdir_rel is not None:
            # A backups dir the run does not write to is still excluded by name.
            _write(root / "backups" / "docker-tier.sql.gz", b"not archived")
        if old_backups:
            bdir = arm.home / (bdir_rel or f"{root_rel}/backups")
            for name, mtime in [
                ("pseudolife_lite_memory-20200101-000000.sql.gz", OLD),
                ("pseudolife_lite_memory-20200102-000000.sql.gz", OLD),
                ("pseudolife_lite_state-20200101-000000.tar.gz", OLD),
                ("pseudolife_lite_state-20200102-000000.tar.gz", OLD),
                ("pseudolife_lite_state-29990101-000000.tar.gz", time.time()),
                ("pseudolife_memory-20200101-000000.sql.gz", OLD),  # Docker tier: never ours
                ("notes.txt", OLD),
            ]:
                _write(bdir / name, b"old", mtime)
        for directory in [root / "memory_state", root / "chromadb", root / "notes-ü",
                          root / "empty", root / "embedded_pg", root / "backups",
                          root / "locked"]:
            if directory.exists():
                os.utime(directory, (FIXED, FIXED))
        os.chmod(root / "locked", stat.S_IREAD | stat.S_IEXEC)
    return setup


def with_pg0(inner=None):
    """Hard-link the real bundled pg_dump into the arm home's ~/.pg0."""
    def setup(arm):
        if inner:
            inner(arm)
        source = _pg0_version() / "bin"
        target = arm.home / ".pg0" / "installation" / _pg0_version().name / "bin"
        target.mkdir(parents=True)
        for item in source.iterdir():
            if item.is_file():
                try:
                    os.link(item, target / item.name)
                except OSError:  # another filesystem (Linux /tmp)
                    shutil.copy2(item, target / item.name)
    return setup


def with_symlink(arm):
    tree()(arm)
    link = arm.home / "bank" / "link"
    os.symlink("config.yaml", link)
    os.utime(link, (FIXED, FIXED), follow_symlinks=False)


def drop_pg0(arm, _obs):
    shutil.rmtree(arm.home / ".pg0", ignore_errors=True)


def check_bank(arm, obs):
    drop_pg0(arm, obs)
    if _BASELINE is not None and _bank.dump(DB) != _BASELINE:
        raise RuntimeError(f"{arm.name}: backup changed the source bank")
    obs["db"] = {"source_unchanged": True}


# ---------------------------------------------------------------- rules

_TS = re.compile(rb"(pseudolife_lite_(?:memory|state)-)([0-9]{8}-[0-9]{6})(\.)")


def _in_window(stamp: bytes, window: list[float]) -> bool:
    wanted = stamp.decode()
    return any(time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) == wanted
               for t in range(int(window[0]) - 1, int(window[1]) + 2))


def _stamp(data: bytes, window: list[float]) -> bytes:
    return _TS.sub(lambda m: m.group(1) + b"<ts>" + m.group(3)
                   if _in_window(m.group(2), window) else m.group(0), data)


def _tar_view(data: bytes) -> bytes:
    members = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            content = tar.extractfile(member).read() if member.isreg() else b""
            members.append({
                "name": member.name, "type": member.type.decode(), "mode": oct(member.mode),
                "uid": member.uid, "gid": member.gid,
                "uname": member.uname if WINDOWS else "<free>",
                "gname": member.gname if WINDOWS else "<free>",
                "mtime": repr(member.mtime), "size": member.size,
                "linkname": member.linkname, "sha256": hashlib.sha256(content).hexdigest(),
            })
    return json.dumps(members, indent=1, ensure_ascii=False).encode()


_RESTRICT = re.compile(rb"^\\restrict ([A-Za-z0-9]+)(?=\r?$)", re.M)


def _dump_view(data: bytes) -> bytes:
    """Decompressed SQL; the per-run key becomes <key> only when exactly one
    \\restrict and one \\unrestrict carry the same key."""
    text = gzip.decompress(data)
    keys = _RESTRICT.findall(text)
    if len(keys) == 1:
        key = keys[0]
        pair = (b"\\restrict " + key, b"\\unrestrict " + key)
        if all(text.count(token) == 1 for token in pair):
            text = text.replace(pair[0], b"\\restrict <key>").replace(
                pair[1], b"\\unrestrict <key>")
    return text


@normalize.rule("backup-files")
def backup_files(obs: dict) -> None:
    """Timestamps in the new file names (validated against the arm's own
    window), archives read back by member, dumps decompressed. Two files that
    normalize to one name stay distinct, so an extra file still shows."""
    window = obs["window"]
    for field in ("stdout", "stderr"):
        obs[field] = base64.b64encode(
            _stamp(base64.b64decode(obs[field]), window)).decode()
    files = {}
    for rel, value in obs["files"].items():
        new_rel = _stamp(rel.encode(), window).decode()
        if value.startswith("file:") and new_rel != rel:
            data = base64.b64decode(value[5:])
            view = _tar_view(data) if rel.endswith(".tar.gz") else _dump_view(data)
            value = "file:" + base64.b64encode(view).decode()
        while new_rel in files:
            new_rel += " <normalized-collision>"
        files[new_rel] = value
    obs["files"] = files


_SHUTDOWN_FLUSH = re.compile(rb"Exception ignored in: <_io\.TextIOWrapper name='<stdout>'"
                             rb"[^\r\n]*\r?\n(?:BrokenPipeError|OSError): [^\r\n]*\r?\n\Z")


@normalize.rule("backup-stdout-closed")
def backup_stdout_closed(obs: dict) -> None:
    """Declared substitution ``backup-stdout-closed``: CPython reports its
    failed shutdown flush of a closed stdout with an ignored-exception trailer
    and exit 120; the native leaf exits 120 without the interpreter trailer.
    Only that exact trailer with exit 120 is removed."""
    stderr = base64.b64decode(obs["stderr"])
    if obs["exit"] == 120 and _SHUTDOWN_FLUSH.search(stderr):
        obs["stderr"] = base64.b64encode(_SHUTDOWN_FLUSH.sub(b"", stderr)).decode()


# ---------------------------------------------------------------- cases

RULES = ("backup-files",)


def _have_pg0() -> bool:
    return PG0.is_dir() and any((p / "bin").is_dir() for p in PG0.iterdir())


def cases() -> list[Case]:
    # DSN cases need the bundled pg_dump this host's home carries.
    have_pg0 = _have_pg0()
    dsn = _source() if have_pg0 else "postgresql://unused"
    absent = _bank.url(ABSENT_DB) if have_pg0 else "postgresql://unused"
    pg0_bin = str(_pg0_version() / "bin") if have_pg0 else ""
    dsn_platforms = ("windows", "linux") if have_pg0 else ()
    sep = "\\" if WINDOWS else "/"
    data = ["--data-dir", f"{{HOME}}{sep}bank"]
    no_pg_dump = {"PATH": f"{{HOME}}{sep}no-bin"}
    return [
        Case("help", ["backup", "--help"]),
        Case("missing-absolute", ["backup", "--data-dir", f"{{HOME}}{sep}absent-µ"]),
        Case("missing-relative", ["backup", "--data-dir", "absent-µ"]),
        Case("file-mode", ["backup", *data], setup=tree(), rules=RULES),
        Case("file-mode-env-data-dir", ["backup"],
             env={"PSEUDOLIFE_MCP_DATA_DIR": f"{{HOME}}{sep}bank"},
             setup=tree(), rules=RULES),
        Case("file-mode-cwd-data", ["backup"], setup=tree("cwd/data"), rules=RULES),
        Case("file-mode-out-absolute-keep-half-day",
             ["backup", *data, "--out", f"{{HOME}}{sep}elsewhere", "--keep-days", "0.5"],
             setup=tree(bdir_rel="elsewhere"), rules=RULES),
        Case("file-mode-out-relative", ["backup", *data, "--out", "bk"],
             setup=tree(bdir_rel="cwd/bk"), rules=RULES),
        Case("file-mode-out-direct-child",
             ["backup", *data, "--out", f"{{HOME}}{sep}bank{sep}mybk"],
             setup=tree(bdir_rel="bank/mybk"), rules=RULES),
        Case("file-mode-keep-one-minute", ["backup", *data, "--keep-days", "0.0007"],
             setup=tree(), rules=RULES),
        Case("file-mode-empty-data-dir", ["backup", *data],
             setup=lambda arm: (arm.home / "bank").mkdir(), rules=RULES),
        Case("file-mode-stdout-closed", ["backup", *data], setup=tree(), stdout_closed=True,
             rules=RULES + ("backup-stdout-closed",)),
        # DSN cases look pg_dump up (the home's ~/.pg0 bundle, then PATH):
        # core's preflight proves PATH finds none outside the home, except
        # in dsn-path, which runs this host's bundled pg_dump from its PATH.
        Case("dsn-pg0-bundle", ["backup", *data],
             env={"PSEUDOLIFE_MCP_DATABASE_URL": dsn, **no_pg_dump},
             setup=with_pg0(tree()), after=check_bank, platforms=dsn_platforms, rules=RULES, timeout=120,
             programs=("pg_dump",)),
        Case("dsn-path", ["backup", *data, "--keep-days", "10"],
             env={"PSEUDOLIFE_MCP_DATABASE_URL": dsn,
                  "PATH": pg0_bin + os.pathsep + f"{{HOME}}{sep}no-bin"},
             setup=tree(), after=check_bank, platforms=dsn_platforms, rules=RULES, timeout=120,
             real_programs=("pg_dump",)),
        Case("dsn-pg-dump-missing", ["backup", *data],
             env={"PSEUDOLIFE_MCP_DATABASE_URL": dsn, **no_pg_dump},
             setup=tree(), platforms=dsn_platforms, rules=RULES, programs=("pg_dump",)),
        Case("dsn-pg-dump-fails", ["backup", *data],
             env={"PSEUDOLIFE_MCP_DATABASE_URL": absent, **no_pg_dump},
             setup=with_pg0(tree()), after=drop_pg0, platforms=dsn_platforms, rules=RULES, timeout=60,
             programs=("pg_dump",)),
        Case("posix-symlink-member", ["backup", *data], setup=with_symlink,
             platforms=("linux",), rules=RULES),
    ]


B = "shim/src/cli/backup/"
MUTANTS = [
    Mutant("backup-archives-backups-dir", "backup", B + "archive.rs",
           'const EXCLUDE: [&str; 2] = ["embedded_pg", "backups"];',
           'const EXCLUDE: [&str; 2] = ["embedded_pg", "embedded_pg"];',
           ("file-mode", "file-mode-out-absolute-keep-half-day")),
    Mutant("backup-rotates-dumps-in-file-mode", "backup", B + "mod.rs",
           "    if include_dumps {\n        patterns.insert(0, (DUMP_PREFIX, \".sql.gz\"));",
           "    if true {\n        patterns.insert(0, (DUMP_PREFIX, \".sql.gz\"));", ("file-mode",)),
    Mutant("backup-rotation-order", "backup", B + "mod.rs",
           "names.sort_by_key(|name| path_sort_key(name));",
           "names.sort_by_key(|name| std::cmp::Reverse(path_sort_key(name)));", ("file-mode",)),
    Mutant("backup-nested-member-order", "backup", B + "archive.rs",
           "        names.sort();\n",
           "        names.sort_by_key(|name| name.to_lowercase());\n", ("file-mode",)),
    Mutant("backup-dump-keeps-owner", "backup", B + "dump.rs",
           '.args(["--no-owner", "--no-acl", "--dbname", dsn])',
           '.args(["--no-acl", "--dbname", dsn])', ("dsn-path",)),
    Mutant("backup-skipped-wording", "backup", B + "mod.rs",
           "file-mode state archived only", "file mode state archived only", ("file-mode",)),
    Mutant("backup-pg-dump-failure-strip", "backup", B + "dump.rs",
           'format!("pg_dump failed: {}", python_strip(&text))',
           'format!("pg_dump failed: {}", text)', ("dsn-pg-dump-fails",)),
    Mutant("backup-stdout-failure-exit", "backup", B + "mod.rs",
           "    } else {\n        120\n    })",
           "    } else {\n        0\n    })",
           ("file-mode-stdout-closed",)),
]
if WINDOWS:
    MUTANTS += [
        Mutant("backup-member-mode-windows", "backup", B + "archive.rs",
               "let mut mode = if readonly { 0o444 } else { 0o666 };",
               "let mut mode = if readonly { 0o444 } else { 0o644 };", ("file-mode",)),
        Mutant("backup-dotfile-suffix-windows", "backup", B + "archive.rs",
               "let text = path.to_string_lossy();",
               "let text = path.extension().map(|ext| format!(\".{}\", ext.to_string_lossy()))"
               ".unwrap_or_default();",
               ("file-mode",)),
        Mutant("backup-top-order-windows", "backup", B + "archive.rs",
               "children.sort_by_key(|(name, _)| super::path_sort_key(name));",
               "children.sort();", ("file-mode",)),
    ]
else:
    MUTANTS += [
        Mutant("backup-member-mode-posix", "backup", B + "archive.rs",
               "meta.permissions().mode() & 0o7777", "meta.permissions().mode() & 0o7700",
               ("file-mode",)),
    ]

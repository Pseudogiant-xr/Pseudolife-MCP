"""CLI-BACKUP-DSN: ``pseudolife-mcp backup`` (explicit DSN and file mode).

Canonical argv: ``backup [--data-dir P] [--out P] [--keep-days N]`` with
space-separated values, each option at most once, P a canonical path
(``str(Path(P)) == P``, no ``..``) and N a plain decimal; plus ``--help`` at
COLUMNS=80. The bank is the explicit ``PSEUDOLIFE_MCP_DATABASE_URL``; without
one, a data dir holding no embedded bank is archived in file mode. Every case
that would need the lite tier's embedded instance defers (Rust tests cover the
deferrals; this row compares only answered cases).

Contract per arm: exit code, both streams byte-exact (the timestamp in the
two file names is normalized against the arm's own clock window), every file
under the home, the state archive read back member by member (name, type,
mode, uid, gid, uname, gname, mtime, size, link target, content hash), the
dump decompressed (pg_dump's per-run ``\\restrict`` key normalized), and the
source bank unchanged. Container bytes (tar header layout, gzip headers,
compression) are free.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import time
from pathlib import Path

from .. import normalize
from ..core import WINDOWS, Case
from ..mutants import Mutant
from . import _bank

DB = "pl_cf_w1c_backup_src"
ABSENT_DB = "pl_cf_w1c_backup_absent"
PG0 = Path.home() / ".pg0" / "installation"
_SEEDED = False
_BASELINE: dict | None = None
FIXED = 1_790_000_000.123456  # seeded mtimes, identical in both arms
OLD = time.time() - 30 * 86400


def _source() -> str:
    global _SEEDED, _BASELINE
    if not _SEEDED:
        _bank.create(DB)
        from tests.test_transfer_cli import _seed_bank  # noqa: PLC0415 (oracle seeder)
        with _bank.connect(DB) as conn:
            _seed_bank(conn)
            conn.commit()
        _BASELINE = _bank.dump(DB)
        _SEEDED = True
    return _bank.url(DB)


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
        _write(root / "run.cmd", "@echo off\n")
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
                          root / "empty", root / "embedded_pg", root / "backups"]:
            if not directory.exists():
                continue
            os.utime(directory, (FIXED, FIXED))
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
                os.link(item, target / item.name)
    return setup


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


_RESTRICT = re.compile(rb"^(\\(?:un)?restrict )[A-Za-z0-9]+(?=\r?$)", re.M)


def _dump_view(data: bytes) -> bytes:
    text = gzip.decompress(data)
    return _RESTRICT.sub(rb"\1<key>", text)


@normalize.rule("backup-files")
def backup_files(obs: dict) -> None:
    """Timestamps in the two new file names (validated against the arm's own
    window), archives read back by member, dumps decompressed."""
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
        files[new_rel] = value
    obs["files"] = files


# ---------------------------------------------------------------- cases

RULES = ("backup-files",)


def cases() -> list[Case]:
    dsn = _source()
    absent = _bank.url(ABSENT_DB)
    pg0_bin = str(_pg0_version() / "bin")
    data = ["--data-dir", "{HOME}/bank"] if not WINDOWS else ["--data-dir", "{HOME}\\bank"]
    sep = "\\" if WINDOWS else "/"
    return [
        Case("help", ["backup", "--help"]),
        Case("missing-absolute", ["backup", "--data-dir", f"{{HOME}}{sep}absent-µ"]),
        Case("missing-relative", ["backup", "--data-dir", "absent-µ"]),
        Case("file-mode", ["backup", *data], setup=tree(), rules=RULES),
        Case("file-mode-env-data-dir", ["backup"], env={"PSEUDOLIFE_MCP_DATA_DIR": f"{{HOME}}{sep}bank"},
             setup=tree(), rules=RULES),
        Case("file-mode-cwd-data", ["backup"], setup=tree("cwd/data"), rules=RULES),
        Case("file-mode-out-absolute-keep-half-day",
             ["backup", *data, "--out", f"{{HOME}}{sep}elsewhere", "--keep-days", "0.5"],
             setup=tree(bdir_rel="elsewhere"), rules=RULES),
        Case("file-mode-out-relative", ["backup", *data, "--out", "bk"],
             setup=tree(bdir_rel="cwd/bk"), rules=RULES),
        Case("file-mode-out-direct-child", ["backup", *data, "--out", f"{{HOME}}{sep}bank{sep}mybk"],
             setup=tree(bdir_rel="bank/mybk"), rules=RULES),
        Case("file-mode-keep-one-minute", ["backup", *data, "--keep-days", "0.0007"],
             setup=tree(), rules=RULES),
        Case("file-mode-empty-data-dir", ["backup", *data],
             setup=lambda arm: (arm.home / "bank").mkdir(), rules=RULES),
        Case("dsn-pg0-bundle", ["backup", *data], env={"PSEUDOLIFE_MCP_DATABASE_URL": dsn},
             setup=with_pg0(tree()), after=check_bank, rules=RULES, timeout=120),
        Case("dsn-path", ["backup", *data, "--keep-days", "10"],
             env={"PSEUDOLIFE_MCP_DATABASE_URL": dsn,
                  "PATH": pg0_bin + os.pathsep + os.environ.get("PATH", "")},
             setup=tree(), after=check_bank, rules=RULES, timeout=120),
        Case("dsn-pg-dump-missing", ["backup", *data], env={"PSEUDOLIFE_MCP_DATABASE_URL": dsn},
             setup=tree(), rules=RULES),
        Case("dsn-pg-dump-fails", ["backup", *data], env={"PSEUDOLIFE_MCP_DATABASE_URL": absent},
             setup=with_pg0(tree()), after=drop_pg0, rules=RULES, timeout=60),
        Case("posix-symlink-member", ["backup", *data],
             setup=lambda arm: (tree()(arm), os.symlink("config.yaml", arm.home / "bank" / "link")),
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
    Mutant("backup-dump-keeps-owner", "backup", B + "dump.rs",
           '.args(["--no-owner", "--no-acl", "--dbname", dsn])',
           '.args(["--no-acl", "--dbname", dsn])', ("dsn-path",)),
    Mutant("backup-member-mode", "backup", B + "archive.rs",
           "let mut mode = if readonly { 0o444 } else { 0o666 };",
           "let mut mode = if readonly { 0o444 } else { 0o644 };", ("file-mode",)),
    Mutant("backup-skipped-wording", "backup", B + "mod.rs",
           "file-mode state archived only", "file mode state archived only", ("file-mode",)),
    Mutant("backup-pg-dump-failure-strip", "backup", B + "dump.rs",
           'format!("pg_dump failed: {}", python_strip(&text))',
           'format!("pg_dump failed: {}", text)', ("dsn-pg-dump-fails",)),
]

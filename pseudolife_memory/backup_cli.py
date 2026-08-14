"""``pseudolife-mcp backup`` — tier-agnostic bank backup.

The Docker tier keeps ``ops/backup.ps1`` (pg_dump inside the container +
state-volume tar). This command serves the pip tiers, mirroring that
script's shape:

* pg_dump | gzip of the bank when a DSN is resolvable — the explicit
  ``PSEUDOLIFE_MCP_DATABASE_URL``, or the lite tier's embedded instance
  (attached, or started for the duration; a data dir with no existing
  pgdata is NEVER initialized by a backup);
* a tar.gz of the rest of the data dir (chromadb, memory_state, config)
  — a pg_dump alone loses every ``document_ingest`` on restore, same
  reasoning as ops/backup.ps1;
* rotation of this tool's own files past ``--keep-days`` (default 7),
  run only after the fresh backup succeeded, and never touching files
  it didn't create.
"""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Mapping

from pseudolife_memory.storage import embedded_pg

# Deliberately disjoint from the Docker tier's artifact names
# (ops/backup.* writes `pseudolife_memory-*.sql.gz` / `pseudolife_state-*.tgz`
# into the same default `data/backups` directory, and ops/restore.* globs
# `pseudolife_memory-*.sql.gz` for its newest-file candidate): sharing that
# namespace would let this tool's rotation delete real Docker-tier bank
# dumps, and would feed lite dumps to the Docker restore picker
# (review finding, 2026-08-14). `pseudolife_lite_*` matches neither glob.
_DUMP_PREFIX = "pseudolife_lite_memory-"
_STATE_PREFIX = "pseudolife_lite_state-"
# Never archived into the state tar: the dump covers the bank, and the
# backups dir must not recursively swallow itself.
_STATE_EXCLUDE = {"embedded_pg", "backups"}


def perform_backup(
    data_dir: Path,
    keep_days: float = 7.0,
    out: Path | None = None,
    environ: Mapping[str, str] = os.environ,
) -> dict:
    """Back up the bank at ``data_dir``. Returns
    ``{"dump": Path | None, "state": Path, "pruned": [Path, ...]}``."""
    data_dir = Path(data_dir)
    bdir = Path(out) if out else data_dir / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")

    dsn = environ.get("PSEUDOLIFE_MCP_DATABASE_URL")
    own_instance = None
    if not dsn and embedded_pg.available():
        pgdata = data_dir / "embedded_pg"
        if (pgdata / "PG_VERSION").exists():
            dsn, own_instance = embedded_pg.attach_or_start(data_dir)
        # No PG_VERSION: nothing to dump — a backup must never be the
        # thing that initializes a bank.

    dump: Path | None = None
    try:
        if dsn:
            pg_dump = _find_pg_dump()
            if pg_dump is None:
                raise RuntimeError(
                    "pg_dump not found — install PostgreSQL client tools "
                    "or the pseudolife-mcp[lite] extra (whose embedded "
                    "runtime bundles it)."
                )
            dump = _dump_bank(dsn, pg_dump, bdir, ts)
        state = _archive_state(data_dir, bdir, ts)
    finally:
        if own_instance is not None:
            # Stop exactly the instance THIS call started; attaching to a
            # running daemon's instance must never shut it down.
            own_instance.stop()

    # Rotate strictly after a successful fresh backup, and only the kinds
    # this run actually produced — a run that made no bank dump (file
    # mode) must never reduce the number of dump copies on disk.
    pruned = _rotate(bdir, keep_days, include_dumps=dump is not None)
    return {"dump": dump, "state": state, "pruned": pruned}


def _find_pg_dump(home: Path | None = None) -> Path | None:
    """Locate pg_dump: the lite tier's bundled binary first, then PATH.

    The bundled one (under ``~/.pg0/installation/<ver>/bin``) matches the
    embedded server's major exactly, which PATH's — if any — may not.
    """
    home = home or Path.home()
    exe = "pg_dump.exe" if os.name == "nt" else "pg_dump"
    install_root = home / ".pg0" / "installation"
    if install_root.is_dir():
        for vdir in sorted(install_root.iterdir(), reverse=True):
            cand = vdir / "bin" / exe
            if cand.exists():
                return cand
    which = shutil.which("pg_dump")
    return Path(which) if which else None


def _dump_bank(dsn: str, pg_dump: Path, bdir: Path, ts: str) -> Path:
    target = bdir / f"{_DUMP_PREFIX}{ts}.sql.gz"
    partial = bdir / f"{_DUMP_PREFIX}{ts}.sql.gz.part"
    # --no-owner --no-acl: the embedded instance's role is `postgres`,
    # the Docker tier's is `pseudolife` — an owner-qualified dump aborts
    # the documented lite→Docker restore path with `role "postgres" does
    # not exist` under restore.*'s ON_ERROR_STOP (review finding,
    # 2026-08-14; restorability is pinned by the roundtrip test).
    proc = subprocess.Popen(
        [str(pg_dump), "--no-owner", "--no-acl", "--dbname", dsn],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        with gzip.open(partial, "wb", compresslevel=9) as gz:
            shutil.copyfileobj(proc.stdout, gz)
        stderr = proc.stderr.read().decode(errors="replace")
        if proc.wait() != 0:
            raise RuntimeError(f"pg_dump failed: {stderr.strip()}")
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.rename(target)
    return target


def _archive_state(data_dir: Path, bdir: Path, ts: str) -> Path:
    target = bdir / f"{_STATE_PREFIX}{ts}.tar.gz"
    partial = bdir / f"{_STATE_PREFIX}{ts}.tar.gz.part"
    bdir_resolved = bdir.resolve()
    try:
        with tarfile.open(partial, "w:gz") as tar:
            for child in sorted(Path(data_dir).iterdir()):
                if child.name in _STATE_EXCLUDE:
                    continue
                if child.resolve() == bdir_resolved:
                    continue  # --out placed inside the data dir
                tar.add(child, arcname=child.name)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.rename(target)
    return target


def _rotate(
    bdir: Path, keep_days: float, include_dumps: bool = True,
) -> list[Path]:
    """Delete this tool's own backup files older than ``keep_days``.

    ``include_dumps=False`` (a run that produced no bank dump) rotates
    only state archives — old dump copies are never reduced by a run
    that added none.
    """
    cutoff = time.time() - keep_days * 86400
    patterns = [f"{_STATE_PREFIX}*.tar.gz"]
    if include_dumps:
        patterns.insert(0, f"{_DUMP_PREFIX}*.sql.gz")
    pruned: list[Path] = []
    for pattern in patterns:
        for f in sorted(Path(bdir).glob(pattern)):
            if f.stat().st_mtime < cutoff:
                f.unlink()
                pruned.append(f)
    return pruned


def _default_data_dir(environ: Mapping[str, str]) -> Path:
    """Mirror the daemon's data-dir resolution (embedded_pg + mcp_server)."""
    raw = environ.get("PSEUDOLIFE_MCP_DATA_DIR")
    if raw:
        return Path(raw)
    lite_default = embedded_pg.default_lite_data_dir()
    if embedded_pg.available() and lite_default.exists():
        return lite_default
    return Path.cwd() / "data"


def run_backup() -> None:
    parser = argparse.ArgumentParser(
        prog="pseudolife-mcp backup",
        description="Back up the memory bank (pg_dump + state archive).",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=None,
        help="bank data dir (default: PSEUDOLIFE_MCP_DATA_DIR, the lite "
             "tier's per-user dir, or ./data)",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="destination dir (default: <data-dir>/backups)",
    )
    parser.add_argument(
        "--keep-days", type=float, default=7.0,
        help="rotate this tool's own backups older than N days (default 7)",
    )
    args = parser.parse_args(sys.argv[2:])
    data_dir = args.data_dir or _default_data_dir(os.environ)
    if not data_dir.exists():
        print(f"data dir {data_dir} does not exist — nothing to back up.",
              file=sys.stderr)
        sys.exit(1)
    try:
        result = perform_backup(
            data_dir, keep_days=args.keep_days, out=args.out,
        )
    except RuntimeError as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        sys.exit(1)
    if result["dump"]:
        print(f"bank dump:     {result['dump']}")
    else:
        print("bank dump:     skipped (no database configured — file-mode "
              "state archived only)")
    print(f"state archive: {result['state']}")
    for f in result["pruned"]:
        print(f"rotated out:   {f}")

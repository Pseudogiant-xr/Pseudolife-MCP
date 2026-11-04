"""Docker bank artifacts and restore, independent of any compose checkout."""
from __future__ import annotations

import json
import os
import posixpath
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from pseudolife_memory import update_cli as up


def validate_state(path: Path) -> None:
    """Reject paths and links that could escape the state volume on replay."""
    try:
        with tarfile.open(path, "r:gz") as archive:
            for item in archive:
                name = posixpath.normpath(item.name)
                if name.startswith("/") or name == ".." or name.startswith("../") or "\\" in name:
                    raise ValueError("path")
                if item.isdev() or item.isfifo():
                    raise ValueError("special file")
                if item.issym() or item.islnk():
                    link = posixpath.normpath(posixpath.join(posixpath.dirname(name) if item.issym() else "", item.linkname))
                    if link.startswith("/") or link == ".." or link.startswith("../") or "\\" in link:
                        raise ValueError("link")
    except (OSError, tarfile.TarError, ValueError) as exc:
        raise up.UpdateError("the state archive is unreadable or contains unsafe paths; use a complete, "
                             "unmodified state backup. Nothing was restored", 2) from exc


def _ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def copy_from_container(docker: str, source: str, target: Path) -> None:
    """Read Docker's tar stream ourselves: a sudo Docker CLI otherwise
    creates root-owned host files that this user's backup cannot protect."""
    try:
        with tempfile.TemporaryFile() as stream:
            proc = subprocess.run([docker, "cp", source, "-"], stdout=stream, stderr=subprocess.PIPE,
                                  stdin=subprocess.DEVNULL, timeout=3600, check=False)
            if proc.returncode:
                raise ValueError("copy")
            stream.seek(0)
            with tarfile.open(fileobj=stream, mode="r:") as archive:
                members = archive.getmembers()
                if len(members) != 1 or not members[0].isfile():
                    raise ValueError("file")
                with archive.extractfile(members[0]) as content:
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as output:
                        shutil.copyfileobj(content, output)
    except (OSError, ValueError, tarfile.TarError, subprocess.TimeoutExpired) as exc:
        raise up.UpdateError("the Docker backup file could not be copied out; check Docker and free space, "
                             "then retry backup before restoring anything") from exc


class DockerBank:
    def __init__(self, *, container: str | None = None, daemon_container: str | None = None,
                 db: str | None = None, user: str | None = None, log=print):
        self.docker = up.docker_cmd()
        self.pg = container or up.PG_CONTAINER
        self.daemon = daemon_container or up.DAEMON_CONTAINER
        self.log = log
        self.running = self.inspect(self.daemon)
        pg = self.inspect(self.pg)
        env = dict(v.split("=", 1) for v in self.running.get("Config", {}).get("Env", []) if "=" in v)
        pg_env = dict(v.split("=", 1) for v in pg.get("Config", {}).get("Env", []) if "=" in v)
        source = {}
        if env.get("PSEUDOLIFE_MCP_DATABASE_URL"):
            from psycopg.conninfo import conninfo_to_dict
            try:
                source = conninfo_to_dict(env["PSEUDOLIFE_MCP_DATABASE_URL"])
            except Exception:  # driver errors may include the credential-bearing URL
                raise up.UpdateError("cannot read the daemon's database configuration; restore its original "
                                     "PSEUDOLIFE_MCP_DATABASE_URL and retry. Nothing was restored", 2) from None
        if source.get("host") and container is None:
            names = {self.pg, (pg.get("Name") or "").lstrip("/"), pg.get("Config", {}).get("Hostname")}
            for network in pg.get("NetworkSettings", {}).get("Networks", {}).values():
                names.update(network.get("Aliases") or [])
                names.add(network.get("IPAddress"))
            if source["host"] not in names:
                raise up.UpdateError("the daemon's database host cannot be matched to the default PostgreSQL "
                                     "container; name its actual database container with --container. Nothing was restored", 2)
        self.user = user or source.get("user") or pg_env.get("POSTGRES_USER") or "postgres"
        self.db = db or source.get("dbname") or pg_env.get("POSTGRES_DB") or self.user
        self.image = self.running.get("Image")
        if not self.image or not any(m.get("Destination") == "/data" for m in self.running.get("Mounts", [])):
            raise up.UpdateError("the daemon has no reusable /data mount or local image; restore its Docker "
                                 "container configuration before backing up or restoring", 2)
        if not pg.get("State", {}).get("Running"):
            raise up.UpdateError(f"PostgreSQL is stopped; run docker start {self.pg} and retry. Nothing was restored", 2)

    def command(self, *args, check=True, timeout=3600):
        code, output = up.run_cli([self.docker, *args], timeout=timeout, stdout_only=True)
        if check and code:
            # Docker/psql diagnostics can include private data or resolved URLs.
            raise up.UpdateError(f"Docker {args[0]} failed for the backup/restore; check the containers "
                                 f"with docker logs {self.pg} and docker logs {self.daemon}, then retry")
        return code, output

    def inspect(self, container):
        _, output = self.command("inspect", container, timeout=60)
        try:
            result = json.loads(output)[0]
            if not isinstance(result, dict):
                raise ValueError("container")
            return result
        except (ValueError, IndexError, TypeError) as exc:
            raise up.UpdateError(f"cannot inspect {container}; restore its Docker container configuration and retry", 2) from exc

    @contextmanager
    def helper(self, script, *, writable=False):
        _, output = self.command("create", "--network", "none", "--user", "0", "--volumes-from",
                                  self.daemon + ("" if writable else ":ro"), "--entrypoint", "sh", self.image,
                                  "-c", script)
        helper = output.strip()
        if not helper:
            raise up.UpdateError("Docker did not create the state helper; retry after Docker answers")
        try:
            yield helper
        finally:
            self.command("rm", "-f", helper, check=False, timeout=120)

    def backup(self, *, out: Path | None = None, keep_days=7.0) -> dict:
        directory = Path(out) if out else up.data_dir() / "backups"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = up._stamp() + "-" + secrets.token_hex(4)
        dump = directory / f"pseudolife_memory-{stamp}.sql.gz"
        state = directory / f"pseudolife_state-{stamp}.tgz"
        dump_part, state_part = Path(str(dump) + ".part"), Path(str(state) + ".part")
        remote = f"/tmp/pl_backup-{stamp}.sql.gz"
        state_tmp = f"/tmp/pl_state-{stamp}.tgz"
        try:
            self.command("exec", self.pg, "sh", "-c", 'pg_dump --no-owner --no-acl -U "$1" -d "$2" -Z9 > "$3"',
                         "_", self.user, self.db, remote)
            copy_from_container(self.docker, f"{self.pg}:{remote}", dump_part)
            if not up._dump_complete(dump_part):
                raise up.UpdateError("the database dump is incomplete; retry backup before restoring anything")
            # A helper also works when move or an operator already stopped the
            # daemon. Read-only inherited mounts keep this step observational.
            with self.helper(f"tar czf {state_tmp} -C /data .") as helper:
                self.command("start", "-a", helper)
                copy_from_container(self.docker, f"{helper}:{state_tmp}", state_part)
            validate_state(state_part)
            dump_part.replace(dump)
            state_part.replace(state)
        finally:
            self.command("exec", self.pg, "rm", "-f", remote, check=False, timeout=120)
            dump_part.unlink(missing_ok=True)
            state_part.unlink(missing_ok=True)
        # Only this implementation's nonce-stamped files are rotated; legacy
        # script/update artifacts and incomplete runs are outside its namespace.
        pruned = []
        for prefix, suffix in (("pseudolife_memory", "sql.gz"), ("pseudolife_state", "tgz")):
            pattern = rf"{prefix}-\d{{8}}-\d{{6}}-[0-9a-f]{{8}}\.{re.escape(suffix)}"
            files = sorted((p for p in directory.iterdir() if re.fullmatch(pattern, p.name)), reverse=True)
            for path in files[3:]:
                if path.stat().st_mtime < time.time() - keep_days * 86400:
                    path.unlink()
                    pruned.append(path)
        return {"dump": dump, "state": state, "pruned": pruned}

    def restore(self, backup_file: Path, *, state_archive: Path | None = None, leave_stopped=False) -> dict:
        backup_file = Path(backup_file).resolve()
        if not up._dump_complete(backup_file):
            raise up.UpdateError("the backup is unreadable or incomplete; use a complete .sql.gz bank dump. "
                                 "Nothing was restored", 2)
        if state_archive is not None:
            state_archive = Path(state_archive).resolve()
            validate_state(state_archive)
        # The safety pair deliberately ignores PSEUDOLIFE_MCP_DATA_DIR: a
        # checkout's ./data is not a durable recovery location.
        safety_dir = up.home() / ".pseudolife-mcp" / "backups"
        if any((parent / ".git").exists() for parent in (safety_dir, *safety_dir.parents)):
            raise up.UpdateError("the per-user safety backup directory is inside a checkout; move the checkout "
                                 "out of ~/.pseudolife-mcp before restoring. Nothing was restored", 2)
        was_running = self.running.get("State", {}).get("Running", False)
        destructive = False
        safety = None
        remote = f"/tmp/pl_restore-{secrets.token_hex(8)}.sql.gz"
        try:
            if was_running:
                self.command("stop", self.daemon)
            self.log("==> Safety-dumping the current database and state volume...")
            safety = self.backup(out=safety_dir)
            self.log(f"Safety backup: {safety['dump']} + {safety['state']}")
            self.command("cp", str(backup_file), f"{self.pg}:{remote}")
            self.log(f"==> Dropping + recreating {self.db}...")
            destructive = True
            database = _ident(self.db)
            self.command("exec", self.pg, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", self.user, "-d", "postgres",
                         "-c", f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")
            self.command("exec", self.pg, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", self.user, "-d", "postgres",
                         "-c", f"CREATE DATABASE {database}")
            self.command("exec", self.pg, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", self.user, "-d", "postgres",
                         "-c", f"REVOKE CONNECT ON DATABASE {database} FROM PUBLIC")
            # Separate decompression from psql: POSIX sh has no pipefail, so
            # a failed gunzip must not look like a successful empty restore.
            sql_tmp = remote + ".sql"
            try:
                self.command("exec", self.pg, "sh", "-c", 'gunzip -c "$1" > "$2"', "_", remote, sql_tmp)
                self.command("exec", self.pg, "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", self.user,
                             "-d", self.db, "-f", sql_tmp)
            finally:
                self.command("exec", self.pg, "rm", "-f", sql_tmp, check=False, timeout=120)
            if state_archive is not None:
                # Preserve an active move gate throughout replay: restoring an
                # archive must never reopen a quiesced target behind move's back.
                script = ("find /data -mindepth 1 -maxdepth 1 ! -name move.json -exec rm -rf -- {} + && "
                          "tar --exclude=move.json --exclude=./move.json -xzf /tmp/pl_state.tgz -C /data")
                with self.helper(script, writable=True) as helper:
                    self.command("cp", str(state_archive), f"{helper}:/tmp/pl_state.tgz")
                    self.command("start", "-a", helper)
            if not leave_stopped:
                self.command("start", self.daemon)
            self.log("Restore complete; daemon " + ("left stopped (--leave-stopped)." if leave_stopped else "started."))
            return {"safety_dump": safety["dump"], "safety_state": safety["state"], "left_stopped": leave_stopped}
        except BaseException as exc:
            if not destructive and was_running:
                self.command("start", self.daemon, check=False, timeout=120)
            if destructive and isinstance(exc, Exception):
                raise up.UpdateError(f"restore failed after database replacement began; the daemon is left stopped. "
                                     f"Recover with pseudolife-mcp restore --backup-file {safety['dump']} "
                                     f"--state-archive {safety['state']} --yes") from exc
            raise
        finally:
            self.command("exec", self.pg, "rm", "-f", remote, check=False, timeout=120)

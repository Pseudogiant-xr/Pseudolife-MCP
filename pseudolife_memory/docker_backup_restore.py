"""Docker bank artifacts and restore, independent of any compose checkout."""
from __future__ import annotations

import gzip
import json
import os
import posixpath
import re
import secrets
import shlex
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
        # tarfile stops at tar's end markers and need not consume gzip's
        # trailer. Verify CRC and EOF before any restore can delete state.
        with gzip.open(path, "rb") as stream:
            while stream.read(1024 * 1024):
                pass
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            links = {posixpath.normpath(item.name): item.linkname for item in members if item.issym()}

            def resolve(parts, seen=frozenset()):
                current = []
                for part in parts:
                    if part in ("", "."):
                        continue
                    if part == "..":
                        if not current:
                            raise ValueError("escaping link")
                        current.pop()
                        continue
                    current.append(part)
                    key = "/".join(current)
                    if key in links:
                        if key in seen:
                            # A loop cannot be traversed by the filesystem,
                            # and therefore cannot escape the volume.
                            return None
                        target = links[key]
                        if target.startswith("/") or "\\" in target:
                            raise ValueError("absolute link")
                        current = resolve(current[:-1] + target.split("/"), seen | {key})
                        if current is None:
                            return None
                return current

            def check(item):
                name = posixpath.normpath(item.name)
                if name.startswith("/") or ".." in item.name.split("/") or "\\" in name:
                    raise ValueError("path")
                if item.isdev() or item.isfifo():
                    raise ValueError("special file")
                if item.issym() or item.islnk():
                    if item.linkname.startswith("/") or "\\" in item.linkname:
                        raise ValueError("link")
                    prefix = posixpath.dirname(name).split("/") if item.issym() else []
                    resolve(prefix + item.linkname.split("/"), frozenset({name}))
                resolve(name.split("/"))
            for item in members:
                check(item)
            # Duplicate entries can change an earlier link's meaning during
            # extraction. Check that transient graph as well as the final one.
            links.clear()
            for item in members:
                resolve(posixpath.normpath(item.name).split("/"))
                if item.issym():
                    links[posixpath.normpath(item.name)] = item.linkname
                else:
                    links.pop(posixpath.normpath(item.name), None)
                check(item)
    except (OSError, EOFError, tarfile.TarError, ValueError, RecursionError) as exc:
        raise up.UpdateError("the state archive is unreadable or contains unsafe paths; use a complete, "
                             "unmodified state backup. Nothing was restored", 2) from exc


def _ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _shell_command(argv) -> str:
    if os.name == "nt":
        return " ".join(arg if re.fullmatch(r"[A-Za-z0-9_./:\\-]+", arg)
                        else "'" + arg.replace("'", "''") + "'" for arg in argv)
    return shlex.join(argv)


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
                 db: str | None = None, user: str | None = None, db_port: int | None = None,
                 daemon_url: str | None = None, log=print):
        self.docker = up.docker_cmd()
        self.pg = container or up.PG_CONTAINER
        self.daemon = daemon_container or up.DAEMON_CONTAINER
        self.log = log
        self.running = self.inspect(self.daemon)
        pg = self.inspect(self.pg)
        env = dict(v.split("=", 1) for v in self.running.get("Config", {}).get("Env", []) if "=" in v)
        source = {}
        if env.get("PSEUDOLIFE_MCP_DATABASE_URL"):
            from psycopg.conninfo import conninfo_to_dict
            try:
                source = conninfo_to_dict(env["PSEUDOLIFE_MCP_DATABASE_URL"])
            except Exception:  # driver errors may include the credential-bearing URL
                raise up.UpdateError("cannot read the daemon's database configuration; restore its original "
                                     "PSEUDOLIFE_MCP_DATABASE_URL and retry. Nothing was restored", 2) from None
        if (source.get("service") or env.get("PGSERVICE")) and not (container and db and user):
            raise up.UpdateError("the daemon's libpq service profile cannot be resolved here; name the actual "
                                 "target with --container <postgres> --db <database> --user <role>. Nothing was restored", 2)
        source_host = source.get("hostaddr") or env.get("PGHOSTADDR") or source.get("host") or env.get("PGHOST")
        if source_host and container is None:
            networks = pg.get("NetworkSettings", {}).get("Networks", {})
            shared = set(self.running.get("NetworkSettings", {}).get("Networks", {})) & set(networks)
            names = set()
            for key in shared:
                network = networks[key]
                names.update((self.pg, (pg.get("Name") or "").lstrip("/"), pg.get("Config", {}).get("Hostname")))
                names.update(network.get("Aliases") or [])
                names.add(network.get("IPAddress"))
            if source_host not in names:
                raise up.UpdateError("the daemon's database host cannot be matched to the default PostgreSQL "
                                     "container; name its actual database container with --container. Nothing was restored", 2)
        # Container initialization variables do not describe libpq's current
        # connection. In particular, an omitted dbname defaults to its user.
        connection_user = source.get("user") or env.get("PGUSER")
        connection_db = source.get("dbname") or env.get("PGDATABASE") or connection_user
        if not env.get("PSEUDOLIFE_MCP_DATABASE_URL"):
            connection_user = connection_db = None
        self.user, self.db = user or connection_user, db or connection_db
        if not self.user or not self.db:
            raise up.UpdateError("the daemon's database defaults cannot be identified safely; name its actual "
                                 "bank with --db <database> --user <role>. Nothing was restored", 2)
        try:
            self.db_port = int(db_port if db_port is not None else
                               source.get("port") or env.get("PGPORT") or "5432")
            if not 1 <= self.db_port <= 65535:
                raise ValueError("port range")
        except (ValueError, TypeError) as exc:
            raise up.UpdateError("the database port is ambiguous; name the selected container's server port "
                                 "with --db-port <port>. Nothing was restored", 2) from exc
        self._database_arg(self.db)
        self.image = self.running.get("Image")
        if not self.image or not any(m.get("Destination") == "/data" for m in self.running.get("Mounts", [])):
            raise up.UpdateError("the daemon has no reusable /data mount or local image; restore its Docker "
                                 "container configuration before backing up or restoring", 2)
        if not pg.get("State", {}).get("Running"):
            raise up.UpdateError(f"PostgreSQL is stopped; run docker start {self.pg} and retry. Nothing was restored", 2)
        self.daemon_url = daemon_url
        host_ports = self.running.get("HostConfig", {}).get("PortBindings") or {}
        network_ports = self.running.get("NetworkSettings", {}).get("Ports") or {}
        bindings = host_ports.get("8765/tcp") or network_ports.get("8765/tcp") or []
        if not self.daemon_url and bindings:
            host = bindings[0].get("HostIp") or "127.0.0.1"
            host = "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
            host = f"[{host}]" if ":" in host else host
            self.daemon_url = f"http://{host}:{bindings[0]['HostPort']}"

    def command(self, *args, check=True, timeout=3600):
        code, output = up.run_cli([self.docker, *args], timeout=timeout, stdout_only=True, stderr_on_failure=True)
        if check and code:
            # Docker/psql diagnostics can include private data or resolved URLs.
            raise up.UpdateError(f"Docker {args[0]} failed for the backup/restore; check the containers "
                                 f"with docker logs {self.pg} and docker logs {self.daemon}, then retry")
        return code, output

    @staticmethod
    def _database_arg(database: str) -> str:
        from psycopg.conninfo import make_conninfo
        try:
            return make_conninfo(dbname=database)
        except Exception:
            raise up.UpdateError("the database selector is invalid; name its literal database with --db "
                                 "and retry. Nothing was restored", 2) from None

    def psql(self, database: str, *args):
        # The container is the target authority. Inherited libpq address or
        # service settings must never redirect a backup or destructive query.
        code, output = self.command("exec", self.pg, "env", "-u", "PGHOSTADDR", "-u", "PGSERVICE", "-u", "PGSERVICEFILE",
                                    "psql", "-X", "-h", "/var/run/postgresql", "-p", str(self.db_port),
                                    "-U", self.user, "-d", self._database_arg(database), *args, check=False)
        if code:
            reason = output.lower()
            if code == 2 and "socket" in reason and ("no such file" in reason or "connection refused" in reason):
                raise up.UpdateError(f"PostgreSQL's local socket is unavailable on port {self.db_port}; "
                                     "name the selected container's server port with --db-port <port> and retry", 2)
            raise up.UpdateError(f"PostgreSQL failed inside {self.pg}; check docker logs {self.pg} before retrying")
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

    def backup(self, *, out: Path | None = None, keep_days=7.0, include_database=True) -> dict:
        directory = Path(out) if out else up.data_dir() / "backups"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = up._stamp() + "-" + secrets.token_hex(4)
        dump = directory / f"pseudolife_memory-{stamp}.sql.gz"
        state = directory / f"pseudolife_state-{stamp}.tgz"
        dump_part, state_part = Path(str(dump) + ".part"), Path(str(state) + ".part")
        remote = f"/tmp/pl_backup-{stamp}.sql.gz"
        state_tmp = f"/tmp/pl_state-{stamp}.tgz"
        try:
            if include_database:
                self.command("exec", self.pg, "sh", "-c",
                             'unset PGHOSTADDR PGSERVICE PGSERVICEFILE; pg_dump --no-owner --no-acl -h "$1" -p "$2" -U "$3" -d "$4" -Z9 > "$5"',
                             "_", "/var/run/postgresql", str(self.db_port), self.user, self._database_arg(self.db), remote)
                copy_from_container(self.docker, f"{self.pg}:{remote}", dump_part)
                if not up._dump_complete(dump_part):
                    raise up.UpdateError("the database dump is incomplete; retry backup before restoring anything")
            # A helper also works when move or an operator already stopped the
            # daemon. Read-only inherited mounts keep this step observational.
            with self.helper(f"tar czf {state_tmp} -C /data .") as helper:
                self.command("start", "-a", helper)
                copy_from_container(self.docker, f"{helper}:{state_tmp}", state_part)
            validate_state(state_part)
            if include_database:
                dump_part.replace(dump)
            state_part.replace(state)
        finally:
            self.command("exec", self.pg, "rm", "-f", remote, check=False, timeout=120)
            dump_part.unlink(missing_ok=True)
            state_part.unlink(missing_ok=True)
        # Only this implementation's nonce-stamped files are rotated; legacy
        # script/update artifacts and incomplete runs are outside its namespace.
        pruned = []
        if keep_days is None:
            return {"dump": dump if include_database else None, "state": state, "pruned": pruned}
        for prefix, suffix in (("pseudolife_memory", "sql.gz"), ("pseudolife_state", "tgz")):
            pattern = rf"{prefix}-\d{{8}}-\d{{6}}-[0-9a-f]{{8}}\.{re.escape(suffix)}"
            files = sorted((p for p in directory.iterdir() if re.fullmatch(pattern, p.name)), reverse=True)
            for path in files[3:]:
                if path.stat().st_mtime < time.time() - keep_days * 86400:
                    path.unlink()
                    pruned.append(path)
        return {"dump": dump if include_database else None, "state": state, "pruned": pruned}

    def database_exists(self):
        literal = "'" + self.db.replace("'", "''") + "'"
        _, output = self.psql("postgres", "-Atc", f"SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname = {literal})")
        if output.strip() not in ("t", "f"):
            raise up.UpdateError("cannot determine whether the destination database exists; verify PostgreSQL "
                                 "and retry. Nothing was restored", 2)
        return output.strip() == "t"

    def wait_ready(self):
        for attempt in range(30):
            health = up.fetch_json(self.daemon_url.rstrip("/") + "/health", timeout=3)
            # v0.20.0 first released the readiness contract (2026-10-20
            # changelog entry). Earlier releases can attest only liveness;
            # a present ready:false is never overridden by that compatibility.
            legacy = (health and "ready" not in health and up.plain_release(health.get("version"))
                      and up.version_key(health["version"]) < (0, 20, 0))
            if health and health.get("status") == "ok" and (health.get("ready") is True or legacy):
                return
            if attempt < 29:
                up.sleep(1.5)
        raise up.UpdateError(f"the restored daemon did not become ready at {self.daemon_url}; "
                             f"check docker logs {self.daemon} and the --daemon-url setting")

    def restore(self, backup_file: Path, *, state_archive: Path | None = None, leave_stopped=False,
                safety_dir: Path | None = None) -> dict:
        backup_file = Path(backup_file).resolve()
        if not up._dump_complete(backup_file):
            raise up.UpdateError("the backup is unreadable or incomplete; use a complete .sql.gz bank dump. "
                                 "Nothing was restored", 2)
        if state_archive is not None:
            state_archive = Path(state_archive).resolve()
            validate_state(state_archive)
        if not leave_stopped and not self.daemon_url:
            raise up.UpdateError("the daemon has no published health port; name its endpoint with --daemon-url "
                                 "or use --leave-stopped. Nothing was restored", 2)
        exists = self.database_exists()
        # The safety pair deliberately ignores PSEUDOLIFE_MCP_DATA_DIR: a
        # checkout's ./data is not a durable recovery location.
        safety_dir = (Path(safety_dir) if safety_dir else up.home() / ".pseudolife-mcp" / "backups").resolve()
        if any((parent / ".git").exists() for parent in (safety_dir, *safety_dir.parents)):
            raise up.UpdateError(f"the safety backup directory {safety_dir} is inside a checkout; choose "
                                 "--safety-dir <directory outside a checkout>. Nothing was restored", 2)
        for mount in self.running.get("Mounts", []):
            destination = mount.get("Destination", "")
            if destination != "/data" and not destination.startswith("/data/"):
                continue
            source_path = mount.get("Source")
            if not source_path:
                continue
            if os.name == "nt":
                mapped = re.fullmatch(r"/(?:run/desktop/mnt/host|host_mnt)/([A-Za-z])/(.*)", source_path)
                if mapped:
                    source_path = mapped[1] + ":/" + mapped[2]
            source_path = Path(source_path)
            if source_path.is_absolute() and safety_dir.is_relative_to(source_path.resolve()):
                raise up.UpdateError("the safety backup would be inside the state mount being replaced; choose "
                                     "--safety-dir <directory outside the state mount>. Nothing was restored", 2)
        was_running = self.running.get("State", {}).get("Running", False)
        destructive = False
        safety = None
        remote = f"/tmp/pl_restore-{secrets.token_hex(8)}.sql.gz"
        try:
            if was_running:
                self.command("stop", self.daemon)
            self.log("==> Safety-dumping the current database and state volume...")
            safety = self.backup(out=safety_dir, keep_days=None, include_database=exists)
            self.log(f"Safety backup: {safety['dump']} + {safety['state']}" if exists else
                     f"Safety state backup: {safety['state']} (destination database was absent)")
            self.command("cp", str(backup_file), f"{self.pg}:{remote}")
            self.log(f"==> Dropping + recreating {self.db}...")
            up._flush_output()
            destructive = True
            database = _ident(self.db)
            self.psql("postgres", "-v", "ON_ERROR_STOP=1", "-c", f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")
            self.psql("postgres", "-v", "ON_ERROR_STOP=1", "-c", f"CREATE DATABASE {database}")
            self.psql("postgres", "-v", "ON_ERROR_STOP=1", "-c", f"REVOKE CONNECT ON DATABASE {database} FROM PUBLIC")
            # Separate decompression from psql: POSIX sh has no pipefail, so
            # a failed gunzip must not look like a successful empty restore.
            sql_tmp = remote + ".sql"
            try:
                self.command("exec", self.pg, "sh", "-c", 'gunzip -c "$1" > "$2"', "_", remote, sql_tmp)
                self.psql(self.db, "-q", "-v", "ON_ERROR_STOP=1", "-f", sql_tmp)
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
                self.wait_ready()
            self.log("Restore complete; daemon " + ("left stopped (--leave-stopped)." if leave_stopped else "started."))
            return {"safety_dump": safety["dump"], "safety_state": safety["state"], "left_stopped": leave_stopped}
        except BaseException as exc:
            if not destructive and was_running:
                self.command("start", self.daemon, check=False, timeout=120)
            if destructive and isinstance(exc, Exception):
                stopped, _ = self.command("stop", self.daemon, check=False, timeout=120)
                argv = ["pseudolife-mcp", "restore", "--backup-file", str(safety["dump"] or backup_file)]
                recovery_state = safety["state"] if safety["dump"] else state_archive
                if recovery_state:
                    argv += ["--state-archive", str(recovery_state)]
                argv += ["--container", self.pg, "--daemon-container", self.daemon, "--db", self.db,
                         "--user", self.user, "--db-port", str(self.db_port), "--safety-dir", str(safety_dir), "--yes"]
                if leave_stopped:
                    argv.append("--leave-stopped")
                if self.daemon_url:
                    argv += ["--daemon-url", self.daemon_url]
                state = "the daemon is left stopped" if stopped == 0 else f"run docker stop {self.daemon} first"
                reason = str(exc) if isinstance(exc, up.UpdateError) else type(exc).__name__
                raise up.UpdateError(f"restore failed after database replacement began ({reason}); {state}. "
                                     f"Recover with {_shell_command(argv)}") from exc
            raise
        finally:
            self.command("exec", self.pg, "rm", "-f", remote, check=False, timeout=120)

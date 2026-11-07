"""Seed the Rust mirror's private PostgreSQL from a nightly backup.

The mirror (``ops/docker-compose.rust-mirror.yml``, spec
``docs/specs/2026-11-06-full-rust-shadow.md`` §4.2) is its own Compose
project on a network with no route out. This tool runs on the Docker host
with the standard library and the docker CLI (``config`` also needs PyYAML):

    seed.py seed --backups data/backups --secret-file S [--backup NAME]
    seed.py tokens --secret-file S --out mirror-tokens.env
    seed.py config --backups data/backups --backup NAME --out DIR/config.yaml
    seed.py health [--timeout 1800]

``seed`` restores one backup into ``pl_cf_mir_restore`` inside the mirror's
PostgreSQL container and rewrites every credential to replay-only values
derived from the mirror secret. Only then is it renamed ``pl_cf_mir_seed``
and cloned as ``pl_cf_mir_bank``, the mirror daemon's database; a failure
on the way drops it. Backups are only read: nothing in the backup directory is
written, rotated or deleted (the container mounts it read-only).

Refusals, each before any statement that changes anything:

* a target DSN naming the live PostgreSQL service, container or port 5433
  (on the daemon box ``127.0.0.1:5433`` is the live bank), or a database
  outside ``pl_cf_mir_*``;
* a container outside the ``pseudolife-mirror`` project, or one publishing
  port 5433;
* a server whose cluster holds ``pseudolife_memory``, the live bank's name,
  so no spelling of a DSN can walk around the text check;
* less than 8 GiB free under Docker's data root, or an incomplete dump.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.parse
from pathlib import Path

MIRROR_PROJECT = "pseudolife-mirror"
LIVE_PROJECT = "pseudolife-mcp"
LIVE_HOSTS = {"pseudolife-pg", "pseudolife-mcp-postgres"}
LIVE_PORTS = {5433}
PRODUCTION_DB = "pseudolife_memory"
MIRROR_DB = re.compile(r"pl_cf_mir_[a-z0-9_]{1,40}")
SEED_DB, BANK_DB = "pl_cf_mir_seed", "pl_cf_mir_bank"
# Restored and rewritten here, renamed to SEED_DB only once no live
# credential is left: a failed rewrite must not leave a cloneable bank
# holding live secrets.
RESTORE_DB = "pl_cf_mir_restore"
DEFAULT_DSN = f"postgresql://pseudolife@mirror-pg:5432/{SEED_DB}"
BACKUP_NAME = re.compile(r"pseudolife_memory-(\d{8}-\d{6})\.sql\.gz")
DUMP_COMPLETE = b"-- PostgreSQL database dump complete"
DISK_FLOOR = 8 * 1024 ** 3
RESERVED_PRINCIPALS = {"default", "daemon", "maintainer"}
# meta rows holding a secret (py/storage/maintainer.py:59, postgres.py:1513).
META_SECRETS = ("maintainer_secret_v1", "dream_ack_secret_v1")
# Rows that are credentials and nothing else: cleared, never rewritten.
CLEARED_TABLES = ("console_sessions", "console_signin_codes", "maintainer_passkeys",
                  "maintainer_bootstrap", "maintainer_nonces", "rejected_bearers")


class Refused(SystemExit):
    """A refusal: nothing was changed. Exit status 3."""

    def __init__(self, message: str):
        super().__init__(3)
        self.message = message

    def __str__(self) -> str:
        return self.message


def refuse_live_dsn(dsn: str) -> tuple[str, int, str]:
    """(host, port, database) of a mirror DSN, or Refused."""
    parts = urllib.parse.urlsplit(dsn)
    if parts.scheme not in ("postgresql", "postgres"):
        raise Refused(f"not a PostgreSQL DSN: scheme {parts.scheme!r}; nothing was changed")
    host = (parts.hostname or "").lower()
    try:
        port = parts.port or 5432
    except ValueError:
        raise Refused("the DSN's port is not a number; nothing was changed") from None
    database = urllib.parse.unquote(parts.path.lstrip("/"))
    if host in LIVE_HOSTS or port in LIVE_PORTS:
        raise Refused(f"refusing {host}:{port}: it is the live PostgreSQL service or port "
                      "(on the daemon box 127.0.0.1:5433 is the live bank); point the mirror "
                      "at its own mirror-pg service. Nothing was changed.")
    if database == PRODUCTION_DB or not MIRROR_DB.fullmatch(database):
        raise Refused(f"refusing database {database!r}: the mirror only writes pl_cf_mir_* "
                      "databases. Nothing was changed.")
    return host, port, database


def refuse_live_server(facts: dict) -> None:
    """``facts`` is what the server said about itself (``server_facts``)."""
    if facts["has_production"]:
        raise Refused(f"refusing this server: its cluster holds {PRODUCTION_DB!r}, so it is "
                      "the live bank's PostgreSQL, whatever the DSN said. Nothing was changed.")
    if not MIRROR_DB.fullmatch(facts["database"]):
        raise Refused(f"refusing this server: it reached {facts['database']!r}, outside "
                      "pl_cf_mir_*. Nothing was changed.")


def replay_token(secret: str, kind: str, name: str) -> str:
    """The replay-only credential for a principal or board agent."""
    return hashlib.sha256(f"{secret}:{kind}:{name}".encode()).hexdigest()


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def credential_sql(secret: str) -> str:
    """One transaction that rewrites every credential (spec §4.2) and fails
    if any live value survives. The token derivation matches replay_token:
    a stored hash is sha256(hex token), as both daemons hash a bearer or an
    agent key (py/principals.py:92, py/storage/coordination.py:1027)."""
    s = _sql_text(secret)

    def hashed(kind: str, column: str) -> str:
        token = f"encode(sha256(convert_to({s} || ':{kind}:' || {column}, 'UTF8')), 'hex')"
        return f"encode(sha256(convert_to({token}, 'UTF8')), 'hex')"

    meta_keys = ", ".join(_sql_text(k) for k in META_SECRETS)
    cleared = "\n".join(f"DELETE FROM {t};" for t in CLEARED_TABLES)
    left = " OR ".join(f"EXISTS (SELECT 1 FROM {t})" for t in CLEARED_TABLES)
    return f"""BEGIN;
CREATE TEMP TABLE mirror_live_values ON COMMIT DROP AS
  SELECT token_hash AS v FROM principals WHERE token_hash IS NOT NULL
  UNION ALL SELECT credential_hash FROM coordination_agents WHERE credential_hash IS NOT NULL
  UNION ALL SELECT value #>> '{{}}' FROM meta WHERE key IN ({meta_keys});
UPDATE principals SET token_hash = {hashed('principal', 'principal')}
  WHERE token_hash IS NOT NULL;
UPDATE principals SET code_hash = NULL, code_expires_at = NULL, paired_code_hash = NULL;
UPDATE coordination_agents SET credential_hash = {hashed('agent', 'agent_id')}
  WHERE credential_hash IS NOT NULL;
UPDATE meta SET value = to_jsonb(encode(sha256(convert_to({s} || ':meta:' || key, 'UTF8')), 'hex'))
  WHERE key IN ({meta_keys});
{cleared}
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM principals p JOIN mirror_live_values l ON p.token_hash = l.v)
     OR EXISTS (SELECT 1 FROM coordination_agents a JOIN mirror_live_values l ON a.credential_hash = l.v)
     OR EXISTS (SELECT 1 FROM meta m JOIN mirror_live_values l ON m.value #>> '{{}}' = l.v
                WHERE m.key IN ({meta_keys}))
     OR EXISTS (SELECT 1 FROM principals WHERE code_hash IS NOT NULL OR paired_code_hash IS NOT NULL)
     OR {left} THEN
    RAISE EXCEPTION 'mirror credential rewrite left a live credential';
  END IF;
END $$;
COMMIT;
"""


TOKEN_PRINCIPALS_SQL = """
SELECT DISTINCT principal FROM (
  SELECT principal FROM coordination_agents
  UNION SELECT principal FROM client_sessions WHERE principal IS NOT NULL) seen
WHERE principal <> '' AND principal NOT IN (SELECT principal FROM principals)
ORDER BY 1
"""


def tokens_env(secret: str, principals: list[str]) -> str:
    """The mirror daemon's bearer map: replay-only tokens under the names the
    live environment map served (they sign sessions and board agents but
    have no stored row), never the live values (spec §4.2). A name with any
    stored row, revoked ones included, stays stored: an environment bearer
    would shadow its revocation and grant operator rights it never had."""
    names = [p for p in principals if p and p not in RESERVED_PRINCIPALS and "," not in p]
    pairs = ",".join(f"{replay_token(secret, 'principal', p)}:{p}" for p in names)
    return (f"PSEUDOLIFE_MIRROR_TOKENS={pairs}\n"
            f"PSEUDOLIFE_MIRROR_TOKEN={replay_token(secret, 'principal', 'default')}\n")


def mirror_config(live: dict) -> dict:
    """The live config with every outbound path and autonomous writer off
    (spec §2.3): no extractor or judge URL (the no-op extractor), no backlog
    dreams, deep-dream ticks, judges, stall notices or release checks."""
    cfg = json.loads(json.dumps(live or {}))
    memory = cfg.setdefault("memory", {})
    dream = memory.setdefault("dream", {})
    dream.update(enabled=False, stall_notice=False, extractor_base_url="", fallback_base_url="")
    deep = memory.setdefault("deep_dream", {})
    deep.update(auto_tick=False, judges_enabled=False)
    for key in [k for k in deep if k.endswith("_url")]:
        deep[key] = ""
    for section in (dream, deep):  # no live API key reaches the mirror
        for key in [k for k in section if k.endswith("api_key")]:
            del section[key]
    cfg.setdefault("updates", {})["check_releases"] = False
    return cfg


class Docker:
    """The docker CLI; tests replace ``run``."""

    def run(self, args: list[str], input: bytes | None = None, redact: str = "") -> str:
        done = subprocess.run(["docker", *args], input=input, capture_output=True)
        if done.returncode:
            # psql quotes a failing statement's text, which may hold the secret.
            err = done.stderr.decode(errors="replace").strip()
            raise SystemExit(f"docker {args[0]} failed (exit {done.returncode}): "
                             f"{(err.replace(redact, '<secret>') if redact else err)[:400]}")
        return done.stdout.decode()

    def container(self, service: str, running: bool = True) -> str | None:
        ids = self.run(["ps", "-q", "--no-trunc", *([] if running else ["-a"]),
                        "--filter", f"label=com.docker.compose.project={MIRROR_PROJECT}",
                        "--filter", f"label=com.docker.compose.service={service}"]).split()
        if len(ids) > 1:
            raise Refused(f"{len(ids)} containers serve {service}; expected one. Nothing was changed.")
        if not ids:
            return None
        info = json.loads(self.run(["inspect", ids[0]]))[0]
        labels = info["Config"]["Labels"] or {}
        if labels.get("com.docker.compose.project") != MIRROR_PROJECT \
                or info["Name"].lstrip("/") in LIVE_HOSTS:
            raise Refused(f"refusing container {info['Name']}: it is not the mirror's. "
                          "Nothing was changed.")
        for bindings in (info["NetworkSettings"].get("Ports") or {}).values():
            if any(int(b.get("HostPort") or 0) in LIVE_PORTS for b in bindings or []):
                raise Refused(f"refusing container {info['Name']}: it publishes the live port. "
                              "Nothing was changed.")
        return ids[0]

    def psql(self, cid: str, database: str, sql: str, redact: str = "") -> str:
        # The statement travels on stdin, never in argv, where ps would show it.
        return self.run(["exec", "-i", cid, "psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1",
                         "-U", "pseudolife", "-d", database, "-f", "-"],
                        input=sql.encode(), redact=redact)


def server_facts(docker: Docker, cid: str, database: str) -> dict:
    out = docker.psql(cid, database, "SELECT current_database(), EXISTS (SELECT 1 FROM "
                      f"pg_database WHERE datname = '{PRODUCTION_DB}');\n").strip()
    name, has = out.split("|")
    return {"database": name, "has_production": has == "t"}


def pick_backup(directory: Path, name: str | None) -> Path:
    found = sorted(p for p in directory.glob("pseudolife_memory-*.sql.gz")
                   if BACKUP_NAME.fullmatch(p.name))
    if name:
        found = [p for p in found if p.name == name]
    if not found:
        raise Refused(f"no backup {name or 'pseudolife_memory-<stamp>.sql.gz'} in {directory}. "
                      "Nothing was changed.")
    return found[-1]


def dump_complete(path: Path) -> bool:
    """pg_dump's end marker is among the last lines (read-only stream)."""
    tail = b""
    with gzip.open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            tail = (tail + chunk)[-4096:]
    return DUMP_COMPLETE in tail


def require_disk(docker: Docker, floor: int = DISK_FLOOR) -> int:
    root = docker.run(["info", "--format", "{{.DockerRootDir}}"]).strip() or "/"
    free = shutil.disk_usage(root).free
    if free < floor:
        raise Refused(f"{free / 1024 ** 3:.1f} GiB free under {root}, below the 8 GiB floor; "
                      "free space before seeding. Nothing was changed.")
    return free


def seed(docker: Docker, backups: Path, secret: str, backup: str | None = None,
         dsn: str = DEFAULT_DSN) -> dict:
    _, _, database = refuse_live_dsn(dsn)
    if database != SEED_DB:
        raise Refused(f"seed restores into {SEED_DB}, not {database!r}. Nothing was changed.")
    if len(secret) < 32:
        raise Refused("the mirror secret must be at least 32 characters. Nothing was changed.")
    cid = docker.container("mirror-pg")
    if cid is None:
        raise Refused("the mirror's PostgreSQL is not running: "
                      f"docker compose -p {MIRROR_PROJECT} -f ops/docker-compose.rust-mirror.yml up -d mirror-pg")
    if docker.container("mirror-daemon"):
        raise Refused("the mirror daemon is running; stop it first: docker compose -p "
                      f"{MIRROR_PROJECT} -f ops/docker-compose.rust-mirror.yml stop mirror-daemon")
    refuse_live_server(server_facts(docker, cid, "pl_cf_mir_home"))
    path = pick_backup(backups, backup)
    free = require_disk(docker)
    if not dump_complete(path):
        raise Refused(f"{path.name} has no end-of-dump marker: it is incomplete. Nothing was changed.")
    record = {"backup": path.name, "backup_bytes": path.stat().st_size, "free_bytes_before": free}
    docker.psql(cid, "pl_cf_mir_home", f"""
DROP DATABASE IF EXISTS {BANK_DB} WITH (FORCE);
DROP DATABASE IF EXISTS {SEED_DB} WITH (FORCE);
DROP DATABASE IF EXISTS {RESTORE_DB} WITH (FORCE);
CREATE DATABASE {RESTORE_DB};
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pseudolife_shadow') THEN
    CREATE ROLE pseudolife_shadow NOLOGIN;  -- the dump's GRANTs name it
  END IF;
END $$;
""")
    try:
        started = time.monotonic()
        # Positional arguments, never interpolated: the name passed BACKUP_NAME.
        # pipefail: a gunzip failure fails the restore, not only psql's.
        docker.run(["exec", cid, "bash", "-o", "pipefail", "-c",
                    'gunzip -c "/backups/$1" | psql -X -q -v ON_ERROR_STOP=1 '
                    '--single-transaction -U pseudolife -d "$2" > /dev/null',
                    "bash", path.name, RESTORE_DB])
        record["restore_seconds"] = round(time.monotonic() - started, 1)
        refuse_live_server(server_facts(docker, cid, RESTORE_DB))
        docker.psql(cid, RESTORE_DB, credential_sql(secret), redact=secret)
    except BaseException:
        docker.psql(cid, "pl_cf_mir_home", f"DROP DATABASE IF EXISTS {RESTORE_DB} WITH (FORCE);\n")
        raise
    started = time.monotonic()
    docker.psql(cid, "pl_cf_mir_home", f"ALTER DATABASE {RESTORE_DB} RENAME TO {SEED_DB};\n"
                f"CREATE DATABASE {BANK_DB} TEMPLATE {SEED_DB};\n")
    record["clone_seconds"] = round(time.monotonic() - started, 1)
    sizes = docker.psql(cid, SEED_DB, "SELECT pg_database_size(current_database()), "
                        "(SELECT value #>> '{}' FROM meta WHERE key = 'schema_version');\n").strip()
    size, schema = sizes.split("|")
    record.update(database_bytes=int(size), schema_version=schema)
    return record


def health(docker: Docker, timeout: float, expect_schema: str | None = None) -> dict:
    """Wait for the mirror daemon: ok, ready, a writer, the bank's schema."""
    end = time.monotonic() + timeout
    last = "not started"
    while time.monotonic() < end:
        cid = docker.container("mirror-daemon")
        if cid:
            try:
                body = json.loads(docker.run(["exec", cid, "curl", "-fsS", "--max-time", "5",
                                              "http://127.0.0.1:8765/health"]))
            except (SystemExit, ValueError) as e:
                last = str(e)[:200]
            else:
                schema = str(body.get("schema", ""))
                if body.get("ready") is True and body.get("auth") is False:
                    raise Refused("the mirror daemon serves without a bearer: start it with the "
                                  "tokens file from `seed.py tokens` (--env-file).")
                if body.get("status") == "ok" and body.get("ready") is True \
                        and not body.get("read_only") and not body.get("counters_import_required") \
                        and (expect_schema is None or schema == expect_schema):
                    return body
                last = f"status={body.get('status')} ready={body.get('ready')} schema={schema}"
        time.sleep(5)
    raise SystemExit(f"the mirror daemon did not become healthy within {timeout:g}s ({last})")


def _secret(path: Path) -> str:
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise Refused(f"{path} is readable by others; chmod 600 it. Nothing was changed.")
    return path.read_text(encoding="utf-8").strip()


def _write_private(path: Path, data: str, uid: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
    if uid is not None and hasattr(os, "chown"):
        os.chown(path, uid, uid)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--backups", type=Path, required=True)
    s.add_argument("--backup")
    s.add_argument("--secret-file", type=Path, required=True)
    s.add_argument("--dsn", default=DEFAULT_DSN)
    s.add_argument("--record", type=Path)
    t = sub.add_parser("tokens")
    t.add_argument("--secret-file", type=Path, required=True)
    t.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("config")
    c.add_argument("--backups", type=Path, required=True)
    c.add_argument("--backup", required=True)
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--uid", type=int, default=10001)
    h = sub.add_parser("health")
    h.add_argument("--timeout", type=float, default=1800)
    h.add_argument("--schema")
    args = ap.parse_args(argv)
    docker = Docker()
    try:
        if args.cmd == "seed":
            record = seed(docker, args.backups, _secret(args.secret_file), args.backup, args.dsn)
            if args.record:
                _write_private(args.record, json.dumps(record, indent=2) + "\n")
            print(json.dumps(record))
        elif args.cmd == "tokens":
            cid = docker.container("mirror-pg")
            if cid is None:
                raise Refused("the mirror's PostgreSQL is not running. Nothing was changed.")
            refuse_live_server(server_facts(docker, cid, BANK_DB))
            names = docker.psql(cid, BANK_DB, TOKEN_PRINCIPALS_SQL + ";\n").split()
            _write_private(args.out, tokens_env(_secret(args.secret_file), names))
            print(f"wrote {len(names)} replay-only principals to {args.out}")
        elif args.cmd == "config":
            import yaml
            stamp = BACKUP_NAME.fullmatch(pick_backup(args.backups, args.backup).name).group(1)
            with tarfile.open(args.backups / f"pseudolife_state-{stamp}.tgz", "r:gz") as tar:
                names = set(tar.getnames())
                member = next((n for n in ("./config.yaml", "config.yaml") if n in names), None)
                if member is None:
                    raise Refused(f"pseudolife_state-{stamp}.tgz holds no config.yaml")
                live = yaml.safe_load(tar.extractfile(member).read())
            _write_private(args.out, yaml.safe_dump(mirror_config(live), sort_keys=False),
                           args.uid if getattr(os, "geteuid", lambda: 1)() == 0 else None)
            print(f"wrote the mirror config from pseudolife_state-{stamp}.tgz to {args.out}")
        else:
            body = health(docker, args.timeout, args.schema)
            print(json.dumps({k: body.get(k) for k in ("status", "ready", "read_only", "build")}))
    except Refused as e:
        print(f"seed.py: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())

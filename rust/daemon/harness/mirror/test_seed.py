"""The mirror seed's refusals, credential rewrite and overlay shape.

The database case runs the rewrite through ``docker exec psql`` exactly as
``seed.py`` does, on a throwaway PostgreSQL with no volume (``tmpfs``) on an
internal network; it skips only where no Docker engine answers.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))

import seed  # noqa: E402

SECRET = "s" * 40
LIVE = "a" * 64  # a live-shaped SHA-256 hex value


@pytest.mark.parametrize("dsn", [
    "postgresql://pseudolife@pseudolife-pg:5432/pl_cf_mir_seed",
    "postgresql://pseudolife@PSEUDOLIFE-MCP-POSTGRES:5432/pl_cf_mir_seed",
    "postgresql://pseudolife@127.0.0.1:5433/pl_cf_mir_seed",
    "postgresql://pseudolife@localhost:5433/pl_cf_mir_seed",
    "postgresql://pseudolife@mirror-pg:5432/pseudolife_memory",
    "postgresql://pseudolife@mirror-pg:5432/pl_cf_w2e_x",
    "postgresql://pseudolife@mirror-pg:notaport/pl_cf_mir_seed",
    "mysql://mirror-pg/pl_cf_mir_seed",
])
def test_live_or_foreign_dsn_is_refused(dsn):
    with pytest.raises(seed.Refused, match="Nothing was changed|nothing was changed"):
        seed.refuse_live_dsn(dsn)


@pytest.mark.parametrize("dsn", [seed.DEFAULT_DSN,
                                 "postgresql://u@127.0.0.1:5432/pl_cf_mir_bank",
                                 "postgres://u:p@mirror-pg/pl_cf_mir_home"])
def test_mirror_dsn_near_misses_are_admitted(dsn):
    assert seed.MIRROR_DB.fullmatch(seed.refuse_live_dsn(dsn)[2])


def test_a_cluster_holding_the_production_bank_is_refused():
    with pytest.raises(seed.Refused, match="live bank's PostgreSQL"):
        seed.refuse_live_server({"database": "pl_cf_mir_seed", "has_production": True})
    with pytest.raises(seed.Refused, match="outside"):
        seed.refuse_live_server({"database": "postgres", "has_production": False})
    seed.refuse_live_server({"database": "pl_cf_mir_seed", "has_production": False})


class FakeDocker(seed.Docker):
    """Records every docker call; answers psql with canned server facts."""

    def __init__(self, tmp: Path, *, production=False, project=seed.MIRROR_PROJECT,
                 daemon=False, free=100 * 1024 ** 3, ports=None):
        self.calls, self.production, self.project = [], production, project
        self.daemon, self.free, self.ports, self.tmp = daemon, free, ports or {}, tmp

    def run(self, args, input=None, redact=""):
        self.calls.append((args, input))
        if args[0] == "ps":
            service = args[-1].rsplit("=", 1)[1]
            return "" if service == "mirror-daemon" and not self.daemon else f"id-{service}\n"
        if args[0] == "inspect":
            return json.dumps([{"Name": "/" + args[1], "Config": {"Labels": {
                "com.docker.compose.project": self.project}}, "NetworkSettings": {"Ports": self.ports}}])
        if args[0] == "info":
            return str(self.tmp)
        if args[0] == "exec" and input and b"current_database(), EXISTS" in input:
            return f"{args[args.index('-d') + 1]}|{'t' if self.production else 'f'}\n"
        if args[0] == "exec" and input and b"pg_database_size" in input:
            return "284000000|59\n"
        return ""

    def changed(self) -> bool:
        return any(a[0] == "exec" and (a[2] == "sh" or (i and b"current_database(), EXISTS" not in i))
                   for a, i in self.calls)


def backups(tmp: Path, complete=True) -> Path:
    d = tmp / "backups"
    d.mkdir()
    body = b"COPY entries FROM stdin;\n\\.\n" + (seed.DUMP_COMPLETE + b"\n" if complete else b"")
    with gzip.open(d / "pseudolife_memory-20261106-031701.sql.gz", "wb") as f:
        f.write(body)
    (d / "pseudolife_memory-20261107-031701.sql.gz.part").write_bytes(b"")
    return d


@pytest.mark.parametrize("case,match", [
    ({"production": True}, "live bank's PostgreSQL"),
    ({"project": seed.LIVE_PROJECT}, "not the mirror's"),
    ({"ports": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5433"}]}}, "live port"),
    ({"daemon": True}, "daemon is running"),
    ({"free": 7 * 1024 ** 3}, "below the 8 GiB floor"),
])
def test_seed_refuses_before_changing_anything(tmp_path, monkeypatch, case, match):
    monkeypatch.setattr(seed.shutil, "disk_usage",
                        lambda p: type("U", (), {"free": case.get("free", 100 * 1024 ** 3)}))
    docker = FakeDocker(tmp_path, **{k: v for k, v in case.items() if k != "free"})
    with pytest.raises(seed.Refused, match=match):
        seed.seed(docker, backups(tmp_path), SECRET)
    assert not docker.changed(), docker.calls


def test_seed_refuses_an_incomplete_dump_and_a_live_dsn(tmp_path, monkeypatch):
    monkeypatch.setattr(seed.shutil, "disk_usage", lambda p: type("U", (), {"free": 10 ** 12}))
    docker = FakeDocker(tmp_path)
    with pytest.raises(seed.Refused, match="incomplete"):
        seed.seed(docker, backups(tmp_path, complete=False), SECRET)
    with pytest.raises(seed.Refused, match="live PostgreSQL"):
        seed.seed(docker, tmp_path, SECRET, dsn="postgresql://p@127.0.0.1:5433/pl_cf_mir_seed")
    assert not docker.changed()


def test_seed_restores_rewrites_then_clones(tmp_path, monkeypatch):
    monkeypatch.setattr(seed.shutil, "disk_usage", lambda p: type("U", (), {"free": 10 ** 12}))
    docker = FakeDocker(tmp_path)
    record = seed.seed(docker, backups(tmp_path), SECRET)
    assert record["backup"] == "pseudolife_memory-20261106-031701.sql.gz"
    assert record["schema_version"] == "59" and record["database_bytes"] == 284000000
    execs = [(a, i) for a, i in docker.calls if a[0] == "exec"]
    restore = next(a for a, _ in execs if a[2] == "sh")
    # Positional arguments only: the backup name never reaches the script text.
    assert restore[-3:] == ["sh", record["backup"], seed.SEED_DB] and "$1" in restore[4]
    order = [("restore" if a[2] == "sh" else (i or b"").decode()) for a, i in execs]
    rewrite = next(n for n, s in enumerate(order) if "mirror_live_values" in s)
    assert order.index("restore") < rewrite < next(n for n, s in enumerate(order) if "TEMPLATE" in s)
    assert all(SECRET not in " ".join(a) for a, _ in docker.calls)  # stdin only, never argv


def test_errors_never_echo_the_secret(monkeypatch):
    done = subprocess.CompletedProcess([], 1, b"", f"LINE 3: ...'{SECRET}:principal:'".encode())
    monkeypatch.setattr(seed.subprocess, "run", lambda *a, **k: done)
    with pytest.raises(SystemExit) as e:
        seed.Docker().psql("cid", "pl_cf_mir_seed", "SELECT 1", redact=SECRET)
    assert SECRET not in str(e.value) and "<secret>" in str(e.value)


def test_tokens_env_maps_replay_tokens_to_live_names_only():
    env = seed.tokens_env(SECRET, ["claude-code", "default", "daemon", "codex", "a,b"])
    pairs = re.search(r"PSEUDOLIFE_MIRROR_TOKENS=(.*)", env).group(1).split(",")
    assert [p.rsplit(":", 1)[1] for p in pairs] == ["claude-code", "codex"]
    assert pairs[0].rsplit(":", 1)[0] == seed.replay_token(SECRET, "principal", "claude-code")
    assert seed.replay_token(SECRET, "principal", "x") != seed.replay_token(SECRET + "x", "principal", "x")


def test_mirror_config_turns_every_outbound_path_off_and_keeps_the_rest():
    live = {"memory": {"miras": {"preset": "flat"}, "dream": {"extractor_base_url": "http://h:1/v1",
            "fallback_base_url": "http://h:2/v1", "extractor_model": "m"},
            "deep_dream": {"judge_url": "http://h:3", "judge_third_url": "http://h:4"}}}
    cfg = seed.mirror_config(live)
    dream, deep = cfg["memory"]["dream"], cfg["memory"]["deep_dream"]
    assert dream["extractor_base_url"] == dream["fallback_base_url"] == "" and not dream["enabled"]
    assert deep["judge_url"] == deep["judge_third_url"] == "" and not deep["judges_enabled"]
    assert not deep["auto_tick"] and not dream["stall_notice"] and not cfg["updates"]["check_releases"]
    assert cfg["memory"]["miras"] == {"preset": "flat"} and dream["extractor_model"] == "m"
    assert live["memory"]["dream"]["extractor_base_url"] == "http://h:1/v1"  # not mutated


def test_overlay_has_no_route_out_and_no_live_credentials():
    text = (REPO / "ops" / "docker-compose.rust-mirror.yml").read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    assert doc["name"] == seed.MIRROR_PROJECT
    assert doc["networks"] == {"mirror": {"internal": True}}
    for name, svc in doc["services"].items():
        assert "ports" not in svc and "network_mode" not in svc, name
        assert svc["networks"] == ["mirror"] and svc["oom_score_adj"] == 1000, name
        assert svc["mem_limit"] and svc["memswap_limit"] == svc["mem_limit"], name
    pg, daemon = doc["services"]["mirror-pg"], doc["services"]["mirror-daemon"]
    assert any(v.endswith(":/backups:ro") for v in pg["volumes"])
    assert daemon["user"] == "10001:10001" and daemon["cap_drop"] == ["ALL"]
    env = daemon["environment"]
    for key in ("TZ", "PSEUDOLIFE_WRITER_ID", "PSEUDOLIFE_MCP_TOOLSET"):
        assert key in env
    assert "MIRROR_TOKENS" in env["PSEUDOLIFE_MCP_TOKENS"] and "MIRROR_TOKEN" in env["PSEUDOLIFE_MCP_TOKEN"]
    assert "PSEUDOLIFE_MCP_TOKEN" not in re.sub(r"PSEUDOLIFE_MCP_TOKENS?:", "", text)
    assert env["PSEUDOLIFE_RELEASE_CHECK"] == "0" and float(env["PSEUDOLIFE_SESSION_IDLE_SECONDS"]) > 1e7
    dsn = re.sub(r"\$\{[^}]*\}", "pw", env["PSEUDOLIFE_MCP_DATABASE_URL"])
    assert seed.refuse_live_dsn(dsn)[2] == seed.BANK_DB
    assert any(v.endswith(":/models:ro") for v in daemon["volumes"])


def _engine() -> bool:
    """A Linux engine (a Windows runner's engine runs Windows containers)."""
    try:
        done = subprocess.run(["docker", "info", "--format", "{{.OSType}}"], capture_output=True,
                              text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and done.stdout.strip() == "linux"


@pytest.fixture(scope="module")
def throwaway_pg():
    if not _engine():
        pytest.skip("no Linux Docker engine answers here")
    image = os.environ.get("PSEUDOLIFE_MIRROR_TEST_PG_IMAGE", "pgvector/pgvector:pg18")
    name = f"plmirtest-{uuid.uuid4().hex[:10]}"
    subprocess.run(["docker", "network", "create", "--internal", name], check=True, capture_output=True)
    cid = subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", name,
                          "--tmpfs", "/var/lib/postgresql", "--memory", "512m",
                          "-e", "POSTGRES_USER=pseudolife", "-e", "POSTGRES_PASSWORD=throwaway",
                          "-e", "POSTGRES_DB=pl_cf_mir_seed", image],
                         check=True, capture_output=True, text=True).stdout.strip()
    try:
        for _ in range(60):
            if subprocess.run(["docker", "exec", cid, "pg_isready", "-U", "pseudolife", "-d",
                               "pl_cf_mir_seed"], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        from pseudolife_memory.storage.schema import SCHEMA_SQL
        seed.Docker().psql(cid, "pl_cf_mir_seed", "CREATE EXTENSION IF NOT EXISTS vector;\n" + SCHEMA_SQL)
        yield cid
    finally:
        subprocess.run(["docker", "stop", cid], capture_output=True)
        subprocess.run(["docker", "network", "rm", name], capture_output=True)


def _rows(cid: str, sql: str) -> list[list[str]]:
    out = seed.Docker().psql(cid, "pl_cf_mir_seed", sql + ";\n")
    return [line.split("|") for line in out.splitlines() if line]


def _plant_live_credentials(cid: str) -> None:
    seed.Docker().psql(cid, "pl_cf_mir_seed", f"""
TRUNCATE principals, coordination_agents, console_sessions, console_signin_codes,
  maintainer_passkeys, maintainer_bootstrap, maintainer_nonces, rejected_bearers CASCADE;
DELETE FROM meta WHERE key IN ('maintainer_secret_v1', 'dream_ack_secret_v1');
INSERT INTO principals (principal, token_hash, code_hash, code_expires_at, paired_code_hash, created_at)
  VALUES ('paired-laptop', '{LIVE}', '{"b" * 64}', 9e9, '{"c" * 64}', 1), ('pending', NULL, '{"d" * 64}', 9e9, NULL, 1);
INSERT INTO coordination_agents (agent_id, principal, credential_hash, created_at, last_activity)
  VALUES ('{"1" * 32}', 'claude-code', '{"e" * 64}', 1, 1), ('{"2" * 32}', 'codex', NULL, 1, 1);
INSERT INTO meta (key, value) VALUES ('maintainer_secret_v1', '"{"f" * 64}"'), ('dream_ack_secret_v1', '"{"0" * 64}"');
INSERT INTO console_sessions (session_id, token_hash, principal, source, issuer, method, created_at, expires_at)
  VALUES ('s1', '{"9" * 64}', 'maintainer', 'console', 'daemon', 'passkey', 1, 9e9);
INSERT INTO console_signin_codes (code_hash, principal, source, issuer, origin, expires_at, created_at)
  VALUES ('{"8" * 64}', 'maintainer', 'console', 'daemon', 'https://x', 9e9, 1);
INSERT INTO maintainer_passkeys (credential_id, public_key, alg, label, enrolled_by, state, created_at)
  VALUES ('cred', '\\x00', -7, 'key', 'maintainer', 'active', 1);
INSERT INTO maintainer_bootstrap (code_hash, expires_at) VALUES ('{"7" * 64}', 9e9);
INSERT INTO maintainer_nonces (nonce, expires_at) VALUES ('n1', 9e9);
""")


def test_credential_rewrite_leaves_no_live_value(throwaway_pg):
    cid = throwaway_pg
    _plant_live_credentials(cid)
    seed.Docker().psql(cid, "pl_cf_mir_seed", seed.credential_sql(SECRET))

    def hashed(kind, name):
        return hashlib.sha256(seed.replay_token(SECRET, kind, name).encode()).hexdigest()

    assert _rows(cid, "SELECT principal, token_hash, code_hash, code_expires_at, paired_code_hash "
                 "FROM principals ORDER BY 1") == [
        ["paired-laptop", hashed("principal", "paired-laptop"), "", "", ""], ["pending", "", "", "", ""]]
    assert _rows(cid, "SELECT agent_id, credential_hash FROM coordination_agents ORDER BY 1") == [
        ["1" * 32, hashed("agent", "1" * 32)], ["2" * 32, ""]]
    meta = dict(_rows(cid, "SELECT key, value #>> '{}' FROM meta WHERE key LIKE '%secret_v1'"))
    assert set(meta) == set(seed.META_SECRETS)
    assert all(re.fullmatch(r"[0-9a-f]{64}", v) and v not in ("f" * 64, "0" * 64) for v in meta.values())
    for table in seed.CLEARED_TABLES:
        assert _rows(cid, f"SELECT count(*) FROM {table}") == [["0"]], table


def test_the_rewrite_check_fails_closed_when_a_live_value_survives(throwaway_pg):
    cid = throwaway_pg
    _plant_live_credentials(cid)
    sql = seed.credential_sql(SECRET)
    skipped = re.sub(r"UPDATE coordination_agents SET credential_hash.*?;\n", "", sql, flags=re.S)
    assert skipped != sql
    with pytest.raises(SystemExit, match="left a live credential"):
        seed.Docker().psql(cid, "pl_cf_mir_seed", skipped, redact=SECRET)
    # The whole transaction rolled back: the live value is still there, unrewritten.
    assert _rows(cid, "SELECT credential_hash FROM coordination_agents WHERE credential_hash IS NOT NULL") \
        == [["e" * 64]]

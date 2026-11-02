"""Opt-in Linux disposable-bank proof (never uses an installed bank).

Run with --rust-bin, --out and --pg-container on the test PostgreSQL host.
PSEUDOLIFE_TEST_PG_HOST_PORT must be loopback:5434 and the admin test
credentials must be in PSEUDOLIFE_TEST_PG_USER/PSEUDOLIFE_TEST_PG_PASSWORD.
It creates only s3_* databases, records process IDs through daemons.Daemon,
and drops the database in finally. No live backup is read or restored.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daemons
import shadow_compare as compare


def snapshot(dsn):
    """Every row of every non-system table, plus every sequence, unnormalized."""
    result = {}
    with psycopg.connect(dsn) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        tables = conn.execute("SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                              "WHERE c.relkind IN ('r','p','m') AND n.nspname NOT IN ('pg_catalog','information_schema') "
                              "AND n.nspname NOT LIKE 'pg_toast%' ORDER BY 1,2").fetchall()
        for schema, table in tables:
            rows = conn.execute(sql.SQL("SELECT row_to_json(t)::text FROM {} t ORDER BY row_to_json(t)::text")
                                .format(sql.Identifier(schema, table))).fetchall()
            result[f"{schema}.{table}"] = [r[0] for r in rows]
        seqs = conn.execute("SELECT schemaname,sequencename FROM pg_sequences WHERE schemaname NOT LIKE 'pg_%'").fetchall()
        for schema, sequence in seqs:
            result[f"sequence:{schema}.{sequence}"] = list(conn.execute(sql.SQL("SELECT last_value,is_called FROM {}")
                                                                       .format(sql.Identifier(schema, sequence))).fetchone())
    return result


def lease_holders(dsn):
    with psycopg.connect(dsn) as conn:
        return [r[0] for r in conn.execute(
            "SELECT pid FROM pg_locks WHERE locktype='advisory' AND granted AND database=(SELECT oid FROM pg_database WHERE datname=current_database()) "
            "AND classid=((hashtextextended('pseudolife-bank-writer',0) >> 32) & 4294967295)::oid "
            "AND objid=(hashtextextended('pseudolife-bank-writer',0) & 4294967295)::oid AND objsubid=1 ORDER BY pid")]


def bound_reads(port, bank_id):
    headers = {"Authorization": "Bearer synthetic-shadow-proof", "X-PL-Bank": bank_id,
               "X-PL-Principal": "default", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
    for number, method, params in (
            (1, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                               "clientInfo": {"name": "bound-shadow-proof", "version": "1"}}),
            (None, "notifications/initialized", {}),
            (2, "tools/call", {"name": "memory_search", "arguments": {"query": "writer lease"}})):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            payload = {"jsonrpc": "2.0", "method": method, "params": params}
            if number is not None:
                payload["id"] = number
            conn.request("POST", "/mcp", json.dumps(payload), headers)
            response = conn.getresponse()
            raw = response.read()
            if number is None:
                assert response.status == 202
                continue
            if "text/event-stream" in (response.getheader("content-type") or ""):
                messages = [json.loads(line[5:]) for line in raw.decode().splitlines() if line.startswith("data:")]
                body = next((message for message in messages if message.get("id") == number), {})
            else:
                body = json.loads(raw)
            assert response.status == 200 and "result" in body and not body["result"].get("isError"), "valid bound MCP request failed"
            if response.getheader("mcp-session-id"):
                headers["Mcp-Session-Id"] = response.getheader("mcp-session-id")
        finally:
            conn.close()


def apply_role(container, user, database, password, expect_failure=False):
    script = (HERE.parents[2] / "ops/sql/shadow_reader_role.sql").read_text(encoding="utf-8")
    # Random test password has only hex digits; never argv or output.
    payload = f"\\set shadow_password '{password}'\n" + script
    result = subprocess.run(["sudo", "-n", "docker", "exec", "-i", container,
                             "psql", "-X", "-q", "-U", user, "-d", database],
                            input=payload, text=True, capture_output=True)
    if expect_failure:
        expected = "PUBLIC grants" if expect_failure is True else expect_failure
        assert result.returncode and expected in result.stderr, "unsafe inherited grants were accepted"
    elif result.returncode:
        error = next((line.split("ERROR:", 1)[1].strip() for line in result.stderr.splitlines()
                      if "ERROR:" in line), "PostgreSQL administrator access is required")
        raise RuntimeError("shadow SQL role setup failed: " + error.replace(password, "[redacted]"))


def proof(binary, out, container, triage=()):
    if os.name == "nt":
        raise ValueError("this proof runs on the Linux test PostgreSQL host")
    if os.environ.get("PSEUDOLIFE_TEST_PG_HOST_PORT") != "127.0.0.1:5434":
        raise ValueError("proof requires the dedicated loopback test PostgreSQL on port 5434")
    user = os.environ["PSEUDOLIFE_TEST_PG_USER"]
    admin = dict(host="127.0.0.1", port=5434, user=user, password=os.environ["PSEUDOLIFE_TEST_PG_PASSWORD"])
    name = "s3_shadow_" + uuid.uuid4().hex[:12]
    dsn = make_conninfo(dbname=name, **admin)
    root = out.parent / name
    root.mkdir(parents=True)
    writer = shadow = None
    writer_paused = False
    created = False
    owns_role = False
    membership_roles = []
    try:
        with psycopg.connect(dbname="postgres", autocommit=True, **admin) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
            created = True
        print("disposable database created; seeding with w2d_seed.py", flush=True)
        config = """embedding:
  device: cpu
memory:
  dream:
    enabled: false
  reranker:
    enabled: false
updates:
  check_releases: false
coordination:
  enabled: true
"""
        writer_home = daemons.make_home(root, "python", config)
        env = daemons.base_env(writer_home, {
            "PSEUDOLIFE_MCP_DATABASE_URL": dsn, "PSEUDOLIFE_MCP_TOKEN": "synthetic-shadow-proof",
            "PSEUDOLIFE_MCP_AUTOSAVE_SECONDS": "3600", "PSEUDOLIFE_SESSION_REAP_SECONDS": "3600",
            "PSEUDOLIFE_RELEASE_CHECK": "0", "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2",
            "PSEUDOLIFE_BUILD_GIT_SHA": os.environ.get("S3_HEAD", "uncommitted"),
            "PSEUDOLIFE_BUILD_DIRTY": os.environ.get("S3_DIRTY", "true"),
            "PSEUDOLIFE_BUILD_SOURCE": "checkout",
        })
        with (root / "seed.log").open("wb") as log:
            seeded = subprocess.run([sys.executable, str(HERE / "w2d_seed.py"), "0"],
                                    cwd=HERE.parents[2], env=dict(env, PYTHONPATH=str(HERE.parents[2])),
                                    stdout=log, stderr=subprocess.STDOUT)
        if seeded.returncode:
            raise RuntimeError(f"w2d_seed exit {seeded.returncode}; see seed.log")
        print("seed complete; applying shadow role twice", flush=True)
        password = secrets.token_hex(24)
        with psycopg.connect(dsn) as conn:
            assert not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='pseudolife_shadow'").fetchone(), "test shadow role already exists; do not change another proof's credentials"
        owns_role = True
        apply_role(container, user, name, password)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("GRANT UPDATE(value) ON meta TO pseudolife_shadow")
            conn.execute("ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO pseudolife_shadow")
        apply_role(container, user, name, password)  # Idempotency.
        with psycopg.connect(dsn, autocommit=True) as conn:
            if conn.info.server_version >= 160000:
                parent, grantor = name + "_parent", name + "_grantor"
                for role in (parent, grantor):
                    conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
                    membership_roles.append(role)
                conn.execute(sql.SQL("GRANT {} TO {} WITH ADMIN OPTION").format(sql.Identifier(parent), sql.Identifier(grantor)))
                conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(grantor)))
                conn.execute(sql.SQL("GRANT {} TO pseudolife_shadow").format(sql.Identifier(parent)))
                conn.execute("RESET ROLE")
                apply_role(container, user, name, password, expect_failure="still has role memberships")
                conn.execute(sql.SQL("REVOKE {} FROM pseudolife_shadow GRANTED BY {}").format(sql.Identifier(parent), sql.Identifier(grantor)))
        apply_role(container, user, name, password)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE SEQUENCE shadow_fixture_seq")
            conn.execute("GRANT USAGE ON SEQUENCE shadow_fixture_seq TO PUBLIC")
        apply_role(container, user, name, password, expect_failure=True)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("REVOKE USAGE ON SEQUENCE shadow_fixture_seq FROM PUBLIC")
            conn.execute("ALTER DEFAULT PRIVILEGES GRANT USAGE ON SEQUENCES TO PUBLIC")
        apply_role(container, user, name, password, expect_failure=True)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("ALTER DEFAULT PRIVILEGES REVOKE USAGE ON SEQUENCES FROM PUBLIC")
            conn.execute("CREATE VIEW shadow_meta_view AS SELECT key,value FROM meta")
            conn.execute("GRANT UPDATE(value) ON shadow_meta_view TO PUBLIC")
        apply_role(container, user, name, password, expect_failure=True)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("REVOKE UPDATE(value) ON shadow_meta_view FROM PUBLIC")
        shadow_dsn = make_conninfo(host="127.0.0.1", port=5434, dbname=name,
                                  user="pseudolife_shadow", password=password)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE TABLE shadow_future_fixture (id integer)")
        with psycopg.connect(shadow_dsn, autocommit=True) as conn:
            conn.execute("SELECT * FROM shadow_future_fixture").fetchall()
            conn.execute("SET default_transaction_read_only = off")
            for statement in ("INSERT INTO shadow_future_fixture VALUES (1)",
                              "UPDATE meta SET value=value", "DELETE FROM meta", "CREATE TABLE shadow_forbidden(id integer)"):
                try:
                    conn.execute(statement)
                except psycopg.errors.InsufficientPrivilege:
                    pass
                else:
                    raise AssertionError("shadow role unexpectedly has write privileges")
        writer = daemons.python_daemon(writer_home, daemons.free_port(), env).start(wait_s=180)
        python = compare.Endpoint(f"http://127.0.0.1:{writer.port}", "synthetic-shadow-proof", replay=False)
        python.initialize()
        for query in ("writer lease", "deployment rollback", "database host"):
            status, body = python.read({"tool": "memory_search", "args": {"query": query}})
            if status != 200:
                raise AssertionError("writer seed search failed")
        cases = compare.corpus(dsn=dsn, limit=3)
        # Include reads of access-counted entries and GETs, with no Python
        # requests during the table-invariance interval.
        with psycopg.connect(dsn) as conn:
            bank_id = str(uuid.uuid4())
            conn.execute("INSERT INTO meta(key,value) VALUES ('coordination_bank_id',%s::jsonb) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(bank_id),))
            entry_id = conn.execute("SELECT min(id) FROM entries").fetchone()[0]
            # The duplicates GET used to clean this stale marker, making a
            # nominal read attempt DELETE/UPDATE on a nonempty bank.
            from psycopg.types.json import Jsonb
            conn.execute("INSERT INTO meta(key,value) VALUES ('curation_auto_dismissals_v1',%s) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (Jsonb({"stale": {
                             "store": "lesson", "a": {"key": "missing-a"}, "b": {"key": "missing-b"}}}),))
        cases += [{"name": "get", "tool": "memory_get", "args": {"entry_id": entry_id}},
                  {"name": "entry", "path": "/api/entry", "params": {"id": entry_id}}]
        # Keep Python's lease but quiesce its queued principal-activity flush
        # and all other background writers during the invariance interval.
        # No table is excluded to accommodate the writer's pending telemetry.
        assert writer.proc.poll() is None
        os.kill(writer.proc.pid, signal.SIGSTOP)
        writer_paused = True
        deadline = time.monotonic() + 10
        with psycopg.connect(dsn, autocommit=True) as conn:
            while conn.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                               "AND pid<>pg_backend_pid() AND state='active'").fetchone()[0]:
                if time.monotonic() >= deadline:
                    raise AssertionError("writer database requests did not quiesce")
                time.sleep(.05)
        before = snapshot(dsn)
        held = lease_holders(dsn)
        assert len(held) == 1, "Python must own the writer lease"
        shadow_config = config.replace("  dream:\n    enabled: false", "  dream:\n    enabled: true\n  retrieval_log:\n    enabled: true")
        home = daemons.make_home(root, "rust", shadow_config)
        rust_env = daemons.base_env(home, dict(env, PSEUDOLIFE_MCP_DATA_DIR=str(home / "data"),
                                              PSEUDOLIFE_MCP_DATABASE_URL=shadow_dsn,
                                              PSEUDOLIFE_READ_ONLY="1", PSEUDOLIFE_MCP_AUTOSAVE_SECONDS="0.1",
                                              PSEUDOLIFE_SESSION_REAP_SECONDS="0.1"))
        for key in ("ORT_DYLIB_PATH", "PSEUDOLIFE_DAEMON_ONNX_DIR", "PSEUDOLIFE_DAEMON_RERANK_DIR"):
            if key in os.environ:
                rust_env[key] = os.environ[key]
        shadow = daemons.rust_daemon(binary.resolve(), home, daemons.free_port(), rust_env).start(wait_s=180)
        rust = compare.Endpoint(f"http://127.0.0.1:{shadow.port}", "synthetic-shadow-proof")
        rust.initialize()
        bound_reads(shadow.port, bank_id)
        status, effective_config = rust.request("GET", "/api/config")
        assert status == 200
        effective_values = {k["path"]: k["value"] for group in effective_config["groups"] for k in group["knobs"]}
        for key in ("memory.retrieval_log.enabled", "memory.dream.enabled"):
            assert effective_values[key] is False, "config reports an overridden shadow setting as enabled"
        assert rust.request("GET", "/api/agents")[0] == 200
        refresh_line = "read-only shadow: resident state refreshed"
        initial_refreshes = shadow.log.read_text(errors="replace").count(refresh_line)
        assert initial_refreshes == 1
        observations = []
        for case in cases:
            status, body = rust.read(case)
            observations.append({"name": case["name"], "status": status})
            if "tool" in case:
                assert status == 200, f"read tool failed: {case['tool']} ({status})"
        for path in json.loads((HERE / "goldens/routes.json").read_text())["GET"]:
            status, response = rust.request("GET", path)
            observations.append({"name": path, "status": status})
            if path in ("/api/dream/status", "/api/curation/duplicates"):
                assert status == 403 and response["error"] == "read_only_shadow"
        # Malformed write arguments are irrelevant: the shadow boundary must
        # refuse before any tool body can acquire identity or start work.
        for tool, args in (("memory_store", {"text": "forbidden"}),
                           ("memory_episode_start", {"title": "forbidden"}),
                           ("memory_dream", {})):
            _, envelope = rust.rpc("tools/call", {"name": tool, "arguments": args})
            assert "read_only_shadow" in json.dumps(envelope), tool
        routes = json.loads((HERE / "goldens/routes.json").read_text())["POST"]
        for path in routes + ["/api/pair", "/api/hook/session-start", "/api/hook/session-end",
                             "/api/console/link", "/api/console/session",
                             "/api/console/sessions/revoke"]:
            status, body = rust.request("POST" if path != "/api/hook/session-start" else "GET", path, {})
            assert status == 403 and body["error"] == "read_only_shadow", path
        assert lease_holders(dsn) == held, "shadow acquired a lease"
        after = snapshot(dsn)
        assert before == after, "Rust burst changed table or sequence state"
        daemon_log = shadow.log.read_text(errors="replace")
        assert daemon_log.count(refresh_line) == initial_refreshes, "unchanged reads rehydrated the bank"
        assert "read-only transaction" not in daemon_log and "permission denied" not in daemon_log, "a read attempted a forbidden write"
        # Exercise clean exit's autosave path too.
        shadow.stop()
        assert snapshot(dsn) == before, "Rust shutdown changed state"
        os.kill(writer.proc.pid, signal.SIGCONT)
        writer_paused = False
        shadow.start(wait_s=180)
        rust = compare.Endpoint(f"http://127.0.0.1:{shadow.port}", "synthetic-shadow-proof")
        python = compare.Endpoint(f"http://127.0.0.1:{writer.port}", "synthetic-shadow-proof")
        summary = compare.compare(python, rust, cases, triage)
        assert python.read({"tool": "memory_search", "args": {"query": "shadow-replay-only-fixture"}})[0] == 200
        replay_corpus = compare.corpus(dsn=dsn, limit=100)
        assert not any(c.get("args", {}).get("query") == "shadow-replay-only-fixture" for c in replay_corpus)
        with psycopg.connect(dsn) as conn:
            assert conn.execute("SELECT 1 FROM retrieval_events WHERE query_text='shadow-replay-only-fixture' "
                                "AND session_id LIKE 'shadow-compare:%%'").fetchone()
        # A writer commit after shadow hydration must be visible on the next
        # shadow read; keeping the startup resident snapshot would miss it.
        _, answer = python.rpc("tools/call", {"name": "memory_fact_set", "arguments": {
            "entity": "shadow-refresh-probe", "attribute": "phase", "value": "committed-after-hydration"}})
        assert not answer.get("result", {}).get("isError") and "result" in answer
        time.sleep(1.1)  # The shadow deliberately reuses a successful probe for 1 s.
        status, fact = rust.read({"tool": "memory_fact_get", "args": {
            "entity": "shadow-refresh-probe", "attribute": "phase"}})
        assert status == 200 and fact.get("record") and fact["record"]["value"] == "committed-after-hydration", "shadow resident state is stale"
        os.kill(writer.proc.pid, signal.SIGSTOP)
        writer_paused = True
        deadline = time.monotonic() + 10
        with psycopg.connect(dsn, autocommit=True) as conn:
            while conn.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                               "AND pid<>pg_backend_pid() AND state='active'").fetchone()[0]:
                assert time.monotonic() < deadline, "writer did not quiesce"
                time.sleep(.05)
        # Awareness has a non-initializing writer fast path. The shadow must
        # refresh even when this GET is the only read after an episode change.
        assert rust.request("GET", "/api/agents")[0] == 200
        refreshes_before_change = shadow.log.read_text(errors="replace").count(refresh_line)
        probe_line = "read-only shadow: resident state probed"
        probes_before_change = shadow.log.read_text(errors="replace").count(probe_line)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("INSERT INTO episodes(id,title,started_at,session_key) VALUES (%s,%s,%s,%s)",
                         ("shadow-awareness-probe", "Fresh writer session", time.time(), "shadow-awareness-probe"))
        time.sleep(1.1)
        def awareness(_):
            return compare.Endpoint(f"http://127.0.0.1:{shadow.port}", "synthetic-shadow-proof").request("GET", "/api/agents")
        with ThreadPoolExecutor(max_workers=16) as pool:
            for status, agents in pool.map(awareness, range(16)):
                assert status == 200 and "shadow-awareness-probe" in json.dumps(agents), "shadow awareness is stale"
        assert shadow.log.read_text(errors="replace").count(refresh_line) == refreshes_before_change + 1, "concurrent refreshes were not coalesced"
        assert shadow.log.read_text(errors="replace").count(probe_line) == probes_before_change + 1, "concurrent readers did not share a probe"
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("UPDATE episodes SET ended_at=%s WHERE id='shadow-awareness-probe'", (time.time(),))
        time.sleep(1.1)
        assert "shadow-awareness-probe" not in json.dumps(rust.request("GET", "/api/agents")[1])
        # A transaction that began earlier can commit after a newer row;
        # a max(xmin)/max(updated_at) marker alone would miss this change.
        with psycopg.connect(dsn) as older:
            older.execute("UPDATE episodes SET ended_at=NULL,title='Older transaction committed late' "
                          "WHERE id='shadow-awareness-probe'")
            with psycopg.connect(dsn, autocommit=True) as newer:
                newer.execute("INSERT INTO episodes(id,title,started_at,session_key) VALUES (%s,%s,%s,%s)",
                              ("shadow-newer-probe", "Newer transaction", time.time(), "shadow-newer-probe"))
            time.sleep(1.1)
            assert "shadow-awareness-probe" not in json.dumps(rust.request("GET", "/api/agents")[1])
            older.commit()
        time.sleep(1.1)
        assert "Older transaction committed late" in json.dumps(rust.request("GET", "/api/agents")[1]), "late commit was missed"
        os.kill(writer.proc.pid, signal.SIGCONT)
        writer_paused = False
        shadow.stop()
        writer.stop()
        # The gates must not leak into the default writer mode. This same
        # fixture now has one Rust writer, with ordinary bank privileges.
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM meta WHERE key='dream_ack_secret_v1'")
            old_events = conn.execute("SELECT count(*) FROM retrieval_events").fetchone()[0]
            old_access = conn.execute("SELECT sum(access_count) FROM entries").fetchone()[0]
        writer_home = daemons.make_home(root, "rust-writer", config)
        writer_env = daemons.base_env(writer_home, dict(env, PSEUDOLIFE_MCP_DATA_DIR=str(writer_home / "data"),
                                                       PSEUDOLIFE_MCP_DATABASE_URL=make_conninfo(dsn, options="-c search_path=public")))
        for key in ("ORT_DYLIB_PATH", "PSEUDOLIFE_DAEMON_ONNX_DIR", "PSEUDOLIFE_DAEMON_RERANK_DIR"):
            if key in os.environ:
                writer_env[key] = os.environ[key]
        shadow = daemons.rust_daemon(binary.resolve(), writer_home, daemons.free_port(), writer_env)
        shadow.start(wait_s=180)
        writer_rust = compare.Endpoint(f"http://127.0.0.1:{shadow.port}", "synthetic-shadow-proof")
        writer_rust.initialize()
        for case in ({"tool": "memory_search", "args": {"query": "writer lease"}},
                     {"tool": "memory_get", "args": {"entry_id": entry_id}}):
            assert writer_rust.read(case)[0] == 200
        status, overview = writer_rust.request("GET", "/api/overview")
        assert status == 200 and "error" not in overview["dream"]
        with psycopg.connect(dsn) as conn:
            assert conn.execute("SELECT count(*) FROM retrieval_events").fetchone()[0] > old_events
            assert conn.execute("SELECT sum(access_count) FROM entries").fetchone()[0] > old_access
            assert conn.execute("SELECT 1 FROM meta WHERE key='dream_ack_secret_v1'").fetchone()
        summary["proof"] = {"table_and_sequence_count": len(before), "unchanged": True,
                            "snapshot_sha256": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
                            "no_shadow_lease": True, "role_idempotent": True,
                            "future_table_select": True, "write_privileges_denied": True,
                            "writer_telemetry_and_dream_initialization": True,
                            "later_writer_commit_visible": True,
                            "awareness_episode_changes_visible": True,
                            "unchanged_burst_hydrations": initial_refreshes,
                            "concurrent_readers": 16, "coalesced_refreshes": 1,
                            "coalesced_probes": 1, "probe_reuse_seconds": 1,
                            "bound_mcp_initialization_and_search": True,
                            "older_transaction_late_commit_visible": True,
                            "tagged_replays_excluded": True, "effective_config_overrides": True,
                            "surviving_membership_refused": bool(membership_roles),
                            "reads": observations}
        out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        out.with_suffix(".txt").write_text(compare.digest(summary), encoding="utf-8")
        print(f"proof: {len(before)} tables/sequences unchanged; no shadow lease; {summary['cases']} comparisons", flush=True)
    finally:
        if writer_paused and writer and writer.proc.poll() is None:
            os.kill(writer.proc.pid, signal.SIGCONT)
        for daemon in (shadow, writer):
            if daemon:
                daemon.stop()
        if created:
            with psycopg.connect(dbname="postgres", autocommit=True, **admin) as conn:
                conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            print("disposable database dropped", flush=True)
        if owns_role:
            with psycopg.connect(dbname="postgres", autocommit=True, **admin) as conn:
                conn.execute("DROP ROLE IF EXISTS pseudolife_shadow")
                for role in reversed(membership_roles):
                    conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rust-bin", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pg-container", required=True)
    parser.add_argument("--triage", type=Path)
    args = parser.parse_args()
    proof(args.rust_bin, args.out, args.pg_container,
          json.loads(args.triage.read_text()) if args.triage else ())

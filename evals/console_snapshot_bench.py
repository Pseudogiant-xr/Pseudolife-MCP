"""What the Console's board snapshot costs on a board the size of the live one.

Seeds a scratch database through the real board store (register, attach,
send, receive, ack, mark_attempt, prune), then times the Console timeline
query three ways (spec 2026-11-02, section 2, "a measured run on a bank of
at least the live board's size"):

* ``old``: the pre-v59 single-principal query, verbatim (the copy
  ``tests/test_coordination_console.py`` keeps as its reference), for the
  busiest principal;
* ``new-one``: the v59 query with that principal alone as the scope;
* ``new-owner``: the v59 query with the whole owner scope.

Each arm runs on a fresh connection right after ``ANALYZE`` (the cold-ish
run: empty plan cache, shared buffers and the OS cache not dropped), then
ten more times; each run is a read-only REPEATABLE READ transaction under
the snapshot's own ``statement_timeout = '5s'``, the v59 arms with its
``jit = off``, and once more with JIT the other way as a diagnostic. Then ``EXPLAIN (ANALYZE,
BUFFERS)``. The full ``console_snapshot`` call (roster, counts, timeline) is
timed too for the two v59 arms. No model, GPU or live-bank access; every body
is synthetic filler.

The default board: 470 agents over five principals (four the owner's, one
someone else's), the owner's ``fleet`` carrying 400 idle agents; 25,000
sends over 6.8 simulated days, a third of them to three conductor-like
agents, most agents getting a handful; recipients pull and ack as they go,
leaving about one in twelve unacknowledged to expire; a prune every 30
simulated minutes. ``maintainer`` is in the owner scope but registers no
agent here.

Run with ``python -m evals.console_snapshot_bench --out <new-file.json>``.
The connection is the test server's (PSEUDOLIFE_TEST_PG_HOST_PORT and the
suite's own login, ``tests.pg_defaults.default_login``), never the bank's;
the scratch database is created and dropped by this run.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import platform
import random
import statistics
import sys
import time

import psycopg
from psycopg import sql

DAY = 86400
SCRATCH_PREFIX = "pseudolife_memory_bench_console_"
CONDUCTORS = ("desk", "laptop", "default")


def _percentiles(values):
    ordered = sorted(values)
    pick = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))]  # noqa: E731
    return {"n": len(ordered), "median_ms": round(statistics.median(ordered) * 1000, 2),
            "p90_ms": round(pick(0.90) * 1000, 2), "max_ms": round(max(ordered) * 1000, 2)}


@contextmanager
def scratch_database(admin_url: str):
    name = f"{SCRATCH_PREFIX}{os.getpid()}_{int(time.time())}"
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        params = conninfo_to_dict(admin_url)
        params["dbname"] = name
        dsn = make_conninfo(**params)
        from pseudolife_memory.storage.schema import assert_disposable_database, ensure_schema
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("SET search_path TO public")
            assert_disposable_database(conn)
            ensure_schema(conn)
        yield name, dsn
    finally:
        with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)))


def seed(dsn: str, *, sends: int, days: float, seed_value: int, sim: list) -> dict:
    from pseudolife_memory.storage.coordination import (
        CoordinationConnection, CoordinationError, CoordinationStore)
    rng = random.Random(seed_value)
    storage = CoordinationConnection(dsn)
    store = CoordinationStore(storage, clock=lambda: sim[0])
    errors: dict[str, int] = {}
    started = time.perf_counter()

    def call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except CoordinationError as exc:
            errors[exc.code] = errors.get(exc.code, 0) + 1
            return None

    try:
        layout = {"desk": 30, "laptop": 25, "default": 10, "fleet": 400, "guest": 5}
        agents: dict[str, list] = {}
        for principal, count in layout.items():
            agents[principal] = []
            for i in range(count):
                sim[0] += 0.5
                row = store.register(principal, label=f"{principal}-{i}", project="bench",
                                     task="console", status="working")
                agents[principal].append({**row, "principal": principal})
        conductors = [agents[p][0] for p in CONDUCTORS]
        attached = {}
        for agent in conductors:
            out = store.attach(agent["principal"], agent["agent_id"], agent["credential"],
                               attachment_id="bench", wake_enabled=True)
            attached[agent["agent_id"]] = out["generation"]
        busy = [a for p in ("desk", "laptop", "default") for a in agents[p][1:]]
        guests = agents["guest"]
        fleet = agents["fleet"]
        fleet_senders = rng.sample(fleet, 20)
        # A Zipf-like weight over the busy agents: a few get much, most little.
        weights = [1.0 / (rank + 1) ** 1.1 for rank in range(len(busy))]
        cursor: dict[str, str | None] = {}
        abandoned: set[str] = set()
        step = days * DAY / sends
        next_prune = sim[0] + 1800
        counts = {"receives": 0, "acks": 0, "attempts": 0, "prunes": 0}
        for n in range(sends):
            sim[0] += rng.uniform(0.5, 1.5) * step
            roll = rng.random()
            if roll < 0.36:
                recipient = rng.choice(conductors)
            elif roll < 0.42:
                recipient = rng.choice(fleet)
            elif roll < 0.46:
                recipient = rng.choice(guests)
            else:
                recipient = rng.choices(busy, weights)[0]
            if recipient["principal"] == "guest" and rng.random() < 0.7:
                sender = rng.choice([g for g in guests if g is not recipient])
            elif rng.random() < 0.01:
                sender = rng.choice(fleet_senders)
            else:
                sender = rng.choice([a for a in busy + guests[:1] if a is not recipient])
            sent = call(store.send, sender["principal"], sender["agent_id"], sender["credential"],
                        to=recipient["agent_id"], text=f"synthetic board note {n:06d} " * 4,
                        request_id=f"r{n}")
            if sent is None:
                continue
            if rng.random() < 0.08:
                abandoned.add(sent["message_id"])
            rid = recipient["agent_id"]
            if rid in attached and rng.random() < 0.4:
                again = call(store.attach, recipient["principal"], rid, recipient["credential"],
                             attachment_id="bench", wake_enabled=True)
                if again is not None:
                    attached[rid] = again["generation"]
                    if call(store.mark_attempt, recipient["principal"], rid,
                            recipient["credential"], message_id=sent["message_id"],
                            attachment_id="bench", generation=attached[rid]) is not None:
                        counts["attempts"] += 1
            if recipient["principal"] != "fleet" and rng.random() < (0.3 if rid in attached else 0.5):
                page = call(store.receive, recipient["principal"], rid, recipient["credential"],
                            after=cursor.get(rid), limit=50)
                if page is not None:
                    counts["receives"] += 1
                    cursor[rid] = page["after"]
                    for message in page["messages"]:
                        if message["message_id"] in abandoned:
                            continue
                        if call(store.ack, recipient["principal"], rid, recipient["credential"],
                                message_id=message["message_id"]) is not None:
                            counts["acks"] += 1
            if sim[0] >= next_prune:
                store.prune()
                counts["prunes"] += 1
                next_prune = sim[0] + 1800
        store.prune()
        conn = storage.conn
        events = dict(conn.execute("SELECT event, count(*) FROM coordination_events "
                                   "GROUP BY event ORDER BY event").fetchall())
        by_principal = dict(conn.execute(
            "SELECT principal, count(*) FROM coordination_agents GROUP BY principal").fetchall())
        received = [r[0] for r in conn.execute(
            "SELECT count(*) FROM coordination_events WHERE event='send' "
            "GROUP BY recipient_agent_id ORDER BY 1 DESC").fetchall()]
        span = conn.execute("SELECT min(created_at), max(created_at) FROM coordination_events").fetchone()
    finally:
        storage.close()
    return {"seconds": round(time.perf_counter() - started, 1), "errors": errors, **counts,
            "agents_by_principal": by_principal, "agents": sum(by_principal.values()),
            "events": events, "events_total": sum(events.values()),
            "sends_received_per_agent": {
                "agents_with_mail": len(received), "max": received[0], "top5": received[:5],
                "median": statistics.median(received), "p90": received[int(0.1 * len(received))]},
            "span_days": round((span[1] - span[0]) / DAY, 2)}


def _nodes(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _nodes(value)


def time_query(dsn: str, query: str, params, repeats: int, *, jit_off: bool = False) -> dict:
    """First run on a fresh connection right after ANALYZE, then ``repeats``.
    ``jit_off`` adds ``SET LOCAL jit = off``, as the v59 snapshot does."""
    local = "SET LOCAL statement_timeout = '5s'" + ("; SET LOCAL jit = off" if jit_off else "")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("ANALYZE coordination_events, coordination_agents, coordination_messages")
    times, rows = [], None
    with psycopg.connect(dsn) as conn:
        for _ in range(1 + repeats):
            start = time.perf_counter()
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                conn.execute(local)
                rows = conn.execute(query, params).fetchall()
            times.append(time.perf_counter() - start)
        with conn.transaction():
            conn.execute(local)
            plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query,
                                params).fetchone()[0]
            text = [r[0] for r in conn.execute("EXPLAIN (ANALYZE, BUFFERS) " + query,
                                               params).fetchall()]
    top = plan[0]
    visible = [n for n in _nodes(plan) if n.get("Subplan Name") == "CTE visible_mail"]
    return {"cold_ish_ms": round(times[0] * 1000, 2), "repeats": _percentiles(times[1:]),
            "rows_returned": len(rows),
            "explain": {"execution_ms": top["Execution Time"], "planning_ms": top["Planning Time"],
                        "shared_hit_blocks": top["Plan"].get("Shared Hit Blocks"),
                        "shared_read_blocks": top["Plan"].get("Shared Read Blocks"),
                        "visible_mail_rows": (visible[0]["Actual Rows"] * visible[0]["Actual Loops"]
                                              if visible else None),
                        "jit_total_ms": (top.get("JIT") or {}).get("Timing", {}).get("Total"),
                        "jit_functions": (top.get("JIT") or {}).get("Functions"),
                        "text": text},
            "_rows": rows}


def time_snapshot(dsn: str, principal: str, scope, sim_end: float, repeats: int) -> dict:
    from pseudolife_memory.storage.coordination import CoordinationConnection, CoordinationStore
    storage = CoordinationConnection(dsn)
    store = CoordinationStore(storage, clock=lambda: sim_end)
    times = []
    try:
        for _ in range(1 + repeats):
            start = time.perf_counter()
            store.console_snapshot(principal, scope=scope, limit=50)
            times.append(time.perf_counter() - start)
    finally:
        storage.close()
    return {"first_ms": round(times[0] * 1000, 2), "repeats": _percentiles(times[1:])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.console_snapshot_bench",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="a NEW JSON file")
    parser.add_argument("--sends", type=int, default=25000)
    parser.add_argument("--days", type=float, default=6.8)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20261102)
    args = parser.parse_args(argv)
    out = Path(args.out)
    if out.exists():
        parser.error(f"{out} exists; results are never overwritten")
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo))
    # The test server's own login, as the suite resolves it (an override, else
    # the test login file); never the bank owner's.
    from psycopg.conninfo import make_conninfo
    from tests.pg_defaults import default_login
    host, _, port = os.environ["PSEUDOLIFE_TEST_PG_HOST_PORT"].rpartition(":")
    user, password = default_login()
    admin_url = make_conninfo(host=host, port=port, dbname="postgres", user=user,
                              password=password)
    from pseudolife_memory.principals import owner_principals
    from pseudolife_memory.storage.coordination import CONSOLE_EVENTS_CAP, CONSOLE_TIMELINE_SQL
    from pseudolife_memory.utils.config import CoordinationConfig
    from tests.test_coordination_console import _REFERENCE_TIMELINE
    owners = sorted(owner_principals(
        CoordinationConfig(not_owner_principals=["guest"]), store=None,
        token_map={"t1": "desk", "t2": "laptop", "t3": "fleet", "t4": "guest"},
        single_token="t0"))
    sim_end = time.time()
    sim = [sim_end - args.days * DAY]
    head = os.popen(f"git -C {repo} rev-parse HEAD").read().strip()
    with scratch_database(admin_url) as (name, dsn):
        with psycopg.connect(dsn) as conn:
            server = conn.execute("SHOW server_version").fetchone()[0]
            settings = dict(conn.execute(
                "SELECT name, setting FROM pg_settings WHERE name IN "
                "('shared_buffers','work_mem','effective_cache_size','random_page_cost',"
                "'max_parallel_workers_per_gather')").fetchall())
        seeded = seed(dsn, sends=args.sends, days=args.days, seed_value=args.seed, sim=sim)
        with psycopg.connect(dsn) as conn:
            busiest = conn.execute(
                "SELECT principal, count(*) FROM coordination_events WHERE principal = ANY(%s) "
                "AND event IN ('send','read','ack','attempt','served','woke','lease_expire') "
                "GROUP BY principal ORDER BY 2 DESC LIMIT 1", (owners,)).fetchone()
            sizes = dict(conn.execute(
                "SELECT relname, pg_total_relation_size(oid) FROM pg_class WHERE relname IN "
                "('coordination_events','coordination_messages','coordination_agents')").fetchall())
        principal = busiest[0]
        queries = {
            "old": (_REFERENCE_TIMELINE, (principal,) * 5),
            "new-one": (CONSOLE_TIMELINE_SQL, {"scope": [principal], "cap": CONSOLE_EVENTS_CAP}),
            "new-owner": (CONSOLE_TIMELINE_SQL, {"scope": owners, "cap": CONSOLE_EVENTS_CAP}),
        }
        # Each arm as it ships: the pre-v59 query ran with the server's JIT,
        # the v59 snapshot turns JIT off. The diagnostic arms flip that.
        shipped_jit_off = {"old": False, "new-one": True, "new-owner": True}
        arms = {name: time_query(dsn, *query, args.repeats, jit_off=shipped_jit_off[name])
                for name, query in queries.items()}
        diagnostics = {
            name + ("-jit-on" if shipped_jit_off[name] else "-jit-off"):
                time_query(dsn, *query, args.repeats, jit_off=not shipped_jit_off[name])
            for name, query in queries.items()}
        for arm in diagnostics.values():
            arm.pop("_rows")
        old_rows, one_rows = arms["old"].pop("_rows"), arms["new-one"].pop("_rows")
        arms["new-owner"].pop("_rows")
        # The page is the first 100 rows; columns: seq, event, created_at,
        # agent_id, recipient_agent_id, message_id, ...
        old_page, one_page = old_rows[:100], one_rows[:100]
        # Whole rows, so the artifact shows which column differs (an
        # expiry's expired_count, under the bounded visible mail).
        only_old = [list(r) for r in old_page if r not in one_page]
        only_new = [list(r) for r in one_page if r not in old_page]
        snapshots = {"new-one": time_snapshot(dsn, principal, [principal], sim[0], args.repeats),
                     "new-owner": time_snapshot(dsn, principal, owners, sim[0], args.repeats)}
    result = {
        "harness": "evals/console_snapshot_bench.py", "commit": head,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": platform.python_version(), "platform": platform.platform(),
        "cpus": os.cpu_count(),
        "memory_gb": round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1),
        "postgres": server, "postgres_settings": settings, "scratch_database": name,
        "workload": {"sends": args.sends, "days": args.days, "seed": args.seed,
                     "repeats": args.repeats, "owner_scope": owners,
                     "busiest_owner_principal": {"principal": principal, "events": busiest[1]}},
        "seeded": seeded, "table_bytes": sizes,
        "statement_timeout_ms": 5000,
        "timeline_query": arms,
        "old_vs_new_one_same_rows": old_rows == one_rows,
        "old_vs_new_one_page_diff": {"only_old": only_old, "only_new": only_new},
        "jit_diagnostic": diagnostics,
        "console_snapshot_call": snapshots,
        "notes": ["cold-ish = the first execution on a fresh connection right after ANALYZE; "
                  "shared buffers and the OS page cache were not dropped",
                  "timeline times are execute + fetch inside the snapshot's read-only "
                  "REPEATABLE READ transaction with statement_timeout 5s",
                  "timeline_query arms run as they ship: old with the server's JIT (on), the "
                  "v59 arms with SET LOCAL jit = off as console_snapshot sets it; "
                  "jit_diagnostic runs each arm the other way"],
    }
    out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({arm: {"cold_ish_ms": a["cold_ish_ms"], **a["repeats"],
                            "visible_mail_rows": a["explain"]["visible_mail_rows"],
                            "jit_total_ms": a["explain"]["jit_total_ms"]}
                      for arm, a in {**arms, **diagnostics}.items()}, indent=1))
    print(json.dumps({"page_diff": result["old_vs_new_one_page_diff"]}))
    print(json.dumps({"seeded": {k: seeded[k] for k in ("seconds", "agents", "events_total",
                                                         "events", "span_days")},
                      "snapshots": snapshots, "busiest": busiest}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

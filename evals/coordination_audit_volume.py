"""What the board's audit log costs: rows, bytes and mutation latency.

Replays one synthetic coordination night into a private scratch database on
the bench server and measures the ``coordination_events`` table it leaves, plus
per-call latency with the chain append against a control arm whose append is
a no-op (the same workload, the same first-read stamping, no log rows). No
model, GPU or live-bank access; every body is synthetic filler.

The default scale is the 2026-09-23/24 fifteen-session trial as its board
export recorded it: 40 agents and 623 messages over 9.6 hours, bodies of mean
439, median 397 and at most 2110 bytes. That export has no status history (the
gap this log closes), so ``--updates-per-agent`` is an assumption, stated in
the artifact.

Run with ``python -m evals.coordination_audit_volume --out <new-file.json>``.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import platform
import random
import statistics
import sys
import time
import uuid

import psycopg
from psycopg import sql

REFERENCE = ("2026-09-23/24 fifteen-session coordination trial board export: 40 agents, "
             "623 messages, 273,426 body bytes (mean 439, median 397, max 2110) over 9.6 h")
DAY = 86400


def body(rng: random.Random, n: int) -> str:
    size = max(40, min(2110, int(rng.gauss(440, 250))))
    return (f"synthetic board note {n:05d} " * (size // 26 + 1))[:size]


def _percentiles(values):
    ordered = sorted(values)
    pick = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))]  # noqa: E731
    return {"n": len(ordered), "p50_ms": round(pick(0.50) * 1000, 3),
            "p95_ms": round(pick(0.95) * 1000, 3), "mean_ms": round(statistics.mean(ordered) * 1000, 3)}


def measure(dsn: str, *, agents: int, messages: int, updates_per_agent: int,
            attaches_per_agent: int, seed: int, audit: bool = True) -> dict:
    """Run the workload once against an empty board in ``dsn``."""
    from pseudolife_memory.storage.coordination import CoordinationConnection, CoordinationStore

    offset = [0.0]
    storage = CoordinationConnection(dsn)
    store = CoordinationStore(storage, clock=lambda: time.time() + offset[0])
    if not audit:
        store._append = lambda events, now, head=None: None
    storage.conn.execute("TRUNCATE coordination_messages, coordination_agents, coordination_events")
    rng = random.Random(seed)
    timings = {"send": [], "receive": [], "ack": [], "update": []}

    def timed(kind, call):
        start = time.perf_counter()
        result = call()
        timings[kind].append(time.perf_counter() - start)
        return result

    try:
        roster = [store.register("synthetic", label=f"agent-{i}", project="synthetic",
                                 task="volume", status="starting",
                                 capabilities={"pull": True, "resumable": True})
                  for i in range(agents)]

        def creds(agent):
            return "synthetic", agent["agent_id"], agent["credential"]

        for agent in roster:
            for n in range(attaches_per_agent):
                attached = store.attach(*creds(agent), attachment_id=f"a{n}")
                store.detach(*creds(agent), attachment_id=f"a{n}",
                             generation=attached["generation"])
        sent_by = {a["agent_id"]: 0 for a in roster}
        for n in range(messages):
            sender, recipient = rng.sample(roster, 2)
            # Stay under the per-sender minute rate by moving the clock on.
            sent_by[sender["agent_id"]] += 1
            if sent_by[sender["agent_id"]] % 50 == 0:
                offset[0] += 61
            timed("send", lambda: store.send(*creds(sender), to=recipient["agent_id"],
                                             text=body(rng, n), request_id=f"r{n}"))
            page = timed("receive", lambda: store.receive(*creds(recipient)))
            for message in page["messages"]:
                timed("ack", lambda: store.ack(*creds(recipient), message_id=message["message_id"]))
        for agent in roster:
            for n in range(updates_per_agent):
                timed("update", lambda: store.update(*creds(agent), status=f"milestone {n}"))
        offset[0] += 8 * DAY
        pruned = store.prune()
        conn = storage.conn
        events = dict(conn.execute("SELECT event, count(*) FROM coordination_events "
                                   "GROUP BY event ORDER BY event").fetchall())
        table = conn.execute("SELECT pg_total_relation_size('coordination_events'), "
                             "pg_relation_size('coordination_events'), "
                             "coalesce(sum(octet_length(payload)), 0) "
                             "FROM coordination_events").fetchone()
    finally:
        storage.close()
    total_events = sum(events.values())
    return {"events": events, "events_total": total_events, "prune": pruned,
            "table_total_bytes": table[0], "table_heap_bytes": table[1],
            "payload_bytes": int(table[2]),
            "bytes_per_event": round(table[0] / total_events, 1) if total_events else None,
            "latency": {kind: _percentiles(values) for kind, values in timings.items() if values}}


@contextmanager
def scratch_database(admin_url: str):
    name = f"pseudolife_audit_volume_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        params = conninfo_to_dict(admin_url)
        params["dbname"] = name
        dsn = make_conninfo(**params)
        from pseudolife_memory.storage.schema import ensure_schema
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("SET search_path TO public")
            ensure_schema(conn)
        yield dsn
    finally:
        with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.coordination_audit_volume",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="a NEW JSON file")
    parser.add_argument("--agents", type=int, default=40)
    parser.add_argument("--messages", type=int, default=623)
    parser.add_argument("--updates-per-agent", type=int, default=15)
    parser.add_argument("--attaches-per-agent", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20260924)
    parser.add_argument("--admin-url", help="bench server admin URL (default: the dev "
                                            "container from ops/.env)")
    args = parser.parse_args(argv)
    out = Path(args.out)
    if out.exists():
        parser.error(f"{out} exists; results are never overwritten")
    admin_url = args.admin_url
    if admin_url is None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from tests.pg_defaults import default_admin_url
        admin_url = default_admin_url()
    workload = {"agents": args.agents, "messages": args.messages,
                "updates_per_agent": args.updates_per_agent,
                "attaches_per_agent": args.attaches_per_agent, "seed": args.seed}
    arms = {}
    with scratch_database(admin_url) as dsn:
        with psycopg.connect(dsn) as conn:
            server = conn.execute("SHOW server_version").fetchone()[0]
        for arm, audit in (("control", False), ("audit", True), ("control-repeat", False),
                           ("audit-repeat", True)):
            arms[arm] = measure(dsn, audit=audit, **workload)
    audit = arms["audit"]
    per_night = audit["table_total_bytes"]
    result = {
        "harness": "evals/coordination_audit_volume.py",
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": platform.python_version(), "platform": platform.platform(),
        "postgres": server, "reference": REFERENCE,
        "workload": {**workload, "per_message": "send, pull receive, ack",
                     "assumption": "updates_per_agent and attaches_per_agent are assumed; "
                                   "the reference export records neither"},
        "arms": arms,
        "per_trial_night_mb": round(per_night / 1e6, 2),
        "if_every_night_were_a_trial_mb": {str(days): round(per_night * days / 1e6, 1)
                                           for days in (30, 90, 365)},
    }
    out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("per_trial_night_mb",
                                             "if_every_night_were_a_trial_mb")}))
    for arm, data in arms.items():
        print(arm, data["events_total"], "events", {k: v["p50_ms"] for k, v in data["latency"].items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

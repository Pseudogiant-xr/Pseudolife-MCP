"""Differential check of the cortex-first search block (slice W2-D cortex).

usage: python w2d_cortex_check.py [--mode NAME ...] [--mutant NAME] [--keep]

Seeds one disposable bank through the Python daemon's own write path
(``MemoryService.cortex_write`` / ``set_add`` / ``store`` / ``supersede``,
and ``PostgresStorage.add_trace``, the call the dream makes for a trace),
logs one retrieval event per query with ``MemoryService.search``, then for
each mode clones the seed twice (``CREATE DATABASE ... TEMPLATE``): a
Python copy and a Rust copy. On the Python copy it runs, per query, what
``web/routes.py:_search`` runs for the cortex block:
``cortex_search(q, top_k=5, min_score=guard_min_score)`` and, for a
non-empty block, ``attach_served_facts(event_id, facts)``; it records the
query vector Python used (``_embedder.encode_query``). The Rust copy gets
the same cases through the ignored Rust test
``read::cortex_search::tests::differential_cases_from_harness``, with that
same vector.

Comparison rules:

* entries are compared by value, exactly (scores are rounded to 4 dp on
  both sides and the query vector is shared);
* time is frozen: Python runs with ``time.time`` patched to ``FROZEN`` and
  Rust gets ``now = FROZEN``, so ``age``, ``effective_confidence``,
  ``stale`` and ``slot_reads.last_read_at`` are deterministic;
* bank state: every table of both copies is dumped (``dbstate``) before
  and after the queries. Each side's change set (rows added and removed per
  table) must be equal, and the final ``slot_reads`` and
  ``retrieval_events`` tables must be equal row for row. Python's service
  init runs (and writes what it writes) before the "before" dump.

Exit status: 0 only when every mode has zero entry diffs and zero state
diffs. ``--mutant NAME`` builds Rust with ``--features mutants`` and sets
``PSEUDOLIFE_DAEMON_MUTANT``; that run must exit non-zero.

Every database this script creates, clones or drops must match
``pl_cf_w2d_cortex_[a-z0-9_]+``; anything else is refused before a
connection opens. Logs in only with the test login
(``~/.pseudolife-mcp/test-pg.env``). Needs ``CARGO_TARGET_DIR`` set by the
caller; builds with ``-j 2``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))
import dbstate  # noqa: E402

HOST_PORT = os.environ.get("PSEUDOLIFE_TEST_PG_HOST_PORT", "127.0.0.1:5433")
LOGIN_FILE = Path(os.environ.get("PSEUDOLIFE_TEST_PG_LOGIN_FILE")
                  or Path.home() / ".pseudolife-mcp" / "test-pg.env")
DISPOSABLE = re.compile(r"pl_cf_w2d_cortex_[a-z0-9_]{1,40}")
SEED = "pl_cf_w2d_cortex_seed"
RUST_TEST = "read::cortex_search::tests::differential_cases_from_harness"

BM25 = {"k1": 1.5, "b": 0.75, "weight": 0.3, "top_n": 20, "min_score": 0.1}
MODES = {
    # The shipped defaults: annotate, cortex BM25 off, pins on.
    "default": {"yaml": "", "stale_policy": "annotate", "bm25": None},
    # Cortex BM25 on (memory.bm25.cortex_enabled) and stale facts demoted.
    "bm25_demote": {"yaml": "memory:\n  bm25:\n    cortex_enabled: true\n"
                            "  search:\n    stale_policy: demote\n",
                    "stale_policy": "demote", "bm25": BM25},
    # Stale values quarantined behind the wrapper.
    "quarantine": {"yaml": "memory:\n  search:\n    stale_policy: quarantine\n",
                   "stale_policy": "quarantine", "bm25": None},
}

QUERIES = [
    "where is the payments database hosted",
    "payments-db port",
    "what port does payments db listen on",
    "how do I start the bench server?",
    "the bench server's config",
    "bench server gpu",
    "what languages is the project written in",
    "project license",
    "deploy procedure",
    "is the daemon healthy",
    "daemon deployment status",
    "what is the latest release version",
    "which CI runner do we use",
    "editor theme",
    "when is the team standup",
    "I don't know who maintains this",
    "Don role",
    "payments db replicas",
    "migration rules for payments-db",
    "library pinned version",
    "quantum chromodynamics lattice gauge theory",
    "zeta_identifier_9 lookup",
    "PAYMENTS DB write rule",
    "payments-database analytics access",
    "what is probably true about the cache",
    "which platforms does the release support",
]


# ---- disposable databases ------------------------------------------------------------

def _login() -> tuple[str, str]:
    values = {}
    for line in LOGIN_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    try:
        return values["PSEUDOLIFE_TEST_PG_USER"], values["PSEUDOLIFE_TEST_PG_PASSWORD"]
    except KeyError:
        raise SystemExit(f"{LOGIN_FILE}: no test login; run `pseudolife-mcp test-login create`")


def _check(name: str) -> str:
    if not DISPOSABLE.fullmatch(name):
        raise ValueError(f"refusing non-disposable database name {name!r}")
    return name


def dsn(name: str) -> str:
    user, password = _login()
    host, port = HOST_PORT.rsplit(":", 1)
    return (f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
            f"@{host}:{port}/{_check(name)}")


def _admin():
    user, password = _login()
    host, port = HOST_PORT.rsplit(":", 1)
    return psycopg.connect(host=host, port=int(port), user=user, password=password,
                           dbname="postgres", autocommit=True, connect_timeout=10)


def create(name: str, template: str | None = None) -> str:
    drop(name)
    with _admin() as conn:
        if template is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(_check(name))))
        else:
            conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(_check(name)), sql.Identifier(_check(template))))
    return dsn(name)


def drop(name: str) -> None:
    with _admin() as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(_check(name))))


def existing() -> list[str]:
    with _admin() as conn:
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE 'pl_cf_w2d_cortex_%'")
        return sorted(r[0] for r in rows if DISPOSABLE.fullmatch(r[0]))


# ---- seeding through Python's own write path ------------------------------------------

def _close(svc) -> None:
    try:
        svc.flush()
    finally:
        svc._storage.close()


def _entry_id(conn, text: str) -> int:
    row = conn.execute("SELECT id FROM entries WHERE text = %s ORDER BY id DESC LIMIT 1",
                       (text,)).fetchone()
    assert row is not None, f"entry not stored: {text!r}"
    return int(row[0])


def seed(work: Path) -> tuple[float, list[int | None]]:
    """Seed ``SEED`` and log one retrieval event per query. Returns the seed
    wall-clock origin and the event ids."""
    from pseudolife_memory.memory.cortex import _norm_key
    from pseudolife_memory.service import MemoryService

    data = work / "seed"
    data.mkdir(parents=True, exist_ok=True)
    (data / "config.yaml").write_text("", encoding="utf-8")
    seed_dsn = create(SEED)
    svc = MemoryService(data_dir=str(data), config_path=str(data / "config.yaml"),
                        database_url=seed_dsn)
    t0 = time.time()
    day = 86400.0
    log = []

    def fact(e, a, v, *, days_ago=1.0, **kw):
        out = svc.cortex_write(e, a, v, now=t0 - days_ago * day, **kw)
        log.append((e, a, v, out["action"]))
        return out

    fact("payments-db", "host", "db01.internal.example", support="user", days_ago=5)
    fact("payments-db", "port", "5432", support="user")
    fact("Payments DB", "owner", "platform team", support="agent")
    fact("payments-db", "write rule", "never run migrations during business hours",
         support="user", distortion_tolerance="constraint")
    fact("payments-db", "access rule", "read replicas only for analytics",
         support="user", distortion_tolerance="constraint")
    fact("payments-db", "backup rule", "nightly full backup before any schema change",
         support="user", distortion_tolerance="constraint")
    fact("bench server", "launch rule",
         "always launch through the helper script, never by hand",
         support="user", distortion_tolerance="constraint")
    fact("bench server", "gpu", "one 24GB card", support="user")
    fact("deploy", "procedure", "backup, rollback tag, recreate, health check, clients",
         support="user", days_ago=3)
    fact("daemon", "deployment status", "healthy on release 0.9", support="agent",
         freshness_class="volatile", days_ago=60)
    fact("library", "pinned version", "2.1", support="user", freshness_class="slow",
         days_ago=300)
    fact("release", "latest version", "0.9.0", support="assistant")
    fact("editor", "theme", "solarized dark", support="user", days_ago=10)
    fact("editor", "theme", "gruvbox", support="user", confidence=0.95, days_ago=2)
    fact("CI", "runner", "self-hosted linux box", support="user", days_ago=4)
    fact("CI", "runner", "github hosted", support="agent", days_ago=1)
    fact("project", "license", "Apache-2.0", support="user")
    fact("team", "standup time", "10:00 UTC", support="user")
    fact("Don", "role", "maintainer", support="user")
    fact("cache", "eviction", "least recently used", support="agent", stance="probably")
    fact("zeta_identifier_9", "meaning", "an opaque lookup key", support="user")
    for m, origin in (("Python", "user"), ("Rust", "user"), ("TypeScript", "assistant")):
        out = svc.set_add("project", "languages", m, origin=origin)
        log.append(("project", "languages", m, out["action"]))
    for e, a, m, origin in (("payments-db", "replicas", "replica-a", "user"),
                            ("payments-db", "replicas", "standby replica in eu-west", "user"),
                            ("release", "platforms", "linux", "assistant"),
                            ("release", "platforms", "windows and macos", "assistant")):
        out = svc.set_add(e, a, m, origin=origin)
        log.append((e, a, m, out["action"]))

    # Source entries and traces: the dream's own storage call links a slot
    # to the entry it came from; correcting that entry afterwards records a
    # durable invalidation, which the served fact must flag.
    texts = {
        "host": "The payments database host is db01 in the internal network.",
        "langs": "The project is written in Python and Rust, with a TypeScript console.",
        "deploy": "Deploy with a backup, a rollback tag, a recreate, a health check, then clients.",
    }
    for text in texts.values():
        svc.store(text, source="w2d-seed")
    with psycopg.connect(seed_dsn, autocommit=True) as conn:
        ids = {k: _entry_id(conn, t) for k, t in texts.items()}
    now = time.time()
    st = svc._storage
    st.add_trace(_norm_key("payments-db"), _norm_key("host"), ids["host"], now)
    st.add_trace(_norm_key("project"), _norm_key("languages"), ids["langs"], now)
    st.add_trace(_norm_key("deploy"), _norm_key("procedure"), ids["deploy"], now)
    st.add_trace(_norm_key("payments-db"), _norm_key("port"), ids["host"], now)
    r1 = svc.supersede(entry_id=ids["host"],
                       new_text="The payments database host moved to db02 in the internal network.")
    r2 = svc.supersede(entry_id=ids["langs"],
                       new_text="The project is written in Python and Rust only.")
    log.append(("supersede", str(r1.get("action") or r1.get("ok") or r1)[:60], "", ""))
    log.append(("supersede", str(r2.get("action") or r2.get("ok") or r2)[:60], "", ""))

    events: list[int | None] = []
    for q in QUERIES:
        res = svc.search(q, top_k=5, rerank=False, return_event_id=True)
        events.append(res.get("retrieval_event_id"))
    _close(svc)
    print("seeded:", json.dumps(log, ensure_ascii=False))
    return t0, events


# ---- the two sides --------------------------------------------------------------------

def run_python(mode: str, py_dsn: str, work: Path, frozen: float,
               events: list[int | None]) -> tuple[list[dict], dict, dict]:
    from pseudolife_memory.service import MemoryService

    data = work / f"py_{mode}"
    data.mkdir(parents=True, exist_ok=True)
    (data / "config.yaml").write_text(MODES[mode]["yaml"], encoding="utf-8")
    svc = MemoryService(data_dir=str(data), config_path=str(data / "config.yaml"),
                        database_url=py_dsn)
    svc.cortex_stats()  # finish the service init before the "before" dump
    assert svc.config.memory.search.stale_policy == MODES[mode]["stale_policy"]
    assert bool(svc.config.memory.bm25.cortex_enabled) == (MODES[mode]["bm25"] is not None)
    cc = svc.config.memory.cortex
    assert cc.enabled and cc.search_first and cc.pin_constraints and cc.read_tracking
    before = dbstate.dump(py_dsn)
    cases = []
    real_time = time.time
    time.time = lambda: frozen
    try:
        for q, evt in zip(QUERIES, events):
            emb = svc._embedder.encode_query(q)
            vec = emb.detach().cpu().float().reshape(-1).tolist()
            # The dense ranking before pinning, so coverage can tell a pin
            # that rescued an unranked constraint from one already ranked.
            ranked_slots = sorted({(r.entity, r.attribute) for r, _ in
                                   svc._cortex.search(emb, top_k=5, min_score=cc.guard_min_score)})
            facts = svc.cortex_search(q, top_k=5, min_score=cc.guard_min_score)["entries"]
            if facts and evt is not None:
                svc.attach_served_facts(evt, facts)
            cases.append({"query": q, "vec": vec, "event_id": evt, "top_k": 5,
                          "min_score": cc.guard_min_score, "ranked_slots": ranked_slots,
                          "entries": json.loads(json.dumps(facts))})
    finally:
        time.time = real_time
    svc._storage.close()  # no flush: only what the queries wrote may differ
    after = dbstate.dump(py_dsn)
    return cases, before, after


def run_rust(mode: str, rs_dsn: str, work: Path, frozen: float, cases: list[dict],
             mutant: str | None) -> tuple[list[dict], dict, dict]:
    inp = work / f"rs_{mode}_in.json"
    out = work / f"rs_{mode}_out.json"
    knobs = {"pin_constraints": True, "read_tracking": True, "traces_enabled": True,
             "stale_policy": MODES[mode]["stale_policy"], "bm25": MODES[mode]["bm25"]}
    inp.write_text(json.dumps({
        "now": frozen, "knobs": knobs,
        "cases": [{k: c[k] for k in ("query", "vec", "event_id", "top_k", "min_score")}
                  for c in cases]}), encoding="utf-8")
    before = dbstate.dump(rs_dsn)
    env = dict(os.environ, W2D_CORTEX_DSN=rs_dsn, W2D_CORTEX_IN=str(inp),
               W2D_CORTEX_OUT=str(out))
    cmd = ["cargo", "test", "-j", "2", "-p", "pseudolife-daemon",
           "--manifest-path", str(REPO / "rust" / "Cargo.toml")]
    if mutant:
        cmd += ["--features", "mutants"]
        env["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
    else:
        env.pop("PSEUDOLIFE_DAEMON_MUTANT", None)
    cmd += ["--", "--ignored", "--exact", RUST_TEST]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-40:])
        raise SystemExit(f"rust side failed (exit {proc.returncode}):\n{tail}")
    results = json.loads(out.read_text(encoding="utf-8"))["results"]
    after = dbstate.dump(rs_dsn)
    return results, before, after


# ---- comparison -----------------------------------------------------------------------

def value_diff(a, b, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k not in b:
                out.append(f"{path}/{k}: only in python ({json.dumps(a[k])[:120]})")
            elif k not in a:
                out.append(f"{path}/{k}: only in rust ({json.dumps(b[k])[:120]})")
            else:
                out += value_diff(a[k], b[k], f"{path}/{k}")
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: python has {len(a)} items, rust {len(b)}: "
                    f"{json.dumps(a)[:300]} vs {json.dumps(b)[:300]}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += value_diff(x, y, f"{path}[{i}]")
        return out
    if a != b or type(a) is bool and type(b) is not bool or type(b) is bool and type(a) is not bool:
        return [f"{path}: python {json.dumps(a)[:200]} vs rust {json.dumps(b)[:200]}"]
    return []


def _rowset(state: dict, table: str) -> set[str]:
    t = state["rows"].get(table, {"rows": []})
    return {json.dumps(r, sort_keys=True) for r in t["rows"]}


def state_delta(before: dict, after: dict) -> dict:
    out = {}
    for table in sorted(set(before["rows"]) | set(after["rows"])):
        b, a = _rowset(before, table), _rowset(after, table)
        if a - b or b - a:
            out[table] = {"added": sorted(a - b), "removed": sorted(b - a)}
    return out


def state_diffs(py_before, py_after, rs_before, rs_after) -> tuple[list[str], dict]:
    diffs = []
    dp, dr = state_delta(py_before, py_after), state_delta(rs_before, rs_after)
    for table in sorted(set(dp) | set(dr)):
        p = dp.get(table, {"added": [], "removed": []})
        r = dr.get(table, {"added": [], "removed": []})
        for kind in ("added", "removed"):
            for row in sorted(set(p[kind]) - set(r[kind]))[:5]:
                diffs.append(f"{table}: row {kind} only by python: {row[:300]}")
            for row in sorted(set(r[kind]) - set(p[kind]))[:5]:
                diffs.append(f"{table}: row {kind} only by rust: {row[:300]}")
    for table in ("public.slot_reads", "public.retrieval_events"):
        if py_after["rows"].get(table) != rs_after["rows"].get(table):
            diffs.append(f"{table}: final rows differ")
    return diffs, dp


def coverage(cases: list[dict]) -> dict[str, int]:
    """How many served Python entries exercised each contract branch, so a
    green run cannot hide a seed that stopped reaching one."""
    out = {k: 0 for k in ("set", "pinned", "pinned_unranked", "re_verify", "contested",
                          "assistant", "stale", "warning", "quarantined", "stance",
                          "source_entries", "empty_blocks")}
    for c in cases:
        if not c["entries"]:
            out["empty_blocks"] += 1
        for e in c["entries"]:
            out["set"] += e.get("kind") == "set"
            out["pinned"] += bool(e.get("pinned"))
            out["re_verify"] += bool(e.get("re_verify"))
            out["contested"] += bool(e.get("contested"))
            out["assistant"] += e.get("origin") == "assistant"
            out["stale"] += bool(e.get("stale"))
            out["warning"] += "warning" in e
            out["quarantined"] += "last_known_value" in e
            out["stance"] += "stance" in e
            out["source_entries"] += bool(e.get("source_entries"))
        ranked = {tuple(s) for s in c.get("ranked_slots", [])}
        out["pinned_unranked"] += sum(1 for e in c["entries"] if e.get("pinned")
                                      and (e["entity"], e["attribute"]) not in ranked)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", action="append", choices=sorted(MODES))
    ap.add_argument("--mutant")
    ap.add_argument("--keep", action="store_true", help="keep the databases")
    args = ap.parse_args()
    if not os.environ.get("CARGO_TARGET_DIR"):
        raise SystemExit("set CARGO_TARGET_DIR")
    modes = args.mode or list(MODES)
    work = Path(tempfile.mkdtemp(prefix="w2d_cortex_",
                                 dir=os.environ.get("PSEUDOLIFE_MCP_DATA_DIR")))
    created = [SEED]
    total_cases = total_diffs = total_state = 0
    try:
        t0, events = seed(work)
        frozen = t0 + 7200.0
        for mode in modes:
            py_name, rs_name = f"pl_cf_w2d_cortex_py_{mode}", f"pl_cf_w2d_cortex_rs_{mode}"
            created += [py_name, rs_name]
            py_dsn, rs_dsn = create(py_name, SEED), create(rs_name, SEED)
            cases, py_before, py_after = run_python(mode, py_dsn, work, frozen, events)
            results, rs_before, rs_after = run_rust(mode, rs_dsn, work, frozen, cases,
                                                    args.mutant)
            diffs = []
            served = 0
            for c, r in zip(cases, results):
                served += len(c["entries"])
                for d in value_diff(c["entries"], r["entries"], f"[{c['query']}]"):
                    diffs.append(d)
            sdiffs, delta = state_diffs(py_before, py_after, rs_before, rs_after)
            total_cases += len(cases)
            total_diffs += len(diffs)
            total_state += len(sdiffs)
            written = {t: len(v["added"]) + len(v["removed"]) for t, v in delta.items()}
            print(f"mode {mode}: coverage {json.dumps(coverage(cases))}")
            print(f"mode {mode}: {len(cases)} cases, {served} served facts, "
                  f"{len(diffs)} entry diffs, {len(sdiffs)} state diffs; "
                  f"python wrote {written}")
            for d in (diffs + sdiffs)[:40]:
                print("  DIFF", d)
    finally:
        if not args.keep:
            for name in created:
                drop(name)
    print(f"total: {total_cases} cases, {total_diffs} entry diffs, "
          f"{total_state} state diffs" + (f" (mutant {args.mutant})" if args.mutant else ""))
    return 1 if (total_diffs or total_state) else 0


if __name__ == "__main__":
    raise SystemExit(main())

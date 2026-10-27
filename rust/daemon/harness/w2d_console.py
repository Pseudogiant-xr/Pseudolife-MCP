"""Console read projections differential (CUTOVER B2a): the Python daemon
(oracle) against the Rust daemon over ``GET /api/facts``,
``/api/facts/history``, ``/api/world``, ``/api/lessons``, ``/api/sources``,
``/api/recent``, ``/api/chain``, ``/api/entry``, ``/api/episodes``,
``/api/episodes/summary`` and ``/api/consolidation``.

usage:
  python w2d_console.py live    --rust-bin PATH [--only NAME ...] [--out FILE]
  python w2d_console.py mutants --rust-bin PATH_BUILT_WITH_FEATURE_MUTANTS [--mutants NAME ...]

It runs on ``w2e.py``'s engine, unchanged: each scenario seeds one template
bank through the Python daemon's own write path, clones it for each side,
starts both daemons and sends every operation to both, comparing the
answer by value (with ``w2e``'s declared response normalizers: record clocks
null-or-not, ``age`` one step apart, episode ids renamed ``<ep#i>``) and the
full database state of both banks after every operation, so a read's side
effects (``/api/entry``'s access count and retrieval credit, the query
anchor's access accrual in ``/api/consolidation``) are compared too.

This module adds one operation, ``get``: a Console GET whose ``query``
values may name a row on each side (``@id:<entry text>``, ``@ep:<title>``,
resolved by ``w2e.resolve_args``); its answer is ``{"isError", "status",
"value"}``, so a refusal compares by status as well as body. Nothing here
edits ``w2e.py``: the scenarios, the mutants and the ``get`` dispatch are
registered on the imported module.
"""
from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import w2e  # noqa: E402

# Deliberate breaks (``--features mutants``) the scenarios must catch.
MUTANTS = ["console-facts-no-contender", "console-sources-by-name", "console-chain-limit-clamped",
           "console-episodes-unsorted", "mcp-get-no-db-bump", "mcp-recent-oldest-first",
           "mcp-consolidation-unsorted"]

OPS = ("get",)


def run_get(op: dict, port: int, dsn: str) -> dict:
    query = {}
    for k, v in op.get("query", {}).items():
        if isinstance(v, str) and v.startswith("@"):
            v = w2e.resolve_args({"v": v}, dsn)["v"]
        query[k] = str(v)
    path = op["path"] + ("?" + urllib.parse.urlencode(query) if query else "")
    r = w2e.w1a.call(port, "GET", path, [w2e.AUTH], None)
    value = r.get("json", r.get("bytes"))
    return {"isError": r["status"] != 200, "status": r["status"], "value": value}


_base_run_op = w2e.run_op


def run_op(op: dict, port: int, mcp, dsn: str) -> dict | None:
    if op["op"] in OPS:
        return run_get(op, port, dsn)
    return _base_run_op(op, port, mcp, dsn)


def get(path: str, **query) -> dict:
    return {"op": "get", "path": path, "query": query}


store, sess, fact, tool = w2e.store, w2e.sess, w2e.fact, w2e.tool
set_add, set_remove, world_set = w2e.set_add, w2e.set_remove, w2e.world_set
session_title, ep_start, ep_end, sql = w2e.session_title, w2e.ep_start, w2e.ep_end, w2e.sql
lesson_row, trace_sql, supersede = w2e.lesson_row, w2e.trace_sql, w2e.supersede
LESSON_TEXTS = w2e.LESSON_TEXTS


def _close(key: str) -> dict:
    return {"op": "http", "method": "POST", "path": "/api/hook/session-end",
            "body": {"session_id": key}}


READ_SEED = [
    # Two sessions: one closed (titled), one left open with a sub-episode.
    store("Alpha deploys go through the blue-green pipeline.", [sess("cr-a")],
          source="ops", tags=["deploy", "Infra"]),
    store("Alpha keeps its runbook in the ops wiki.", [sess("cr-a")], source="notes",
          tags=["docs"]),
    store("The payments database lives on host db-7.", [sess("cr-a")], source="ops",
          tags=["db", "infra"]),
    session_title("Alpha", [sess("cr-a")]),
    _close("cr-a"),
    store("Beta measured the cache hit rate at 91 percent.", [sess("cr-b")], source="eval",
          tags=["cache"]),
    store("Beta measured the cache hit rate at ninety one percent again.", [sess("cr-b")],
          source="eval", tags=["cache"]),
    session_title("Beta", [sess("cr-b")]),
    ep_start("Beta sub", [sess("cr-b")]),
    store("Beta sub-episode note on cache eviction.", [sess("cr-b")], source="eval"),
    # A correction: the old note stays as superseded history.
    store("The staging cluster runs on version 3."),
    supersede("The staging cluster runs on version 4.",
              old_text="The staging cluster runs on version 3."),
    *[store(t, [sess("cr-l")]) for t in LESSON_TEXTS],
    # Canonical facts: a user rewrite (a superseded version), a parked agent
    # contender, a set slot with a removed member, mixed-case sort keys.
    fact("blue-green pipeline", "status", "retired", origin="user", confidence=0.9),
    fact("blue-green pipeline", "status", "decommissioned", origin="user", confidence=0.9),
    fact("Payments DB", "host", "db-7", origin="user", confidence=0.9),
    fact("Payments DB", "host", "db-9"),
    fact("auth tokens", "rotation", "monthly", origin="user", confidence=0.9),
    fact("Zeta service", "owner", "platform team"),
    set_add("Team", "members", "alice"),
    set_add("Team", "members", "bob"),
    set_remove("Team", "members", "alice"),
    tool("memory_alias", entity="auth tokens", alias="bearer tokens"),
    tool("memory_graph_relate", src="blue-green pipeline", relation="uses", dst="Jenkins"),
    trace_sql("Alpha deploys go through the blue-green pipeline.", "blue-green-pipeline",
              "status"),
    trace_sql("The payments database lives on host db-7.", "payments-db", "host"),
    # Correcting a traced source after its fact was confirmed flags the fact
    # (evidence supersession, read from memory_trace_invalidations).
    store("Auth tokens rotate every month per the security runbook."),
    trace_sql("Auth tokens rotate every month per the security runbook.", "auth-tokens",
              "rotation"),
    supersede("Auth tokens rotate every quarter per the security runbook.",
              old_text="Auth tokens rotate every month per the security runbook."),
    # World facts.
    world_set("PostgreSQL", "latest major", "18", source_url="https://www.postgresql.org/",
              source_quote="PostgreSQL 18 released", freshness_class="slow"),
    world_set("Rust", "edition", "2024", source_url="https://doc.rust-lang.org/edition-guide/",
              source_quote="Rust 2024 edition", freshness_class="evergreen"),
    # Lessons (only the dream writes them: rows after the seed daemon stops).
    lesson_row("deploy engine to host", "approach", "deploy-engine-to-host", "approach",
               "Deploy through the blue-green pipeline, never by hand.",
               "blue-green pipeline", "+", "success", "current", LESSON_TEXTS[0]),
    lesson_row("run integration tests", "pitfall", "run-integration-tests", "pitfall",
               "The suite needs the bench Postgres on port 5433.", None, "-",
               "failure", "current", LESSON_TEXTS[1]),
    lesson_row("Rotate credentials", "tool-choice", "rotate-credentials", "tool-choice",
               "Rotate bearer tokens with the CLI.", "bearer tokens", "+", "success",
               "current", LESSON_TEXTS[2]),
    lesson_row("deploy engine to host", "approach", "deploy-engine-to-host", "approach",
               "Deploy by hand over ssh.", "blue-green pipeline", "+", "success",
               "superseded", LESSON_TEXTS[0]),
]


def scenarios() -> dict[str, dict]:
    return {
        "console-reads": {
            "config": None,
            "seed": READ_SEED,
            "ops": [
                # /api/facts: the cap and its coercions (default, a cut, a
                # negative and a non-numeric limit).
                get("/api/facts"),
                get("/api/facts", limit=2),
                get("/api/facts", limit=-1),
                get("/api/facts", limit="many"),
                # /api/facts/history: a scalar slot's versions, a set slot's
                # events, an unknown slot, and the absent parameters (None).
                get("/api/facts/history", entity="blue-green pipeline", attribute="status"),
                get("/api/facts/history", entity="team", attribute="MEMBERS"),
                get("/api/facts/history", entity="nobody", attribute="nothing"),
                get("/api/facts/history", entity="Payments DB"),
                get("/api/facts/history"),
                # /api/world and /api/lessons.
                get("/api/world"),
                get("/api/world", limit=1),
                get("/api/lessons"),
                get("/api/lessons", limit=1),
                get("/api/lessons", limit=0),
                # /api/sources.
                get("/api/sources"),
                # /api/recent: default n, a cut, each filter, tags normalized,
                # an unknown episode, a negative n.
                get("/api/recent"),
                get("/api/recent", n=2),
                get("/api/recent", source="ops,eval"),
                get("/api/recent", tag=" INFRA , cache"),
                get("/api/recent", episode="@ep:Alpha"),
                get("/api/recent", episode="no-such-episode"),
                get("/api/recent", source="ops", tag="db"),
                get("/api/recent", n=-3),
                get("/api/recent", tag=" , "),
                # /api/chain: an entity with facts, edges and a lesson; the
                # limit at 1, at 0 (keeps one event), negative (the node's
                # SQL refuses it), an alias, an unknown and a blank entity.
                get("/api/chain", entity="blue-green pipeline"),
                get("/api/chain", entity="blue-green pipeline", limit=1),
                get("/api/chain", entity="blue-green pipeline", limit=0),
                get("/api/chain", entity="blue-green pipeline", limit=-2),
                get("/api/chain", entity="bearer tokens"),
                get("/api/chain", entity="nobody here"),
                get("/api/chain"),
                # /api/entry: a live entry (its access count and retrieval
                # credit follow), the same again, a superseded one, an
                # unknown id, a non-numeric and an absent id.
                get("/api/entry", id="@id:The payments database lives on host db-7."),
                get("/api/entry", id="@id:The payments database lives on host db-7."),
                get("/api/entry", id="@id:The staging cluster runs on version 3."),
                get("/api/entry", id=987654321),
                get("/api/entry", id="seven"),
                get("/api/entry"),
                # /api/episodes: newest first, the cap, a negative cap (all).
                get("/api/episodes"),
                get("/api/episodes", limit=1),
                get("/api/episodes", limit=-1),
                get("/api/episodes", limit=0),
                # /api/episodes/summary: closed, open sub-episode, unknown,
                # absent id.
                get("/api/episodes/summary", id="@ep:Alpha"),
                get("/api/episodes/summary", id="@ep:Beta"),
                get("/api/episodes/summary", id="no-such-episode"),
                get("/api/episodes/summary"),
                # /api/consolidation: no anchor, a query anchor (its access
                # accrual is in the bank comparison), an episode anchor with
                # filters, the cap's edges (0 keeps none, negative drops from
                # the end in episode mode), a low cohesion floor.
                get("/api/consolidation"),
                get("/api/consolidation", q="cache hit rate"),
                get("/api/consolidation", q="cache hit rate", min_cohesion=0.2),
                get("/api/consolidation", episode="@ep:Beta", min_cohesion=0.3),
                get("/api/consolidation", episode="@ep:Beta", min_cohesion=0.3, source="eval",
                    tag="cache"),
                get("/api/consolidation", episode="@ep:Beta", min_cohesion=0.3, top_k=0),
                get("/api/consolidation", episode="@ep:Beta", min_cohesion=0.3, top_k=-1),
                get("/api/consolidation", q="cache hit rate", top_k=0, min_cohesion=0.2),
                get("/api/consolidation", q="cache hit rate", top_k=-1),
                get("/api/consolidation", q="cache hit rate", min_cohesion="x"),
            ],
        },
        # Stale rows under the "demote" policy: sorted last, with the
        # warning, in the cortex and world dumps. The slots are aged after
        # the seed daemon stops, so both daemons hydrate them stale.
        "console-reads-demote": {
            "config": "memory:\n  search:\n    stale_policy: demote\n",
            "seed": [
                fact("alpha host", "ip", "10.0.0.4", origin="user", confidence=0.9,
                     freshness_class="volatile"),
                fact("beta host", "ip", "10.0.0.5", origin="user", confidence=0.9),
                world_set("Alpha release", "version", "1.2",
                          source_url="https://example.com/alpha",
                          source_quote="Alpha 1.2", freshness_class="volatile"),
                world_set("Beta release", "version", "2.0",
                          source_url="https://example.com/beta",
                          source_quote="Beta 2.0", freshness_class="evergreen"),
                sql("UPDATE facts SET asserted_at = 1000, last_confirmed = 1000, "
                    "tx_time = 1000 WHERE entity_norm = 'alpha-host'", after_stop=True),
                sql("UPDATE world_facts SET asserted_at = 1000, last_confirmed = 1000, "
                    "retrieved_at = 1000 WHERE entity_norm = 'alpha-release'", after_stop=True),
            ],
            "ops": [
                get("/api/facts"),
                get("/api/world"),
            ],
        },
    }


SCENARIOS = scenarios()


def install() -> None:
    """Register the scenarios, the mutants and the ``get`` dispatch on ``w2e``."""
    w2e.SCENARIOS.update(SCENARIOS)
    for m in MUTANTS:
        if m not in w2e.MUTANTS:
            w2e.MUTANTS.append(m)
    w2e.run_op = run_op


def main() -> int:
    install()
    argv = sys.argv[1:]
    if "--only" not in argv:
        argv += ["--only", *SCENARIOS]
    if argv and argv[0] == "mutants" and "--mutants" not in argv:
        argv += ["--mutants", *MUTANTS]
    sys.argv = [sys.argv[0], *argv]
    return w2e.main()


if __name__ == "__main__":
    raise SystemExit(main())

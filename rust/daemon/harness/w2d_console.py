"""Console read projections differential (CUTOVER B2a): the Python daemon
(oracle) against the Rust daemon over ``GET /api/facts``,
``/api/facts/history``, ``/api/world``, ``/api/lessons``, ``/api/sources``,
``/api/recent``, ``/api/chain``, ``/api/entry``, ``/api/episodes``,
``/api/episodes/summary``, ``/api/consolidation``, ``/api/overview``,
``/api/agents``' default view, the maintainer reads ``/api/maintainer``,
``/sent`` and ``/inbox`` (the maintainer's board state is seeded as rows)
and ``GET /api/config``.

usage:
  python w2d_console.py live    --rust-bin PATH [--only NAME ...] [--out FILE]
  python w2d_console.py mutants --rust-bin PATH_BUILT_WITH_FEATURE_MUTANTS [--mutants NAME ...]

It runs on ``w2e.py``'s engine, unchanged: each scenario seeds one template
bank through the Python daemon's own write path, clones it for each side,
starts both daemons and sends every operation to both, comparing the
answer by value (with ``w2e``'s declared response normalizers: record clocks
null-or-not, ``age`` one step apart, episode ids renamed ``<ep#i>``) and the
full database state of both banks after every operation, so a read's side
effects (``/api/entry``'s access count, and its retrieval credit to the
event a session's search logged; the query anchor's access accrual in
``/api/consolidation``) are compared too.

This module adds one operation, ``get``: a Console GET whose ``query``
values may name a row on each side (``@id:<entry text>``, ``@ep:<title>``,
resolved by ``w2e.resolve_args``) and whose ``headers`` follow the bearer
(an ``X-PL-Session``); its answer is ``{"isError", "status",
"value"}``, so a refusal compares by status as well as body. Nothing here
edits ``w2e.py``: the scenarios, the mutants and the ``get`` dispatch are
registered on the imported module.

It adds a second, ``write_config``, on each side's data directory
(``w2e.HOME_OPS``): it replaces config.yaml under the running daemons, so
``/api/config`` serves the values each booted with beside the file's
``saved`` ones. ``config_path`` names each side's own home, so it compares
as ``<home>/data/config.yaml``.
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
           "console-episodes-unsorted", "mcp-get-no-db-bump", "mcp-get-no-use-credit",
           "mcp-recent-oldest-first", "mcp-consolidation-unsorted",
           "console-overview-contested-rows", "console-agents-no-limit-refusal",
           "console-agents-ignore-limit", "maintainer-gate-open", "maintainer-config-unchecked",
           "maintainer-available-any-key", "maintainer-roles-any-grant",
           "config-read-typed-values", "config-read-no-saved"]

OPS = ("get",)


def run_get(op: dict, port: int, dsn: str) -> dict:
    query = {}
    for k, v in op.get("query", {}).items():
        if isinstance(v, str) and v.startswith("@"):
            v = w2e.resolve_args({"v": v}, dsn)["v"]
        query[k] = str(v)
    path = op["path"] + ("?" + urllib.parse.urlencode(query) if query else "")
    headers = [w2e.AUTH] + [tuple(h) for h in op.get("headers", [])]
    r = w2e.w1a.call(port, "GET", path, headers, None)
    value = r.get("json", r.get("bytes"))
    return {"isError": r["status"] != 200, "status": r["status"], "value": value}


_base_run_op = w2e.run_op


def run_op(op: dict, port: int, mcp, dsn: str) -> dict | None:
    if op["op"] in OPS:
        return run_get(op, port, dsn)
    return _base_run_op(op, port, mcp, dsn)


# ``/api/overview``'s ``dream.idle_seconds`` is ``now - (newest entry's ts)``
# on each side, read moments apart: like ``w2e.idle_diffs`` for
# ``memory_dream``, both must be numbers >= 0 within this bound (seconds;
# a step's wall time plus slack), and the rest of the answer is exact.
IDLE_BOUND_S = 10.0

_base_compare = w2e.compare_response


def compare_response(py, rs, tol, stats) -> list[str]:
    out = []

    def dream(r):
        v = r.get("value") if isinstance(r, dict) else None
        d = v.get("dream") if isinstance(v, dict) else None
        return d if isinstance(d, dict) and "idle_seconds" in d else None

    a, b = dream(py), dream(rs)
    if a is not None and b is not None:
        x, y = a["idle_seconds"], b["idle_seconds"]
        numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0
                      for v in (x, y))
        if not numbers or abs(x - y) > IDLE_BOUND_S:
            out.append(f"dream.idle_seconds: python {x!r} vs rust {y!r} "
                       f"(not both >= 0 within {IDLE_BOUND_S}s)")
        py = {**py, "value": {**py["value"], "dream": {**a, "idle_seconds": "<idle>"}}}
        rs = {**rs, "value": {**rs["value"], "dream": {**b, "idle_seconds": "<idle>"}}}
    py, rs, homes = config_homes(py), config_homes(rs), []
    for r in (py, rs):
        v = r.get("value") if isinstance(r, dict) else None
        if isinstance(v, dict) and "config_path" in v:
            homes.append(v["config_path"])
    if homes and not all(str(h) == "<home>/data/config.yaml" for h in homes):
        out.append(f"config_path: python/rust {homes!r} not each side's data/config.yaml")
    return out + _base_compare(py, rs, tol, stats)


def config_homes(r):
    """``/api/config``'s ``config_path`` with the side's home as ``<home>``."""
    v = r.get("value") if isinstance(r, dict) else None
    if not (isinstance(v, dict) and isinstance(v.get("config_path"), str)):
        return r
    path = Path(v["config_path"])
    home = path.parent.parent
    shown = (f"<home>/{path.parent.name}/{path.name}" if path.parent.name == "data"
             and home.name else v["config_path"])
    return {**r, "value": {**v, "config_path": shown}}


_base_home_op = w2e.home_op


def home_op(op: dict, home: Path) -> dict:
    if op["op"] == "write_config":
        (home / "data" / "config.yaml").write_text(op["text"], encoding="utf-8")
        return {"isError": False, "value": None}
    return _base_home_op(op, home)


def write_config(text: str) -> dict:
    return {"op": "write_config", "text": text}


def get(path: str, headers=(), **query) -> dict:
    return {"op": "get", "path": path, "query": query, "headers": list(headers)}


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
    # An episode whose first seed forms the smaller cluster, so the
    # clusters' cohesion x size order differs from their seed order.
    store("The office cat sleeps on the server rack every afternoon.", [sess("cr-g")]),
    store("Every afternoon the office cat naps on top of the server rack.", [sess("cr-g")]),
    store("The deploy freeze starts on Friday at noon.", [sess("cr-g")]),
    store("No deploys after Friday noon: the freeze begins then.", [sess("cr-g")]),
    store("The Friday noon deploy freeze blocks all releases.", [sess("cr-g")]),
    session_title("Gamma", [sess("cr-g")]),
    _close("cr-g"),
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
    # A contested set slot that keeps two live members: its contender is
    # parked against the slot, so both member rows carry the flag and the
    # overview counts one contested slot, not two rows. No tool parks one
    # on a set slot (only the assistant-origin guard does, which the set
    # tools do not expose), so the contender is a row copied from a member,
    # hydrated by both daemons.
    set_add("Infra", "hosts", "alpha"),
    set_add("Infra", "hosts", "3 spare nodes"),
    sql("CREATE TEMP TABLE parked AS SELECT * FROM facts WHERE entity_norm = 'infra' "
        "AND value = 'alpha'; UPDATE parked SET id = nextval(pg_get_serial_sequence("
        "'facts', 'id')), value = 'gamma', value_norm = NULL, kind = 'scalar', "
        "status = 'contested'; INSERT INTO facts SELECT * FROM parked", after_stop=True),
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
                get("/api/facts", limit="100000000000000000000"),
                get("/api/recent", n="-100000000000000000000"),
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
                # A session's search served it: a dereference under that
                # session credits the event (retrieval_uses), another
                # session's does not.
                w2e.search("which host runs the payments database", [sess("cr-x")], top_k=3),
                get("/api/entry", [sess("cr-y")],
                    id="@id:The payments database lives on host db-7."),
                get("/api/entry", [sess("cr-x")],
                    id="@id:The payments database lives on host db-7."),
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
                get("/api/consolidation", episode="@ep:Gamma"),
                get("/api/consolidation", episode="@ep:Gamma", min_cohesion=0.5),
                get("/api/consolidation", episode="@ep:Beta", min_cohesion=0.3, top_k=0),
                get("/api/consolidation", episode="@ep:Beta", min_cohesion=0.3, top_k=-1),
                get("/api/consolidation", q="cache hit rate", top_k=0, min_cohesion=0.2),
                get("/api/consolidation", q="cache hit rate", top_k=-1),
                get("/api/consolidation", q="cache hit rate", min_cohesion="x"),
                # /api/overview: every layer's count, the stats, dream and
                # loop blocks in one answer.
                get("/api/overview"),
                # /api/agents (default view): open sessions other than the
                # caller's, the limit below the configured cap, a limit
                # below 1 (the service's ValueError, a 400), a non-numeric
                # limit (the default 5).
                get("/api/agents"),
                get("/api/agents", [sess("cr-b")]),
                get("/api/agents", limit=1),
                get("/api/agents", limit=0),
                get("/api/agents", limit=-2),
                get("/api/agents", limit="x"),
                get("/api/agents", view="other"),
            ],
        },
        # With the board off, awareness answers its disabled shape before
        # the limit is checked.
        "console-agents-off": {
            "config": "coordination:\n  enabled: false\n",
            "seed": [store("An open session's note.", [sess("off-a")])],
            "ops": [
                get("/api/agents"),
                get("/api/agents", limit=0),
                get("/api/overview"),
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


MAINTAINER_CONFIG = ("coordination:\n  maintainer:\n    rp_id: console.example.com\n"
                     "    origin: https://console.example.com:8443\n")
NOW = "extract(epoch from now())"


def _agent(agent_id: str, principal: str, label: str, **cols) -> dict:
    names = ["agent_id", "principal", "label", "created_at", "last_activity", *cols]
    values = [f"'{agent_id}'", f"'{principal}'", f"'{label}'", NOW, NOW,
              *[str(v) for v in cols.values()]]
    return sql(f"INSERT INTO coordination_agents ({', '.join(names)}) "
               f"VALUES ({', '.join(values)})")


def _message(mid: str, sender: str, recipient: str, sender_principal: str, text: str,
             at: float, seq: int, **cols) -> dict:
    names = ["message_id", "sender_agent_id", "recipient_agent_id", "sender_principal", "text",
             "request_id", "fingerprint", "recipient_sequence", "created_at", "expires_at", *cols]
    values = [f"'{mid}'", f"'{sender}'", f"'{recipient}'", f"'{sender_principal}'", f"'{text}'",
              f"'req-{mid}'", f"'fp-{mid}'", str(seq), f"{NOW} - {at}", f"{NOW} + 86400",
              *[str(v) for v in cols.values()]]
    return sql(f"INSERT INTO coordination_messages ({', '.join(names)}) "
               f"VALUES ({', '.join(values)})")


def _event(event: str, actor: str, payload: str, *, principal: str = "", agent_id: str = "",
           ago: float = 0) -> dict:
    return sql("INSERT INTO coordination_events (seq, event, actor, principal, agent_id, payload, "
               "created_at, prev_hash, hash) SELECT COALESCE(MAX(seq), 0) + 1, "
               f"'{event}', '{actor}', '{principal}', '{agent_id}', '{payload}', {NOW} - {ago}, "
               "'seed', 'seed' FROM coordination_events")


def _lease(name: str, holder: str, fence: int) -> dict:
    return sql("INSERT INTO coordination_leases (name, holder_agent_id, holder_principal, fence, "
               f"acquired_at, expires_at) VALUES ('{name}', '{holder}', 'default', {fence}, "
               f"{NOW}, {NOW} + 3600)")


def _passkey(cid: str, state: str, active_from: str, created: int, label: str) -> dict:
    return sql("INSERT INTO maintainer_passkeys (credential_id, public_key, alg, label, "
               f"enrolled_by, state, active_from, created_at) VALUES ('{cid}', '\\x00', -7, "
               f"'{label}', 'bootstrap', '{state}', {active_from}, {created})")


# The maintainer's board state, as rows (the reads verify no audit chain
# and hold no resident copy): passkeys in each state, the maintainer's
# address and two sessions, its sent mail and the replies, a granted and an
# unrecorded delegate hold, a coordinator, and key-change records (one
# with a payload that is not an object, which the listing skips).
MAINTAINER_SEED = [
    _passkey("cred-active-0001", "active", "0", 100, "laptop"),
    _passkey("cred-pending-0002", "pending", "NULL", 200, "phone"),
    _passkey("cred-revoked-0003", "revoked", "0", 300, "old key"),
    _agent("maint-0001", "maintainer", "maintainer", status="'maintainer'"),
    _agent("agent-alpha-0001", "default", "alpha session",
           attachment_id="'att-1'", lease_until=f"{NOW} + 600", wake_enabled="true"),
    _agent("agent-beta-0002", "default", "beta session"),
    _message("m-sent-1", "maint-0001", "agent-alpha-0001", "maintainer", "Please rebase.", 30, 1,
             origin="'maintainer'", maintainer_proof="'{\"label\": \"laptop\"}'::jsonb",
             wake="'{\"decision\": \"rung\"}'::jsonb"),
    _message("m-sent-2", "maint-0001", "agent-beta-0002", "maintainer", "Hold the deploy.", 20, 1,
             origin="'maintainer'", repudiated_at=f"{NOW} - 5"),
    _message("m-reply-1", "agent-alpha-0001", "maint-0001", "default", "Rebased.", 10, 1,
             reply_to="'m-sent-1'"),
    _message("m-reply-2", "agent-beta-0002", "maint-0001", "default", "Holding.", 5, 2,
             acknowledged_at=f"{NOW} - 1"),
    _lease("delegate:Pseudolife-MCP", "agent-alpha-0001", 3),
    _event("lease_delegate", "operator", '{"name": "delegate:Pseudolife-MCP", "fence": 3}',
           agent_id="agent-alpha-0001"),
    _lease("delegate:Other", "agent-beta-0002", 1),
    _lease("coordinator:Pseudolife-MCP", "agent-beta-0002", 2),
    _event("maintainer_key", "daemon", '{"change": "enrolled", "credential_id": '
           '"cred-active-0001", "label": "laptop", "by": "bootstrap", "path": "console"}',
           principal="default", ago=50),
    _event("maintainer_key", "operator", '[1, 2]', ago=40),
    _event("maintainer_key", "operator", '{"change": "revoked", "credential_id": '
           '"cred-revoked-0003", "by": "host", "path": "host", "revoked": "cred-revoked-0003"}',
           ago=30),
]

MAINTAINER_GETS = [
    get("/api/maintainer"),
    get("/api/maintainer/sent"),
    get("/api/maintainer/sent", limit=1),
    get("/api/maintainer/sent", limit=0),
    get("/api/maintainer/sent", limit="x"),
    get("/api/maintainer/inbox"),
    get("/api/maintainer/inbox", limit=999),
]


def maintainer_scenarios() -> dict[str, dict]:
    return {
        # The near-miss legitimate caller: an admitted bearer on a board
        # with a usable passkey origin.
        "console-maintainer": {
            "config": MAINTAINER_CONFIG,
            "seed": [store("A note before the maintainer reads."), *MAINTAINER_SEED],
            "ops": [
                *MAINTAINER_GETS,
                # A key whose quarantine has not passed signs nothing. One
                # fixed future stamp (2100-01-01): this op runs on each bank
                # separately, so a clock read here would differ per side.
                sql("UPDATE maintainer_passkeys SET active_from = 4102444800 "
                    "WHERE credential_id = 'cred-active-0001'"),
                get("/api/maintainer"),
            ],
        },
        # Refused: no maintainer origin configured (409 with the rule).
        "console-maintainer-unset": {
            "config": None,
            "seed": [store("A note.")],
            "ops": [get("/api/maintainer"), get("/api/maintainer/sent")],
        },
        "console-maintainer-bad-origin": {
            "config": ("coordination:\n  maintainer:\n    rp_id: console.example.com\n"
                       "    origin: https://console.example.com:443\n"),
            "seed": [store("A note.")],
            "ops": [get("/api/maintainer/inbox")],
        },
        # Refused: the bearer's principal is not admitted to the board.
        "console-maintainer-not-admitted": {
            "config": MAINTAINER_CONFIG.replace(
                "coordination:\n", "coordination:\n  allowed_principals: [someone-else]\n"),
            "seed": [store("A note.")],
            "ops": [get("/api/maintainer"), get("/api/maintainer/inbox")],
        },
        # Refused: the board is off.
        "console-maintainer-board-off": {
            "config": MAINTAINER_CONFIG.replace("coordination:\n",
                                                "coordination:\n  enabled: false\n"),
            "seed": [store("A note.")],
            "ops": [get("/api/maintainer")],
        },
    }


def _knob_golden() -> dict:
    import json
    return json.loads((HERE / "goldens" / "config-knobs.json").read_text(encoding="utf-8"))


def config_scenarios() -> dict[str, dict]:
    # The fixtures the Rust unit tests check against Python's read_config
    # (``gen_config_knobs.py``), here through both running daemons.
    golden = _knob_golden()
    ops = [get("/api/config")]
    for case in golden["read"]:
        ops += [write_config(case["saved"]), get("/api/config")]
    return {
        # Boot values served as YAML typed them, defaults where unset; the
        # restart knobs' ``saved`` values, absent when equal or not
        # coercible; a 500 when a saved int knob is infinite.
        "console-config": {
            "config": golden["boot"],
            "seed": [store("A note before the config reads.")],
            "ops": ops,
        },
        # No config.yaml at all: every knob at its default.
        "console-config-defaults": {
            "config": None,
            "seed": [store("A note.")],
            "ops": [get("/api/config")],
        },
    }


SCENARIOS = {**scenarios(), **maintainer_scenarios(), **config_scenarios()}


def install() -> None:
    """Register the scenarios, the mutants and the ``get`` dispatch on ``w2e``."""
    w2e.SCENARIOS.update(SCENARIOS)
    for m in MUTANTS:
        if m not in w2e.MUTANTS:
            w2e.MUTANTS.append(m)
    w2e.run_op = run_op
    w2e.compare_response = compare_response
    w2e.home_op = home_op
    if "write_config" not in w2e.HOME_OPS:
        w2e.HOME_OPS = (*w2e.HOME_OPS, "write_config")


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

"""W2-D live differential harness for GET /api/search and the MCP
memory_search tool: the Python daemon (oracle) against the Rust daemon,
scenario by scenario, each on its own copy of one seeded disposable bank.
MCP cases compare each daemon's tools/call structured content (an error
result as status 422) under the same rules as the REST bodies.

usage:
  python w2d_search.py live    --rust-bin PATH [--only NAME ...] [--out FILE] [--record]
  python w2d_search.py golden  --rust-bin PATH [--only NAME ...] [--out FILE]
  python w2d_search.py mutants --rust-bin PATH_BUILT_WITH_FEATURE_MUTANTS [--only NAME ...]

Per case it compares the status, the content type and the body by value
(exact, with the declared rules below). After every scenario it dumps both
banks and diffs every table; ``retrieval_events`` rows are compared field by
field with the float rule. Exit status 0 only when nothing differs.

Declared rules, never applied by hand:
* SCORE_TOL: the float leaves named in FLOAT_KEYS (scores and fusion
  inputs) may differ by 2e-4 (torch and ONNX embeddings differ in the last
  bits; spec "Free"). Every other value, and every value's JSON type
  (bool, int, float, string), must match exactly.
* Tie order: two entries may swap places only when their scores are within
  SCORE_TOL of each other (spec "Free": tie order), and never in a list that
  is ordered by stream position (contiguity entries or a fired timeline,
  including dense-only timeline results).
  A swap renumbers only the swapped rows' ``rank`` in the event.
* Coverage: each scenario names the channels it must exercise (REQUIRE); a
  run where the oracle did not exercise them fails.
* ``retrieval_events.created_at`` and other wall-clock columns: null-or-not.
* ``age`` (``_relative_time`` of the wall clock): equal, or the same unit
  one step apart, since the two daemons answer moments apart.

Environment: ORT_DYLIB_PATH, PSEUDOLIFE_DAEMON_ONNX_DIR (Qwen3 export) and
PSEUDOLIFE_DAEMON_RERANK_DIR (cross-encoder export) for the Rust side;
PSEUDOLIFE_TEST_PG_HOST_PORT / the test login for the bank.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daemons  # noqa: E402
import dbstate  # noqa: E402
os.environ["PL_HARNESS_SLICE"] = "w2d"
import pgdisposable as pg  # noqa: E402
from run import DB_DECLARED, bearer, call, settled, wait_settled  # noqa: E402,F401

REPO = HERE.parents[2]
GOLDENS = HERE / "goldens" / "w2d"
SCORE_TOL = 2e-4
FLOAT_KEYS = {"score", "dense", "ce", "bm25", "slot", "recency", "margin"}
AGE_RE = re.compile(r"(\d+) (\w+?)s? ago")
TOKEN = "tok-w2d-search-0001"
# Python-only init writes owned by other slices (W1-A's list, minus the
# retrieval event this slice now writes).
DECLARED = [d for d in DB_DECLARED if d[1] not in ("retrieval_events", "retrieval_events_id_seq")]
CLOCK_COLUMNS = {
    ("retrieval_events", "created_at"): "clock: wall clock per event (storage/postgres.py:2160)",
    ("relations", "created_at"): "clock: wall clock per row (storage/postgres.py:765)",
    ("slot_reads", "last_read_at"): "clock: wall clock per read",
    ("slot_reads", "first_read_at"): "clock: wall clock per read",
}
# Each deliberate break (rust/daemon/src/mutants.rs, `--features mutants`)
# and the scenario that must catch it.
MUTANTS = {"search-skip-event": "log-off-control", "search-no-access-bump": "default",
           "search-timeline-unsorted": "timeline", "search-chronicle-limit": "default",
           "search-no-contiguity": "contiguity", "search-rrf-off": "rrf-pool",
           "search-no-cortex": "default", "search-rerank-unfused": "margin-gate",
           "search-neighbours-reversed": "contiguity", "search-recency-off": "continuum",
           "search-cortex-stripped-q": "default", "w2d-cortex-skip-slot-reads": "default",
           "mcp-search-no-dedup": "mcp-search", "mcp-search-no-correct-with": "mcp-search",
           "search-no-subtree": "mcp-search",
           "search-no-transport-session": "mcp-transport-session"}
pg.DISPOSABLE_NAME = re.compile(r"pl_cf_w2d_[a-z0-9_]{1,40}")


def q(**kw) -> str:
    return "/api/search?" + urllib.parse.urlencode(kw)


QUERIES = json.loads((HERE / "queries.json").read_text(encoding="utf-8"))
if isinstance(QUERIES, dict):
    QUERIES = QUERIES.get("queries", [])
CRAFTED = [
    "when did we deploy the release",
    "how many times did we deploy",
    "what happened on 2026-08-12",
    "process_chunk_v2 KeyError",
    "PLX-4471",
    "do I have a cat",
    "what os does the homelab box run",
    "which port does the daemon listen on",
    "what services run on the homelab box",
    "where is the extractor endpoint",
    "first we backed up the bank then what",
    "staging database host",
]


def default_cases() -> list[dict]:
    auth = [bearer(TOKEN)]
    out = []
    for i, text in enumerate(QUERIES[:20]):
        out.append(case(f"corpus {i}", q(q=text, top_k=8), auth))
    for i, text in enumerate(CRAFTED):
        out.append(case(f"crafted {i}", q(q=text, top_k=8), auth))
    out += [
        case("aggregation across all chronicle events",
             q(q="how many times did release bank daemon happen", top_k=8), auth),
        case("repeat crafted 5 (access counts)", q(q=CRAFTED[5], top_k=8), auth),
        case("top_k blank", q(q="deploy the daemon", top_k=""), auth),
        case("top_k 0", q(q="deploy the daemon", top_k=0), auth),
        case("top_k 3", q(q="deploy the daemon", top_k=3), auth),
        case("source filter", q(q="deploy", source="conversation"), auth),
        case("tag filter", q(q="deploy", tag="DEPLOY, bug"), auth),
        case("band flat", q(q="deploy", band="flat"), auth),
        case("unknown band", q(q="deploy", band="working"), auth),
        case("blank q", q(q="   "), auth),
        case("min_score 0.5", q(q="deploy the daemon", min_score="0.5"), auth),
        case("min_score 0.95", q(q="bearer token", min_score="0.95"), auth),
        case("bm25 off", q(q="PLX-4471 writer lease", bm25="false"), auth),
        case("bm25 auto", q(q="PLX-4471 writer lease", bm25="auto"), auth),
        case("recency off", q(q="deploy the daemon", disable_recency_boost="true"), auth),
        case("rerank on", q(q="how do I back up the bank", rerank="1"), auth),
        case("rerank on, top_k 30 (over budget)", q(q="how do I back up the bank", rerank="1", top_k=30), auth),
        case("session header", q(q="deploy the daemon"), auth + [("X-PL-Session", "sess-w2d-1")]),
        case("open-episode session header", q(q="failed deploy incident"),
             auth + [("X-PL-Session", "sess-w2d-seed")]),
        case("slot channel: jacque top_k 1", q(q="is jacque female", top_k=1), auth),
        case("slot channel: staging migration top_k 1", q(q="staging migration", top_k=1), auth),
        case("slot channel: superseded endpoint top_k 1", q(q="extractor endpoint old gpu box", top_k=1), auth),
        # Review finding (Codex, 2026-10-09): the cortex block encodes the raw
        # `q`; only the entry search strips it.
        case("padded query (cortex sees the raw q)", q(q="   what os does the homelab box run  "), auth),
        case("slot channel under explicit floor", q(q="is jacque female", top_k=1, min_score="0.7"), auth),
        case("two session headers", q(q="bearer token"),
             auth + [("X-PL-Session", "sess-a"), ("X-PL-Session", "sess-b")]),
        # Review finding (Opus, 2026-10-09): a client that hangs up must not
        # cut the search's writes short (Python finishes in its executor).
        dict(case("client disconnects mid-search", q(q="what services run on the homelab box"), auth),
             disconnect=True),
        case("after the disconnect", q(q="what os does the homelab box run"), auth),
    ]
    return out


def case(name, path, headers=(), declared=None):
    return {"name": name, "method": "GET", "path": path, "headers": list(headers), "declared": declared}


def mcp_case(name, args, headers=()):
    """A memory_search tools/call; "<seed-episode>" in a list becomes the
    seeded session's episode id."""
    return {"name": name, "mcp": args, "headers": list(headers), "declared": None,
            "path": f"mcp memory_search {json.dumps(args)}"}


def mcp_call(port: int, c: dict, episode: str | None, transport: list | None = None) -> dict:
    """``transport`` collects each call's ``mcp-session-id``, so a retrieval
    event attributed to it can be compared across the two daemons."""
    from mcp_wire import Session, sse_data
    args = {k: ([episode if x == "<seed-episode>" else x for x in v] if isinstance(v, list) else v)
            for k, v in c["mcp"].items()}
    s = Session(port, TOKEN, dict(c["headers"]))
    if transport is not None:
        transport.append(s.sid)
    r = s.tool("memory_search", args)
    s.close()
    try:
        res = json.loads(sse_data(r)[0])["result"]
    except (IndexError, KeyError, ValueError):
        return {"status": r["status"], "headers": {"content-type": "mcp-transport"}, "bytes": r["body"]}
    return {"status": 422 if res.get("isError") else 200, "headers": {"content-type": "mcp"},
            "json": res.get("structuredContent")}


class Scenario:
    name = ""
    config_yaml: str | None = None
    env: dict[str, str] = {}
    # Coverage the oracle must show: dotted path into coverage() -> minimum.
    require: dict[str, int] = {}

    def cases(self) -> list[dict]:
        return default_cases()


class Default(Scenario):
    name = "default"
    require = {"with_entries": 40, "with_events": 3, "with_cortex": 30, "channels.dense": 1,
               "channels.bm25": 1, "channels.slot": 1, "rerank_fired": 1,
               "rerank_skips.candidate_budget_exceeded": 1, "sessions": 2, "episodes": 1,
               "disconnects": 1, "chronicle_over_limit": 1}


class Timeline(Scenario):
    name = "timeline"
    # A high default floor thins the dense pool, so the timeline's low
    # `0.3 x norm` injections survive the cut to top_k.
    config_yaml = "memory:\n  search:\n    timeline_channel: true\n    min_score: 0.6\n"
    require = {"via.timeline": 1, "timeline_fired": 1, "with_events": 1}

    def cases(self):
        auth = [bearer(TOKEN)]
        return [case(f"temporal {i}", q(q=t, top_k=8), auth) for i, t in enumerate(
            ["when did we deploy the release", "what happened first", "in what order did we update",
             "the timeline of the migration", "since the migration what changed", "deploy the daemon"])] + [
            case(f"temporal {b} {i}", q(q=t, top_k=8, bm25=b), auth)
            for b in ("auto", "false") for i, t in enumerate(
                ["when did the session step happen", "what happened before the deploy runbook",
                 "in what order did the session steps go"])]


class Contiguity(Scenario):
    name = "contiguity"
    config_yaml = "memory:\n  search:\n    contiguity_neighbors: 2\n"
    require = {"via.contiguity": 1, "channels.contiguity": 1}

    def cases(self):
        auth = [bearer(TOKEN)]
        return [case(f"neighbours {i}", q(q=t, top_k=4), auth) for i, t in enumerate(
            ["first we backed up the bank", "clients were updated", "bearer token", "deploy the daemon",
             "restarting the bench Postgres"])]


class RrfPool(Scenario):
    name = "rrf-pool"
    config_yaml = ("memory:\n  search:\n    fusion: rrf\n    candidate_pool_multiplier: 3\n"
                   "  reranker:\n    enabled: true\n    top_n: 40\n")
    require = {"rerank_fired": 1, "channels.bm25": 1}

    def cases(self):
        auth = [bearer(TOKEN)]
        return [case(f"rrf {i}", q(q=t, top_k=6), auth) for i, t in enumerate(CRAFTED[:6] + QUERIES[:4])]


class MarginGate(Scenario):
    name = "margin-gate"
    config_yaml = "memory:\n  reranker:\n    enabled: true\n    skip_margin: 0.05\n"
    require = {"rerank_fired": 1, "rerank_skips.unambiguous_margin": 1}

    def cases(self):
        auth = [bearer(TOKEN)]
        return [case(f"margin {i}", q(q=t, top_k=5), auth) for i, t in enumerate(CRAFTED[:8])]


class LogOff(Scenario):
    name = "log-off"
    config_yaml = "memory:\n  retrieval_log:\n    enabled: false\n  cortex:\n    enabled: false\n"

    def cases(self):
        auth = [bearer(TOKEN)]
        return [case(f"quiet {i}", q(q=t), auth) for i, t in enumerate(CRAFTED[:4])]


class LogOffControl(Scenario):
    """The default config again, few cases: the event-log mutant's catcher."""
    name = "log-off-control"

    def cases(self):
        auth = [bearer(TOKEN)]
        return [case(f"logged {i}", q(q=t), auth) for i, t in enumerate(CRAFTED[:3])]


class Continuum(Scenario):
    """Review finding (Opus, 2026-10-09): multi-band presets with depth
    recency. The flat-seeded rows are reseated into the first band."""
    name = "continuum"
    config_yaml = "memory:\n  recency_boost_enabled: true\n  miras:\n    preset: continuum\n"
    require = {"recency_boost": 1, "with_entries": 4}

    def cases(self):
        auth = [bearer(TOKEN)]
        return [case(f"continuum {i}", q(q=t, top_k=6), auth) for i, t in enumerate(CRAFTED[:5])] + [
            case("band working", q(q="deploy the daemon", band="working"), auth),
            case("band forever (empty)", q(q="deploy the daemon", band="forever"), auth),
            case("recency off", q(q="deploy the daemon", disable_recency_boost="1"), auth),
            case("band flat (not in preset)", q(q="deploy", band="flat"), auth)]


class HideSuperseded(Scenario):
    """Review finding (Opus): hide_superseded across dense, BM25, neighbours."""
    name = "hide-superseded"
    config_yaml = "memory:\n  hide_superseded: true\n  search:\n    contiguity_neighbors: 2\n"
    require = {"with_entries": 3}

    def cases(self):
        auth = [bearer(TOKEN)]
        return [case(f"hidden {i}", q(q=t, top_k=4), auth) for i, t in enumerate(
            ["extractor endpoint old gpu box", "where is the extractor endpoint",
             "the extractor endpoint moved", "staging database host"])]


class ConfidenceFloor(Scenario):
    name = "confidence-floor"
    config_yaml = "memory:\n  search_confidence_floor: 0.6\n"
    require = {"low_confidence_true": 1, "low_confidence_false": 1}

    def cases(self):
        auth = [bearer(TOKEN)]
        return [case(f"floor {i}", q(q=t), auth) for i, t in enumerate(
            ["PLX-4471", "zebra crossing", "My cat is named Jacque and she is a Ragdoll."])]


class PointerExpired(Scenario):
    """The active-session pointer past its TTL attributes nothing."""
    name = "pointer-expired"
    env = {"PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS": "1"}
    require = {"events_rows": 2}

    def cases(self):
        auth = [bearer(TOKEN)]
        return [case(f"stale pointer {i}", q(q=t), auth) for i, t in enumerate(CRAFTED[:2])]


class ConfidenceRounding(Scenario):
    """Psycopg reads REAL through decimal text before rounding to four places."""
    name = "confidence-rounding"
    require = {"confidence_boundary": 1}

    def cases(self):
        return [case("hydrated decimal confidence", q(q="rounding boundary confidence"),
                     [bearer(TOKEN)])]


EPISODE_PROBE = "episode filter probe child step deploy health check"


class McpTransportSession(Scenario):
    """PSEUDOLIFE_LEGACY_TRANSPORT_SESSION=1, no X-PL-Session and an expired
    pointer: the retrieval event falls to the MCP transport session
    (``_resolve_writer`` tier 4, service.py:974-985). A header still wins."""
    name = "mcp-transport-session"
    env = {"PSEUDOLIFE_LEGACY_TRANSPORT_SESSION": "1", "PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS": "1"}
    require = {"transport_sessions": 2, "events_rows": 3}

    def cases(self):
        return [*(mcp_case(f"transport session {i}", {"query": t}) for i, t in enumerate(CRAFTED[:2])),
                mcp_case("a header outranks the transport", {"query": CRAFTED[2]},
                         [("X-PL-Session", "sess-header-wins")])]


def scrub_transport(state: dict, ids: list) -> None:
    """Each arm's own mcp-session-id becomes ``<transport-N>`` (call order)."""
    names = {sid: f"<transport-{i}>" for i, sid in enumerate(ids) if sid}
    ev = state["rows"].get("public.retrieval_events")
    if not ev or not names:
        return
    col = ev["columns"].index("session_id")
    for row in ev["rows"]:
        if row[col] in names:
            row[col] = names[row[col]]


class McpSearch(Scenario):
    """memory_search over MCP: the projection (compact, verbose, truncation,
    replaced_by, cortex whitelist, correct_with, dedup, low_confidence),
    fact_k narrowing and the list filters. ``explain`` is declared
    (MCP-SEARCH-EXPLAIN) and not compared."""
    name = "mcp-search"
    mcp_extra_seed = True
    require = {"with_entries": 12, "with_cortex": 8, "correct_with.contested": 1,
               "correct_with.aged": 1, "truncated": 1, "replaced_by": 1,
               "episode_filter_child_hit": 1}

    def cases(self):
        os_q = "what os does the homelab box run"
        return [
            mcp_case("compact, contested fact and a restated entry", {"query": os_q}),
            mcp_case("verbose", {"query": os_q, "verbose": True}),
            mcp_case("top_k 2 narrows the cortex block", {"query": os_q, "top_k": 2}),
            mcp_case("top_k 1", {"query": "which port does the daemon listen on", "top_k": 1}),
            mcp_case("aged volatile fact", {"query": "when is the deploy window"}),
            mcp_case("superseded hit", {"query": "extractor endpoint old gpu box", "top_k": 3}),
            mcp_case("long entries truncate", {"query": "how does dreaming consolidate memory"}),
            mcp_case("long entries, verbose", {"query": "how does dreaming consolidate memory",
                                               "verbose": True}),
            mcp_case("source filter", {"query": "deploy", "sources": ["conversation"]}),
            mcp_case("empty lists are no filter", {"query": "deploy", "sources": [], "tags": [],
                                                   "bands": [], "episodes": []}),
            mcp_case("tag filter", {"query": "deploy", "tags": ["DEPLOY", " bug"]}),
            mcp_case("band flat", {"query": "deploy", "bands": ["flat"]}),
            mcp_case("unknown band", {"query": "deploy", "bands": ["working", "flat"]}),
            mcp_case("blank query", {"query": "   "}),
            mcp_case("episode filter reaches the child episode",
                     {"query": EPISODE_PROBE, "episodes": ["<seed-episode>"]}),
            mcp_case("session header", {"query": "deploy the daemon"}, [("X-PL-Session", "sess-mcp")]),
            mcp_case("min_score and recency", {"query": "deploy the daemon", "min_score": 0.5,
                                               "disable_recency_boost": True}),
            mcp_case("rerank on, bm25 off", {"query": "how do I back up the bank", "rerank": True,
                                             "bm25": False}),
            mcp_case("padded query", {"query": "   what os does the homelab box run  "}),
            mcp_case("nothing relevant", {"query": "zebra crossing quantum marmalade"}),
        ]


SCENARIOS = {s.name: s for s in (Default, Timeline, Contiguity, RrfPool, MarginGate, LogOff,
                                  LogOffControl, Continuum, HideSuperseded, ConfidenceFloor,
                                  PointerExpired, ConfidenceRounding, McpSearch,
                                  McpTransportSession)}


# ---- comparison ----------------------------------------------------------------------

def kind(v) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    return type(v).__name__


def diff(a, b, path="", tol_keys=("score",)) -> list[str]:
    """Strict by-value diff: types must match (True is not 1, 1 is not 1.0),
    floats exact except the declared float leaves."""
    if kind(a) != kind(b):
        return [f"{path}: python {kind(a)} {json.dumps(a)[:160]} vs rust {kind(b)} {json.dumps(b)[:160]}"]
    if isinstance(a, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k not in b:
                out.append(f"{path}.{k}: only in python")
            elif k not in a:
                out.append(f"{path}.{k}: only in rust")
            else:
                out += diff(a[k], b[k], f"{path}.{k}", tol_keys)
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}: length python {len(a)} vs rust {len(b)}"]
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in diff(x, y, f"{path}[{i}]", tol_keys)]
    if path.endswith(".age") and isinstance(a, str) and isinstance(b, str) and a != b:
        ma, mb = AGE_RE.fullmatch(a), AGE_RE.fullmatch(b)
        if ma and mb and ma.group(2) == mb.group(2) and abs(int(ma.group(1)) - int(mb.group(1))) <= 1:
            return []
        return [f"{path}: python {a!r} vs rust {b!r}"]
    if isinstance(a, float) or isinstance(b, float):
        leaf = path.rsplit(".", 1)[-1]
        if leaf in tol_keys:
            return [] if abs(float(a) - float(b)) <= SCORE_TOL else [f"{path}: python {a} vs rust {b}"]
    return [] if a == b else [f"{path}: python {json.dumps(a)[:160]} vs rust {json.dumps(b)[:160]}"]


def align_ties(py_list: list, rs_list: list, key: str = "id", *, ordered=False) -> tuple[list, list[str]]:
    """Reorder the Rust list to Python's where the only difference is a swap
    among entries whose scores are within SCORE_TOL; report each swap. Lists
    ordered by stream position (any contiguity or timeline entry) are never
    reordered: their order is exact."""
    notes = []
    if ordered or len(py_list) != len(rs_list):
        return rs_list, notes
    if any(isinstance(x, dict) and x.get("via") in ("contiguity", "timeline") for x in py_list + rs_list):
        return rs_list, notes
    rs = list(rs_list)
    for i, want in enumerate(py_list):
        if not isinstance(want, dict) or i >= len(rs) or rs[i].get(key) == want.get(key):
            continue
        j = next((j for j in range(i + 1, len(rs)) if rs[j].get(key) == want.get(key)), None)
        if j is None:
            continue
        span = [x.get("score", 0.0) for x in rs[i:j + 1]]
        if max(span) - min(span) <= SCORE_TOL:
            rs.insert(i, rs.pop(j))
            notes.append(f"tie swap at {i}: {want.get(key)}")
    return rs, notes


def compare(py: dict, rs: dict, *, timeline_fired=False) -> tuple[list[str], list[str]]:
    diffs, notes = [], []
    if py["status"] != rs["status"]:
        diffs.append(f"status: python {py['status']} vs rust {rs['status']}")
    if py["headers"].get("content-type") != rs["headers"].get("content-type"):
        diffs.append(f"content-type: {py['headers'].get('content-type')} vs {rs['headers'].get('content-type')}")
    a, b = py.get("json"), rs.get("json")
    if a is None or b is None:
        return diffs + ([] if py.get("bytes") == rs.get("bytes") else ["body bytes differ"]), notes
    if py["status"] in (400, 500) and isinstance(a.get("error"), str) and isinstance(b.get("error"), str):
        a = dict(a, error="<free>")
        b = dict(b, error="<free>")
    if isinstance(a, dict) and isinstance(b, dict):
        for k in ("entries", "cortex"):
            if isinstance(a.get(k), list) and isinstance(b.get(k), list):
                key = "id" if k == "entries" else "value"
                b = dict(b)
                b[k], n = align_ties(a[k], b[k], key, ordered=timeline_fired and k == "entries")
                notes += [f"{k}: {x}" for x in n]
    diffs += diff(a, b)
    return diffs, notes


# ---- bank state -----------------------------------------------------------------------

def scrub(state: dict) -> list[str]:
    declared = []
    for kind_, table, key, why in DECLARED:
        t = state["rows"].get(f"public.{table}")
        if not t:
            continue
        keep = [r for r in t["rows"] if kind_ == "row" and r[0] != key]
        if len(keep) != len(t["rows"]):
            declared.append(f"{table}{'.' + key if key else ''}: {why}")
            t["rows"] = keep
    return declared


def timeline_queries(state: dict) -> set[str]:
    """Oracle telemetry supplies ordering context even without via=timeline."""
    events = state["rows"].get("public.retrieval_events") or {}
    records = [dict(zip(events.get("columns", []), row)) for row in events.get("rows", [])]
    return {r["query_text"] for r in records
            if ((r.get("params") or {}).get("timeline") or {}).get("fired")}


def bank_diff(py_state: dict, rs_state: dict) -> list[str]:
    out = []
    ev = "public.retrieval_events"
    a = py_state["rows"].pop(ev, None)
    b = rs_state["rows"].pop(ev, None)
    if (a is None) != (b is None):
        out.append(f"{ev}: present on one side only")
    elif a is not None:
        cols = a["columns"]
        if len(a["rows"]) != len(b["rows"]):
            out.append(f"{ev}: python {len(a['rows'])} rows vs rust {len(b['rows'])}")
        for ra, rb in zip(a["rows"], b["rows"]):
            params = dict(zip(cols, ra)).get("params") or {}
            ordered = bool((params.get("timeline") or {}).get("fired"))
            for c, x, y in zip(cols, ra, rb):
                if c == "created_at":
                    if (x is None) != (y is None):
                        out.append(f"{ev}[{ra[0]}].{c}: null on one side")
                    continue
                if c == "served" and isinstance(x, list) and isinstance(y, list):
                    aligned, notes = align_ties(x, y, "entry_id", ordered=ordered)
                    if notes:
                        # A tie swap renumbers `rank` for the swapped rows only.
                        moved = {i for i, (u, v) in enumerate(zip(y, aligned)) if u is not v}
                        y = [dict(r, rank=x[i]["rank"]) if i in moved and isinstance(r, dict) else r
                             for i, r in enumerate(aligned)]
                out += diff(x, y, f"{ev}[{ra[0]}].{c}", tol_keys=FLOAT_KEYS)
    out += dbstate.diff(py_state, rs_state)
    return out


# ---- running --------------------------------------------------------------------------

def rust_env(extra: dict[str, str]) -> dict[str, str]:
    out = dict(extra)
    for key in ("ORT_DYLIB_PATH", "PSEUDOLIFE_DAEMON_ONNX_DIR", "PSEUDOLIFE_DAEMON_RERANK_DIR",
                "PSEUDOLIFE_DAEMON_MUTANT", "PSEUDOLIFE_DAEMON_ORT_THREADS"):
        if os.environ.get(key):
            out[key] = os.environ[key]
    return out


def common_env(dsn: str) -> dict[str, str]:
    return {"PSEUDOLIFE_RELEASE_CHECK": "0", "PSEUDOLIFE_MCP_TOKEN": TOKEN,
            "PSEUDOLIFE_MCP_DATABASE_URL": dsn}


def seed_template(name: str, paragraphs: int) -> None:
    dsn = pg.create(name)
    home = daemons.make_home(daemons.scratch_root(), "w2d-seed", None)
    env = daemons.base_env(home, {"PSEUDOLIFE_MCP_DATABASE_URL": dsn})
    r = subprocess.run([sys.executable, str(HERE / "w2d_seed.py"), str(paragraphs)],
                       env=dict(env, PYTHONPATH=str(REPO)), capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"seeding failed: {r.stderr[-3000:]}")
    print(r.stdout.strip(), flush=True)
    # Linux 2026-10-13: a fresh default scenario crossed "just now" ->
    # "1 minute ago" between oracle and candidate. Keep timestamps and the
    # comparator unchanged; start after that first, nonnumeric age bucket.
    print("waiting 70 seconds for the initial relative-age bucket", flush=True)
    time.sleep(70)


def coverage(responses: list[dict], state: dict) -> dict:
    """What the oracle actually exercised, so a green run cannot be vacuous."""
    cov = {"with_entries": 0, "with_events": 0, "with_cortex": 0, "via": {}, "channels": {},
           "rerank_fired": 0, "rerank_skips": {}, "timeline_fired": 0, "events_rows": 0,
           "recency_boost": 0, "sessions": 0, "transport_sessions": 0, "episodes": 0, "low_confidence_true": 0,
           "low_confidence_false": 0, "disconnects": 0, "chronicle_over_limit": 0, "confidence_boundary": 0,
           "correct_with": {"contested": 0, "aged": 0}, "truncated": 0, "replaced_by": 0,
           "episode_filter_child_hit": 0}
    for r in responses:
        body = r.get("json") or {}
        if not isinstance(body, dict):
            continue
        cov["with_entries"] += bool(body.get("entries"))
        cov["with_events"] += bool(body.get("events"))
        cov["chronicle_over_limit"] += len(body.get("events") or []) > 3
        cov["with_cortex"] += bool(body.get("cortex"))
        cov["confidence_boundary"] += any(
            f.get("entity") == "rounding boundary" and f.get("confidence") == 0.0001
            and f.get("effective_confidence") == 0.0001 for f in body.get("cortex") or [])
        cov["disconnects"] += bool(r.get("disconnected"))
        if "low_confidence" in body and body.get("query"):
            cov["low_confidence_true" if body["low_confidence"] else "low_confidence_false"] += 1
        if body.get("query") == EPISODE_PROBE:
            cov["episode_filter_child_hit"] += any(
                (e.get("text") or "").startswith("Episode filter probe")
                for e in body.get("entries") or [])
        for f in body.get("cortex") or []:
            cw = f.get("correct_with") or ""
            if cw.startswith("memory_fact_get("):
                cov["correct_with"]["contested"] += 1
            elif cw.startswith("memory_fact_set("):
                cov["correct_with"]["aged"] += 1
        for e in body.get("entries") or []:
            cov["truncated"] += bool(e.get("truncated"))
            cov["replaced_by"] += "replaced_by" in e
            if e.get("via"):
                cov["via"][e["via"]] = cov["via"].get(e["via"], 0) + 1
    ev = state["rows"].get("public.retrieval_events")
    if ev:
        cols = ev["columns"]
        for row in ev["rows"]:
            cov["events_rows"] += 1
            rec = dict(zip(cols, row))
            for s_ in rec.get("served") or []:
                ch = (s_.get("components") or {}).get("channel")
                cov["channels"][ch] = cov["channels"].get(ch, 0) + 1
            rr = (rec.get("params") or {}).get("reranker") or {}
            cov["rerank_fired"] += bool(rr.get("fired"))
            if rr.get("skip_reason"):
                cov["rerank_skips"][rr["skip_reason"]] = cov["rerank_skips"].get(rr["skip_reason"], 0) + 1
            cov["timeline_fired"] += bool(((rec.get("params") or {}).get("timeline") or {}).get("fired"))
            cov["recency_boost"] += bool((rec.get("params") or {}).get("recency_boost"))
            cov["sessions"] += rec.get("session_id") is not None
            cov["transport_sessions"] += str(rec.get("session_id") or "").startswith("<transport-")
            cov["episodes"] += rec.get("episode_id") is not None
    return cov


def unmet(cov: dict, require: dict[str, int]) -> list[str]:
    out = []
    for path, minimum in require.items():
        v = cov
        for part in path.split("."):
            v = v.get(part, 0) if isinstance(v, dict) else 0
        if v < minimum:
            out.append(f"coverage {path}: {v} < {minimum}")
    return out


def disconnect(port: int, path: str, headers) -> None:
    """Send the request and hang up without reading the answer."""
    import socket
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s_:
        lines = [f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{port}"] + [f"{k}: {v}" for k, v in headers]
        s_.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        time.sleep(0.05)


def config_for(scn: Scenario) -> str:
    """The scenario's config.yaml with the dream pass off: its background
    sweep writes dream state (W3-H's slice) on the Python side mid-run."""
    nl = chr(10)
    dream = "  dream:" + nl + "    enabled: false" + nl
    head = "memory:" + nl
    if scn.config_yaml is None:
        return head + dream
    assert scn.config_yaml.startswith(head), scn.name
    return head + dream + scn.config_yaml[len(head):]


def derive_template(template: str, tag: str) -> str:
    """The shared seed plus the MCP scenario's own facts, in a template of
    its own so the other scenarios (and their goldens) are unchanged."""
    name = pg.name(f"pl_cf_w2d_{tag}_t")
    if name in pg.existing():
        pg.drop(name)
    dsn = pg.create(name, template=template)
    home = daemons.make_home(daemons.scratch_root(), f"w2d-{tag}-seed", None)
    env = daemons.base_env(home, {"PSEUDOLIFE_MCP_DATABASE_URL": dsn})
    r = subprocess.run([sys.executable, str(HERE / "w2d_seed.py"), "--mcp-extra"],
                       env=dict(env, PYTHONPATH=str(REPO)), capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"mcp seeding failed: {r.stderr[-3000:]}")
    return name


def seed_episode(template: str) -> str | None:
    import psycopg
    with psycopg.connect(pg.dsn(template)) as conn:
        row = conn.execute("SELECT id FROM episodes WHERE title = %s", ("W2-D seed session",)).fetchone()
    return row[0] if row else None


def run_scenario(scn: Scenario, binary: Path, template: str, mode: str, record: bool) -> dict:
    tag = scn.name.replace("-", "_")
    derived = None
    if getattr(scn, "mcp_extra_seed", False):
        if mode == "golden":
            # Its template is derived through the Python daemon; a recording
            # replayed against the base template would differ by design.
            raise SystemExit(f"{scn.name} runs live only (derived template)")
        derived = template = derive_template(template, tag)
    episode = seed_episode(template) if mode != "golden" else None
    if any("mcp" in c and "<seed-episode>" in json.dumps(c["mcp"]) for c in scn.cases()) \
            and episode is None:
        raise RuntimeError("the seeded session episode is missing from the template")
    dbs = {side: pg.name(f"pl_cf_w2d_{tag}_{suffix}")
           for side, suffix in (("python", "py"), ("rust", "rs"))}
    sides = ("python", "rust") if mode != "golden" else ("rust",)
    dsns = {k: pg.create(dbs[k], template=template) for k in sides}
    procs = {}
    root = daemons.scratch_root()
    try:
        if "python" in sides:
            home = daemons.make_home(root, f"w2d-{tag}-py", config_for(scn))
            procs["python"] = daemons.python_daemon(home, daemons.free_port(),
                                                    daemons.base_env(home, dict(common_env(dsns["python"]),
                                                                                **scn.env)))
        home = daemons.make_home(root, f"w2d-{tag}-rs", config_for(scn))
        procs["rust"] = daemons.rust_daemon(binary, home, daemons.free_port(),
                                            daemons.base_env(home, rust_env(dict(common_env(dsns["rust"]),
                                                                                 **scn.env))))
        for d in procs.values():
            d.start(300)
        wait_settled([d.port for d in procs.values()], timeout=900, token=TOKEN)
        golden = json.loads((GOLDENS / f"{scn.name}.json").read_text(encoding="utf-8")) if mode == "golden" else None
        rows = []
        py_seen = []
        response_pairs = []
        transport = {side: [] for side in sides}
        for i, c in enumerate(scn.cases()):
            if c.get("disconnect"):
                for d in procs.values():
                    disconnect(d.port, c["path"], c["headers"])
                time.sleep(15)  # both finish (or abandon) the search before the next case
                py_seen.append({"disconnected": True})
                response_pairs.append(None)
                rows.append({"case": c["name"], "path": c["path"][:140], "python_status": None,
                             "rust_status": None, "diffs": [], "notes": [], "_python": None})
                continue
            if "mcp" in c:
                py_r = (mcp_call(procs["python"].port, c, episode, transport["python"])
                        if "python" in procs else golden["responses"][i])
                rs_r = mcp_call(procs["rust"].port, c, episode, transport["rust"])
            else:
                py_r = (call(procs["python"].port, "GET", c["path"], c["headers"]) if "python" in procs
                        else golden["responses"][i])
                rs_r = call(procs["rust"].port, "GET", c["path"], c["headers"])
            py_seen.append(py_r)
            response_pairs.append((py_r, rs_r))
            rows.append({"case": c["name"], "path": c["path"][:140], "python_status": py_r["status"],
                         "rust_status": rs_r["status"], "diffs": [], "notes": [],
                         "_python": py_r if record else None})
    finally:
        for d in procs.values():
            d.stop()
    states = {}
    declared = {}
    for side in sides:
        st = dbstate.normalize(dbstate.dump(pg.dsn(dbs[side])), CLOCK_COLUMNS)
        scrub_transport(st, transport[side])
        declared[side] = scrub(st)
        states[side] = st
    if mode == "golden":
        py_state = golden["db_state"]
    else:
        py_state = states["python"]
    ordered_queries = timeline_queries(py_state)
    for row, pair in zip(rows, response_pairs):
        if pair is not None:
            py_r, rs_r = pair
            body = py_r.get("json")
            ordered = isinstance(body, dict) and body.get("query") in ordered_queries
            row["diffs"], row["notes"] = compare(py_r, rs_r, timeline_fired=ordered)
    cov = coverage(py_seen, py_state)
    cov_fail = unmet(cov, scn.require)
    db_diffs = bank_diff(json.loads(json.dumps(py_state)), states["rust"])
    if record:
        GOLDENS.mkdir(parents=True, exist_ok=True)
        (GOLDENS / f"{scn.name}.json").write_text(json.dumps({
            "rules": {"score_tol": SCORE_TOL, "clock_columns": sorted(f"{t}.{c}" for t, c in CLOCK_COLUMNS),
                      "declared": [d[3] for d in DECLARED]},
            "responses": [r.pop("_python") for r in rows],
            "coverage": cov,
            "db_state": states["python"]}, indent=1, sort_keys=True), encoding="utf-8")
    for r in rows:
        r.pop("_python", None)
    for side in sides:
        pg.drop(dbs[side])
    if derived:
        pg.drop(derived)
    return {"scenario": scn.name, "cases": rows, "db_diffs": db_diffs + cov_fail, "declared": declared,
            "coverage": cov}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("live", "golden", "mutants"))
    ap.add_argument("--rust-bin", type=Path, required=True)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--paragraphs", type=int, default=150)
    ap.add_argument("--reuse-template", action="store_true")
    args = ap.parse_args()
    # Live-only scenarios (a derived template) stay out of default replay.
    names = args.only or [n for n, scn in SCENARIOS.items()
                          if args.mode != "golden" or not getattr(scn, "mcp_extra_seed", False)]
    template = pg.name("pl_cf_w2d_template")
    if args.mode != "golden" and (not args.reuse_template or template not in pg.existing()):
        seed_template(template, args.paragraphs)
    if args.mode == "mutants":
        outcome = {}
        baselines = set()
        previous_mutant = os.environ.pop("PSEUDOLIFE_DAEMON_MUTANT", None)
        try:
            for m, scn in MUTANTS.items():
                if args.only and scn not in args.only:
                    continue
                if scn not in baselines:
                    os.environ.pop("PSEUDOLIFE_DAEMON_MUTANT", None)
                    baseline = run_scenario(SCENARIOS[scn](), args.rust_bin, template, "live", False)
                    if (unmet(baseline["coverage"], SCENARIOS[scn].require)
                            or baseline["db_diffs"]
                            or any(c["diffs"] for c in baseline["cases"])):
                        print(f"invalid baseline for {scn}; mutant {m} is not qualified", flush=True)
                        return 1
                    baselines.add(scn)
                    print(f"baseline {scn}: clean and covered", flush=True)
                os.environ["PSEUDOLIFE_DAEMON_MUTANT"] = m
                result = run_scenario(SCENARIOS[scn](), args.rust_bin, template, "live", False)
                if unmet(result["coverage"], SCENARIOS[scn].require):
                    print(f"invalid oracle coverage for mutant {m}; not a kill", flush=True)
                    return 1
                n_diff = sum(bool(c["diffs"]) for c in result["cases"]) + bool(result["db_diffs"])
                outcome[m] = n_diff
                print(f"mutant {m}: {n_diff} diffs", flush=True)
        finally:
            if previous_mutant is None:
                os.environ.pop("PSEUDOLIFE_DAEMON_MUTANT", None)
            else:
                os.environ["PSEUDOLIFE_DAEMON_MUTANT"] = previous_mutant
        survivors = [m for m, n in outcome.items() if n == 0]
        print(json.dumps({"mutants": outcome, "survivors": survivors}, indent=1))
        return 1 if survivors else 0
    results = [run_scenario(SCENARIOS[n](), args.rust_bin, template, args.mode, args.record) for n in names]
    n_cases = sum(len(r["cases"]) for r in results)
    n_diff = 0
    for r in results:
        for c in r["cases"]:
            if c["diffs"]:
                n_diff += 1
                print(f"DIFF [{r['scenario']}] {c['case']}: {c['diffs'][:5]}")
            for n in c["notes"]:
                print(f"note [{r['scenario']}] {c['case']}: {n}")
        for d in r["db_diffs"][:20]:
            print(f"DB DIFF [{r['scenario']}] {d}")
        print(f"[{r['scenario']}] declared python-only writes: {r['declared'].get('python')}")
        print(f"[{r['scenario']}] coverage: {json.dumps(r['coverage'])}")
    db = sum(1 for r in results if r["db_diffs"])
    summary = {"scenarios": len(results), "cases": n_cases, "case_diffs": n_diff, "db_diff_scenarios": db}
    print(json.dumps(summary))
    if args.out:
        args.out.write_text(json.dumps({"summary": summary, "results": results}, indent=1), encoding="utf-8")
    return 1 if (n_diff or db) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        for database in pg.existing():
            pg.drop(database)
        print("cleanup: owned disposable databases dropped", flush=True)

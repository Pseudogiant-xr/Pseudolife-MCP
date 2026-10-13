"""Write-path differential harness (slice W2-E): the Python daemon (oracle)
against the Rust daemon, write by write.

usage:
  python w2e.py live    --rust-bin PATH [--only NAME ...] [--out FILE] [--record]
  python w2e.py golden  --rust-bin PATH [--only NAME ...] [--out FILE]
  python w2e.py calibrate --rust-bin PATH [--out FILE]   (no tolerances: records them)
  python w2e.py mutants --rust-bin PATH_BUILT_WITH_FEATURES_HARNESS_MUTANTS [--only NAME ...]

The Rust binary must be built with ``--features harness`` (``mutants`` adds
the deliberate breaks). Each scenario prepares one template bank through the
Python daemon's own write path (seed calls only, never hand-written rows),
clones it for each side, starts both daemons, then sends every operation to
both. After every operation it compares:

* the response by value: Python's MCP ``tools/call`` result
  (``structuredContent``, ``isError``) against the Rust harness route's
  answer; ``surprise`` within the measured tolerance, everything else exact;
* the full database state of both banks (``dbstate.dump``), after the
  declared normalizers below, which replace values by rule, never by hand.

Normalizers (declared, spec "Free for W1 and W2"):

* wall-clock columns and meta fields keep only null-or-not;
* episode ids (``uuid4().hex``) are renamed ``<ep#i>`` in ``started_at``
  order wherever they appear (any table, any JSON value or key);
* generic session titles keep their shape, not their minute;
* ``entries.embedding`` and ``entries.surprise`` are compared numerically
  per entry id against tolerances set from a measurement
  (``goldens/w2e-tolerance.json``), then replaced by a token;
* rows are compared as sorted sets (row order is the primary key's order,
  which renamed ids change), except that entry ids themselves must match.

Exit status: 0 only when no response and no bank state differs.
Environment: ``ORT_DYLIB_PATH`` and ``PSEUDOLIFE_DAEMON_ONNX_DIR`` for the
Rust daemon; the test login (``pgdisposable``) for the banks.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daemons  # noqa: E402
import dbstate  # noqa: E402
import pgdisposable as pg  # noqa: E402
import run as w1a  # noqa: E402

pg.DISPOSABLE_NAME = re.compile(r"pl_cf_w2e_[a-z0-9_]{1,40}")
GOLDENS = HERE / "goldens" / "w2e"
TOLERANCE_FILE = HERE / "goldens" / "w2e-tolerance.json"
TOKEN = "tok-default-w2e-0001"
AUTH = ("Authorization", f"Bearer {TOKEN}")

# (table, column) holding wall-clock seconds (value or list of values).
CLOCK = {("entries", "ts"), ("entries", "superseded_at"), ("episodes", "started_at"),
         ("episodes", "ended_at"), ("client_sessions", "started_at"),
         ("client_sessions", "ended_at"), ("client_sessions", "start_times")}
# meta key -> paths (keys) inside its JSON value that are wall-clock.
META_CLOCK = {"capacity_true_drops": {"last_at"}, "active_session_pointer": {"ts"}}
EP_ID = re.compile(r"^[0-9a-f]{32}$")
GENERIC = re.compile(r"^session - \d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
MUTANTS = ["store-tags-raw", "store-skip-session-episode", "store-no-hint", "store-surprise-raw",
           "store-cancellable"]


# ── MCP client for the Python oracle ──────────────────────────────────────

class Mcp:
    """Minimal streamable-HTTP MCP client: initialize once, then tools/call."""

    def __init__(self, port: int):
        self.port, self.session, self.ids = port, None, 0

    def _post(self, payload: dict, extra=()) -> dict | None:
        headers = [AUTH, ("Content-Type", "application/json"),
                   ("Accept", "application/json, text/event-stream"), *extra]
        if self.session:
            headers.append(("mcp-session-id", self.session))
        body = json.dumps(payload).encode()
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=300)
        c.request("POST", "/mcp", body=body, headers=dict(headers))
        r = c.getresponse()
        raw = r.read()
        sid = r.getheader("mcp-session-id")
        if sid:
            self.session = sid
        ctype = r.getheader("content-type") or ""
        c.close()
        if r.status >= 300:
            raise RuntimeError(f"/mcp answered {r.status}: {raw[:300]!r}")
        if not raw.strip():
            return None
        if ctype.startswith("text/event-stream"):
            msgs = [json.loads(line[5:]) for line in raw.decode().splitlines()
                    if line.startswith("data:") and line[5:].strip()]
            return next((m for m in reversed(msgs) if "id" in m), None)
        return json.loads(raw)

    def init(self) -> None:
        self.ids += 1
        self._post({"jsonrpc": "2.0", "id": self.ids, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "w2e-harness", "version": "0"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def abandon(self, name: str, args: dict, headers=()) -> None:
        self.ids += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.ids, "method": "tools/call",
                           "params": {"name": name, "arguments": args}}).encode()
        abandon(self.port, "/mcp", [AUTH, ("Content-Type", "application/json"),
                                    ("Accept", "application/json, text/event-stream"),
                                    ("mcp-session-id", self.session), *headers], body)

    def call(self, name: str, args: dict, headers=()) -> dict:
        self.ids += 1
        msg = self._post({"jsonrpc": "2.0", "id": self.ids, "method": "tools/call",
                          "params": {"name": name, "arguments": args}}, headers)
        res = msg["result"]
        value = res.get("structuredContent")
        if value is None:
            value = json.loads(res["content"][0]["text"])
        # FastMCP wraps a non-object return under "result".
        return {"isError": bool(res.get("isError")), "value": value}


CRLF = chr(13) + chr(10)


def abandon(port: int, path: str, headers: list, body: bytes, after_s: float = 0.15) -> None:
    """Send a request and close the socket before the answer: a client that
    disconnects mid-call (a killed session, a timeout)."""
    import socket
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    head = [f"POST {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", f"Content-Length: {len(body)}",
            *[f"{k}: {v}" for k, v in headers], "", ""]
    s.sendall(CRLF.join(head).encode("latin-1") + body)
    time.sleep(after_s)
    s.close()


def rust_tool(port: int, name: str, args: dict, headers=()) -> dict:
    r = w1a.call(port, "POST", f"/api/_harness/tool/{name}",
                 [AUTH, ("Content-Type", "application/json"), *headers],
                 json.dumps(args).encode())
    return {"isError": r["status"] != 200, "value": r.get("json", r.get("bytes"))}


# ── scenarios ─────────────────────────────────────────────────────────────

def store(text, headers=(), **kw) -> dict:
    return {"op": "tool", "name": "memory_store", "args": {"text": text, **kw},
            "headers": list(headers)}


def sess(s: str) -> tuple[str, str]:
    return ("X-PL-Session", s)


SLOT_TEXTS = [
    "I have a Ragdoll cat named Jacque",
    "I gave him away last spring",
    "the daemon port is 8765",
    "host: 10.0.0.5",
    "Our build server hostname is ci-box-2.",
    "My editor is vim; my shell is zsh.",
]

LABEL_TEXTS = [
    "Never run the full suite natively on Windows.",
    "You must rebase before opening the PR.",
    "According to the docs, the bank volume is external.",
    "Rule: no PII in commits.",
    "I always prefer tabs over spaces.",
    "The daemon must be restarted after a schema bump.",
]

SCENARIOS: dict[str, dict] = {
    "store-resume-invalid": {
        "config": None,
        "env": {"PSEUDOLIFE_SESSION_RESUME_SECONDS": "not-a-number"},
        "seed_env": {"PSEUDOLIFE_SESSION_RESUME_SECONDS": "21600"},
        "seed": [
            store("A closed root survives invalid duration settings.", [sess("invalid")]),
            {"op": "http", "method": "POST", "path": "/api/hook/session-end",
             "body": {"session_id": "invalid"}},
        ],
        "ops": [store("Invalid resume must refuse before mutation.", [sess("invalid")])],
    },
    "store-resume-disabled": {
        "config": None,
        "env": {"PSEUDOLIFE_SESSION_RESUME_SECONDS": "0_0"},
        "seed": [
            store("A durable note in the session that will close.", [sess("disabled")]),
            {"op": "http", "method": "POST", "path": "/api/hook/session-end",
             "body": {"session_id": "disabled"}},
        ],
        "ops": [store("Disabled resume opens a fresh root.", [sess("disabled")])],
    },
    "store-reaper": {
        "config": "memory:\n  meta_filter:\n    enabled: true\n  dream:\n    enabled: false\n  retrieval_log:\n    enabled: false\n",
        "env": {"PSEUDOLIFE_SESSION_IDLE_SECONDS": "2",
                "PSEUDOLIFE_SESSION_REAP_SECONDS": "0.1",
                "PSEUDOLIFE_SESSION_RESUME_SECONDS": "3600"},
        "seed": [],
        "ops": [
            # Episode creation precedes admission: even a filtered store's
            # empty root must be visible to the background reaper.
            store("I don't have any memories about cats saved.", [sess("reaped")]),
            {"op": "sleep", "s": 4},
        ],
    },
    # Shipped defaults: gate inert, meta filter off, auto-promote off.
    "store-defaults": {
        "config": None,
        "seed": [],
        "ops": [
            store("The bench Postgres listens on port 5433 for disposable test banks."),
            store("   "),
            store(""),
            store("  Leading and trailing spaces are stripped.  "),
            store("Tags are normalized", tags=[" Decision ", "decision", "", "BLOCKER"]),
            store("Status note from a peer session", source="status"),
            store("A user said this", source="user", origin="user"),
            store("The bench Postgres listens on port 5433 for disposable test banks."),
            *[store(t) for t in LABEL_TEXTS],
            # Explicit labels only in the MCP vocabulary: anything else is a
            # binding refusal (W2-G); the body's own ValueError text is
            # proven by the label goldens.
            store("Explicit labels win", authority="directive", distortion_tolerance="belief"),
            store("Observation label", authority="observation"),
            store("Quoted label on a rule", authority="quoted", distortion_tolerance="episodic"),
            store("", authority="observation"),
            *[store(t) for t in SLOT_TEXTS],
            store("First note in session A", [sess("sess-a")]),
            store("Second note in session A", [sess("sess-a")]),
            store("A note in session B", [sess("sess-b")], tags=["b"]),
            store("Handle attribution to A", episode="@root:sess-a:12"),
            store("Handle plus a different header", [sess("sess-b")], episode="@root:sess-a:12"),
            store("Unknown handle", episode="ffffffffffff"),
            store("Short handle", episode="abc"),
            store("Empty handle", episode=""),
            store("Empty header falls through", [sess("")]),
            # The client hangs up mid-call; Python finishes the store on its
            # worker thread regardless.
            # The 2026-10-10 Linux run missed the cancellation mutant with a
            # short input; fill the 512-token embedding cap so the socket can
            # close while the tool body is still working.
            {**store("Stored although the caller disconnected. " * 160,
                     [sess("sess-a")]), "abandon": True},
            store("The next store after a disconnect", [sess("sess-a")]),
        ],
    },
    # The gate on: meta filter, surprise threshold, contradiction admission.
    "store-gate": {
        "config": "memory:\n  surprise_threshold: 0.35\n  meta_filter:\n    enabled: true\n",
        "seed": [],
        "ops": [
            store("I don't have any memories about cats saved."),
            store("I don't have any memories about cats saved.", source="user"),
            store("The release pipeline publishes to PyPI with trusted publishing."),
            store("The release pipeline publishes to PyPI with trusted publishing."),
            store("Release pipeline publishes to PyPI using trusted publishing."),
            store("My dog's name is Rex."),
            store("My dog's name is Max."),
            store("The staging server is running."),
            store("The staging server is not running."),
            store("I have a cat named Jacque."),
            store("I no longer have Jacque."),
            store("the api port is 8080"),
            store("the api port is 9090"),
            store("Completely unrelated: the tea kettle whistles at boiling."),
            # Near-duplicates with no contradiction anchor (no capitals,
            # numbers, negation or possession cues): a plain gate drop.
            store("the tea kettle whistles loudly when the water boils"),
            store("the tea kettle whistles when the water boils"),
            store("the kettle whistles loudly when water boils"),
        ],
    },
    # Capacity: a one-band custom preset of five entries forces true drops.
    "store-capacity": {
        "config": "memory:\n  miras:\n    preset: custom\n    bands:\n      - name: flat\n        max_entries: 5\n",
        "seed": [],
        "ops": [
            store("System prompt text that should be retained.", source="system"),
            store("A tool call record, cheap to lose.", source="tool_call"),
            store("Thinking trace, the cheapest to lose.", source="llm_thinking"),
            store("User statement about the weather in Hobart.", source="user"),
            store("Agent note about the deploy checklist."),
            store("Agent note about rotating the bearer tokens."),
            store("Another thinking trace about indexes.", source="llm_thinking"),
            store("A second user statement about lunch.", source="user"),
        ],
    },
    # A bank seeded by Python: a registered session, a closed root, an open
    # root, the active-session pointer, and a tombstone.
    "store-seeded-episodes": {
        "config": None,
        "seed_env": {"PSEUDOLIFE_SESSION_REAP_SECONDS": "1", "PSEUDOLIFE_SESSION_IDLE_SECONDS": "1",
                     "PSEUDOLIFE_SESSION_RESUME_SECONDS": "2"},
        "seed": [
            {"op": "http", "method": "GET", "path": "/api/hook/session-start?session_id=sess-empty"},
            {"op": "sleep", "s": 8},
            store("Closed session note", [sess("sess-closed")]),
            {"op": "http", "method": "POST", "path": "/api/hook/session-end",
             "body": {"session_id": "sess-closed"}},
            store("Open session note", [sess("sess-open")]),
            {"op": "http", "method": "GET", "path": "/api/hook/session-start?session_id=sess-ptr"},
        ],
        "ops": [
            store("No header: the active-session pointer attributes it"),
            store("Back in the open session", [sess("sess-open")]),
            store("Resume the closed root by its key", [sess("sess-closed")]),
            store("Recreate the swept root by its handle", episode="@tomb:12"),
            store("A brand new session", [sess("sess-new")]),
        ],
    },
}


# ── per-side argument templates ───────────────────────────────────────────

def resolve_args(args: dict, dsn: str) -> dict:
    out = {}
    import psycopg
    for k, v in args.items():
        if isinstance(v, str) and v.startswith("@root:"):
            _, key, n = v.split(":")
            with psycopg.connect(dsn) as c:
                row = c.execute("SELECT id FROM episodes WHERE session_key = %s AND parent_id IS NULL "
                                "ORDER BY started_at LIMIT 1", (key,)).fetchone()
            v = row[0][:int(n)] if row else "0" * int(n)
        elif isinstance(v, str) and v.startswith("@tomb:"):
            n = int(v.split(":")[1])
            with psycopg.connect(dsn) as c:
                row = c.execute("SELECT value FROM meta WHERE key = 'episode_tombstones'").fetchone()
            ids = sorted((row[0] or {}).keys()) if row else []
            v = ids[0][:n] if ids else "0" * n
        out[k] = v
    return out


# ── state normalization ───────────────────────────────────────────────────

def _table(state, name):
    return state["rows"].get(f"public.{name}")


def _col(t, name):
    return t["columns"].index(name) if t and name in t["columns"] else None


def extract_numeric(state: dict) -> dict:
    """Pull entry embeddings and surprises out by entry id, leaving tokens."""
    t = _table(state, "entries")
    out = {}
    if not t:
        return out
    i_id, i_emb, i_s = _col(t, "id"), _col(t, "embedding"), _col(t, "surprise")
    for row in t["rows"]:
        emb = row[i_emb]
        out[row[i_id]] = {"embedding": [float(x) for x in emb.strip("[]").split(",")] if emb else None,
                          "surprise": row[i_s]}
        row[i_emb] = "<embedding>" if emb is not None else None
        row[i_s] = "<surprise>" if row[i_s] is not None else None
    return out


def episode_map(state: dict) -> dict[str, str]:
    ids: list[str] = []
    t = _table(state, "episodes")
    if t:
        i_id, i_st = _col(t, "id"), _col(t, "started_at")
        ids += [r[i_id] for r in sorted(t["rows"], key=lambda r: (r[i_st] or 0, r[i_id]))]
    m = _table(state, "meta")
    if m:
        for key, value in m["rows"]:
            if key in ("episode_tombstones", "deferred_empty_roots") and isinstance(value, dict):
                def when(k):
                    v = value[k]
                    return v.get("ended_at", 0) if isinstance(v, dict) else v
                ids += [k for k in sorted(value, key=lambda k: (when(k) or 0, k)) if k not in ids]
    return {eid: f"<ep#{i}>" for i, eid in enumerate(dict.fromkeys(ids))}


def _rename(v, emap):
    if isinstance(v, str):
        if v in emap:
            return emap[v]
        if GENERIC.match(v):
            return "session - <minute>"
        return v
    if isinstance(v, list):
        return [_rename(x, emap) for x in v]
    if isinstance(v, dict):
        return {_rename(k, emap): _rename(x, emap) for k, x in v.items()}
    return v


def _clock(v):
    if isinstance(v, list):
        return [_clock(x) for x in v]
    return None if v is None else "<clock>"


HEX_RUN = re.compile(r"[0-9a-f]{8,32}")


def rename_prefixes(v, emap: dict[str, str]):
    """Episode-id prefixes inside response strings (handles in hints)."""
    if isinstance(v, str):
        def sub(m):
            hits = [tok for eid, tok in emap.items() if eid.startswith(m.group(0))]
            return f"{hits[0]}[:{len(m.group(0))}]" if len(hits) == 1 else m.group(0)
        return HEX_RUN.sub(sub, v)
    if isinstance(v, list):
        return [rename_prefixes(x, emap) for x in v]
    if isinstance(v, dict):
        return {k: rename_prefixes(x, emap) for k, x in v.items()}
    return v


def normalize_state(state: dict, before: dict) -> tuple[dict, dict, dict]:
    state = w1a.scrub_declared_rows(state, before)["state"]
    state = dbstate.normalize(state, w1a.DB_NONDETERMINISTIC, before)
    numeric = extract_numeric(state)
    emap = episode_map(state)
    for qualified, t in state["rows"].items():
        name = qualified.split(".", 1)[1]
        for idx, col in enumerate(t["columns"]):
            if (name, col) in CLOCK:
                for row in t["rows"]:
                    row[idx] = _clock(row[idx])
        if name == "meta":
            for row in t["rows"]:
                key, value = row
                if key in META_CLOCK and isinstance(value, dict):
                    row[1] = {k: (_clock(x) if k in META_CLOCK[key] else x) for k, x in value.items()}
                elif key == "episode_tombstones" and isinstance(value, dict):
                    row[1] = {k: {**x, "ended_at": _clock(x.get("ended_at"))} if isinstance(x, dict) else x
                              for k, x in value.items()}
                elif key == "deferred_empty_roots" and isinstance(value, dict):
                    row[1] = {k: _clock(x) for k, x in value.items()}
        t["rows"] = [_rename(r, emap) for r in t["rows"]]
        if name != "entries":
            t["rows"].sort(key=lambda r: json.dumps(r, sort_keys=True))
    return state, numeric, emap


def state_diff(a: dict, b: dict) -> list[str]:
    """``dbstate.diff``, with list-valued catalog entries diffed row by row
    so a moved sequence names itself."""
    out = []
    for d in dbstate.diff(a, b):
        if d.startswith("/catalog/"):
            key = d.split(":", 1)[0].rsplit("/", 1)[1]
            if key not in a["catalog"] or key not in b["catalog"]:
                out.append(d)
                continue
            ra = {json.dumps(r) for r in a["catalog"][key]}
            rb = {json.dumps(r) for r in b["catalog"][key]}
            out += [f"/catalog/{key}: python {r}" for r in sorted(ra - rb)]
            out += [f"/catalog/{key}: rust {r}" for r in sorted(rb - ra)]
        else:
            out.append(d)
    return out


def compare_numeric(py: dict, rs: dict, tol: dict, stats: dict) -> list[str]:
    diffs = []
    for eid in sorted(set(py) | set(rs)):
        a, b = py.get(eid), rs.get(eid)
        if a is None or b is None:
            continue  # the row diff reports the missing entry
        ea, eb = a["embedding"], b["embedding"]
        if ea and eb:
            mad = max(abs(x - y) for x, y in zip(ea, eb))
            cos = sum(x * y for x, y in zip(ea, eb)) / (
                math.sqrt(sum(x * x for x in ea)) * math.sqrt(sum(y * y for y in eb)))
            stats["max_abs_diff"] = max(stats.get("max_abs_diff", 0.0), mad)
            stats["min_cosine"] = min(stats.get("min_cosine", 1.0), cos)
            if tol and (mad > tol["embedding_max_abs_diff"] or cos < tol["embedding_min_cosine"]):
                diffs.append(f"entries[{eid}].embedding: max abs diff {mad:.3e}, cosine {cos:.9f}")
        sa, sb = a["surprise"], b["surprise"]
        if (sa is None) != (sb is None):
            diffs.append(f"entries[{eid}].surprise: python {sa} vs rust {sb}")
        elif sa is not None:
            d = abs(sa - sb)
            stats["surprise_max_diff"] = max(stats.get("surprise_max_diff", 0.0), d)
            if tol and d > tol["surprise_abs"]:
                diffs.append(f"entries[{eid}].surprise: python {sa} vs rust {sb}")
    return diffs


def compare_response(py: dict, rs: dict, tol: dict, stats: dict) -> list[str]:
    a, b = json.loads(json.dumps(py)), json.loads(json.dumps(rs))
    out = []
    va, vb = a.get("value"), b.get("value")
    if isinstance(va, dict) and isinstance(vb, dict) and "surprise" in va and "surprise" in vb:
        d = abs(float(va["surprise"]) - float(vb["surprise"]))
        stats["response_surprise_max_diff"] = max(stats.get("response_surprise_max_diff", 0.0), d)
        # The response is rounded to 4 dp, so one rounding unit is allowed
        # on top of the stored tolerance; the decision (`reason`) stays exact.
        if tol and d > tol["surprise_abs"] + 1e-4:
            out.append(f"surprise: python {va['surprise']} vs rust {vb['surprise']}")
        va["surprise"] = vb["surprise"] = "<surprise>"
    out += w1a.diff_values(a, b)
    return out


# ── running ───────────────────────────────────────────────────────────────

def side_env(scn: dict, dsn: str, extra: dict | None = None) -> dict:
    env = {"PSEUDOLIFE_RELEASE_CHECK": "0", "PSEUDOLIFE_PLUGIN_DIR": str(w1a.REPO / "plugin"),
           "PSEUDOLIFE_MCP_DATABASE_URL": dsn, "PSEUDOLIFE_MCP_TOKEN": TOKEN}
    env.update(scn.get("env", {}))
    env.update(extra or {})
    return env


def run_op(op: dict, port: int, mcp: Mcp | None, dsn: str, rust: bool) -> dict | None:
    if op["op"] == "sleep":
        time.sleep(op["s"])
        return {"isError": False, "value": None}
    if op["op"] == "http":
        body = json.dumps(op["body"]).encode() if "body" in op else None
        hdrs = [AUTH] + ([("Content-Type", "application/json")] if body else [])
        r = w1a.call(port, op["method"], op["path"], hdrs, body)
        return {"isError": r["status"] != 200, "value": r.get("json", r.get("bytes"))}
    args = resolve_args(op["args"], dsn)
    headers = [tuple(h) for h in op.get("headers", [])]
    if op.get("abandon"):
        if rust:
            abandon(port, f"/api/_harness/tool/{op['name']}",
                    [AUTH, ("Content-Type", "application/json"), *headers],
                    json.dumps(args).encode())
        else:
            mcp.abandon(op["name"], args, headers)
        time.sleep(4)  # let a call that survives the disconnect finish
        return {"isError": False, "value": "<abandoned>"}
    if rust:
        return rust_tool(port, op["name"], args, headers)
    return mcp.call(op["name"], args, headers)


def prepare_template(scn: dict, tag: str, root: Path, owned: list[str]) -> str:
    template = pg.name(f"pl_cf_w2e_{tag}_t")
    owned.append(template)
    dsn = pg.create(template)
    home = daemons.make_home(root, f"{tag}-seed", scn.get("config"))
    d = daemons.python_daemon(home, daemons.free_port(),
                              daemons.base_env(home, side_env(scn, dsn, scn.get("seed_env"))))
    try:
        d.start(300)
        w1a.wait_settled([d.port], token=TOKEN)
        mcp = Mcp(d.port)
        mcp.init()
        for op in scn["seed"]:
            r = run_op(op, d.port, mcp, dsn, rust=False)
            if r and r["isError"]:
                raise RuntimeError(f"seed op failed: {op} -> {r}")
    finally:
        d.stop()
    return template


def run_scenario(name: str, binary: Path, root: Path, mode: str, record: bool,
                 tol: dict, mutant: str | None = None) -> dict:
    owned = []
    try:
        return _run_scenario(name, binary, root, mode, record, tol, mutant, owned)
    finally:
        for database in reversed(owned):
            pg.drop(database)


def _run_scenario(name: str, binary: Path, root: Path, mode: str, record: bool,
                  tol: dict, mutant: str | None, owned: list[str]) -> dict:
    scn = SCENARIOS[name]
    tag = name.replace("-", "_")
    if mode == "golden":
        return run_golden(name, binary, root, tol, owned)
    template = prepare_template(scn, tag, root, owned)
    dbs = {side: pg.name(f"pl_cf_w2e_{tag}_{suffix}")
           for side, suffix in (("python", "py"), ("rust", "rs"))}
    owned.extend(dbs.values())
    dsns = {k: pg.create(v, template=template) for k, v in dbs.items()}
    before = {k: dbstate.dump(dsn) for k, dsn in dsns.items()}
    procs = {}
    steps, stats = [], {}
    try:
        home = daemons.make_home(root, f"{tag}-py", scn.get("config"))
        procs["python"] = daemons.python_daemon(home, daemons.free_port(),
                                                daemons.base_env(home, side_env(scn, dsns["python"])))
        renv = w1a.rust_env(side_env(scn, dsns["rust"]))
        if mutant:
            renv["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
        home = daemons.make_home(root, f"{tag}-rs", scn.get("config"))
        procs["rust"] = daemons.rust_daemon(binary, home, daemons.free_port(), daemons.base_env(home, renv))
        for d in procs.values():
            d.start(300)
        w1a.wait_settled([d.port for d in procs.values()], token=TOKEN)
        mcp = Mcp(procs["python"].port)
        mcp.init()
        for i, op in enumerate([{"op": "init"}] + scn["ops"]):
            row = {"step": i, "op": op, "diffs": []}
            if op["op"] != "init":
                py = run_op(op, procs["python"].port, mcp, dsns["python"], rust=False)
                rs = run_op(op, procs["rust"].port, None, dsns["rust"], rust=True)
                row["python"], row["rust"] = py, rs
            sp, np_, ep = normalize_state(dbstate.dump(dsns["python"]), before["python"])
            sr, nr, er = normalize_state(dbstate.dump(dsns["rust"]), before["rust"])
            if op["op"] != "init":
                row["python"] = rename_prefixes(row["python"], ep)
                row["rust"] = rename_prefixes(row["rust"], er)
                row["diffs"] += compare_response(row["python"], row["rust"], tol, stats)
            row["diffs"] += compare_numeric(np_, nr, tol, stats)
            row["diffs"] += [f"db {d}" for d in state_diff(sp, sr)]
            steps.append(row)
            if record:
                row["_python_state"], row["_python_numeric"] = sp, np_
    finally:
        for d in procs.values():
            d.stop()
    result = {"scenario": name, "mutant": mutant, "steps": steps, "stats": stats}
    if record:
        GOLDENS.mkdir(parents=True, exist_ok=True)
        seen: set = set()
        (GOLDENS / f"{name}.json").write_text(json.dumps(
            {"scenario": name, "normalizers": "harness/w2e.py normalize_state, pack",
             "steps": [{"op": s["op"], "python": s.get("python"),
                        "state": pack(s["_python_state"], full=s["step"] == 0),
                        "numeric_new": pack_numeric(s["_python_numeric"], seen)} for s in steps]},
            indent=1, sort_keys=True), encoding="utf-8")
        for s in steps:
            s.pop("_python_state", None)
            s.pop("_python_numeric", None)
    return result


# Tables the write path touches: recorded every step (the rest only at init).
WRITE_TABLES = ("entries", "episodes", "meta", "client_sessions")


def pack(state: dict, full: bool) -> dict:
    if full:
        return state
    return {"rows": {f"public.{t}": state["rows"][f"public.{t}"] for t in WRITE_TABLES
                     if f"public.{t}" in state["rows"]},
            "catalog": {"sequences": state["catalog"]["sequences"]}}


def pack_numeric(numeric: dict, seen: set) -> dict:
    """New entries only, embeddings as base64 little-endian float32."""
    import base64
    import struct
    out = {}
    for eid, v in numeric.items():
        if eid in seen:
            continue
        seen.add(eid)
        emb = v["embedding"]
        out[str(eid)] = {"surprise": v["surprise"], "embedding_f32": base64.b64encode(
            struct.pack(f"<{len(emb)}f", *emb)).decode() if emb else None}
    return out


def unpack_numeric(packed: dict, into: dict) -> dict:
    import base64
    import struct
    for eid, v in packed.items():
        raw = base64.b64decode(v["embedding_f32"]) if v["embedding_f32"] else None
        into[int(eid)] = {"surprise": v["surprise"],
                          "embedding": list(struct.unpack(f"<{len(raw) // 4}f", raw)) if raw else None}
    return into


def run_golden(name: str, binary: Path, root: Path, tol: dict, owned: list[str]) -> dict:
    """Replay a recorded scenario against the Rust daemon alone, from a bank
    its own constructor builds. Seeded scenarios need the Python daemon to
    seed and are skipped (declared)."""
    scn = SCENARIOS[name]
    if scn["seed"]:
        return {"scenario": name, "skipped": "seeded through the Python daemon", "steps": [],
                "stats": {}}
    golden = json.loads((GOLDENS / f"{name}.json").read_text(encoding="utf-8"))
    tag = name.replace("-", "_")
    db = pg.name(f"pl_cf_w2e_{tag}_rs")
    owned.append(db)
    dsn = pg.create(db)
    before = dbstate.dump(dsn)
    home = daemons.make_home(root, f"{tag}-rs", scn.get("config"))
    d = daemons.rust_daemon(binary, home, daemons.free_port(),
                            daemons.base_env(home, w1a.rust_env(side_env(scn, dsn))))
    steps, stats, numeric = [], {}, {}
    try:
        # The oracle's template was booted once to seed it and once to run,
        # so its writer-lease epoch is 2: boot the Rust bank twice as well.
        d.start(300)
        w1a.wait_settled([d.port], token=TOKEN)
        d.stop()
        d.start(300)
        w1a.wait_settled([d.port], token=TOKEN)
        for i, rec in enumerate(golden["steps"]):
            op = rec["op"]
            row = {"step": i, "op": op, "diffs": []}
            rs = run_op(op, d.port, None, dsn, rust=True) if op["op"] != "init" else None
            sr, nr, er = normalize_state(dbstate.dump(dsn), before)
            if rs is not None:
                row["rust"] = rename_prefixes(rs, er)
                row["diffs"] += compare_response(rec["python"], row["rust"], tol, stats)
            unpack_numeric(rec["numeric_new"], numeric)
            row["diffs"] += compare_numeric(numeric, nr, tol, stats)
            row["diffs"] += [f"db {x}" for x in state_diff(rec["state"], pack(sr, full=i == 0))]
            steps.append(row)
    finally:
        d.stop()
    return {"scenario": name, "steps": steps, "stats": stats}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["live", "golden", "mutants", "calibrate"])
    ap.add_argument("--rust-bin", type=Path, required=True)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--record", action="store_true")
    args = ap.parse_args()
    args.rust_bin = args.rust_bin.resolve()
    root = daemons.scratch_root() / "w2e"
    root.mkdir(parents=True, exist_ok=True)
    names = args.only or list(SCENARIOS)
    tol = {} if args.mode == "calibrate" else json.loads(TOLERANCE_FILE.read_text(encoding="utf-8"))
    results = []
    if args.mode == "mutants":
        caught = {}
        for m in MUTANTS:
            diffs = 0
            for n in names:
                r = run_scenario(n, args.rust_bin, root, "live", False, tol, mutant=m)
                diffs += sum(len(s["diffs"]) for s in r["steps"])
            caught[m] = diffs
            print(f"mutant {m}: {diffs} diffs")
        summary = {"mutants": caught, "uncaught": [m for m, d in caught.items() if d == 0]}
        print(json.dumps(summary))
        if args.out:
            args.out.write_text(json.dumps(summary, indent=1), encoding="utf-8")
        return 1 if summary["uncaught"] else 0
    for n in names:
        r = run_scenario(n, args.rust_bin, root, "golden" if args.mode == "golden" else "live",
                         args.record, tol)
        results.append(r)
        for s in r["steps"]:
            for d in s["diffs"][:6]:
                print(f"DIFF [{n}] step {s['step']} {json.dumps(s['op'])[:90]}: {d}")
    steps = sum(len(r["steps"]) for r in results)
    diff_steps = sum(1 for r in results for s in r["steps"] if s["diffs"])
    stats = {}
    for r in results:
        for k, v in r["stats"].items():
            stats[k] = min(stats.get(k, v), v) if k == "min_cosine" else max(stats.get(k, v), v)
    summary = {"scenarios": len(results), "steps": steps, "diff_steps": diff_steps, "stats": stats}
    print(json.dumps(summary))
    if args.out:
        args.out.write_text(json.dumps({"summary": summary, "scenarios": results}, indent=1,
                                       default=str), encoding="utf-8")
    if args.mode == "calibrate":
        return 0
    return 1 if diff_steps else 0


if __name__ == "__main__":
    raise SystemExit(main())

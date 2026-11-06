"""Commit-loss fault injection: what each daemon serves when PostgreSQL
commits a write and the daemon never receives the COMMIT reply.

usage:
  python commit_loss.py --rust-bin PATH [--only CASE ...] [--engines python,rust]
                        [--out FILE] [--report FILE] [--keep]
  python commit_loss.py --smoke        (the proxy alone, against plain psycopg)

A TCP proxy (``CommitCutProxy``, asyncio in a background thread) sits
between a daemon and the test PostgreSQL. It frames the frontend stream
(the untyped startup message, then type + length messages) and watches
simple ``Query`` and extended ``Parse``/``Bind`` messages. Armed with a
case's marker (a regex over the statements one transaction sent since its
``BEGIN``), it forwards that transaction's ``COMMIT``, forwards the
server's replies up to the ``CommandComplete`` that answers it, drops that
message and closes both sockets: the server has committed, the client
never learns it. It fires once, then disarms itself. ``SSLRequest`` and
``GSSENCRequest`` are refused by the proxy itself (``N``), so the stream
stays plaintext; daemon DSNs to the proxy carry ``sslmode=disable``.

Each case prepares a template bank through the Python daemon's own tools
(plus declared SQL seeds after the seeding daemon stopped, for rows only
the dream or curation writes), clones it per engine, and then for each
engine:

* (a) arms the proxy and runs the operation: the tool answer;
* (b) what the daemon serves right after (``memory_search`` for each
  watched text, ``memory_get`` of each known id, ``memory_lesson_search`` /
  ``memory_world_search`` where the case watches a lesson or world fact);
* (c) the durable truth, read directly from the bank (not through the
  proxy);
* (d) one more unrelated call (a ``memory_store`` of an unrelated note),
  then the same probes and truth;
* a graceful stop (SIGTERM; the exit flush runs), then the truth again;
* (e) a full restart on a direct DSN, then the probes and truth.

None of the probe reads are pure (``memory_get`` reinforces, a search logs
a retrieval event); the first probe after the fault is also what triggers
the reconnect on both engines, so (b) and (d) may agree.

A case whose proxy never fired is invalid (``fault.fired: false``), not a
pass. Results: a JSON file and a markdown report (paths printed). Never
wired into CI; stand-alone evidence for the commit-unknown divergence rows.

Environment: the test login (``pgdisposable``); ``ORT_DYLIB_PATH`` and
``PSEUDOLIFE_DAEMON_ONNX_DIR`` for the Rust daemon.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import http.client
import itertools
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daemons  # noqa: E402
import pgdisposable  # noqa: E402
import run as w1a  # noqa: E402

pg = pgdisposable.Slice("w7c")
TOKEN = "tok-default-cl-0001"
REVIEWER_TOKEN = "tok-rev-cl-0002"
AUTH = ("Authorization", f"Bearer {TOKEN}")
REVIEWER = ("Authorization", f"Bearer {REVIEWER_TOKEN}")
SSL_REQUEST, GSSENC_REQUEST = 80877103, 80877104


# ── the proxy ─────────────────────────────────────────────────────────────

def _cstrings(body: bytes, n: int) -> list[str]:
    out, pos = [], 0
    for _ in range(n):
        end = body.find(b"\0", pos)
        if end < 0:
            break
        out.append(body[pos:end].decode("utf-8", "replace"))
        pos = end + 1
    return out


def _bare(sql: str) -> str:
    return sql.strip().rstrip(";").strip().upper()


class _Conn:
    def __init__(self, cid: int):
        self.cid = cid
        self.statements: list[str] = []   # since the last BEGIN / COMMIT / ROLLBACK
        self.marked = False
        self.prepared: dict[str, str] = {}
        self.cut_pending = False
        self.cut_statements: list[str] = []
        self.closed = False


class CommitCutProxy:
    """See the module docstring. ``arm(marker)`` / ``disarm()`` from the
    harness thread; ``events`` records every fire."""

    def __init__(self, upstream_host: str, upstream_port: int):
        self.upstream = (upstream_host, upstream_port)
        self.loop = asyncio.new_event_loop()
        self.lock = threading.Lock()
        self.marker: re.Pattern | None = None
        self.events: list[dict] = []
        self.fired = threading.Event()
        self.ids = itertools.count(1)
        self.connections = 0
        self.port = 0
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._server = None

    def start(self) -> "CommitCutProxy":
        self._thread.start()
        fut = asyncio.run_coroutine_threadsafe(
            asyncio.start_server(self._handle, "127.0.0.1", 0), self.loop)
        self._server = fut.result(10)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    def close(self) -> None:
        if self._server is not None:
            self.loop.call_soon_threadsafe(self._server.close)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)

    def arm(self, marker: str) -> None:
        with self.lock:
            self.marker = re.compile(marker, re.I | re.S)
            self.fired.clear()

    def disarm(self) -> None:
        with self.lock:
            self.marker = None

    # Frontend inspection: decides whether this COMMIT is the one to cut.
    def _seen(self, conn: _Conn, sql: str) -> None:
        bare = _bare(sql)
        if bare in ("BEGIN", "START TRANSACTION") or bare.startswith(("BEGIN ", "START TRANSACTION ")):
            conn.statements, conn.marked = [], False
            return
        if bare in ("COMMIT", "END", "COMMIT TRANSACTION"):
            with self.lock:
                fire = self.marker is not None and conn.marked
                if fire:
                    self.marker = None
            if fire:
                conn.cut_pending = True
                conn.cut_statements = list(conn.statements)
            conn.statements, conn.marked = [], False
            return
        if bare in ("ROLLBACK", "ABORT"):
            conn.statements, conn.marked = [], False
            return
        if len(conn.statements) < 200:
            conn.statements.append(sql.strip()[:240])
        with self.lock:
            marker = self.marker
        if marker is not None and marker.search(sql):
            conn.marked = True

    def _inspect(self, conn: _Conn, typ: bytes, body: bytes) -> None:
        if typ == b"Q":
            self._seen(conn, body.rstrip(b"\0").decode("utf-8", "replace"))
        elif typ == b"P":
            name, query = (_cstrings(body, 2) + ["", ""])[:2]
            conn.prepared[name] = query
            if name == "":
                # The unnamed statement runs at its Bind; a named one may
                # be bound many times, so both are judged at Bind.
                return
        elif typ == b"B":
            _portal, stmt = (_cstrings(body, 2) + ["", ""])[:2]
            if stmt in conn.prepared:
                self._seen(conn, conn.prepared[stmt])

    async def _handle(self, creader, cwriter):
        conn = _Conn(next(self.ids))
        self.connections += 1
        try:
            sreader, swriter = await asyncio.open_connection(*self.upstream)
        except OSError:
            cwriter.close()
            return

        def shut():
            if conn.closed:
                return
            conn.closed = True
            for w in (cwriter, swriter):
                try:
                    w.close()
                except Exception:  # noqa: BLE001
                    pass

        async def c2s():
            buf, startup = b"", True
            try:
                while not conn.closed:
                    data = await creader.read(65536)
                    if not data:
                        break
                    buf += data
                    out = bytearray()
                    while True:
                        if startup:
                            if len(buf) < 8:
                                break
                            n = int.from_bytes(buf[:4], "big")
                            if len(buf) < n:
                                break
                            msg, buf = buf[:n], buf[n:]
                            code = int.from_bytes(msg[4:8], "big")
                            if n == 8 and code in (SSL_REQUEST, GSSENC_REQUEST):
                                cwriter.write(b"N")
                                await cwriter.drain()
                                continue
                            out += msg
                            startup = False
                            continue
                        if len(buf) < 5:
                            break
                        n = int.from_bytes(buf[1:5], "big")
                        if len(buf) < 1 + n:
                            break
                        msg, buf = buf[:1 + n], buf[1 + n:]
                        self._inspect(conn, msg[:1], msg[5:])
                        out += msg
                    if out:
                        swriter.write(bytes(out))
                        await swriter.drain()
            except (OSError, ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                if not conn.cut_pending:
                    shut()

        async def s2c():
            buf = b""
            try:
                while not conn.closed:
                    data = await sreader.read(65536)
                    if not data:
                        break
                    buf += data
                    out = bytearray()
                    while len(buf) >= 5:
                        n = int.from_bytes(buf[1:5], "big")
                        if len(buf) < 1 + n:
                            break
                        msg, buf = buf[:1 + n], buf[1 + n:]
                        if conn.cut_pending and msg[:1] in (b"C", b"E"):
                            if msg[:1] == b"C":
                                tag = msg[5:].rstrip(b"\0").decode("utf-8", "replace")
                            else:
                                tag = "ErrorResponse"
                            if tag.startswith(("COMMIT", "ROLLBACK")):
                                if out:
                                    cwriter.write(bytes(out))
                                    await cwriter.drain()
                                self.events.append({
                                    "connection": conn.cid, "server_tag": tag,
                                    "transaction_statements": conn.cut_statements,
                                    "at": time.time()})
                                self.fired.set()
                                shut()
                                return
                            if tag == "ErrorResponse":
                                # The COMMIT itself failed: nothing to cut.
                                self.events.append({
                                    "connection": conn.cid, "server_tag": tag,
                                    "transaction_statements": conn.cut_statements,
                                    "at": time.time(), "note": "server refused the COMMIT"})
                                conn.cut_pending = False
                                self.fired.set()
                        out += msg
                    if out:
                        cwriter.write(bytes(out))
                        await cwriter.drain()
            except (OSError, ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                shut()

        await asyncio.gather(c2s(), s2c())


def proxied_dsn(dsn: str, port: int) -> str:
    parts = urlsplit(dsn)
    netloc = parts.netloc.rsplit("@", 1)
    netloc = f"{netloc[0]}@127.0.0.1:{port}" if len(netloc) == 2 else f"127.0.0.1:{port}"
    query = "&".join(q for q in [parts.query, "sslmode=disable"] if q)
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def upstream() -> tuple[str, int]:
    host, port = pgdisposable.HOST_PORT.rsplit(":", 1)
    return host, int(port)


# ── MCP client ────────────────────────────────────────────────────────────

class Mcp:
    """Minimal streamable-HTTP MCP client: initialize once, then tools/call."""

    def __init__(self, port: int):
        self.port, self.session, self.ids = port, None, 0

    def _post(self, payload: dict, extra=()) -> dict | None:
        headers = {"Authorization": AUTH[1], "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        headers.update(dict(extra))
        if self.session:
            headers["mcp-session-id"] = self.session
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=300)
        c.request("POST", "/mcp", body=json.dumps(payload).encode(), headers=headers)
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
            "clientInfo": {"name": "commit-loss-harness", "version": "0"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, name: str, args: dict, headers=()) -> dict:
        self.ids += 1
        try:
            msg = self._post({"jsonrpc": "2.0", "id": self.ids, "method": "tools/call",
                              "params": {"name": name, "arguments": args}}, headers)
        except Exception as exc:  # noqa: BLE001  a transport failure is an answer here
            return {"isError": True, "value": f"transport: {type(exc).__name__}: {exc}"}
        if msg is None:
            return {"isError": True, "value": "no JSON-RPC answer"}
        if "error" in msg:
            return {"isError": True, "value": {"jsonrpc_error": msg["error"]}}
        res = msg["result"]
        value = res.get("structuredContent")
        if value is None:
            text = (res.get("content") or [{}])[0].get("text", "")
            try:
                value = json.loads(text)
            except (TypeError, ValueError):
                value = text
        return {"isError": bool(res.get("isError")), "value": value}


# ── seeds (the same shapes as w2e.py) ────────────────────────────────────

def tool(name, headers=(), **args) -> dict:
    return {"op": "tool", "name": name, "args": args, "headers": list(headers)}


def store(text, **kw) -> dict:
    return tool("memory_store", text=text, **kw)


def sql(statement: str) -> dict:
    """Seed-only: run after the seeding daemon stopped (its exit flush
    rewrites the slots it holds)."""
    return {"op": "sql", "sql": statement}


def _q(v):
    return "NULL" if v is None else "'" + v.replace("'", "''") + "'"


def lineage_lesson(task, aspect, task_norm, aspect_norm, lesson, source_texts,
                   embedding_text) -> list[dict]:
    """w2e.lineage_lesson: a current lesson whose provenance and synthesis
    lineage audit name the entries with ``source_texts``."""
    texts = ", ".join(_q(t) for t in source_texts)
    return [
        sql("INSERT INTO lessons (entity, attribute, entity_norm, attribute_norm, value, about, "
            "polarity, outcome, status, confidence, origin, support, provenance, asserted_at, "
            "last_confirmed, embedding) SELECT "
            f"{_q(task)}, {_q(aspect)}, {_q(task_norm)}, {_q(aspect_norm)}, {_q(lesson)}, NULL, "
            "'+', 'success', 'current', 0.7, 'agent', '[\"signal:1\"]', "
            "(SELECT jsonb_agg('entry:' || id ORDER BY id) FROM entries "
            f"WHERE text IN ({texts})), 1700000000, 1700000000, embedding "
            f"FROM entries WHERE text = {_q(embedding_text)}"),
        sql("INSERT INTO store_decisions (store, entity_norm, attribute_norm, action, "
            "decided_by, reason, record, decided_at) SELECT 'lesson', "
            f"{_q(task_norm)}, {_q(aspect_norm)}, 'lineage', 'lesson_synthesis', "
            "'verified_dependencies', jsonb_build_object('lesson', "
            f"{_q(lesson)}, 'asserted_at', 1700000000, 'source_entry_ids', "
            f"(SELECT jsonb_agg(id ORDER BY id) FROM entries WHERE text IN ({texts}))), "
            "1700000000.5"),
    ]


def slot_norm(s: str) -> str:
    return re.sub(r"[\s._\-/]+", "-", s.strip().casefold()).strip("-")


def evidence_fp(record: dict) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False)
                          .encode("utf-8")).hexdigest()


def _lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, dict)):
        return "'" + json.dumps(v).replace("'", "''") + "'::jsonb"
    return "'" + str(v).replace("'", "''") + "'"


SLOT_STAMP = 1700000000.0
_SLOT_COMMON = ("entity", "attribute", "value", "polarity", "status", "confidence", "origin",
                "support", "provenance", "asserted_at", "last_confirmed", "supersedes_value",
                "superseded_by_value", "superseded_at", "tx_time", "valid_time", "hlc_phys",
                "hlc_logical", "writer_id", "session_id", "version")


def slot_world(entity, attribute, value, *, status="current", asserted=SLOT_STAMP,
               superseded_at=None, hlc_logical=0) -> dict:
    return {"entity": entity, "attribute": attribute, "value": value, "polarity": "+",
            "status": status, "confidence": 0.9, "origin": "source", "support": ["source"],
            "provenance": [], "asserted_at": asserted, "last_confirmed": asserted,
            "supersedes_value": None, "superseded_by_value": None,
            "superseded_at": superseded_at, "tx_time": asserted, "valid_time": asserted,
            "hlc_phys": int(asserted * 1000), "hlc_logical": hlc_logical, "writer_id": "seed",
            "session_id": None, "version": 1, "source_url": "https://example.org/host",
            "source_quote": value, "retrieved_at": SLOT_STAMP, "freshness_class": "evergreen",
            "content_hash": None, "source_doc_id": None}


def slot_row_sql(row: dict) -> dict:
    cols = dict(row, entity_norm=slot_norm(row["entity"]),
                attribute_norm=slot_norm(row["attribute"]))
    return sql(f"INSERT INTO world_facts ({', '.join(cols)}) "
               f"VALUES ({', '.join(_lit(v) for v in cols.values())})")


def slot_snapshot(row: dict, **changes) -> dict:
    row = dict(row, **changes)
    en, an = slot_norm(row["entity"]), slot_norm(row["attribute"])
    snap = {"store": "world", "key": f"{en}|{an}", "entity_norm": en, "attribute_norm": an}
    snap.update({k: row[k] for k in _SLOT_COMMON})
    snap.update({k: row[k] for k in ("source_url", "source_quote", "retrieved_at",
                                     "freshness_class", "content_hash", "source_doc_id")})
    snap["embedding"] = None
    return snap


def curated_retire(survivor: dict, loser: dict, decided_at=SLOT_STAMP + 500) -> dict:
    """w2e.curated_retire for a world pair kept on side a."""
    retired = dict(loser, status="retired", superseded_at=decided_at)
    record = dict(retired)
    record.update({
        "_curation_evidence_fingerprint": evidence_fp(loser),
        "_curation_retired_fingerprint": evidence_fp(retired),
        "_curation_duplicate": {"version": 1, "keep": "a", "a": survivor, "b": loser,
                                "a_fingerprint": evidence_fp(survivor),
                                "b_fingerprint": evidence_fp(loser),
                                "loser_embedding": None},
    })
    record.pop("embedding")
    return sql("INSERT INTO store_decisions (store, entity_norm, attribute_norm, action, "
               f"decided_by, reason, record, decided_at) VALUES ('world', "
               f"{_lit(loser['entity_norm'])}, {_lit(loser['attribute_norm'])}, 'retire', "
               f"'dream-curation', 'duplicate', {_lit(record)}, {decided_at!r})")


# ── cases ─────────────────────────────────────────────────────────────────

BACKUP = "The backup job runs at 02:00 nightly."
LUNCH = "A note about the team lunch near the office."
BACKUP_LESSON = "Run backups at night."
TTL = "The cache TTL is 300 seconds."
TTL_NEW = "The cache TTL is 600 seconds."
STAGE = ["The staging cluster runs three nodes.", "Staging cluster has three nodes running."]
STAGE_NEW = "The staging cluster runs three nodes in total."
HOST = slot_world("Curate Host", "OS", "Debian 12")
HOST_LOSER = slot_world("Curate Host", "Operating System", "Debian 12 (bookworm)",
                        asserted=SLOT_STAMP + 50, hlc_logical=1)

# Markers: a regex over the statements of the transaction to cut (both
# engines' SQL, checked against pseudolife_memory/storage/postgres.py and
# rust/daemon/src at the tested head).
RETIRE_ENTRY = r"UPDATE\s+entries\s+SET\s+superseded_at\s*=\s*(%s|\$\d)"
DELETE_ENTRY = r"DELETE\s+FROM\s+entries\s+WHERE\s+id\s*=\s*ANY"
REINSTATE_DECISION = r"INSERT\s+INTO\s+entry_reinstatement_decisions"
STORE_DECISION = r"INSERT\s+INTO\s+store_decisions"


def _reinstate_args(dsn: str) -> dict:
    import psycopg
    with psycopg.connect(dsn) as c:
        eid, text, source, sup, by = c.execute(
            "SELECT id, text, source, superseded_at, superseded_by_text FROM entries "
            "WHERE text = %s ORDER BY id LIMIT 1", (TTL,)).fetchone()
    h = lambda s: hashlib.sha256((s or "").encode("utf-8")).hexdigest()  # noqa: E731
    return {"entry_id": eid, "operation_id": "0f1e2d3c-4b5a-4968-8776-655443322110",
            "expected_text_sha256": h(text), "expected_source_sha256": h(source),
            "expected_superseded_at": sup, "expected_superseded_by_text_sha256": h(by),
            "evidence_packet_sha256": "ab" * 32, "reviewer_ids": ["reviewer-1"],
            "reason": "the replacement was wrong"}


CASES: dict[str, dict] = {
    "forget-lesson-source": {
        "about": "memory_forget(scope='memory') of the only entry a current lesson cites",
        "seed": [store(BACKUP), store(LUNCH),
                 *lineage_lesson("schedule backups", "timing", "schedule-backups", "timing",
                                 BACKUP_LESSON, [BACKUP], BACKUP)],
        "marker": DELETE_ENTRY,
        "op": lambda dsn: ("memory_forget", {"scope": "memory", "text": BACKUP}, ()),
        "entries": [BACKUP, LUNCH],
        "lessons": [{"query": "when should backups run", "value": BACKUP_LESSON}],
        "committed": lambda t: not t["entries"][BACKUP],
        "inconsistent_means": "the forgotten entry and its lesson's status disagree",
        "consistent": lambda t: (not t["entries"][BACKUP]) == (
            not any(r["status"] == "current" for r in t["lessons"][BACKUP_LESSON])),
    },
    "supersede": {
        "about": "memory_supersede of one live entry by exact text",
        "seed": [store(TTL)],
        "marker": RETIRE_ENTRY,
        "op": lambda dsn: ("memory_supersede", {"new_text": TTL_NEW, "old_text": TTL}, ()),
        "entries": [TTL, TTL_NEW],
        "committed": lambda t: _retired(t, TTL),
        "inconsistent_means": "half-applied supersession (old retired xor replacement live)",
        "consistent": lambda t: _retired(t, TTL) == _live(t, TTL_NEW),
    },
    "reinstate": {
        "about": "memory_reinstate of a superseded entry (reviewer principal)",
        "seed": [store(TTL), tool("memory_supersede", new_text=TTL_NEW, old_text=TTL)],
        "marker": REINSTATE_DECISION,
        "op": lambda dsn: ("memory_reinstate", _reinstate_args(dsn), (REVIEWER,)),
        "entries": [TTL, TTL_NEW],
        "committed": lambda t: _live(t, TTL),
        "inconsistent_means": "reinstatement decision and entry state disagree",
        "consistent": lambda t: _live(t, TTL) == (t["counts"]["reinstatement_decisions"] > 0),
    },
    "consolidate": {
        "about": "memory_consolidate of two live entries into one",
        "seed": [store(STAGE[0]), store(STAGE[1])],
        "marker": RETIRE_ENTRY,
        "op": lambda dsn: ("memory_consolidate",
                           {"new_text": STAGE_NEW, "replaces": list(STAGE)}, ()),
        "entries": [*STAGE, STAGE_NEW],
        "committed": lambda t: _retired(t, STAGE[0]),
        "inconsistent_means": "half-applied consolidation",
        "consistent": lambda t: _retired(t, STAGE[0]) == _retired(t, STAGE[1])
        == _live(t, STAGE_NEW),
    },
    "restore-world-legacy": {
        "about": "memory_graph_review(restore_slot) of a forgotten world fact (legacy path: "
                 "resident restore, one store_decisions txn per record, then the slot save); "
                 "the cut is the decision's COMMIT",
        "seed": [store("A note so the bank is not empty."),
                 tool("memory_world_set", entity="Rust", attribute="edition", value="2024",
                      source_url="https://doc.rust-lang.org/"),
                 tool("memory_forget", scope="world", entity="Rust", attribute="edition")],
        "marker": STORE_DECISION,
        "op": lambda dsn: ("memory_graph_review",
                           {"action": "restore_slot", "store": "world", "src": "rust|edition"},
                           ()),
        "entries": [],
        "world": [{"query": "rust edition", "entity_norm": "rust", "attribute_norm": "edition",
                   "value": "2024"}],
        "committed": lambda t: t["counts"]["restore_decisions"] > 0,
        "inconsistent_means": "restore decision recorded while the slot is not current",
        "consistent": lambda t: (t["counts"]["restore_decisions"] > 0) == _world_current(
            t, "rust", "edition"),
    },
    "restore-world-curated": {
        "about": "memory_graph_review(restore_slot) of a curated duplicate retirement (one "
                 "transaction: locked-row validation, slot rewrite, restore decision; "
                 "recovery: reconcile_restore / recover_slot_curation)",
        "seed": [store("A note so the bank is not empty."),
                 slot_row_sql(HOST),
                 slot_row_sql(dict(HOST_LOSER, status="retired",
                                   superseded_at=SLOT_STAMP + 500)),
                 curated_retire(slot_snapshot(HOST), slot_snapshot(HOST_LOSER))],
        "marker": STORE_DECISION,
        "op": lambda dsn: ("memory_graph_review",
                           {"action": "restore_slot", "store": "world",
                            "src": "curate host|operating system"}, ()),
        "entries": [],
        "world": [{"query": "Curate Host operating system", "entity_norm": "curate-host",
                   "attribute_norm": "operating-system", "value": "Debian 12 (bookworm)"}],
        "committed": lambda t: t["counts"]["restore_decisions"] > 0,
        "inconsistent_means": "restore decision recorded while the slot is not current",
        "consistent": lambda t: (t["counts"]["restore_decisions"] > 0) == _world_current(
            t, "curate-host", "operating-system"),
    },
}


def _live(t: dict, text: str) -> bool:
    return any(r["live"] for r in t["entries"].get(text, []))


def _retired(t: dict, text: str) -> bool:
    rows = t["entries"].get(text, [])
    return bool(rows) and not any(r["live"] for r in rows)


def _world_current(t: dict, en: str, an: str) -> bool:
    return any(r["status"] == "current" for r in t["world"].get(f"{en}|{an}", []))


# ── durable truth ─────────────────────────────────────────────────────────

def db_truth(case: dict, dsn: str) -> dict:
    import psycopg
    out = {"entries": {}, "lessons": {}, "world": {}, "counts": {}, "meta": {}}
    with psycopg.connect(dsn, autocommit=True) as c:
        for text in case["entries"]:
            rows = c.execute("SELECT id, superseded_at, superseded_by_text FROM entries "
                             "WHERE text = %s ORDER BY id", (text,)).fetchall()
            out["entries"][text] = [{"id": r[0], "live": r[1] is None,
                                     "superseded_by_text": r[2]} for r in rows]
        for w in case.get("lessons", []):
            rows = c.execute("SELECT id, status, superseded_by_value FROM lessons "
                             "WHERE value = %s ORDER BY id", (w["value"],)).fetchall()
            out["lessons"][w["value"]] = [{"id": r[0], "status": r[1],
                                           "superseded_by_value": r[2]} for r in rows]
        for w in case.get("world", []):
            rows = c.execute("SELECT id, value, status FROM world_facts WHERE entity_norm = %s "
                             "AND attribute_norm = %s ORDER BY id",
                             (w["entity_norm"], w["attribute_norm"])).fetchall()
            out["world"][f"{w['entity_norm']}|{w['attribute_norm']}"] = [
                {"id": r[0], "value": r[1], "status": r[2]} for r in rows]
        one = lambda q: c.execute(q).fetchone()[0]  # noqa: E731
        out["counts"] = {
            "entries": one("SELECT count(*) FROM entries"),
            "live_entries": one("SELECT count(*) FROM entries WHERE superseded_at IS NULL"),
            "reinstatement_decisions": one("SELECT count(*) FROM entry_reinstatement_decisions"),
            "trace_invalidations": one("SELECT count(*) FROM memory_trace_invalidations"),
            "restore_decisions": one("SELECT count(*) FROM store_decisions "
                                     "WHERE action = 'restore'"),
            "store_decisions": one("SELECT count(*) FROM store_decisions"),
            "current_lessons": one("SELECT count(*) FROM lessons WHERE status = 'current'"),
            "current_world": one("SELECT count(*) FROM world_facts WHERE status = 'current'"),
        }
        out["meta"] = {k: v for k, v in c.execute(
            "SELECT key, md5(value::text) FROM meta ORDER BY key").fetchall()}
    return out


# ── what the daemon serves ───────────────────────────────────────────────

def _walk(v):
    if isinstance(v, dict):
        yield v
        for x in v.values():
            yield from _walk(x)
    elif isinstance(v, list):
        for x in v:
            yield from _walk(x)


def _entry_hits(answer, text: str) -> list[dict]:
    return [d for d in _walk(answer) if isinstance(d.get("text"), str)
            and d["text"].strip() == text]


def _retired_marked(d: dict) -> bool:
    return bool(d.get("superseded") or d.get("replaced_by") or d.get("superseded_at")
                or d.get("faded"))


def _get_status(r: dict) -> str:
    if r["isError"]:
        return "error"
    v = r["value"]
    if not isinstance(v, dict):
        return "error"
    if v.get("found") is False or v.get("faded"):
        return "absent"
    return "retired" if _retired_marked(v) else "live"


def _value_hits(answer, value: str) -> list[dict]:
    return [d for d in _walk(answer) if value in [x for x in d.values() if isinstance(x, str)]]


def probe(mcp: Mcp, case: dict, known_ids: dict[str, set[int]]) -> dict:
    out = {"entries": {}, "lessons": {}, "world": {}, "errors": []}
    for text in case["entries"]:
        s = mcp.call("memory_search", {"query": text, "top_k": 5})
        hits = _entry_hits(s["value"], text) if not s["isError"] else []
        gets = {}
        for eid in sorted(known_ids.get(text, ())):
            g = mcp.call("memory_get", {"entry_id": eid})
            gets[str(eid)] = {"status": _get_status(g),
                              "answer": g["value"] if g["isError"] else
                              {k: g["value"].get(k) for k in ("found", "faded", "superseded",
                                                              "replaced_by", "text")
                               if isinstance(g["value"], dict) and k in g["value"]}}
            if g["isError"]:
                out["errors"].append(f"memory_get {eid}: {g['value']}")
        out["entries"][text] = {
            "search_error": s["value"] if s["isError"] else None,
            "search_live": any(not _retired_marked(h) for h in hits),
            "search_hits": [{"id": h.get("id"), "retired_marked": _retired_marked(h)}
                            for h in hits],
            "get": gets}
        if s["isError"]:
            out["errors"].append(f"memory_search {text!r}: {s['value']}")
    for w in case.get("lessons", []):
        r = mcp.call("memory_lesson_search", {"query": w["query"]})
        hits = _value_hits(r["value"], w["value"]) if not r["isError"] else []
        out["lessons"][w["value"]] = {
            "error": r["value"] if r["isError"] else None,
            "served_current": any(h.get("status") in (None, "current") for h in hits),
            "hits": [{k: h.get(k) for k in ("status", "superseded_by_value") if k in h}
                     for h in hits]}
        if r["isError"]:
            out["errors"].append(f"memory_lesson_search: {r['value']}")
    for w in case.get("world", []):
        # The resident store's current records by slot (``world_dump``, in
        # both daemons): a seeded row without an embedding never ranks in
        # memory_world_search, so the search alone cannot see it.
        dump = w1a.call(mcp.port, "GET", "/api/world?limit=500", [AUTH], None, timeout=60)
        rows = (dump.get("json") or {}).get("entries", []) if dump["status"] == 200 else []
        current = [row for row in rows if isinstance(row, dict)
                   and slot_norm(str(row.get("entity", ""))) == w["entity_norm"]
                   and slot_norm(str(row.get("attribute", ""))) == w["attribute_norm"]
                   and row.get("value") == w["value"]]
        r = mcp.call("memory_world_search", {"query": w["query"]})
        hits = _value_hits(r["value"], w["value"]) if not r["isError"] else []
        out["world"][f"{w['entity_norm']}|{w['attribute_norm']}"] = {
            "error": None if dump["status"] == 200 else f"/api/world answered {dump['status']}",
            "served_current": bool(current),
            "search_served": any(h.get("status") in (None, "current") for h in hits),
            "search_hits": [{k: h.get(k) for k in ("status", "value") if k in h} for h in hits]}
        if dump["status"] != 200:
            out["errors"].append(f"/api/world: {dump['status']}")
        if r["isError"]:
            out["errors"].append(f"memory_world_search: {r['value']}")
    return out


def compare(case: dict, served: dict, truth: dict) -> list[str]:
    """Each way the served state differs from the durable truth."""
    diffs = []
    for text in case["entries"]:
        rows = truth["entries"].get(text, [])
        by_id = {str(r["id"]): ("live" if r["live"] else "retired") for r in rows}
        s = served["entries"].get(text, {})
        db_live = any(r["live"] for r in rows)
        if s.get("search_error") is None and s.get("search_live") != db_live:
            diffs.append(f"entry {text!r}: search serves it "
                         f"{'live' if s.get('search_live') else 'not live'}, "
                         f"the bank holds it {'live' if db_live else 'not live'}")
        for eid, g in (s.get("get") or {}).items():
            want = by_id.get(eid, "absent")
            if g["status"] != want:
                diffs.append(f"entry {text!r} id {eid}: memory_get serves {g['status']}, "
                             f"the bank holds {want}")
            pointer = g["answer"].get("replaced_by") if isinstance(g["answer"], dict) else None
            row = next((r for r in rows if str(r["id"]) == eid), None)
            if isinstance(pointer, dict) and row and row["superseded_by_text"]:
                succ = [r for t2, rs in truth["entries"].items()
                        if t2 == row["superseded_by_text"] for r in rs if r["live"]]
                if succ and (pointer.get("id") != succ[0]["id"] or not pointer.get("current")):
                    diffs.append(f"entry {text!r} id {eid}: memory_get's replaced_by is "
                                 f"{{id: {pointer.get('id')}, current: {pointer.get('current')}}}, "
                                 f"the bank holds the replacement as id {succ[0]['id']}, live")
    for value, s in served["lessons"].items():
        db_cur = any(r["status"] == "current" for r in truth["lessons"].get(value, []))
        if s["error"] is None and s["served_current"] != db_cur:
            diffs.append(f"lesson {value!r}: served {'current' if s['served_current'] else 'not current'}, "
                         f"the bank holds it {'current' if db_cur else 'not current'}")
    for key, s in served["world"].items():
        en, an = key.split("|")
        db_cur = _world_current(truth, en, an)
        if s["error"] is None and s["served_current"] != db_cur:
            diffs.append(f"world {key}: served {'current' if s['served_current'] else 'not current'}, "
                         f"the bank holds it {'current' if db_cur else 'not current'}")
    diffs += [f"probe error: {e}" for e in served["errors"]]
    return diffs


def duplicates(truth: dict) -> list[str]:
    """Rows a retry or a replayed write may have doubled."""
    out = [f"{len(rows)} rows hold {text!r}" for text, rows in truth["entries"].items()
           if len(rows) > 1]
    for key in ("reinstatement_decisions", "restore_decisions"):
        if truth["counts"][key] > 1:
            out.append(f"{truth['counts'][key]} {key.replace('_', ' ')}")
    return out


def consistent(case: dict, truth: dict) -> bool:
    return case["consistent"](truth) and not duplicates(truth)


def note_ids(known: dict[str, set[int]], truth: dict) -> None:
    for text, rows in truth["entries"].items():
        known.setdefault(text, set()).update(r["id"] for r in rows)


# ── one case on one engine ───────────────────────────────────────────────

def base_side_env(dsn: str) -> dict:
    return {"PSEUDOLIFE_RELEASE_CHECK": "0", "PSEUDOLIFE_PLUGIN_DIR": str(w1a.REPO / "plugin"),
            "PSEUDOLIFE_MCP_DATABASE_URL": dsn, "PSEUDOLIFE_MCP_TOKEN": TOKEN,
            "PSEUDOLIFE_MCP_TOKENS": f"{REVIEWER_TOKEN}:reviewer"}


def make_daemon(engine: str, binary: Path | None, home: Path, dsn: str) -> daemons.Daemon:
    env = base_side_env(dsn)
    port = daemons.free_port()
    if engine == "python":
        d = daemons.python_daemon(home, port, daemons.base_env(home, env))
        # A returning signal handler: uvicorn's lifespan shutdown (and the
        # exit flush) runs on SIGTERM, as in w2e.configure_clean_exit.
        d.argv = [sys.executable, "-c", (
            "import signal\n"
            "for sig in (signal.SIGTERM, getattr(signal, 'SIGBREAK', signal.SIGTERM)):\n"
            "    signal.signal(sig, lambda *_: None)\n"
            "from pseudolife_memory.daemon import run_daemon\n"
            "run_daemon()\n")]
        return d
    return daemons.rust_daemon(binary, home, port, daemons.base_env(home, w1a.rust_env(env)))


def graceful_stop(d: daemons.Daemon) -> dict:
    if d.proc is None or d.proc.poll() is not None:
        return {"clean": False, "note": f"already exited ({d.proc.returncode if d.proc else None})"}
    d.proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
    try:
        code = d.proc.wait(timeout=120)
    except Exception:  # noqa: BLE001
        d.stop()
        return {"clean": False, "note": "did not exit within 120s; stopped by PID"}
    d.stop()
    return {"clean": code == 0, "exit_code": code}


def start(d: daemons.Daemon) -> None:
    d.start(300)
    w1a.wait_settled([d.port], token=TOKEN)


LOG_LINES = re.compile(r"reconnect|connection lost|commit|recover|reconcil|rollback|"
                       r"internal|error|panick|warn", re.I)


def log_excerpt(path: Path, limit: int = 40) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    picked = [ln[:400] for ln in lines if LOG_LINES.search(ln)]
    return picked[-limit:]


def checkpoint(mcp: Mcp, case: dict, dsn: str, known: dict) -> dict:
    truth = db_truth(case, dsn)
    note_ids(known, truth)
    served = probe(mcp, case, known)
    after = db_truth(case, dsn)  # the probes' own writes, for the record
    return {"served": served, "db": truth, "mismatches": compare(case, served, truth),
            "db_consistent": consistent(case, truth), "db_duplicates": duplicates(truth),
            "db_changed_by_probes": _truth_diff(truth, after)}


def _truth_diff(a: dict, b: dict) -> list[str]:
    out = []
    for section in ("entries", "lessons", "world", "counts"):
        if a[section] != b[section]:
            out.append(section)
    changed = sorted(k for k in set(a["meta"]) | set(b["meta"]) if a["meta"].get(k) != b["meta"].get(k))
    if changed:
        out.append("meta: " + ", ".join(changed))
    return out


VARIANTS = {
    "unrelated": "(d) is one unrelated memory_store",
    "retry": "(d) repeats the same call with the same arguments (the Rust error says "
             "'retry once at most'), proxy disarmed",
}


def run_engine(name: str, case: dict, engine: str, binary: Path | None, template: str,
               root: Path, owned: list[str], variant: str = "unrelated") -> dict:
    db = pg.name(f"pl_cf_w7c_{name.replace('-', '_')[:20]}_{engine[:2]}_{variant[:2]}")
    owned.append(db)
    dsn = pg.create(db, template=template)
    proxy = CommitCutProxy(*upstream()).start()
    home = daemons.make_home(root, f"{name}-{engine}-{variant}", None)
    result: dict = {"case": name, "engine": engine, "variant": variant,
                    "about": case["about"], "marker": case["marker"]}
    d = make_daemon(engine, binary, home, proxied_dsn(dsn, proxy.port))
    known: dict[str, set[int]] = {}
    try:
        start(d)
        mcp = Mcp(d.port)
        mcp.init()
        result["baseline"] = checkpoint(mcp, case, dsn, known)
        meta_before = result["baseline"]["db"]["meta"]
        tool_name, args, headers = case["op"](dsn)
        proxy.arm(case["marker"])
        t0 = time.monotonic()
        answer = mcp.call(tool_name, args, headers)
        proxy.fired.wait(5)
        proxy.disarm()
        result["a_answer"] = {"tool": tool_name, "isError": answer["isError"],
                              "value": answer["value"], "wall_s": round(time.monotonic() - t0, 3)}
        result["fault"] = {"fired": bool(proxy.events), "events": proxy.events}
        time.sleep(1.0)
        result["c_db"] = db_truth(case, dsn)
        result["committed"] = case["committed"](result["c_db"])
        result["c_db_consistent"] = consistent(case, result["c_db"])
        result["b"] = checkpoint(mcp, case, dsn, known)
        if variant == "retry":
            again = mcp.call(tool_name, args, headers)
            result["d_call"] = {"tool": tool_name, "isError": again["isError"],
                                "value": again["value"]}
        else:
            again = mcp.call("memory_store", {
                "text": f"An unrelated note stored after the fault ({engine}, {name})."})
            result["d_call"] = {"tool": "memory_store", "isError": again["isError"],
                                "value": again["value"]}
        result["d"] = checkpoint(mcp, case, dsn, known)
        result["health_before_stop"] = _health(d.port)
        result["stop"] = graceful_stop(d)
        result["stop_db"] = db_truth(case, dsn)
        result["stop_db_consistent"] = consistent(case, result["stop_db"])
        result["stop_db_changed"] = _truth_diff(result["d"]["db"], result["stop_db"])
        result["log_before_restart"] = log_excerpt(d.log)
        shutil.copy(d.log, home / "daemon-first.log")
        d = make_daemon(engine, binary, home, dsn)
        start(d)
        mcp = Mcp(d.port)
        mcp.init()
        result["e"] = checkpoint(mcp, case, dsn, known)
        result["e_health"] = _health(d.port)
        result["meta_changed_since_baseline"] = sorted(
            k for k in set(meta_before) | set(result["e"]["db"]["meta"])
            if meta_before.get(k) != result["e"]["db"]["meta"].get(k))
        result["log_after_restart"] = log_excerpt(d.log, 15)
    except Exception as exc:  # noqa: BLE001  recorded, the other runs continue
        result["harness_error"] = f"{type(exc).__name__}: {exc}"
        result["log_tail"] = log_excerpt(d.log)
    finally:
        d.stop()
        proxy.close()
        result["proxy_connections"] = proxy.connections
    return result


def _health(port: int) -> dict:
    try:
        r = w1a.call(port, "GET", "/health", [AUTH], None, timeout=30)
        body = r.get("json") if isinstance(r.get("json"), dict) else {}
        return {"status_code": r["status"], "status": body.get("status"), "db": body.get("db")}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def prepare_template(name: str, case: dict, root: Path, owned: list[str]) -> str:
    template = pg.name(f"pl_cf_w7c_{name.replace('-', '_')[:24]}_t")
    owned.append(template)
    dsn = pg.create(template)
    home = daemons.make_home(root, f"{name}-seed", None)
    d = daemons.python_daemon(home, daemons.free_port(),
                              daemons.base_env(home, base_side_env(dsn)))
    try:
        start(d)
        mcp = Mcp(d.port)
        mcp.init()
        for op in case["seed"]:
            if op["op"] != "tool":
                continue
            r = mcp.call(op["name"], op["args"], [tuple(h) for h in op["headers"]])
            if r["isError"]:
                raise RuntimeError(f"seed op failed: {op} -> {r}")
    finally:
        d.stop()
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        for op in case["seed"]:
            if op["op"] == "sql":
                conn.execute(op["sql"])
    return template


# ── verdicts and the report ──────────────────────────────────────────────

def verdict(r: dict) -> dict:
    case = CASES.get(r["case"], {})
    means = case.get("inconsistent_means", "case invariant broken")

    def bank_flag(where: str, snapshot: dict, ok: bool) -> str | None:
        if ok:
            return None
        dups = duplicates(snapshot)
        return (f"bank inconsistent {where}: "
                + ("; ".join(dups) if dups and case.get("consistent", lambda t: True)(snapshot)
                   else means + (f"; {'; '.join(dups)}" if dups else "")))

    if "harness_error" in r:
        return {"valid": False, "why": r["harness_error"]}
    if not r["fault"]["fired"] or not r["fault"]["events"][0]["server_tag"].startswith("COMMIT"):
        return {"valid": False, "why": "the proxy never cut a committed COMMIT"}
    # A surface that could not be observed is no evidence either way: a probe
    # error anywhere makes the run invalid, never a baseline exclusion.
    probe_errors = [m for key in ("baseline", "b", "d", "e") for m in r[key]["mismatches"]
                    if m.startswith("probe error:")]
    if probe_errors:
        return {"valid": False, "why": "a probe failed: " + "; ".join(sorted(set(probe_errors)))}
    # The stop flush and the restart are part of the evidence: a stop that
    # had to be forced proves neither.
    if not r["stop"].get("clean"):
        return {"valid": False, "why": f"the graceful stop did not complete: {r['stop']}"}
    base = set(r["baseline"]["mismatches"])
    fresh = lambda m: [x for x in m if x not in base]  # noqa: E731
    wrong_after_d = fresh(r["d"]["mismatches"])
    wrong_after_e = fresh(r["e"]["mismatches"])
    flags = []
    if wrong_after_d:
        flags.append("resident divergence after (d)")
    if wrong_after_e:
        flags.append("resident divergence after (e)")
    for key, label in (("d", "at (d)"), ("e", "at (e)")):
        flags.append(bank_flag(label, r[key]["db"], r[key]["db_consistent"]))
    flags.append(bank_flag("after the stop flush", r["stop_db"], r["stop_db_consistent"]))
    if r["stop_db_changed"]:
        flags.append("stop flush changed the bank: " + ", ".join(r["stop_db_changed"]))
    if r["d_call"]["isError"]:
        flags.append(f"(d) call {r['d_call']['tool']} answered an error")
    flags.append(bank_flag("right after the fault", r["c_db"], r["c_db_consistent"]))
    flags = [f for f in flags if f]
    return {"valid": True, "committed": r["committed"],
            "answer_error": r["a_answer"]["isError"],
            "b_matches": not fresh(r["b"]["mismatches"]),
            "d_matches": not wrong_after_d, "e_matches": not wrong_after_e,
            "wrong_after_d": wrong_after_d, "wrong_after_e": wrong_after_e,
            "flags": flags}


def _short(v, n=110) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, sort_keys=True)
    return s if len(s) <= n else s[:n] + "…"


def report(results: list[dict], meta: dict) -> str:
    lines = ["# Commit-loss fault injection: what each daemon serves", "",
             "Each case cuts the connection after PostgreSQL committed the operation's "
             "transaction and before the daemon received the COMMIT reply, then compares what "
             "the daemon serves with the bank at (b) right after, (d) after one unrelated "
             "store, and (e) after a graceful stop and a restart on a direct DSN.", "",
             f"Rust binary sha256: `{meta.get('rust_sha256', 'n/a')}`; repo head: "
             f"`{meta.get('head', 'n/a')}`.", "",
             "Variants: " + "; ".join(f"`{k}`: {v}" for k, v in VARIANTS.items()) + ".", "",
             "| case | variant | engine | fault cut | committed in bank | answer | (d) answer | "
             "(b) served = bank | (d) served = bank | (e) served = bank | flags |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    yn = lambda b: "yes" if b else "**no**"  # noqa: E731
    for r in results:
        v = r["verdict"]
        if not v["valid"]:
            lines.append(f"| {r['case']} | {r.get('variant', '-')} | {r['engine']} | invalid "
                         f"| | | | | | | {v['why']} |")
            continue
        ans = r["a_answer"]
        dc = r["d_call"]
        lines.append(
            f"| {r['case']} | {r['variant']} | {r['engine']} | yes | "
            f"{'yes' if v['committed'] else 'no'} | "
            f"{'error' if ans['isError'] else 'ok'}: {_short(ans['value'], 70)} | "
            f"{dc['tool']} {'error' if dc['isError'] else 'ok'}: {_short(dc['value'], 60)} | "
            f"{yn(v['b_matches'])} | {yn(v['d_matches'])} | {yn(v['e_matches'])} | "
            f"{'; '.join(v['flags']) or '-'} |")
    lines += ["", "## Details", ""]
    for r in results:
        v = r["verdict"]
        lines.append(f"### {r['case']} / {r.get('variant', '-')} / {r['engine']}")
        lines.append("")
        lines.append(f"{r['about']}. Marker: `{r['marker']}`.")
        if not v["valid"]:
            lines += ["", f"Invalid run: {v['why']}", ""]
            continue
        ev = r["fault"]["events"][0]
        lines += ["", f"- cut transaction (connection {ev['connection']}, server tag "
                      f"`{ev['server_tag']}`): " + "; ".join(f"`{_short(s, 90)}`"
                                                        for s in ev["transaction_statements"][:6]),
                  f"- answer: {_short(r['a_answer']['value'], 300)}",
                  f"- (d) {r['d_call']['tool']}: {_short(r['d_call']['value'], 300)}"]
        for key, label in (("b", "(b)"), ("d", "(d)"), ("e", "(e)")):
            mm = r[key]["mismatches"]
            lines.append(f"- {label} mismatches: " + ("; ".join(mm) if mm else "none"))
            if r[key]["db_duplicates"] or not r[key]["db_consistent"]:
                lines.append(f"- {label} bank: consistent={r[key]['db_consistent']}, "
                             f"duplicates={r[key]['db_duplicates']}")
        if r["baseline"]["mismatches"]:
            lines.append("- baseline mismatches (excluded from verdicts): "
                         + "; ".join(r["baseline"]["mismatches"]))
        lines.append(f"- stop: {r['stop']}; bank changed by the stop flush: "
                     f"{r['stop_db_changed'] or 'nothing'}")
        lines.append(f"- meta keys changed since baseline: {r['meta_changed_since_baseline'] or 'none'}")
        reconnects = [ln for ln in r.get("log_before_restart", []) if re.search(
            r"reconnect|connection lost", ln, re.I)]
        if reconnects:
            lines.append(f"- log: `{_short(reconnects[0], 160)}`")
        lines.append("")
    return "\n".join(lines) + "\n"


# ── smoke: the proxy alone ───────────────────────────────────────────────

def smoke() -> int:
    import psycopg
    owned = []
    name = pg.name("pl_cf_w7c_smoke")
    owned.append(name)
    try:
        dsn = pg.create(name)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("CREATE TABLE smoke (id int PRIMARY KEY, note text)")
        proxy = CommitCutProxy(*upstream()).start()
        pdsn = proxied_dsn(dsn, proxy.port)
        try:
            with psycopg.connect(pdsn, autocommit=True) as c:
                with c.transaction():
                    c.execute("INSERT INTO smoke VALUES (1, 'unarmed')")
            print("unarmed commit through the proxy: ok")
            proxy.arm(r"INSERT\s+INTO\s+smoke")
            failed = None
            try:
                with psycopg.connect(pdsn, autocommit=True) as c:
                    c.execute("SELECT 1")
                    with c.transaction():
                        c.execute("INSERT INTO smoke VALUES (%s, %s)", (2, "armed"))
            except psycopg.Error as exc:
                failed = f"{type(exc).__name__}: {exc}"
            print(f"armed commit: client saw {failed or 'NO ERROR'}; events {proxy.events}")
            with psycopg.connect(dsn, autocommit=True) as c:
                rows = c.execute("SELECT id, note FROM smoke ORDER BY id").fetchall()
            print(f"direct read: {rows}")
            ok = failed is not None and rows == [(1, "unarmed"), (2, "armed")] and proxy.events
            with psycopg.connect(pdsn, autocommit=True) as c:
                with c.transaction():
                    c.execute("INSERT INTO smoke VALUES (3, 'after')")
            print("disarmed after firing: ok")
        finally:
            proxy.close()
        print("SMOKE", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        for db in reversed(owned):
            pg.drop(db)


# ── main ──────────────────────────────────────────────────────────────────

def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rust-bin", type=Path)
    ap.add_argument("--only", nargs="*", choices=sorted(CASES))
    ap.add_argument("--engines", default="python,rust")
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument("--out", type=Path)
    ap.add_argument("--report", type=Path)
    ap.add_argument("--keep", action="store_true", help="keep the daemon homes")
    ap.add_argument("--min-free-gb", type=float, default=8.0)
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    if a.smoke:
        return smoke()
    engines = [e.strip() for e in a.engines.split(",") if e.strip()]
    variants = [v.strip() for v in a.variants.split(",") if v.strip()]
    if not set(variants) <= set(VARIANTS):
        ap.error(f"--variants: expected some of {sorted(VARIANTS)}")
    if "rust" in engines and not a.rust_bin:
        ap.error("--rust-bin is required for the rust engine")
    root = Path(tempfile.mkdtemp(prefix=f"commit-loss-{os.getpid()}-", dir=daemons.scratch_root()))
    out = a.out or root / "commit-loss.json"
    rep = a.report or out.with_suffix(".md")
    meta = {"started": time.time(), "engines": engines, "variants": variants}
    if a.rust_bin:
        meta["rust_sha256"] = hashlib.sha256(a.rust_bin.read_bytes()).hexdigest()
    try:
        import subprocess
        meta["head"] = subprocess.run(["git", "-C", str(w1a.REPO), "rev-parse", "HEAD"],
                                      capture_output=True, text=True).stdout.strip()
    except OSError:
        pass
    results: list[dict] = []
    try:
        for name in a.only or list(CASES):
            gb = free_gb(root)
            if gb < a.min_free_gb:
                print(f"stopping: {gb:.2f} GB free, below the {a.min_free_gb} GB floor",
                      flush=True)
                meta["stopped_for_disk"] = gb
                break
            case = CASES[name]
            owned: list[str] = []
            try:
                print(f"[{time.strftime('%H:%M:%S')}] {name}: seeding", flush=True)
                template = prepare_template(name, case, root, owned)
                for variant in variants:
                    for engine in engines:
                        print(f"[{time.strftime('%H:%M:%S')}] {name}: {engine} / {variant}",
                              flush=True)
                        r = run_engine(name, case, engine, a.rust_bin, template, root, owned,
                                       variant)
                        r["verdict"] = verdict(r)
                        results.append(r)
                        print(f"  {engine}/{variant}: "
                              f"{json.dumps(r['verdict'], ensure_ascii=False)}", flush=True)
                        gb = free_gb(root)
                        if gb < a.min_free_gb:
                            raise RuntimeError(f"{gb:.2f} GB free, below the floor")
            except Exception as exc:  # noqa: BLE001
                results.append({"case": name, "engine": "-", "variant": "-",
                                "about": case["about"],
                                "marker": case["marker"],
                                "harness_error": f"{type(exc).__name__}: {exc}",
                                "verdict": {"valid": False,
                                            "why": f"seed: {type(exc).__name__}: {exc}"}})
                print(f"  seed failed: {exc}", flush=True)
            finally:
                for db in reversed(owned):
                    try:
                        pg.drop(db)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  could not drop {db}: {exc}", flush=True)
                if not a.keep:
                    for p in root.glob(f"{name}-*"):
                        shutil.rmtree(p, ignore_errors=True)
            out.write_text(json.dumps({"meta": meta, "results": results}, indent=1,
                                      ensure_ascii=False, default=str), encoding="utf-8")
    finally:
        meta["finished"] = time.time()
        out.write_text(json.dumps({"meta": meta, "results": results}, indent=1,
                                  ensure_ascii=False, default=str), encoding="utf-8")
        rep.write_text(report(results, meta), encoding="utf-8")
        print(f"results: {out}\nreport: {rep}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

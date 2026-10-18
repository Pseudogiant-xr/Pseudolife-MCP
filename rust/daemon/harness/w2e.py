"""Write-path differential harness (slice W2-E): the Python daemon (oracle)
against the Rust daemon, write by write.

usage:
  python w2e.py live    --rust-bin PATH [--only NAME ...] [--out FILE] [--record]
  python w2e.py golden  --rust-bin PATH [--only NAME ...] [--out FILE]
  python w2e.py calibrate --rust-bin PATH [--out FILE]   (no tolerances: records them)
  python w2e.py mutants --rust-bin PATH_BUILT_WITH_FEATURES_HARNESS_MUTANTS [--only NAME ...]
                        [--mutants NAME ...]   (default: every entry of MUTANTS)

Any Rust build serves ``memory_store``, ``memory_fact_set``,
``memory_set_add``, ``memory_set_remove``, ``memory_fact_resolve``,
``memory_episode_start``, ``memory_episode_end``, ``memory_session_title``,
``memory_episode_summary``, ``memory_outcome``, ``memory_reinforce``,
``memory_history``, ``memory_world_set``, ``memory_world_search``,
``memory_lesson_search``, ``memory_supersede``, ``memory_reinstate``,
``memory_forget`` and ``memory_consolidation_candidates`` on ``/mcp`` (``--features mutants`` adds the
deliberate breaks). Each scenario prepares
one template bank through the Python daemon's own write path (seed calls
only, never hand-written rows, except the declared ``sql`` seed op below),
clones it for each side, starts both daemons, then sends every operation to
both. After every operation it compares:

* the response by value: each daemon's MCP ``tools/call`` result
  (``structuredContent``, ``isError``), so argument binding and the
  envelope are compared with the body; ``surprise`` within the measured
  tolerance, everything else exact;
* the full database state of both banks (``dbstate.dump``), after the
  declared normalizers below, which replace values by rule, never by hand.

Normalizers (declared, spec "Free for W1 and W2"):

* wall-clock columns and meta fields keep only null-or-not;
* episode ids (``uuid4().hex``) are renamed ``<ep#i>`` in ``started_at``
  order wherever they appear (any table, any JSON value or key);
* generic session titles keep their shape, not their minute;
* ``entries.embedding`` and ``entries.surprise`` are compared numerically
  per entry id against tolerances set from a measurement
  (``goldens/w2e-tolerance.json``), then replaced by a token; so is
  ``facts.embedding`` and ``world_facts.embedding`` per row id, against the
  same embedding tolerances (both sides rewrite a slot's rows in the same
  order, so the ids match);
* the world cortex clocks: ``world_facts`` ``asserted_at``,
  ``last_confirmed``, ``superseded_at``, ``tx_time``, ``valid_time`` and
  ``hlc_phys`` keep only null-or-not; ``retrieved_at`` (column and answer)
  does only when it lies within a day of now (a write without one takes the
  clock), and an explicit past value compares exactly;
* a search ``score`` in a response (and a candidate cluster's ``cohesion``
  and ``seed_score``), and each ``score`` in a
  ``lesson_search_events.served`` list, compares within 1.5e-4: the two
  embedders agree to the measured tolerance, and scores are rounded to 4
  places;
* the canonical-fact clocks: ``facts`` ``asserted_at``, ``last_confirmed``,
  ``superseded_at``, ``tx_time``, ``valid_time`` and ``hlc_phys`` (the HLC's
  wall-clock milliseconds; ``hlc_logical`` stays exact),
  ``outcome_signals.created_at``, ``retrieval_uses.created_at``,
  ``entities.created_at`` and each
  ``cortex_supersession_log`` row's ``timestamp`` keep only null-or-not;
* the correction clocks: ``memory_trace_invalidations.invalidated_at``
  (the correction's own clock, like ``entries.superseded_at``) and
  ``entry_reinstatement_decisions.decided_at`` keep only null-or-not; a
  correction's ``new_memory_surprise`` compares like a store's ``surprise``;
* the forget clocks: ``lessons.superseded_at``, ``edges.superseded_at`` and
  ``store_decisions.decided_at`` keep only null-or-not, and so does the
  ``superseded_at`` inside a ``store_decisions.record`` snapshot;
* the board columns a session title writes: ``coordination_agents.name_set_at``
  and ``coordination_events.created_at`` keep only null-or-not, and so do the
  audit chain's ``hash`` and ``prev_hash`` (they cover ``created_at``; the
  board harness proves the chain byte for byte);
* in responses, the same record clocks and an episode's ``started_at`` and
  ``ended_at`` keep only null-or-not, a generic session title keeps its
  shape, and a record's ``age`` may differ by one step ("just now" /
  "1 minute ago"), since the two daemons answer seconds apart;
* rows are compared as sorted sets (row order is the primary key's order,
  which renamed ids change), except that entry ids themselves must match.

Arguments may name rows by content, resolved on each side's own bank
just before the call: ``@id:<text>`` (the first row with that text, or for
a deleted row the entry of its first reinstatement decision),
``@idn:<n>:<text>`` (the n-th, from 0), and for the first RETIRED row with
that text ``@sup:<text>`` (its ``superseded_at``) and ``@sha:<field>:<text>``
(the SHA-256 of its ``text``, ``source`` or ``by``, ``superseded_by_text``).

Seed operations may also be ``{"op": "sql"}``: a statement run on the
template only, for state no tool writes (``entity_kinds``, which only an
out-of-process backfill fills). With ``"after_stop": true`` it runs once
the seeding daemon has stopped, for ``facts`` rows: the Python daemon's
exit flush rewrites ``facts`` from its resident cortex and would drop rows
it never loaded (the set-cap scenario's hundred members, with NULL
embeddings so that no cosine decides the cap).

An operation may be ``external(statement)``: a write by another process,
run on both banks between two calls (its result is not compared, only what
the next call makes of it), for a row changed behind the daemons' backs.
Only the scenarios in ``EXTERNAL_WRITES`` may use it.

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
import tempfile
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
         ("client_sessions", "ended_at"), ("client_sessions", "start_times"),
         ("facts", "asserted_at"), ("facts", "last_confirmed"), ("facts", "superseded_at"),
         ("facts", "tx_time"), ("facts", "valid_time"), ("facts", "hlc_phys"),
         ("outcome_signals", "created_at"), ("entities", "created_at"),
         ("coordination_agents", "name_set_at"), ("coordination_events", "created_at"),
         ("coordination_events", "hash"), ("coordination_events", "prev_hash"),
         ("retrieval_uses", "created_at"),
         ("world_facts", "asserted_at"), ("world_facts", "last_confirmed"),
         ("world_facts", "superseded_at"), ("world_facts", "tx_time"),
         ("world_facts", "valid_time"), ("world_facts", "hlc_phys"),
         ("lesson_search_events", "created_at"),
         ("memory_trace_invalidations", "invalidated_at"),
         ("entry_reinstatement_decisions", "decided_at"),
         ("lessons", "superseded_at"), ("edges", "superseded_at"),
         ("store_decisions", "decided_at")}
# Columns that are wall-clock only when written from the clock: a value
# within RECENT_S of now keeps null-or-not, anything else compares exactly.
RECENT_CLOCK = {("world_facts", "retrieved_at")}
RECENT_S = 86400.0
# Record keys in tool answers holding wall-clock seconds.
RESPONSE_CLOCK = {"asserted_at", "last_confirmed", "superseded_at", "tx_time", "valid_time",
                  "started_at", "ended_at"}
RESPONSE_RECENT_CLOCK = {"retrieved_at"}
# A response score's allowance: embeddings within the measured tolerance
# move a 4-place cosine by at most one rounding unit.
SCORE_TOLERANCE = 1.5e-4
# meta key -> paths (keys) inside its JSON value that are wall-clock.
META_CLOCK = {"capacity_true_drops": {"last_at"}, "active_session_pointer": {"ts"}}
EP_ID = re.compile(r"^[0-9a-f]{32}$")
GENERIC = re.compile(r"^session - \d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
# store-cancellable is w2e_shutdown.py's control: over MCP every call runs
# detached from its connection, so only the shutdown drain can observe it.
MUTANTS = ["store-tags-raw", "store-skip-session-episode", "store-no-hint", "store-surprise-raw",
           "mcp-store-no-session",
           # memory_fact_set (spec FS1-FS12): caught by the fact-set-* scenarios.
           "mcp-fact-set-no-supersede", "mcp-fact-set-no-confirm-reinforce",
           "mcp-fact-set-no-correction-signal", "mcp-fact-set-no-entity-mint",
           # memory_set_add / memory_set_remove (spec SA1-SA11): caught by
           # the set-* scenarios.
           "mcp-set-add-no-convert", "mcp-set-add-no-cosine-dedup", "mcp-set-remove-noop",
           # memory_fact_resolve (spec FR1-FR8): caught by the fact-resolve-*
           # scenarios.
           "mcp-fact-resolve-no-user-tier", "mcp-fact-resolve-reject-noop",
           "mcp-fact-resolve-no-set-guard",
           # The episode tools (spec EP1-EP12): caught by the episode-* and
           # session-title scenarios.
           "mcp-episode-start-no-anchor", "mcp-episode-end-pops-root",
           "mcp-session-title-no-entry-stamp", "mcp-session-title-no-board",
           "mcp-episode-summary-count-order",
           # memory_outcome and memory_reinforce (spec OC1-OC12): caught by
           # the outcome-labels scenario.
           "mcp-outcome-no-used-ids-record", "mcp-outcome-credit-latest-only",
           "mcp-outcome-ignore-handle", "mcp-reinforce-no-explicit",
           "mcp-reinforce-no-use-credit",
           # memory_history slot mode (spec HI1-HI7): caught by history-slot.
           "mcp-history-ignore-as-of", "mcp-history-set-unsorted",
           "mcp-history-set-no-removed", "mcp-history-asserted-order",
           # the world cortex (spec WD1-WD12): caught by the world-* scenarios.
           "mcp-world-set-no-confirm", "mcp-world-set-unsafe-allowed",
           "mcp-world-set-no-save", "mcp-world-search-no-correct-with",
           # lessons and the history chain (spec LS1-LS10, CH1-CH6): caught
           # by the lessons scenario.
           "mcp-lesson-search-no-graph-resolve", "mcp-lesson-search-no-log",
           "mcp-history-chain-no-lessons",
           # memory_supersede and memory_reinstate (spec CO1-CO10, RI1-RI9):
           # caught by the supersede-* and reinstate scenarios.
           "mcp-supersede-text-first-live", "mcp-supersede-derived-current-only",
           "mcp-supersede-surprise-gate", "mcp-supersede-no-trace-invalidation",
           "mcp-supersede-resident-stale", "mcp-reinstate-default-principal",
           "mcp-reinstate-ignore-invalidations", "mcp-reinstate-resident-stale",
           "mcp-reinstate-no-mirror",
           # memory_forget (spec FG1-FG16): caught by the forget-* scenarios.
           "mcp-forget-no-bulk-guard", "mcp-forget-delete-digests", "mcp-forget-cortex-stale",
           "mcp-forget-no-lesson-cascade", "mcp-forget-lessons-stale", "mcp-forget-facts-kept",
           "mcp-forget-edges-kept", "mcp-forget-slot-reads-kept", "mcp-forget-no-world-audit",
           # memory_consolidation_candidates (spec CC1-CC9): caught by
           # consolidation-candidates.
           "mcp-consolidation-unsorted", "mcp-consolidation-unverified"]


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


# ── scenarios ─────────────────────────────────────────────────────────────

def store(text, headers=(), **kw) -> dict:
    return {"op": "tool", "name": "memory_store", "args": {"text": text, **kw},
            "headers": list(headers)}


def sess(s: str) -> tuple[str, str]:
    return ("X-PL-Session", s)


def fact(entity, attribute, value, headers=(), **kw) -> dict:
    return {"op": "tool", "name": "memory_fact_set",
            "args": {"entity": entity, "attribute": attribute, "value": value, **kw},
            "headers": list(headers)}


def tool(name, headers=(), **args) -> dict:
    return {"op": "tool", "name": name, "args": args, "headers": list(headers)}


def set_add(entity, attribute, member, headers=()) -> dict:
    return tool("memory_set_add", headers, entity=entity, attribute=attribute, member=member)


def set_remove(entity, attribute, member, headers=()) -> dict:
    return tool("memory_set_remove", headers, entity=entity, attribute=attribute, member=member)


def resolve(entity, attribute, accept) -> dict:
    return tool("memory_fact_resolve", entity=entity, attribute=attribute, accept=accept)


def ep_start(title, headers=(), **kw) -> dict:
    return tool("memory_episode_start", headers, title=title, **kw)


def ep_end(headers=(), **kw) -> dict:
    return tool("memory_episode_end", headers, **kw)


def session_title(title, headers=(), **kw) -> dict:
    return tool("memory_session_title", headers, title=title, **kw)


def ep_summary(eid) -> dict:
    return tool("memory_episode_summary", id=eid)


def outcome(task, result, headers=(), **kw) -> dict:
    return tool("memory_outcome", headers, task=task, outcome=result, **kw)


def reinforce(entry_id, headers=()) -> dict:
    return tool("memory_reinforce", headers, entry_id=entry_id)


def search(query, headers=(), top_k=1) -> dict:
    """Seed-only here: a logged search whose served ids an outcome credits."""
    return tool("memory_search", headers, query=query, top_k=top_k)


def world_set(entity, attribute, value, headers=(), **kw) -> dict:
    return tool("memory_world_set", headers, entity=entity, attribute=attribute, value=value, **kw)


def world_search(query, **kw) -> dict:
    return tool("memory_world_search", query=query, **kw)


def lesson_search(query, headers=(), **kw) -> dict:
    return tool("memory_lesson_search", headers, query=query, **kw)


def history_chain(entity) -> dict:
    return tool("memory_history", entity=entity)


def lesson_row(task, aspect, task_norm, aspect_norm, lesson, about, polarity, outcome,
               status, entry_text) -> dict:
    """Seed-only: a ``lessons`` row (only the dream writes lessons), its
    embedding copied from an entry the Python daemon stored, so both banks
    hold the same vector. After the seeding daemon stops: its exit flush
    rewrites the lessons it holds."""
    def q(v):
        return "NULL" if v is None else "'" + v.replace("'", "''") + "'"
    return sql(
        "INSERT INTO lessons (entity, attribute, entity_norm, attribute_norm, value, about, "
        "polarity, outcome, status, confidence, origin, support, provenance, asserted_at, "
        "last_confirmed, embedding) SELECT "
        f"{q(task)}, {q(aspect)}, {q(task_norm)}, {q(aspect_norm)}, {q(lesson)}, {q(about)}, "
        f"{q(polarity)}, {q(outcome)}, {q(status)}, 0.8, 'agent', '[]', "
        "'[\"signal:2\", \"signal:1\"]', 1700000000, 1700000000, embedding "
        f"FROM entries WHERE text = {q(entry_text)}", after_stop=True)


def history(entity, attribute, **kw) -> dict:
    return tool("memory_history", entity=entity, attribute=attribute, **kw)


def register(episode: str, **fields) -> dict:
    """Seed-only: a board row registered under ``episode`` (a session key)."""
    return {"op": "http", "method": "POST", "path": "/api/coordination/register",
            "body": {"label": "claude-code", "project": "p", "task": "t", "episode": episode,
                     **fields}}


def sql(statement: str, after_stop: bool = False) -> dict:
    """Seed-only: a statement run on the template (see the module docstring)."""
    return {"op": "sql", "sql": statement, "after_stop": after_stop}


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


# memory_fact_set (spec FS1-FS12). Every branch of cortex_write the MCP tool
# reaches, against a template seeded through the Python daemon's own tools.
SCENARIOS.update({
    "fact-set-branches": {
        "config": None,
        "seed": [
            # A session root for the episode handle and the correction signal.
            store("Session note that opens the fs-a root.", [sess("fs-a")]),
            fact("Payments DB", "host", "10.0.0.5", origin="user", confidence=0.9),
            fact("dev box", "os", "Debian 12"),
            fact("pipeline", "schedule", "the deploy pipeline runs nightly on the bench box",
                 origin="action"),
            # The value names an entity nobody has minted yet: its
            # object_entity_id stays NULL until "Billing API" is minted.
            fact("dev box", "runs", "Billing API"),
        ],
        "ops": [
            # inserted; the subject's entity is minted
            fact("Build Server", "hostname", "ci-box-2"),
            # confirmed: case and separators fold to the slot, the agent tier reinforces
            fact("build_server", "Hostname", "ci-box-2"),
            # confirmed: a user restatement adds the user tier
            fact("dev box", "os", " debian 12 ", origin="user", confidence=0.5),
            # compression echo: confirms the richer value and logs it
            fact("pipeline", "schedule", "deploy pipeline runs nightly"),
            # superseded by a user write: correction signal under the fs-a leaf
            fact("Payments DB", "host", "10.0.0.9", [sess("fs-a")], origin="user",
                 confidence=0.9),
            # tier_downgrade: an agent write parks a contender
            fact("Payments DB", "host", "10.0.0.7"),
            # the same value refreshes the contender (action tier added)
            fact("payments db", "host", "10.0.0.7 ", origin="action", confidence=0.6),
            # below_confidence_margin: a new value replaces the contender
            fact("Payments DB", "host", "10.0.0.8", origin="user", confidence=0.5),
            # the contender's value at full weight supersedes and settles the contest;
            # no session: the signal has no episode
            fact("Payments DB", "host", "10.0.0.8", origin="user", confidence=0.9),
            # minting "billing-api" relinks the seeded row's object_entity_id
            fact("Billing API", "owner", "payments team"),
            # a junk-shaped subject keeps the fact but mints no entity
            fact("236 memories", "status", "archived"),
            fact("LOCAL master = 8e2b992", "state", "pushed"),
            # episode handles: valid (session from the root), unknown (warning), empty
            fact("Dev Box", "kernel", "6.8", episode="@root:fs-a:12"),
            fact("Dev Box", "shell", "zsh", episode="ffffffffffff"),
            fact("Dev Box", "editor", "vim", episode=""),
            fact("Dev Box", "pager", "less", episode="abc"),
            # explicit freshness and labels, then a confirm that restates one label
            fact("dev box", "tz", "UTC", freshness_class="volatile", authority="directive",
                 distortion_tolerance="constraint"),
            fact("dev box", "tz", "UTC", authority="quoted"),
            fact("dev box", "locale", "en_AU", freshness_class="slow",
                 authority="observation", distortion_tolerance="preference"),
            fact("dev box", "timezone", "Australia/Hobart", freshness_class="evergreen",
                 distortion_tolerance="episodic"),
            # auto labels inferred from the value
            fact("dev box", "rule", "Never run the full suite natively on Windows."),
            fact("dev box", "doc", "According to the docs, the bank volume is external."),
            # a header session wins over the handle's; the signal takes the handle's episode
            fact("Dev Box", "editor", "emacs", [sess("fs-b")], origin="user",
                 episode="@root:fs-a:12"),
            # an integer confidence binds as a float; agent under user parks
            fact("Dev Box", "editor", "vim", confidence=1),
            fact("dev box", "editor", "nano", origin="action", confidence=0),
        ],
    },
    "fact-set-newer-wins": {
        "config": "memory:\n  cortex:\n    protect_provenance: false\n",
        "seed": [fact("db", "host", "a", origin="user", confidence=0.9)],
        "ops": [
            # tier ignored: an agent write supersedes (no correction signal)
            fact("db", "host", "b"),
            # below the margin: contested, the current answered twice, nothing parked
            fact("db", "host", "c", confidence=0.1),
            fact("db", "host", "c", origin="user", confidence=0.1),
            fact("db", "host", "d", origin="user", confidence=0.9),
        ],
    },
    "fact-set-tuned": {
        "config": ("memory:\n  lessons:\n    enabled: false\n  cortex:\n"
                   "    supersede_confidence_margin: 0.5\n    reinforce_rate: 0.5\n"),
        "seed": [fact("db", "host", "a", origin="user", confidence=0.9)],
        "ops": [
            # within the wider margin: superseded; lessons off, so no signal row
            fact("db", "host", "b", origin="user", confidence=0.45),
            # reinforce_rate 0.5
            fact("db", "host", "b", origin="user", confidence=0.1),
            fact("db", "host", "c", origin="user", confidence=0.1),
        ],
    },
    "fact-set-freshness-auto": {
        "config": None,
        "seed": [
            sql("INSERT INTO entity_kinds (entity_norm, kind, origin, confidence, decided_at) "
                "VALUES ('payments-db', 'system', 'manual', 1.0, 0), "
                "('release-0-9', 'artifact', 'manual', 1.0, 0)"),
        ],
        "ops": [
            fact("Payments DB", "status", "up"),
            fact("payments_db", "deployment-date", "2026-10-01"),
            fact("Payments DB", "schema version", "58"),
            fact("Payments DB", "owner", "payments team"),
            fact("release 0.9", "status", "shipped"),
            fact("unknown thing", "status", "fine"),
            fact("Payments DB", "status", "degraded", freshness_class="evergreen"),
        ],
    },
    "fact-set-set-slot": {
        "config": None,
        "seed": [
            tool("memory_set_add", entity="dev box", attribute="users", member="alice"),
            tool("memory_set_add", entity="dev box", attribute="groups", member="admins"),
            tool("memory_set_remove", entity="dev box", attribute="groups", member="admins"),
        ],
        "ops": [
            # a slot with current members refuses (invalid_argument, nothing written)
            fact("Dev Box", "Users", "bob"),
            # only a removed member: the scalar lands beside the audit row
            fact("dev box", "groups", "ops"),
        ],
    },
})


# memory_set_add / memory_set_remove (spec SA1-SA11). Every branch the MCP
# tools reach. A member's embedding is that of "{entity} {attribute}
# {member}"; the cosines quoted below were measured with the daemon's
# Qwen3-Embedding-0.6B (fp32 torch, CPU), all far from the 0.9 dedup line.
SET_CAP_MEMBERS = (
    "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, value, status, "
    "confidence, asserted_at, last_confirmed, kind, value_norm) "
    "SELECT 'cap box', 'tags', 'cap-box', 'tags', v, 'current', 0.7, 1000.0 + g, 1000.0 + g, "
    "'member', v FROM (SELECT g, 'tag-' || lpad(g::text, 3, '0') AS v "
    "FROM generate_series(0, 99) AS g) AS s ORDER BY g")

SCENARIOS.update({
    "set-add-branches": {
        "config": None,
        "seed": [
            # a labelled volatile user scalar, and an agent contender beside it
            fact("Home Lab", "services", "Jellyfin media server", origin="user", confidence=0.9,
                 freshness_class="volatile", authority="directive",
                 distortion_tolerance="constraint"),
            fact("Home Lab", "services", "Pi-hole DNS ad blocker"),
        ],
        "ops": [
            # converts the scalar (dropped class and labels on the log row), adds
            # the member (cosine 0.71 to the old value) and settles the contender
            set_add("home lab", "Services", "Pi-hole DNS ad blocker", [sess("sa-a")]),
            # the old scalar's value is a member now: confirmed by value
            set_add("Home Lab", "services", "  jellyfin MEDIA server "),
            # confirmed by cosine (0.99 to the converted member)
            set_add("Home Lab", "services", "Jellyfin media streaming server"),
            # added (cosine 0.81 and 0.67 to the two members)
            set_add("Home Lab", "services", "Nextcloud file sync"),
            # blank members are refused with a note; the subject is minted all the same
            set_add("Home Lab", "services", "   "),
            set_add("Bike Rack", "bikes", ""),
            # a real subject is minted, a junk-shaped one is not
            set_add("Garden Shed", "tools", "cordless hedge trimmer"),
            set_add("236 memories", "tags", "archived snapshot"),
            # removed by normalised value; then not found, and an empty slot
            set_remove("HOME LAB", "services", "nextcloud FILE sync "),
            set_remove("Home Lab", "services", "Nextcloud file sync"),
            set_remove("nobody", "things", "anything"),
            # re-added after its removal: a new row beside the removed one
            set_add("Home Lab", "services", "Nextcloud file sync"),
        ],
    },
    "set-add-aggregate": {
        "config": None,
        "seed": [
            fact("Aquarium", "fish count", "32", origin="user"),
            fact("Aquarium", "budget", "$1,500"),
        ],
        "ops": [
            # the same total confirms the scalar
            set_add("aquarium", "Fish Count", " 32 "),
            # anything else parks a contender; the same value refreshes it, a new
            # one replaces it
            set_add("Aquarium", "fish count", "neon tetra"),
            set_add("Aquarium", "fish count", "Neon Tetra "),
            set_add("Aquarium", "fish count", "guppy"),
            set_add("Aquarium", "budget", "new canister filter"),
        ],
    },
    "set-cap": {
        "config": None,
        "seed": [sql(SET_CAP_MEMBERS, after_stop=True)],
        "ops": [
            # a hundred current members: capped, logged, nothing written but the log
            set_add("Cap Box", "tags", "volcanic basalt sample"),
            # dedup runs before the cap
            set_add("cap box", "tags", "TAG-007"),
            # a removal makes room (the slot's rows are rewritten again; the
            # confirm above already marked it dirty)
            set_remove("cap box", "tags", "tag-000"),
            set_add("Cap Box", "tags", "volcanic basalt sample"),
            # cosine 0.66 to the one embedded member: capped again
            set_add("cap box", "tags", "medieval lute tablature"),
        ],
    },
})


# memory_fact_resolve (spec FR1-FR8). Every branch service.cortex_resolve
# reaches over MCP. Contenders are parked through the Python daemon's own
# tools: memory_fact_set at a weaker tier or below the confidence margin
# (protect_provenance is on by default), and memory_set_add's aggregate park.
# A contender with no current scalar exists only once memory_set_add has
# converted the scalar it was parked against; removing every member then
# leaves it parked at a slot with neither a current scalar nor members. The
# member cosines (measured as for the set-* scenarios) sit far below the 0.9
# dedup line: 0.81 (Home Lab), 0.81 (Garden Shed), 0.80 (Bike Rack).
SCENARIOS.update({
    "fact-resolve-scalar": {
        "config": None,
        "seed": [
            # a user scalar; an agent contender, replaced by a second one
            fact("Payments DB", "host", "10.0.0.5", origin="user", confidence=0.9,
                 authority="directive"),
            fact("Payments DB", "host", "10.0.0.6"),
            fact("Payments DB", "host", "10.0.0.7"),
            # a user contender parked below the confidence margin
            fact("Build Server", "hostname", "ci-box-2", origin="user", confidence=0.9),
            fact("Build Server", "hostname", "ci-box-3", origin="user", confidence=0.5),
            # an agent contender under an action scalar
            fact("dev box", "os", "Debian 12", origin="action"),
            fact("dev box", "os", "Debian 13"),
        ],
        "ops": [
            # nothing parked: no_contender with the raw arguments (the accept
            # still ticks the clock)
            resolve("Nobody", "thing", True),
            # accept: the old current superseded, the contender promoted with
            # the user tier and the old value as supersedes_value
            resolve("payments db", "Host", True),
            # nothing left to resolve, either way
            resolve("Payments DB", "host", True),
            resolve("Payments DB", "host", False),
            # the promoted record holds the slot: a restatement confirms it
            fact("Payments DB", "host", "10.0.0.7"),
            # the user tier is already there; accept binds laxly ("yes" is
            # true, so the contender is promoted)
            resolve("Build Server", "hostname", "yes"),
            # reject, bound laxly (0 is false, so the contender is retired
            # rather than promoted); the kept current is answered twice
            resolve("Dev Box", "OS", 0),
            # nothing left to resolve; "no" binds to false
            resolve("dev box", "os", "no"),
        ],
    },
    "fact-resolve-set-slot": {
        "config": None,
        "seed": [
            # a contender parked against a scalar that then converts to a set
            fact("Home Lab", "services", "Jellyfin media server", origin="user",
                 confidence=0.9),
            fact("Home Lab", "services", "Pi-hole DNS ad blocker"),
            set_add("Home Lab", "services", "Nextcloud file sync"),
            # the same, then every member removed: no current, no members;
            # the contender replaced one, so it carries a supersedes_value
            fact("Garden Shed", "tools", "cordless drill", origin="user", confidence=0.9),
            fact("Garden Shed", "tools", "hedge trimmer"),
            fact("Garden Shed", "tools", "leaf blower"),
            set_add("Garden Shed", "tools", "pressure washer"),
            set_remove("Garden Shed", "tools", "cordless drill"),
            set_remove("Garden Shed", "tools", "pressure washer"),
            fact("Bike Rack", "bikes", "road bike", origin="user", confidence=0.9),
            fact("Bike Rack", "bikes", "gravel bike"),
            set_add("Bike Rack", "bikes", "kids balance bike"),
            set_remove("Bike Rack", "bikes", "road bike"),
            set_remove("Bike Rack", "bikes", "kids balance bike"),
            # a member parked by the aggregate guard
            fact("Aquarium", "fish count", "32", origin="user"),
            set_add("Aquarium", "fish count", "neon tetra"),
        ],
        "ops": [
            # accept at a slot with current members: slot_holds_set, nothing written
            resolve("home lab", "Services", True),
            # reject is allowed there: current null, the retired contender answered
            resolve("Home Lab", "services", False),
            # accept with no current: the contender keeps its own supersedes_value
            resolve("garden shed", "tools", True),
            # reject with no current
            resolve("Bike Rack", "bikes", False),
            # the parked member replaces the protected total
            resolve("Aquarium", "Fish Count", True),
        ],
    },
})


# memory_outcome and memory_reinforce (spec OC1-OC12). Entry ids follow the
# seed order (1-5). Seeded searches (top_k 1) give the window its events: id 2
# served twice in sess-a, id 4 once in sess-b, id 5 twice with no session.
# Entry 1 draws the Python daemon's own startup warmup search (a no-session
# event Rust does not write, declared), so no op names it: a session-less
# credit could name that event, and a session's credit would count it as
# served elsewhere.
OUTCOME_SEED = [
    store("The warmup probe runs once when the daemon starts."),
    store("The payments database listens on port 5432 on db-1.", [sess("sess-a")]),
    store("Deploys go out through the blue-green pipeline on Fridays.", [sess("sess-a")]),
    store("The bench box runs Debian 12 with a 4 TB scratch disk.", [sess("sess-b")]),
    store("Nightly backups land in the cold-storage bucket."),
    search("payments database port", [sess("sess-a")]),
    search("which port does the payments database use", [sess("sess-a")]),
    search("bench box scratch disk", [sess("sess-b")]),
    search("nightly backups bucket"),
    search("where do the nightly backups go"),
]

SCENARIOS.update({
    "outcome-labels": {
        "config": None,
        "seed": OUTCOME_SEED,
        "ops": [
            # credited by both sess-a events, unmatched, served elsewhere,
            # unknown; attributed to sess-a's open leaf
            outcome("deploy engine to host", "success", [sess("sess-a")],
                    used_ids=[2, 3, 4, 999]),
            # text with junk tokens and a repeat; a repeat credit writes no
            # row (a JSON list in a string with a non-integer item is
            # refused by the binder, before the body: not this case)
            outcome("deploy engine to host", "failure", [sess("sess-a")],
                    used_ids="2 2 x, true"),
            # handle attribution without a header, every optional field
            outcome("bench maintenance", "correction", episode="@root:sess-b:12",
                    about="debian", detail="the upgrade path", polarity="-"),
            # an unknown handle warns and falls back to the header's leaf
            outcome("unknown handle", "success", [sess("sess-a")], episode="ffffffffffff"),
            # no session: only the most recent NULL-session event per id
            outcome("backups", "success", used_ids="5, 5"),
            # an empty list and junk: the signal lands, the label does not
            outcome("empty label", "success", [sess("sess-a")], used_ids=[]),
            outcome("junk label", "success", [sess("sess-a")], used_ids="abc def"),
            # an id past BIGINT fails the label statement: counted, kept unchecked
            outcome("big id", "success", [sess("sess-a")], used_ids="99999999999999999999 2"),
            # over the cap of fifty (from 2: entry 1 is the warmup's)
            outcome("many ids", "success", [sess("sess-a")], used_ids=list(range(2, 55))),
            # both counters, the resident copy, the dereference credit
            reinforce(2, [sess("sess-a")]),
            reinforce(999999),
            # no session and no NULL-session event serving it: no credit
            reinforce(4),
        ],
    },
    "outcome-lessons-off": {
        "config": "memory:\n  lessons:\n    enabled: false\n",
        "seed": [store("A note so the bank is not empty.")],
        "ops": [
            outcome("lessons are off", "success", used_ids="abc"),
            outcome("lessons are off", "failure", used_ids=[1]),
        ],
    },
    "outcome-log-off": {
        "config": "memory:\n  retrieval_log:\n    enabled: false\n",
        "seed": [store("A note so the bank is not empty.", [sess("sess-a")])],
        "ops": [
            outcome("log is off", "success", [sess("sess-a")], used_ids=[1]),
            reinforce(1, [sess("sess-a")]),
        ],
    },
})


# memory_history slot mode (spec HI1-HI7), on cortex records written through
# the Python daemon's own fact tools. Chain mode answers the declared stub
# and is not compared here.
SCENARIOS.update({
    "history-slot": {
        "config": None,
        "seed": [
            fact("Payments DB", "host", "10.0.0.5", origin="user", confidence=0.9),
            fact("Payments DB", "host", "10.0.0.6", origin="user", confidence=0.9),
            fact("Payments DB", "host", "10.0.0.7", origin="user", confidence=0.9),
            set_add("Home Lab", "hosts", "alpha"),
            set_add("Home Lab", "hosts", "beta"),
            set_remove("Home Lab", "hosts", "alpha"),
            set_add("Home Lab", "hosts", "gamma"),
            # a contender parked below the margin, then the current value
            # restated: its re-confirmation moves its tx_time past the
            # contender's, so transaction order differs from assertion order
            fact("Build Server", "hostname", "ci-box-2", origin="user", confidence=0.9),
            fact("Build Server", "hostname", "ci-box-3", origin="user", confidence=0.5),
            fact("Build Server", "hostname", "ci-box-2", origin="user", confidence=0.9),
        ],
        "ops": [
            history("Payments DB", "host"),
            # the slot key is case- and separator-insensitive
            history("payments db", "HOST"),
            # a cutoff between the first two versions keeps the first
            history("Payments DB", "host", as_of="@txmid:payments-db:host"),
            # ISO with an offset, a naive date (local time), epoch zero
            history("Payments DB", "host", as_of="1970-01-02T00:00:00+00:00"),
            history("Payments DB", "host", as_of="2000-01-01"),
            history("Payments DB", "host", as_of=0.0),
            # float spellings: NaN keeps nothing, infinity everything; both
            # echo as null in structured content (the text block differs,
            # declared)
            history("Payments DB", "host", as_of="nan"),
            history("Payments DB", "host", as_of="inf"),
            # set slot: adds and the removal in time order
            history("Home Lab", "hosts"),
            history("home lab", "Hosts", as_of="9999-12-31T00:00:00+00:00"),
            history("Home Lab", "hosts", as_of="1970-01-02"),
            # transaction time orders the chain, not assertion time
            history("Build Server", "hostname"),
            # an empty slot
            history("Nobody", "nothing"),
            # refused at binding
            history("Payments DB", "host", as_of="yesterday"),
        ],
    },
})


# The world cortex (spec WD1-WD12), on templates whose world facts the
# Python daemon's own tool wrote. A retrieved_at of 1.0 makes a fact stale.
WORLD_SEED = [
    world_set("Rust", "latest stable version", "1.90.0",
              source_url="https://blog.rust-lang.org/", source_quote="Rust 1.90.0 is out.",
              freshness_class="slow"),
    world_set("Python", "latest version", "3.13.0", source_url="https://www.python.org/",
              retrieved_at=1.0),
    world_set("PostgreSQL", "license", "PostgreSQL License", freshness_class="evergreen",
              source_url="https://www.postgresql.org/about/licence/"),
]

SCENARIOS.update({
    "world-facts": {
        "config": None,
        # A non-finite retrieved_at: Python stores and reloads it, so the
        # Rust daemon must start on a bank that holds one.
        "seed": WORLD_SEED + [world_set("Clock", "skew", "unknown", retrieved_at="inf")],
        "ops": [
            # compact entries; the stale one carries correct_with and the note
            world_search("what is the newest Rust release"),
            world_search("python version", verbose=True),
            world_search("database license", top_k=1),
            # the same value restated: confirmed, confidence lifted, citation
            # kept when empty, a new hash recorded
            world_set("rust", "Latest Stable Version", " 1.90.0 ", confidence=0.95,
                      content_hash="sha256:abc"),
            # a different value supersedes, with a session header
            world_set("Rust", "latest stable version", "1.91.0", [sess("sess-w")],
                      source_url="https://blog.rust-lang.org/2026/", retrieved_at=1600000000.0),
            # unsafe schemes are refused before anything runs
            world_set("Evil", "link", "x", source_url="javascript:alert(1)"),
            world_set("Evil", "link", "x", source_url="\tJaVaScRiPt:alert(1)"),
            # a scheme-less citation is inert and allowed
            world_set("Example", "home page", "example.com/page", source_url="example.com/page"),
            world_search("rust release", top_k=10),
            # the non-finite retrieved_at: never stale, undecayed, echoed null
            world_search("clock skew", top_k=1, verbose=True),
            history("Rust", "latest stable version"),
        ],
    },
    "world-facts-demote": {
        "config": "memory:\n  search:\n    stale_policy: demote\n",
        "seed": WORLD_SEED,
        "ops": [
            # the stale fact sorts last and carries the warning
            world_search("python version and rust version", top_k=5),
            world_set("Python", "latest version", "3.14.0", source_url="https://www.python.org/"),
        ],
    },
    "world-facts-quarantine": {
        "config": "memory:\n  search:\n    stale_policy: quarantine\n",
        "seed": WORLD_SEED,
        "ops": [
            # the stale value moves to last_known_value, in both projections
            world_search("python version"),
            world_search("python version", verbose=True),
        ],
    },
})


# Lessons (spec LS1-LS10) and the memory_history chain (CH1-CH6). The
# lessons are SQL seeds (only the dream writes them), asserted in 2023, so a
# fact written now about their subject makes them re_verify: directly for
# "blue-green pipeline", and through the graph alias "bearer tokens" ->
# "auth tokens" for the credentials lesson.
LESSON_TEXTS = [
    "Deploy the engine through the blue-green pipeline, never by hand.",
    "The flaky integration test needs the bench Postgres on port 5433.",
    "Rotate the bearer tokens with the CLI, not by editing files.",
]

SCENARIOS.update({
    "lessons": {
        "config": None,
        "seed": [
            *[store(t, [sess("sess-l")]) for t in LESSON_TEXTS],
            fact("blue-green pipeline", "status", "retired", origin="user", confidence=0.9),
            fact("auth tokens", "rotation", "monthly", origin="user", confidence=0.9),
            tool("memory_alias", entity="auth tokens", alias="bearer tokens"),
            tool("memory_graph_relate", src="blue-green pipeline", relation="uses",
                 dst="Jenkins"),
            # a user rewrite supersedes: the chain's superseded event
            fact("blue-green pipeline", "status", "decommissioned", origin="user",
                 confidence=0.9),
            lesson_row("deploy engine to host", "approach", "deploy-engine-to-host", "approach",
                       "Deploy through the blue-green pipeline, never by hand.",
                       "blue-green pipeline", "+", "success", "current", LESSON_TEXTS[0]),
            lesson_row("run integration tests", "pitfall", "run-integration-tests", "pitfall",
                       "The suite needs the bench Postgres on port 5433.", None, "-",
                       "failure", "current", LESSON_TEXTS[1]),
            lesson_row("rotate credentials", "tool-choice", "rotate-credentials", "tool-choice",
                       "Rotate bearer tokens with the CLI.", "bearer tokens", "+", "success",
                       "current", LESSON_TEXTS[2]),
            lesson_row("deploy engine to host", "approach", "deploy-engine-to-host", "approach",
                       "Deploy by hand over ssh.", "blue-green pipeline", "+", "success",
                       "superseded", LESSON_TEXTS[0]),
        ],
        "ops": [
            # compact entries; re_verify directly and through the graph alias
            lesson_search("how do I deploy the engine to a host"),
            lesson_search("integration tests keep failing", verbose=True),
            lesson_search("rotating credentials", top_k=1),
            # a session's search logs its session and open episode
            lesson_search("deploy and rotate", [sess("sess-l")], top_k=3),
            # the chain: facts, the edge and the about-named lesson
            history_chain("blue-green pipeline"),
            # alias names widen the keys: the bearer-tokens lesson joins
            history_chain("auth tokens"),
            # a lesson matched by its task key
            history_chain("deploy engine to host"),
            history_chain("Nobody at all"),
            history_chain("   "),
        ],
    },
})


SCENARIOS.update({
    # The score floor and a disabled retrieval log: lessons under 0.55 are cut
    # and no lesson_search_events row is written.
    "lessons-config": {
        "config": "memory:\n  lessons:\n    min_confidence: 0.55\n"
                  "  retrieval_log:\n    enabled: false\n",
        "seed": SCENARIOS["lessons"]["seed"],
        "ops": [
            lesson_search("deploy and rotate and test", [sess("sess-l")], top_k=5),
        ],
    },
})


# The episode tools (spec EP1-EP12), on templates whose sessions, board rows
# and active-session pointer the Python daemon's own calls created.
SCENARIOS.update({
    "episode-lifecycle": {
        "config": None,
        "seed": [
            store("First note in session A", [sess("sess-a")]),
            store("A note in session B", [sess("sess-b")]),
            store("Closed session note", [sess("sess-closed")]),
            {"op": "http", "method": "POST", "path": "/api/hook/session-end",
             "body": {"session_id": "sess-closed"}},
        ],
        "ops": [
            # nested under the session root, then under the open sub-episode
            ep_start("Sub A", [sess("sess-a")], hint="porting the episode tools"),
            ep_start("Sub A2", [sess("sess-a")]),
            store("A note inside the nested sub-episode", [sess("sess-a")]),
            # each end pops one level; the session root is never closed
            ep_end([sess("sess-a")]),
            ep_end([sess("sess-a")]),
            ep_end([sess("sess-a")]),
            # another session's sub-episode becomes the global current leaf
            ep_start("Sub B", [sess("sess-b")]),
            # the handle anchors to its own root, not the current leaf
            ep_start("Handle sub", episode="@root:sess-a:12"),
            ep_end(episode="@root:sess-a:12"),
            ep_end(episode="@root:sess-a:12"),
            ep_end([sess("sess-b")]),
            ep_end([sess("sess-b")]),
            # an unknown handle degrades to the header's session, which opens
            # its root lazily, and warns
            ep_start("Unknown handle", [sess("sess-c")], episode="ffffffffffff"),
            # an end refuses an unknown or short handle
            ep_end(episode="ffffffffffff"),
            ep_end(episode="abc"),
            # no handle and no session: the open current leaf is another
            # session's
            ep_end(episode=""),
            # an empty title is a title
            ep_start("", [sess("sess-d")]),
            # nothing open for the session
            ep_end([sess("sess-e")]),
            # no session for a title either
            session_title("No session on this request"),
            ep_summary("@root:sess-a:32"),
            ep_summary("@ep:Sub A2"),
            ep_summary("ffffffffffffffffffffffffffffffff"),
            # a closed root: an end refuses its handle, a start reopens it
            ep_end(episode="@root:sess-closed:12"),
            ep_start("Reopened by its handle", episode="@root:sess-closed:12"),
            ep_summary("@root:sess-closed:32"),
        ],
    },
    "session-title": {
        "config": None,
        "seed": [
            store("First note in session A", [sess("sess-a")], tags=["zeta"]),
            store("Second note in session A", [sess("sess-a")], tags=["alpha", "zeta"]),
            store("A note in session B", [sess("sess-b")]),
            # board rows under the sessions: one the title may name, one an
            # agent named, one in another session
            register("sess-a"),
            register("sess-a", name="Named by its agent"),
            register("sess-b"),
            store("Closed session note", [sess("sess-closed")]),
            {"op": "http", "method": "POST", "path": "/api/hook/session-end",
             "body": {"session_id": "sess-closed"}},
            {"op": "http", "method": "GET", "path": "/api/hook/session-start?session_id=sess-ptr"},
        ],
        "ops": [
            # the root, its entries' stamps and the unnamed board row
            session_title("  Pseudolife-MCP - episode tools  ", [sess("sess-a")]),
            # idempotent: nothing changes
            session_title("Pseudolife-MCP - episode tools", [sess("sess-a")]),
            session_title("   ", [sess("sess-a")]),
            # by handle, without a header
            session_title("Named through the handle", episode="@root:sess-b:12"),
            session_title("Unknown handle", episode="ffffffffffff"),
            session_title("Short handle", episode="abc"),
            # no header: the active-session pointer's root
            session_title("Named by the pointer"),
            # a session with no root yet opens one under the title
            session_title("A brand new session", [sess("sess-new")]),
            # a closed root reopens and takes the title
            session_title("Resumed by its key", [sess("sess-closed")]),
            # a reserved name retitles the session but names no board row
            session_title("maintainer notes", [sess("sess-a")]),
            # the pointer's session nests and pops without a header
            ep_start("Pointer sub"),
            ep_end(),
            ep_summary("@root:sess-a:32"),
        ],
    },
})


# ── per-side argument templates ───────────────────────────────────────────

# memory_supersede (spec CO1-CO10): corrections by id and by exact text on
# templates the Python daemon's own tools wrote. The duplicate row and the
# trace rows are SQL seeds: no tool writes a live exact duplicate, and only
# the dream writes memory_traces.
def supersede(new_text, headers=(), **kw) -> dict:
    return {"op": "tool", "name": "memory_supersede", "args": {"new_text": new_text, **kw},
            "headers": list(headers)}


# Scenarios whose operations may include ``external`` writes.
EXTERNAL_WRITES = {"supersede-branches", "reinstate", "consolidation-candidates"}


def external(statement: str) -> dict:
    """An out-of-band write, run on both banks between two calls."""
    return {"op": "sql", "sql": statement, "external": True}


def trace_sql(text, entity_norm, attribute_norm) -> dict:
    """Seed-only: a memory_traces row tying ``text``'s entry to a slot."""
    return sql("INSERT INTO memory_traces (entity_norm, attribute_norm, entry_id, created_at) "
               f"SELECT '{entity_norm}', '{attribute_norm}', id, 1000.0 FROM entries "
               f"WHERE text = '{text}' ORDER BY id LIMIT 1")


SUPERSEDE_SEED = [
    store("The daemon port is 8765.", [sess("sess-a")]),
    store("Our build server hostname is ci-box-2."),
    store("According to the docs, the bank volume is external."),
    store("Never deploy on Fridays."),
    # a retired "Alpha config is red." twin and a live one
    store("Alpha config is red."),
    supersede("Alpha config is blue.", old_text="Alpha config is red."),
    supersede("Alpha config is red.", old_text="Alpha config is blue."),
    store("Gamma retired note."),
    supersede("Gamma replacement note.", old_text="Gamma retired note."),
    # derived slots: a live one, one whose only member was removed (named by
    # its records), and one no record names (named by its key)
    store("Payments DB host is 10.0.0.5 per the runbook."),
    fact("Payments DB", "host", "10.0.0.5", origin="user", confidence=0.9),
    set_add("Home Lab", "hosts", "alpha"),
    set_remove("Home Lab", "hosts", "alpha"),
    trace_sql("Payments DB host is 10.0.0.5 per the runbook.", "payments-db", "host"),
    trace_sql("Payments DB host is 10.0.0.5 per the runbook.", "home-lab", "hosts"),
    trace_sql("Payments DB host is 10.0.0.5 per the runbook.", "old-box", "ip"),
    store("Omega note behind many derived slots."),
    sql("INSERT INTO memory_traces (entity_norm, attribute_norm, entry_id, created_at) "
        "SELECT 'slot-' || lpad(g::text, 3, '0'), 'attr', e.id, 1000.0 FROM entries e, "
        "generate_series(1, 52) AS g WHERE e.text = 'Omega note behind many derived slots.'"),
    store("Delta duplicated note."),
    store("Epsilon row to vanish."),
    store("Zeta row retired behind the daemon's back."),
    dict(sql("INSERT INTO entries (band, text, embedding, surprise, ts, access_count, source, "
             "episode_id, episode_title, tags, slots, authority, distortion_tolerance, "
             "dream_state) SELECT band, text, embedding, surprise, ts + 1, 0, 'user', "
             "episode_id, episode_title, tags, slots, authority, distortion_tolerance, "
             "dream_state FROM entries WHERE text = 'Delta duplicated note.'"), after_stop=True),
]

SCENARIOS.update({
    "supersede-branches": {
        "config": None,
        "seed": SUPERSEDE_SEED,
        "ops": [
            # by id, with a session header
            supersede("The daemon port is 8766.", [sess("sess-a")],
                      entry_id="@id:The daemon port is 8765."),
            # the retired source is resident as retired; the replacement is resident
            supersede("x", entry_id="@id:The daemon port is 8765."),
            supersede("The daemon port is 8767.", old_text="The daemon port is 8766."),
            # by exact text, an observation
            supersede("Our build server hostname is ci-box-3.",
                      old_text="Our build server hostname is ci-box-2."),
            # labels: quoted carries over; the new text's directive wins its axis
            supersede("The bank volume is internal now.",
                      old_text="According to the docs, the bank volume is external."),
            supersede("Always deploy on Fridays after review.", old_text="Never deploy on Fridays."),
            # a retired twin does not make an exact text ambiguous
            supersede("Alpha config is green.", old_text="Alpha config is red."),
            # the derived slots, live first, the cap's totals
            supersede("Payments DB host is 10.0.0.6 per the runbook.",
                      old_text="Payments DB host is 10.0.0.5 per the runbook."),
            # fifty-two derived slots: the first fifty, the total, truncated
            supersede("Omega note behind fewer derived slots.",
                      old_text="Omega note behind many derived slots."),
            # refusals: nothing changes
            supersede("x", old_text="Gamma replacement note.", entry_id=1),
            supersede("x"),
            supersede("x", entry_id=0),
            supersede("x", entry_id=-3),
            supersede("x", entry_id=99999999999999999999),
            supersede("x", old_text="   "),
            supersede("   ", old_text="Gamma replacement note."),
            supersede("x", entry_id=999999),
            supersede("x", old_text="no such note"),
            supersede("x", old_text="Gamma retired note."),
            supersede("x", entry_id="@id:Gamma retired note."),
            supersede("x", old_text="Delta duplicated note."),
            # the meta filter is off by default: this replacement is stored
            supersede("No relevant memories about the deploy.", old_text="Gamma replacement note."),
            # a row deleted behind the daemon's back
            external("DELETE FROM entries WHERE text = 'Epsilon row to vanish.'"),
            supersede("x", old_text="Epsilon row to vanish."),
            # a row retired behind the daemon's back: supersede_entries refuses
            external("UPDATE entries SET superseded_at = 1700000000.5, superseded_by_text = 'elsewhere' "
                "WHERE text = 'Zeta row retired behind the daemon''s back.'"),
            supersede("x", old_text="Zeta row retired behind the daemon's back."),
        ],
    },
    # Capacity: the victim is chosen with the target already retired, so a
    # full band evicts the target itself when nothing cheaper is there.
    "supersede-capacity": {
        "config": "memory:\n  miras:\n    preset: custom\n    bands:\n      - name: flat\n        max_entries: 5\n",
        "seed": [
            store("System prompt one, retained.", source="system"),
            store("System prompt two, retained.", source="system"),
            store("System prompt three, retained.", source="system"),
            store("Agent note about the release checklist."),
            store("Thinking trace about the index rebuild.", source="llm_thinking"),
        ],
        "ops": [
            supersede("Agent note about the revised release checklist.",
                      old_text="Agent note about the release checklist."),
            supersede("System prompt one, revised.", old_text="System prompt one, retained."),
        ],
    },
    # A surprise threshold: a correction bypasses the gate, so a replacement
    # close to what is held is stored where a plain store would be refused.
    "supersede-gate": {
        "config": "memory:\n  surprise_threshold: 0.35\n",
        "seed": [store("The release freeze starts on Friday at noon.")],
        "ops": [
            # a paraphrase: a plain store is refused below the threshold
            store("Release freeze begins Friday at noon."),
            supersede("Release freeze begins Friday at noon.",
                      old_text="The release freeze starts on Friday at noon."),
        ],
    },
    # The meta filter refuses a replacement: the whole correction rolls back,
    # the trace invalidation included, and the source stays live.
    "supersede-meta-filter": {
        "config": "memory:\n  meta_filter:\n    enabled: true\n",
        "seed": [
            store("The on-call rotation changes on Mondays."),
            trace_sql("The on-call rotation changes on Mondays.", "on-call", "rotation"),
        ],
        "ops": [
            supersede("No relevant memories about the rotation.",
                      old_text="The on-call rotation changes on Mondays."),
            supersede("The on-call rotation changes on Tuesdays.",
                      old_text="The on-call rotation changes on Mondays."),
        ],
    },
    # Traces off: no derived slots are read, but the invalidations still land.
    "supersede-traces-off": {
        "config": "memory:\n  traces:\n    enabled: false\n",
        "seed": [
            store("The queue depth limit is 64."),
            trace_sql("The queue depth limit is 64.", "queue", "depth-limit"),
        ],
        "ops": [
            supersede("The queue depth limit is 128.", old_text="The queue depth limit is 64."),
        ],
    },
})


# memory_reinstate (spec RI1-RI9): reviewed reinstatements of retirements the
# Python daemon's own supersede wrote. The reviewer is a named principal
# (PSEUDOLIFE_MCP_TOKENS); the singular token is the default principal.
REVIEWER = ("Authorization", "Bearer tok-rev-w2e-0002")
OP_A = "0f1e2d3c-4b5a-4968-8776-655443322110"
OP_B = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
OP_C = "2b3c4d5e-6f70-4b8c-9dae-1f2a3b4c5d6e"
OP_D = "3c4d5e6f-7081-4c9d-aebf-2a3b4c5d6e7f"
OP_E = "4d5e6f70-8192-4dae-bfc0-3b4c5d6e7f80"
OP_F = "5e6f7081-92a3-4ebf-c0d1-4c5d6e7f8091"


def reinstate(text, headers=(REVIEWER,), **kw) -> dict:
    args = {"entry_id": f"@id:{text}", "operation_id": OP_A,
            "expected_text_sha256": f"@sha:text:{text}",
            "expected_source_sha256": f"@sha:source:{text}",
            "expected_superseded_at": f"@sup:{text}",
            "expected_superseded_by_text_sha256": f"@sha:by:{text}",
            "evidence_packet_sha256": "ab" * 32, "reviewer_ids": ["reviewer-1", "Zoë"],
            "reason": "the replacement was wrong"}
    args.update(kw)
    return {"op": "tool", "name": "memory_reinstate", "args": args, "headers": list(headers)}


TTL = "The cache TTL is 300 seconds."
SCENARIOS.update({
    "reinstate": {
        "config": None,
        "env": {"PSEUDOLIFE_MCP_TOKENS": "tok-rev-w2e-0002:reviewer"},
        "seed": [
            store(TTL),
            supersede("The cache TTL is 600 seconds.", old_text=TTL),
            store("Backups run nightly at 02:00."),
            trace_sql("Backups run nightly at 02:00.", "backups", "schedule"),
            supersede("Backups run nightly at 03:00.", old_text="Backups run nightly at 02:00."),
            store("The release train leaves on Tuesdays."),
            store("The legacy importer is retired."),
            dict(sql("UPDATE entries SET superseded_at = 1700000000.25, superseded_by_text = NULL "
                     "WHERE text = 'The legacy importer is retired.'"), after_stop=True),
            store("The proxy listens on port 3128."),
            # entry 8: deleted behind the daemons' backs below
            store("Feature flags live in the config service."),
            store("The audit log rotates weekly."),
            sql("UPDATE entries SET superseded_at = 1700000000, superseded_by_text = 'older text' "
                "WHERE text = 'The audit log rotates weekly.'", after_stop=True),
        ],
        "ops": [
            # the default principal is refused before anything runs
            reinstate(TTL, headers=()),
            # request validation, in order
            reinstate(TTL, entry_id=0),
            reinstate(TTL, operation_id="not-a-uuid"),
            reinstate(TTL, operation_id=OP_A.upper()),
            reinstate(TTL, operation_id="{" + OP_A + "}"),
            reinstate(TTL, operation_id="urn:uuid:" + OP_A),
            reinstate(TTL, operation_id=OP_A.replace("-", "")),
            reinstate(TTL, operation_id="0x" + OP_A.replace("-", "")[2:]),
            reinstate(TTL, expected_text_sha256="AB" * 32),
            reinstate(TTL, expected_superseded_at="inf"),
            reinstate(TTL, reviewer_ids=[]),
            reinstate(TTL, reviewer_ids=["  "]),
            reinstate(TTL, reason="   "),
            # the target
            reinstate(TTL, entry_id=999999),
            reinstate(TTL, entry_id=99999999999999999999),
            reinstate("The release train leaves on Tuesdays."),
            reinstate("The legacy importer is retired."),
            reinstate(TTL, expected_text_sha256="0" * 64),
            reinstate("Backups run nightly at 02:00.", operation_id=OP_B),
            # committed, then replayed, then a different request under the UUID
            reinstate(TTL),
            reinstate(TTL),
            reinstate(TTL, reason="a different reason"),
            # retired again by a correction: the replay reports it
            supersede("The cache TTL is 900 seconds.", old_text=TTL),
            reinstate(TTL),
            # an integral retirement time: the fingerprint carries 1700000000.0
            reinstate("The audit log rotates weekly.", operation_id=OP_F,
                      expected_superseded_at=1700000000),
            # the target's row is mirrored before the checks: retired, then
            # inserted, behind the daemons' backs
            external("UPDATE entries SET superseded_at = 1700000002.5, "
                     "superseded_by_text = 'retired elsewhere' "
                     "WHERE text = 'The proxy listens on port 3128.'"),
            reinstate("The proxy listens on port 3128.", operation_id=OP_C),
            external("INSERT INTO entries (band, text, embedding, surprise, ts, access_count, "
                     "source, superseded_at, superseded_by_text, tags, slots, dream_state) "
                     "SELECT band, 'Inserted elsewhere, then retired.', embedding, surprise, "
                     "ts + 1, 0, 'agent', 1700000003.5, 'replacement elsewhere', '[]'::jsonb, "
                     "'[]'::jsonb, 'pending' FROM entries "
                     "WHERE text = 'The proxy listens on port 3128.'"),
            reinstate("Inserted elsewhere, then retired.", operation_id=OP_D),
            # deleted: the mirror drops the resident entry, which then cannot
            # be corrected either
            external("DELETE FROM entries WHERE text = 'Feature flags live in the config service.'"),
            reinstate("Feature flags live in the config service.", operation_id=OP_E, entry_id=8),
            supersede("Feature flags live in the deploy repo.",
                      old_text="Feature flags live in the config service."),
            # a replay after the row is gone reports it missing
            external(f"DELETE FROM entries WHERE text = '{TTL}'"),
            reinstate(TTL),
        ],
    },
})


# memory_forget (spec FG1-FG16) over the four stores, on templates whose
# entries, facts, world facts, graph edge and slot reads the Python daemon's
# own tools wrote. The digest, the trace rows, the lineage lessons and their
# audits, and the edge's agent origin and evidence are SQL seeds: only the
# dream writes those.
def forget(scope, headers=(), **kw) -> dict:
    return {"op": "tool", "name": "memory_forget", "args": {"scope": scope, **kw},
            "headers": list(headers)}


def fact_get(entity, attribute) -> dict:
    return tool("memory_fact_get", entity=entity, attribute=attribute)


def lineage_lesson(task, aspect, task_norm, aspect_norm, lesson, about, source_texts,
                   embedding_text) -> list[dict]:
    """Seed-only: a current lesson whose provenance and synthesis lineage
    audit name the entries with ``source_texts`` (by their ids), its
    embedding copied from ``embedding_text``'s entry."""
    def q(v):
        return "NULL" if v is None else "'" + v.replace("'", "''") + "'"
    texts = ", ".join(q(t) for t in source_texts)
    return [
        sql("INSERT INTO lessons (entity, attribute, entity_norm, attribute_norm, value, about, "
            "polarity, outcome, status, confidence, origin, support, provenance, asserted_at, "
            "last_confirmed, embedding) SELECT "
            f"{q(task)}, {q(aspect)}, {q(task_norm)}, {q(aspect_norm)}, {q(lesson)}, {q(about)}, "
            "'+', 'success', 'current', 0.7, 'agent', '[\"signal:1\"]', "
            "(SELECT jsonb_agg('entry:' || id ORDER BY id) FROM entries "
            f"WHERE text IN ({texts})), 1700000000, 1700000000, embedding "
            f"FROM entries WHERE text = {q(embedding_text)}", after_stop=True),
        sql("INSERT INTO store_decisions (store, entity_norm, attribute_norm, action, "
            "decided_by, reason, record, decided_at) SELECT 'lesson', "
            f"{q(task_norm)}, {q(aspect_norm)}, 'lineage', 'lesson_synthesis', "
            "'verified_dependencies', jsonb_build_object('lesson', "
            f"{q(lesson)}, 'asserted_at', 1700000000, 'source_entry_ids', "
            f"(SELECT jsonb_agg(id ORDER BY id) FROM entries WHERE text IN ({texts}))), "
            "1700000000.5", after_stop=True),
    ]


DIGEST_TEXT = "Digest of the deploy checklist session."
FORGET_SEED = [
    store("Probe entry alpha.", [sess("sess-f")], source="status"),
    store("Probe entry beta.", [sess("sess-f")], source="status"),
    store("Probe entry gamma.", source="status"),
    store("Keep the deploy checklist current.", [sess("sess-f")]),
    store("Scratch entry with a tag.", tags=["Scratch"]),
    store("Payments DB moved to host 10.0.0.9 per the runbook.", [sess("sess-g")]),
    store("Payments DB listens on 5432 per the on-call.", [sess("sess-k")]),
    fact("Payments DB", "host", "10.0.0.9", origin="user", confidence=0.9),
    fact("Payments DB", "port", "5432", origin="user", confidence=0.9),
    store("The backup job runs at 02:00 nightly.", [sess("sess-h")]),
    store("The flaky test needs a warm cache.", [sess("sess-h")]),
    store("A sess-h aside about lunch.", [sess("sess-h")]),
    tool("memory_graph_relate", src="flaky test", relation="uses", dst="warm cache"),
    # the digest of sess-f's root, retired as history by a source forget
    sql("INSERT INTO entries (band, text, embedding, surprise, ts, access_count, source, "
        "episode_id, episode_title, tags, slots, dream_state) SELECT band, "
        f"'{DIGEST_TEXT}', embedding, surprise, ts + 1, 0, 'digest', episode_id, "
        "episode_title, '[]'::jsonb, '[]'::jsonb, 'pending' FROM entries "
        "WHERE text = 'Keep the deploy checklist current.'", after_stop=True),
    # host rests on one entry; port on two, one of which survives
    trace_sql("Payments DB moved to host 10.0.0.9 per the runbook.", "payments-db", "host"),
    trace_sql("Payments DB moved to host 10.0.0.9 per the runbook.", "payments-db", "port"),
    trace_sql("Payments DB listens on 5432 per the on-call.", "payments-db", "port"),
    # one lesson rests on the backup entry alone, one on it and a survivor
    *lineage_lesson("schedule backups", "timing", "schedule-backups", "timing",
                    "Run backups at night.", None, ["The backup job runs at 02:00 nightly."],
                    "The backup job runs at 02:00 nightly."),
    *lineage_lesson("schedule backups", "checks", "schedule-backups", "checks",
                    "Check the deploy checklist after a backup.", "Payments DB",
                    ["The backup job runs at 02:00 nightly.", "A sess-h aside about lunch."],
                    "The backup job runs at 02:00 nightly."),
    # the dream's edge: agent origin, the flaky-test entry its evidence
    sql("UPDATE edges SET origin = 'agent' WHERE relation = 'uses'", after_stop=True),
    sql("INSERT INTO edge_evidence (edge_id, entry_id) SELECT g.id, e.id FROM edges g, "
        "entries e WHERE g.relation = 'uses' "
        "AND e.text = 'The flaky test needs a warm cache.'", after_stop=True),
]

SCENARIOS.update({
    "forget-memory": {
        "config": "memory:\n  delete_confirm_threshold: 2\n",
        "seed": FORGET_SEED,
        "ops": [
            # a filter is required; an empty string is no filter here
            forget("memory"),
            forget("memory", text=""),
            # nothing matches: the note (an empty text is still a filter below)
            forget("memory", text="no such entry"),
            forget("memory", text="", source="status"),
            # a source and its session's digest together: the digest is kept
            # as retired history, the session root queued for regeneration
            forget("memory", substring="deploy checklist"),
            # over the threshold: refused, nothing removed; then confirmed
            forget("memory", source="status"),
            forget("memory", source="status", confirm_bulk=True),
            # the tag filter normalizes
            forget("memory", tag=" SCRATCH "),
            # the cascade: host loses its only source and retires; port keeps one
            forget("memory", text="Payments DB moved to host 10.0.0.9 per the runbook."),
            fact_get("Payments DB", "host"),
            fact_get("Payments DB", "port"),
            # the lesson resting only on this entry retires, with its audit
            forget("memory", text="The backup job runs at 02:00 nightly."),
            lesson_search("when should backups run"),
            # the dream edge resting only on this entry retires
            forget("memory", text="The flaky test needs a warm cache."),
            # by episode id
            forget("memory", episode="@root:sess-h:32"),
            # a digest-only selection deletes the digest
            forget("memory", source="digest"),
        ],
    },
    "forget-slots": {
        "config": None,
        "seed": [
            store("A note so the bank is not empty."),
            fact("Payments DB", "host", "10.0.0.5", origin="user", confidence=0.9),
            fact("Payments DB", "host", "10.0.0.6", origin="user", confidence=0.9),
            fact("Payments DB", "port", "5432", origin="user", confidence=0.9),
            fact("Old Router", "ip", "192.168.0.1", origin="user", confidence=0.9),
            fact_get("Payments DB", "host"),
            fact_get("Payments DB", "port"),
            world_set("Rust", "latest stable version", "1.90.0",
                      source_url="https://blog.rust-lang.org/"),
            world_set("Rust", "edition", "2024", source_url="https://doc.rust-lang.org/"),
            world_set("Python", "latest version", "3.13.0", source_url="https://www.python.org/"),
            *lineage_lesson("deploy engine to host", "approach", "deploy-engine-to-host",
                            "approach", "Deploy through the pipeline.", "Payments DB",
                            ["A note so the bank is not empty."],
                            "A note so the bank is not empty."),
            *lineage_lesson("deploy engine to host", "pitfall", "deploy-engine-to-host",
                            "pitfall", "Never deploy on Fridays.", None,
                            ["A note so the bank is not empty."],
                            "A note so the bank is not empty."),
        ],
        "ops": [
            # the entity is required for these scopes
            forget("fact"),
            forget("world", entity=""),
            forget("fact", entity="Nobody"),
            # one slot, every version, its read counter too
            forget("fact", entity="payments db", attribute="HOST"),
            fact_get("Payments DB", "host"),
            # the whole entity
            forget("fact", entity="Old Router"),
            # world facts retire with an audit row; the slot is rewritten
            forget("world", entity="rust", attribute="Edition"),
            world_search("rust edition"),
            forget("world", entity="Rust"),
            forget("world", entity="Rust"),
            # lessons likewise; the rewritten row is linked to its entities
            forget("lesson", entity="Deploy Engine To Host", attribute="approach"),
            lesson_search("how do I deploy the engine"),
            forget("lesson", entity="deploy engine to host"),
            forget("lesson", entity="nothing here"),
        ],
    },
})


# memory_consolidation_candidates (spec CC1-CC9), read-only, on templates
# whose entries (near-duplicates across sessions, a retired one, a tagged
# one) the Python daemon's own tools wrote.
def candidates(headers=(), **kw) -> dict:
    return {"op": "tool", "name": "memory_consolidation_candidates", "args": kw,
            "headers": list(headers)}


STAGING = "staging database host"
SCENARIOS.update({
    "consolidation-candidates": {
        "config": None,
        "seed": [
            store("The staging database lives on host db-stage-1.", [sess("sess-c1")]),
            store("Staging DB host is db-stage-1.", [sess("sess-c2")]),
            store("db-stage-1 hosts the staging database.", [sess("sess-c3")], source="user"),
            store("Our CI runs on GitHub Actions.", [sess("sess-c1")]),
            store("Continuous integration uses GitHub Actions runners.", [sess("sess-c2")]),
            store("I like oat milk in my coffee.", [sess("sess-c1")]),
            store("Deploys happen on Tuesdays after the staging database check.",
                  [sess("sess-c1")], tags=["ops"]),
            store("The staging database used to be on db-stage-0.", [sess("sess-c1")]),
            supersede("The staging database moved off db-stage-0 to db-stage-1.",
                      old_text="The staging database used to be on db-stage-0."),
        ],
        "ops": [
            # no anchor (an empty query is none): nothing to cluster
            candidates(),
            candidates(query=""),
            # the query anchor: retrieval (access accrual), retired entries
            # excluded, clusters by cosine to the seed
            candidates(query=STAGING),
            candidates(query=STAGING, min_cohesion=0.95),
            candidates(query="continuous integration", sources=["agent"], top_k=5),
            candidates(query=STAGING, tags=[" OPS "]),
            candidates(query="what do we run", min_cluster_size=1, max_clusters=2),
            candidates(query=STAGING, min_cohesion="nan"),
            candidates(query=STAGING, episode="@root:sess-c2:32"),
            # the episode anchor: its entries in band order, score 1.0
            candidates(episode="@root:sess-c1:32", min_cohesion=0.3),
            candidates(episode="@root:sess-c1:32", sources=["user"]),
            candidates(episode="@root:sess-c1:32", tags=["ops"], min_cluster_size=1),
            # a row gone behind the daemons' backs is filtered out
            external("DELETE FROM entries WHERE text = 'Staging DB host is db-stage-1.'"),
            candidates(query=STAGING),
        ],
    },
})


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
        elif isinstance(v, str) and v.startswith("@txmid:"):
            # ``@txmid:<entity_norm>:<attribute_norm>``: the midpoint between
            # the slot's first two transaction times (a cutoff that keeps
            # exactly the first version); both banks hold the same clocks.
            _, ent, attr = v.split(":")
            with psycopg.connect(dsn) as c:
                rows = c.execute("SELECT tx_time FROM facts WHERE entity_norm = %s "
                                 "AND attribute_norm = %s ORDER BY tx_time LIMIT 2",
                                 (ent, attr)).fetchall()
            v = (rows[0][0] + rows[1][0]) / 2
        elif isinstance(v, str) and v.startswith("@ep:"):
            # ``@ep:<title>``: the first episode with that title, whole.
            title = v[len("@ep:"):]
            with psycopg.connect(dsn) as c:
                row = c.execute("SELECT id FROM episodes WHERE title = %s "
                                "ORDER BY started_at LIMIT 1", (title,)).fetchone()
            v = row[0] if row else "0" * 32
        elif isinstance(v, str) and v.startswith(("@id:", "@idn:")):
            if v.startswith("@idn:"):
                _, n, text = v.split(":", 2)
            else:
                n, text = 0, v[len("@id:"):]
            import hashlib
            with psycopg.connect(dsn) as c:
                rows = c.execute("SELECT id FROM entries WHERE text = %s ORDER BY id",
                                 (text,)).fetchall()
                if not rows:
                    rows = c.execute(
                        "SELECT entry_id FROM entry_reinstatement_decisions "
                        "WHERE entry_text_sha256 = %s ORDER BY decided_at LIMIT 1",
                        (hashlib.sha256(text.encode("utf-8")).hexdigest(),)).fetchall()
            v = rows[int(n)][0] if len(rows) > int(n) else 999999
        elif isinstance(v, str) and v.startswith(("@sup:", "@sha:")):
            # The text and source hashes read the first row with the text.
            # The retirement prefers the first reinstatement decision's prior
            # values for that row (a replay repeats its request), then the
            # first retired row with the text; a live row yields values that
            # cannot match.
            import hashlib
            if v.startswith("@sup:"):
                field, text = "sup", v[len("@sup:"):]
            else:
                _, field, text = v.split(":", 2)
            text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            with psycopg.connect(dsn) as c:
                row = c.execute("SELECT id, text, source FROM entries WHERE text = %s "
                                "ORDER BY id LIMIT 1", (text,)).fetchone()
                if row is None:
                    # A deleted row resolves through its first decision (the
                    # audit has no foreign key): a replay repeats its request.
                    gone = c.execute(
                        "SELECT entry_id, entry_source_sha256 FROM "
                        "entry_reinstatement_decisions WHERE entry_text_sha256 = %s "
                        "ORDER BY decided_at LIMIT 1", (text_sha,)).fetchone()
                prior = c.execute(
                    "SELECT prior_superseded_at, prior_superseded_by_text FROM "
                    "entry_reinstatement_decisions WHERE entry_id = %s ORDER BY decided_at "
                    "LIMIT 1", (row[0] if row else (gone[0] if gone else -1),)).fetchone()
                retired = c.execute(
                    "SELECT superseded_at, superseded_by_text FROM entries WHERE text = %s "
                    "AND superseded_at IS NOT NULL ORDER BY id LIMIT 1", (text,)).fetchone()
            retirement = prior or retired or (1.0, "")
            if field == "sup":
                v = retirement[0]
            elif row is None and gone is not None and field in ("text", "source"):
                v = text_sha if field == "text" else gone[1]
            else:
                value = {"text": row[1] if row else "", "source": row[2] if row else "",
                         "by": retirement[1] or ""}[field]
                v = hashlib.sha256(value.encode("utf-8")).hexdigest()
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


def extract_fact_numeric(state: dict, table: str = "facts") -> dict:
    """Pull a canonical table's embeddings out by row id, leaving tokens."""
    t = _table(state, table)
    if t is None:
        return {}
    out = {}
    i_id, i_emb = _col(t, "id"), _col(t, "embedding")
    if i_id is None or i_emb is None:
        return out
    for row in t["rows"]:
        emb = row[i_emb]
        out[row[i_id]] = [float(x) for x in emb.strip("[]").split(",")] if emb else None
        row[i_emb] = "<embedding>" if emb is not None else None
    return out


def extract_lesson_scores(state: dict) -> dict:
    """Pull each ``lesson_search_events.served`` score out by row id, leaving
    tokens (compared within ``SCORE_TOLERANCE``)."""
    t = _table(state, "lesson_search_events")
    if t is None:
        return {}
    i_id, i_served = _col(t, "id"), _col(t, "served")
    out = {}
    for row in t["rows"]:
        served = row[i_served]
        if isinstance(served, list):
            out[row[i_id]] = [x.get("score") if isinstance(x, dict) else None for x in served]
            row[i_served] = [{**x, "score": "<score>"} if isinstance(x, dict) else x
                             for x in served]
    return out


def compare_lesson_scores(py: dict, rs: dict, stats: dict) -> list[str]:
    diffs = []
    for rid in sorted(set(py) & set(rs)):
        for i, (a, b) in enumerate(zip(py[rid], rs[rid])):
            if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
                if a != b:
                    diffs.append(f"lesson_search_events[{rid}].served[{i}].score: {a} vs {b}")
                continue
            d = abs(a - b)
            stats["lesson_score_max_diff"] = max(stats.get("lesson_score_max_diff", 0.0), d)
            if d > SCORE_TOLERANCE:
                diffs.append(f"lesson_search_events[{rid}].served[{i}].score: {a} vs {b}")
    return diffs


def compare_fact_numeric(py: dict, rs: dict, tol: dict, stats: dict,
                         table: str = "facts") -> list[str]:
    diffs = []
    for fid in sorted(set(py) & set(rs)):
        ea, eb = py[fid], rs[fid]
        if (ea is None) != (eb is None):
            diffs.append(f"{table}[{fid}].embedding: python/rust presence mismatch")
        elif ea is not None and len(ea) != len(eb):
            diffs.append(f"{table}[{fid}].embedding: length python {len(ea)} vs rust {len(eb)}")
        elif ea:
            mad = max(abs(x - y) for x, y in zip(ea, eb))
            cos = sum(x * y for x, y in zip(ea, eb)) / (
                math.sqrt(sum(x * x for x in ea)) * math.sqrt(sum(y * y for y in eb)))
            stats["max_abs_diff"] = max(stats.get("max_abs_diff", 0.0), mad)
            stats["min_cosine"] = min(stats.get("min_cosine", 1.0), cos)
            if tol and (mad > tol["embedding_max_abs_diff"] or cos < tol["embedding_min_cosine"]):
                diffs.append(f"{table}[{fid}].embedding: max abs diff {mad:.3e}, cosine {cos:.9f}")
    return diffs


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


def _recent_clock(v):
    """A clock-written value keeps null-or-not; an explicit past one stays."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)             and abs(v - time.time()) < RECENT_S:
        return "<clock>"
    return v


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
    numeric["facts"] = extract_fact_numeric(state)
    numeric["world_facts"] = extract_fact_numeric(state, "world_facts")
    numeric["lesson_scores"] = extract_lesson_scores(state)
    emap = episode_map(state)
    for qualified, t in state["rows"].items():
        name = qualified.split(".", 1)[1]
        for idx, col in enumerate(t["columns"]):
            if (name, col) in CLOCK:
                for row in t["rows"]:
                    row[idx] = _clock(row[idx])
            elif (name, col) in RECENT_CLOCK:
                for row in t["rows"]:
                    row[idx] = _recent_clock(row[idx])
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
                elif key == "cortex_supersession_log" and isinstance(value, list):
                    row[1] = [{**x, "timestamp": _clock(x.get("timestamp"))}
                              if isinstance(x, dict) else x for x in value]
        if name == "store_decisions" and "record" in t["columns"]:
            ri = t["columns"].index("record")
            for row in t["rows"]:
                if isinstance(row[ri], dict) and "superseded_at" in row[ri]:
                    row[ri] = {**row[ri], "superseded_at": _clock(row[ri]["superseded_at"])}
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
    diffs = compare_fact_numeric(py.get("facts", {}), rs.get("facts", {}), tol, stats)
    diffs += compare_fact_numeric(py.get("world_facts", {}), rs.get("world_facts", {}), tol,
                                  stats, "world_facts")
    diffs += compare_lesson_scores(py.get("lesson_scores", {}), rs.get("lesson_scores", {}),
                                   stats)
    skip = ("facts", "world_facts", "lesson_scores")
    py = {k: v for k, v in py.items() if k not in skip}
    rs = {k: v for k, v in rs.items() if k not in skip}
    for eid in sorted(set(py) | set(rs)):
        a, b = py.get(eid), rs.get(eid)
        if a is None or b is None:
            continue  # the row diff reports the missing entry
        ea, eb = a["embedding"], b["embedding"]
        if (ea is None) != (eb is None):
            diffs.append(f"entries[{eid}].embedding: python/rust presence mismatch")
        elif ea is not None and len(ea) != len(eb):
            diffs.append(f"entries[{eid}].embedding: length python {len(ea)} vs rust {len(eb)}")
        elif ea:
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


AGE = re.compile(r"^(?:just now|(\d+) (minute|hour|day)s? ago)$")
AGE_STEP = {"minute": 1, "hour": 2, "day": 3}


def _age_step(text):
    m = AGE.match(text) if isinstance(text, str) else None
    if not m:
        return None
    return (0, 0) if m.group(1) is None else (AGE_STEP[m.group(2)], int(m.group(1)))


def _age_adjacent(x, y) -> bool:
    """One step apart: the same unit one apart, or across a unit boundary
    ("just now" / "1 minute ago", "59 minutes ago" / "1 hour ago", ...)."""
    a, b = _age_step(x), _age_step(y)
    if a is None or b is None:
        return False
    a, b = sorted((a, b))
    if a[0] == b[0]:
        return b[1] - a[1] == 1
    return (a, b) in {((0, 0), (1, 1)), ((1, 59), (2, 1)), ((2, 23), (3, 1))}


def normalize_records(a, b, stats=None):
    """Record clocks keep null-or-not; an ``age`` one step apart compares equal;
    a search ``score`` within ``SCORE_TOLERANCE`` compares equal."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in set(a) & set(b):
            if k in RESPONSE_CLOCK:
                a[k], b[k] = _clock(a[k]), _clock(b[k])
            elif k in RESPONSE_RECENT_CLOCK:
                a[k], b[k] = _recent_clock(a[k]), _recent_clock(b[k])
            elif (k in ("score", "cohesion", "seed_score") and isinstance(a[k], (int, float))
                  and isinstance(b[k], (int, float)) and not isinstance(a[k], bool)):
                d = abs(a[k] - b[k])
                if stats is not None:
                    stats["response_score_max_diff"] = max(
                        stats.get("response_score_max_diff", 0.0), d)
                if d <= SCORE_TOLERANCE:
                    a[k] = b[k] = "<score>"
            elif k == "age" and a[k] != b[k] and _age_adjacent(a[k], b[k]):
                a[k] = b[k] = "<age>"
            else:
                normalize_records(a[k], b[k], stats)
    elif isinstance(a, list) and isinstance(b, list):
        for x, y in zip(a, b):
            normalize_records(x, y, stats)


def generic_titles(v):
    """A generic session title in a response keeps its shape, not its minute."""
    if isinstance(v, str):
        return "session - <minute>" if GENERIC.match(v) else v
    if isinstance(v, list):
        return [generic_titles(x) for x in v]
    if isinstance(v, dict):
        return {k: generic_titles(x) for k, x in v.items()}
    return v


def compare_response(py: dict, rs: dict, tol: dict, stats: dict) -> list[str]:
    a, b = generic_titles(json.loads(json.dumps(py))), generic_titles(json.loads(json.dumps(rs)))
    normalize_records(a, b, stats)
    out = []
    va, vb = a.get("value"), b.get("value")
    for key in ("surprise", "new_memory_surprise"):
        if isinstance(va, dict) and isinstance(vb, dict) and key in va and key in vb:
            d = abs(float(va[key]) - float(vb[key]))
            stats["response_surprise_max_diff"] = max(
                stats.get("response_surprise_max_diff", 0.0), d)
            # The response is rounded to 4 dp, so one rounding unit is allowed
            # on top of the stored tolerance; the decision (`reason`) stays exact.
            if tol and d > tol["surprise_abs"] + 1e-4:
                out.append(f"{key}: python {va[key]} vs rust {vb[key]}")
            va[key] = vb[key] = "<surprise>"
    out += w1a.diff_values(a, b)
    return out


# ── running ───────────────────────────────────────────────────────────────

def run_root(prefix: str = "w2e") -> Path:
    """Keep each invocation's homes and evidence separate, including on failure."""
    return Path(tempfile.mkdtemp(prefix=f"{prefix}-{os.getpid()}-", dir=daemons.scratch_root()))


def side_env(scn: dict, dsn: str, extra: dict | None = None) -> dict:
    env = {"PSEUDOLIFE_RELEASE_CHECK": "0", "PSEUDOLIFE_PLUGIN_DIR": str(w1a.REPO / "plugin"),
           "PSEUDOLIFE_MCP_DATABASE_URL": dsn, "PSEUDOLIFE_MCP_TOKEN": TOKEN}
    env.update(scn.get("env", {}))
    env.update(extra or {})
    return env


def run_op(op: dict, port: int, mcp: Mcp, dsn: str) -> dict | None:
    if op["op"] == "sleep":
        time.sleep(op["s"])
        return {"isError": False, "value": None}
    if op["op"] == "sql":
        import psycopg
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(op["sql"])
        return {"isError": False, "value": None}
    if op["op"] == "http":
        body = json.dumps(op["body"]).encode() if "body" in op else None
        hdrs = [AUTH] + ([("Content-Type", "application/json")] if body else [])
        r = w1a.call(port, op["method"], op["path"], hdrs, body)
        return {"isError": r["status"] != 200, "value": r.get("json", r.get("bytes"))}
    args = resolve_args(op["args"], dsn)
    headers = [tuple(h) for h in op.get("headers", [])]
    if op.get("abandon"):
        mcp.abandon(op["name"], args, headers)
        if op["name"] == "memory_store":
            wait_landed(dsn, args.get("text", ""))
        else:
            time.sleep(4)  # let a call that survives the disconnect finish
        return {"isError": False, "value": "<abandoned>"}
    return mcp.call(op["name"], args, headers)


def wait_landed(dsn: str, text: str, deadline_s: float = 30.0) -> None:
    """Wait until an abandoned store's row exists, up to the deadline: a
    store that survives its caller's disconnect lands whatever the embedder's
    speed, and one that was cancelled never does (the comparison then shows
    the missing row). A fixed sleep judged a slow debug build's 512-token
    embedding as lost."""
    import psycopg
    stripped = text.strip()  # the stored text (service.py strips it)
    end = time.monotonic() + deadline_s
    with psycopg.connect(dsn, autocommit=True) as conn:
        while time.monotonic() < end:
            if conn.execute("SELECT 1 FROM entries WHERE text = %s", (stripped,)).fetchone():
                return
            time.sleep(0.25)


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
            if op.get("after_stop"):
                continue
            r = run_op(op, d.port, mcp, dsn)
            if r and r["isError"]:
                raise RuntimeError(f"seed op failed: {op} -> {r}")
    finally:
        d.stop()
    for op in scn["seed"]:
        if op.get("after_stop"):
            run_op(op, 0, None, dsn)
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
        clients = {side: Mcp(d.port) for side, d in procs.items()}
        for client in clients.values():
            client.init()
        for i, op in enumerate([{"op": "init"}] + scn["ops"]):
            row = {"step": i, "op": op, "diffs": []}
            if op["op"] != "init":
                py = run_op(op, procs["python"].port, clients["python"], dsns["python"])
                rs = run_op(op, procs["rust"].port, clients["rust"], dsns["rust"])
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
WRITE_TABLES = ("entries", "episodes", "meta", "client_sessions", "facts", "entities",
                "outcome_signals", "world_facts", "memory_trace_invalidations",
                "entry_reinstatement_decisions", "lessons", "edges", "store_decisions",
                "slot_reads")


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
        if eid in ("facts", "world_facts", "lesson_scores") or eid in seen:
            continue  # fact embeddings are compared live only (declared)
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
        client = Mcp(d.port)
        client.init()
        for i, rec in enumerate(golden["steps"]):
            op = rec["op"]
            row = {"step": i, "op": op, "diffs": []}
            rs = run_op(op, d.port, client, dsn) if op["op"] != "init" else None
            sr, nr, er = normalize_state(dbstate.dump(dsn), before)
            if rs is not None:
                row["rust"] = rename_prefixes(rs, er)
                row["diffs"] += compare_response(rec["python"], row["rust"], tol, stats)
            unpack_numeric(rec["numeric_new"], numeric)
            row["diffs"] += compare_numeric(numeric, nr, tol, stats)
            got = pack(sr, full=i == 0)
            if i:
                # Recordings that predate a write table are compared on theirs.
                got["rows"] = {k: v for k, v in got["rows"].items() if k in rec["state"]["rows"]}
            row["diffs"] += [f"db {x}" for x in state_diff(rec["state"], got)]
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
    ap.add_argument("--mutants", nargs="*", choices=MUTANTS)
    args = ap.parse_args()
    args.rust_bin = args.rust_bin.resolve()
    root = run_root()
    names = args.only or list(SCENARIOS)
    tol = {} if args.mode == "calibrate" else json.loads(TOLERANCE_FILE.read_text(encoding="utf-8"))
    results = []
    if args.mode == "mutants":
        caught = {}
        for m in args.mutants or MUTANTS:
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

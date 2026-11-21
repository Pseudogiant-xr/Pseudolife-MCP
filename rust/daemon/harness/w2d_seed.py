"""Seed the W2-D test bank with the candidate writer and guarded SQL inputs.

usage: python w2d_seed.py [paragraphs | --mcp-extra [--fact-extra] [--stats-extra]]
The caller supplies PL_HARNESS_SEED_DSN, PL_HARNESS_SEED_RUST and a disposable home.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from struct import pack, unpack

_DATABASE_TURN_MARKER = Path.home() / ".pseudolife-mcp/locks/test-postgres.turn-enabled"

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

import psycopg
from psycopg.types.json import Json

import daemons
from mcp_wire import Session, sse_data
from rust.harness_support.bank_fixture import assert_fixture_database
from rust.harness_support.database_guard import (
    assert_disposable_database, dsn_database_name, fixture_database_pattern, refuse_production_database,
)

SEED_TOKEN = "synthetic-w2d-fixture-token"
CHILD_TEXT = "Episode filter probe: the child step reran the deploy health check."
RESTATEMENT = "OS: Debian 13"
SEED_DATABASE = fixture_database_pattern("w2d")

DOCS = ["docs/guide/configuration.md", "docs/guide/dreaming.md", "README.md",
        "docs/guide/agent-isolation.md", "CONTRIBUTING.md"]

CRAFTED = [
    ("Remote clients authenticate with a bearer token in the Authorization header.", "assistant", None),
    ("Never run docker compose down -v: the bank volumes are external precisely so that is survivable.", "ops-notes", ["ops"]),
    ("A contender is a competing value parked against a slot instead of overwriting it.", "glossary", ["glossary"]),
    ("My cat is named Jacque and she is a Ragdoll.", "conversation", None),
    ("The staging database host is pg-staging-2 since the migration.", "conversation", ["infra"]),
    ("We deployed release 0.15.0 to the homelab box on Tuesday.", "conversation", ["deploy"]),
    ("First we backed up the bank, then we tagged the rollback image, then we recreated the daemon.", "conversation", ["deploy"]),
    ("Later the clients were updated after the daemon came back healthy.", "conversation", ["deploy"]),
    ("process_chunk_v2 raised a KeyError when the cursor was empty.", "tool_result", ["bug"]),
    ("The error code PLX-4471 means the writer lease is held by another daemon.", "tool_result", ["bug"]),
]
# Stored inside one session episode, so contiguity's same-episode path and the
# event's episode attribution are exercised.
EPISODE_KEY = "sess-w2d-seed"
EPISODE_TEXTS = [
    "Session step one: we opened the incident for the failed deploy.",
    "Session step two: the health check showed the daemon could not reach Postgres.",
    "Session step three: restarting the bench Postgres fixed the deploy.",
    "Session step four: we wrote the deploy runbook note afterwards.",
]
SUPERSEDED = ("The extractor endpoint is http://10.0.0.5:1234/v1 on the old GPU box.",
              "The extractor endpoint moved to http://10.0.0.7:1234/v1 on the new GPU box.")
EVENTS = [
    ("2026-08-08", None, "maintainer", "deployed release 0.14.0 to the homelab box"),
    ("2026-08-12", None, "maintainer", "rolled back the release after a failed health check"),
    ("2026-09-01", None, "agent", "migrated the bank to schema 50"),
    (None, "last spring", "maintainer", "first installed the memory daemon on the homelab box"),
    ("2026-09-20", None, "agent", "deployed the reranker margin gate to the daemon"),
]
FACTS = [
    ("rounding boundary", "confidence", "small decimal confidence", 0.00005, "auto"),
    ("homelab box", "os", "Debian 13", 0.9, "auto"),
    ("staging database", "host", "pg-staging-2", 0.8, "auto"),
    ("daemon", "port", "8765", 0.9, "constraint"),
    ("Jacque", "type", "cat", 0.9, "auto"),
]
SET_MEMBERS = [("homelab box", "services", m) for m in ("postgres", "the memory daemon", "a GPU judge")]
HOMELAB_GPU = ("homelab box", "gpu", "RTX 4090", 0.6, "auto")
EXTRA_FACTS = [
    ("dev box", "cpu", "Ryzen 9 7950X", 0.8, "auto"),
    ("printer", "firmware", "2.1.0", 0.7, "auto"),
]
EXTRA_ALIASES = [("printer", "laser"), ("dev box", "builder"), ("dev box", "printer")]
EXTRA_CURRENT_MEMBERS = [("users", "alice")]
EXTRA_REMOVED_MEMBERS = [("users", "bob"), ("groups", "admins")]


def paragraphs(limit: int) -> list[str]:
    out: list[str] = []
    for rel in DOCS:
        path = REPO / rel
        if not path.exists():
            continue
        for para in re.split(r"\n\s*\n", path.read_text(encoding="utf-8")):
            text = " ".join(para.split())
            if 80 <= len(text) <= 900 and not text.startswith(("|", "```", "<")):
                out.append(text)
    return out[:limit]


def require_fixture(condition, detail):
    if not condition:
        raise RuntimeError(f"seed precondition not met: {detail}")


def tool(session, name, **args):
    reply = session.tool(f"memory_{name}", args)
    frames = sse_data(reply)
    message = json.loads(frames[0] if frames else reply["body"])
    result = message.get("result") or {}
    payload = result.get("structuredContent")
    require_fixture(reply["status"] == 200 and not result.get("isError") and isinstance(payload, dict),
                    f"memory_{name} did not return a structured result")
    return payload


def root_episode(port):
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=120)) as conn:
        conn.request("POST", "/api/episode/start",
                     json.dumps({"session_key": EPISODE_KEY, "title": "W2-D seed session"}),
                     {"Content-Type": "application/json", "Authorization": f"Bearer {SEED_TOKEN}"})
        reply = conn.getresponse()
        payload = json.loads(reply.read())
        handle = payload.get("id") or payload.get("episode_id")
        require_fixture(reply.status == 200 and handle, "candidate did not register the seed episode")
        return handle


def open_bank(dsn):
    name = dsn_database_name(dsn)
    if name is None or not SEED_DATABASE.fullmatch(name):
        raise ValueError("W2-D seeding requires a disposable pl_cf_w2d_* database")
    refuse_production_database(name)
    conn = psycopg.connect(dsn)
    try:
        if assert_disposable_database(conn) != name:
            raise ValueError("W2-D seed connection reached a different database")
    except BaseException:
        conn.close()
        raise
    return conn


def write_base(session, port, docs):
    for text in docs:
        tool(session, "store", text=text, source="docs")
    for text, source, tags in CRAFTED:
        tool(session, "store", text=text, source=source, **({"tags": tags} if tags else {}))
    handle = root_episode(port)
    for text in EPISODE_TEXTS:
        tool(session, "store", text=text, source="session-notes", episode=handle)
    tool(session, "store", text=SUPERSEDED[0], source="conversation")
    tool(session, "supersede", old_text=SUPERSEDED[0], new_text=SUPERSEDED[1])
    for entity, attribute, value, conf, tol in FACTS:
        tool(session, "fact_set", entity=entity, attribute=attribute, value=value,
             confidence=conf, distortion_tolerance=tol)
    for entity, attribute, member in SET_MEMBERS:
        tool(session, "set_add", entity=entity, attribute=attribute, member=member)
    entity, attribute, value, confidence, tolerance = HOMELAB_GPU
    tool(session, "fact_set", entity=entity, attribute=attribute, value=value,
         confidence=confidence, distortion_tolerance=tolerance)


def checked_root(conn):
    roots = conn.execute("SELECT id FROM episodes WHERE title = 'W2-D seed session' "
                         "AND session_key = %s AND ended_at IS NULL AND parent_id IS NULL",
                         (EPISODE_KEY,)).fetchall()
    require_fixture(len(roots) == 1, "expected one open W2-D seed root")
    return roots[0][0]


def write_extras(session, dsn, fact):
    with open_bank(dsn) as conn:
        root = checked_root(conn)
    tool(session, "fact_set", entity="deploy window", attribute="weekday", value="thursday afternoon",
         confidence=0.8, freshness_class="volatile")
    out = tool(session, "fact_set", entity="homelab box", attribute="os", value="Ubuntu 24.04", confidence=0.7)
    require_fixture(out.get("action") == "contested", "the competing OS was not parked")
    tool(session, "store", text=RESTATEMENT, source="notes")
    child = tool(session, "episode_start", title="W2-D child step", episode=root)
    tool(session, "store", text=CHILD_TEXT, source="session-notes", episode=child["id"])
    if fact:
        for entity, attribute, value, confidence, tolerance in EXTRA_FACTS:
            tool(session, "fact_set", entity=entity, attribute=attribute, value=value,
                 confidence=confidence, distortion_tolerance=tolerance)
        for entity, alias in EXTRA_ALIASES:
            tool(session, "alias", entity=entity, alias=alias)
        for attribute, member in EXTRA_CURRENT_MEMBERS + EXTRA_REMOVED_MEMBERS:
            tool(session, "set_add", entity="dev box", attribute=attribute, member=member)
        for attribute, member in EXTRA_REMOVED_MEMBERS:
            tool(session, "set_remove", entity="dev box", attribute=attribute, member=member)
        tool(session, "fact_set", entity="build cache", attribute="state", value="warm",
             confidence=0.6, freshness_class="volatile")


def fixture_key(text):
    # Fixture keys contain only ASCII words separated by single spaces.
    return text.lower().replace(" ", "-")


def check_facts(conn, facts):
    for entity, attribute, value, confidence, tolerance in facts:
        rows = conn.execute("SELECT value, confidence::double precision, distortion_tolerance FROM facts "
                            "WHERE entity_norm=%s AND attribute_norm=%s AND kind='scalar' AND status='current'",
                            (fixture_key(entity), fixture_key(attribute))).fetchall()
        # The confidence column stores REAL. Widening both sides compares the
        # stored float32 exactly, including the deliberately tiny confidence.
        stored_confidence = unpack("f", pack("f", confidence))[0]
        expected_tolerance = None if tolerance == "auto" else tolerance
        require_fixture(rows == [(value, stored_confidence, expected_tolerance)],
                        f"missing or incorrect scalar fact {entity}/{attribute}")


def check_members(conn, entity, attribute, expected):
    rows = conn.execute("SELECT status, value FROM facts WHERE entity_norm=%s AND attribute_norm=%s "
                        "AND kind='member' AND status IN ('current','removed')",
                        (fixture_key(entity), fixture_key(attribute))).fetchall()
    require_fixture(sorted(rows) == sorted(expected), f"incorrect set membership for {entity}/{attribute}")


def check_base(conn, docs, cutoff):
    saved = [text for text, in conn.execute("SELECT text FROM entries WHERE source='docs' AND id > %s ORDER BY id",
                                            (cutoff,))]
    require_fixture(saved == docs, f"expected {len(docs)} document paragraphs after the base")
    notes = conn.execute("SELECT text,source,tags FROM entries WHERE id > %s AND source <> 'docs' "
                         "AND text = ANY(%s) ORDER BY id",
                         (cutoff, [text for text, _, _ in CRAFTED])).fetchall()
    require_fixture(notes == [(text, source, tags or []) for text, source, tags in CRAFTED],
                    "crafted notes or tags are missing")
    root = checked_root(conn)
    steps = [text for text, in conn.execute("SELECT text FROM entries WHERE episode_id = %s "
                                           "AND source = 'session-notes' AND id > %s ORDER BY id", (root, cutoff))]
    require_fixture(steps == EPISODE_TEXTS, "session episode is missing seeded steps")
    pair = conn.execute("SELECT text, superseded_by_text, superseded_at IS NOT NULL FROM entries WHERE id > %s "
                        "AND text = ANY(%s) ORDER BY id", (cutoff, list(SUPERSEDED))).fetchall()
    require_fixture(pair == [(SUPERSEDED[0], SUPERSEDED[1], True), (SUPERSEDED[1], None, False)],
                    "missing superseded entry or replacement")
    check_facts(conn, [*FACTS, HOMELAB_GPU])
    check_members(conn, "homelab box", "services", [("current", member) for _, _, member in SET_MEMBERS])


def finish_base(conn, docs, cutoff):
    check_base(conn, docs, cutoff)
    now = time.time()
    conn.execute("INSERT INTO meta (key,value) VALUES ('active_session_pointer',%s) "
                 "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                 (Json({"session_id": EPISODE_KEY, "ts": now}),))
    for date, phrase, actor, desc in EVENTS:
        # These literals use ASCII words, digits and punctuation.
        desc_norm = re.sub(r"[\s._\-/]+", "-", desc.casefold()).strip("-")
        occurred = f"{date}T00:00:00+00:00" if date else None
        conn.execute("INSERT INTO chronicle_events (occurred_at,occurred_phrase,recorded_at,actor,actor_norm,"
                     "description,description_norm) SELECT %s::timestamptz,%s,%s,%s,%s,%s,%s "
                     "WHERE NOT EXISTS (SELECT 1 FROM chronicle_events WHERE actor_norm=%s AND description_norm=%s "
                     "AND occurred_at IS NOT DISTINCT FROM %s::timestamptz AND invalidated_at IS NULL)",
                     (occurred, phrase, now, actor, actor, desc, desc_norm, actor, desc_norm, occurred))


def finish_extras(conn, fact, stats, cutoff):
    now = time.time()
    root = checked_root(conn)
    child = conn.execute("SELECT e.parent_id FROM episodes e JOIN entries t ON t.episode_id=e.id "
                         "WHERE e.title='W2-D child step' AND t.text=%s AND t.id > %s AND e.parent_id=%s",
                         (CHILD_TEXT, cutoff, root)).fetchall()
    require_fixture(child == [(root,)], "child-episode search input is missing")
    os_facts = conn.execute("SELECT status,value FROM facts WHERE entity_norm='homelab-box' "
                            "AND attribute_norm='os' AND status IN ('current','contested') "
                            "ORDER BY status,value").fetchall()
    require_fixture(os_facts == [("contested", "Ubuntu 24.04"), ("current", "Debian 13")],
                    "current and competing OS facts are missing")
    note = conn.execute("SELECT count(*) FROM entries WHERE source='notes' AND text=%s AND id > %s",
                        (RESTATEMENT, cutoff)).fetchone()
    require_fixture(note == (1,), "new restatement for search deduplication is missing")
    updates = [("deploy-window", "weekday", "thursday afternoon", 30)]
    if fact:
        updates.append(("build-cache", "state", "warm", 60))
    for entity, attribute, value, days in updates:
        age = now - days * 86400
        row = conn.execute("UPDATE facts SET asserted_at=%s,last_confirmed=%s WHERE entity_norm=%s "
                           "AND attribute_norm=%s AND value=%s AND status='current' "
                           "AND freshness_class='volatile' RETURNING id", (age, age, entity, attribute, value)).fetchall()
        require_fixture(len(row) == 1, f"expected one volatile {entity}/{attribute} fact")
    if fact:
        check_facts(conn, EXTRA_FACTS)
        targets = [(alias, fixture_key(entity)) for entity, alias in EXTRA_ALIASES]
        aliases = conn.execute("SELECT a.alias, e.canonical FROM entity_aliases a JOIN entities e ON e.id=a.entity_id "
                               "WHERE a.alias = ANY(%s)", ([alias for alias, _ in targets],)).fetchall()
        require_fixture(sorted(aliases) == sorted(targets), "incorrect persisted fact aliases")
        for attribute in ("users", "groups"):
            expected = [("current", value) for slot, value in EXTRA_CURRENT_MEMBERS if slot == attribute]
            expected += [("removed", value) for slot, value in EXTRA_REMOVED_MEMBERS if slot == attribute]
            check_members(conn, "dev box", attribute, expected)
        entry, = conn.execute("SELECT min(id) FROM entries").fetchone()
        require_fixture(entry is not None, "trace source entry is missing")
        conn.execute("INSERT INTO memory_traces VALUES ('dev-box','cpu',%s,%s)", (entry, now))
        for attribute in ("cpu", "users"):
            conn.execute("INSERT INTO memory_trace_invalidations VALUES "
                         "('dev-box',%s,%s,%s,'source_superseded')", (attribute, entry, now + 3600))
        trace = conn.execute("SELECT entry_id FROM memory_traces WHERE entity_norm='dev-box' "
                             "AND attribute_norm='cpu' AND entry_id=%s", (entry,)).fetchall()
        reverify = conn.execute("SELECT attribute_norm FROM memory_trace_invalidations WHERE entity_norm='dev-box' "
                               "AND source_entry_id=%s AND cause='source_superseded' ORDER BY attribute_norm",
                               (entry,)).fetchall()
        require_fixture(trace == [(entry,)] and reverify == [("cpu",), ("users",)],
                        "source trace and corrected-source warnings are missing")
    if stats:
        require_fixture(conn.execute("SELECT count(*) FROM (SELECT id FROM entries WHERE source='docs' "
                                     "ORDER BY id LIMIT %s) AS docs",
                                     (len(STATS_SERVED),)).fetchone()[0] == len(STATS_SERVED),
                        "statistics need the three graduation candidates")
        stats_extra(conn)


# The three entries are served by 8, 5 and 4 of the eight seeded sessions.
# 5/8 falls below the 0.6 share when another session searches.
STATS_SESSIONS = 8
STATS_SERVED = (8, 5, 4)


def stats_extra(conn):
    assert_fixture_database(conn)
    now = time.time()
    ids = [i for i, in conn.execute("SELECT id FROM entries WHERE source='docs' ORDER BY id LIMIT %s",
                                    (len(STATS_SERVED),))]
    last, = conn.execute("SELECT coalesce(max(id),0) FROM retrieval_events").fetchone()
    last_community, = conn.execute("SELECT coalesce(max(id),0) FROM communities").fetchone()
    event_time = now - 3600
    expected = []
    for s in range(STATS_SESSIONS):
        served = [{"entry_id": e, "rank": i, "score": 0.9}
                  for i, (e, n) in enumerate(zip(ids, STATS_SERVED)) if s < n]
        conn.execute("INSERT INTO retrieval_events (query_text,origin,session_id,served,created_at) "
                     "VALUES (%s,'search',%s,%s,%s)",
                     (f"graduation probe {s}", f"grad-session-{s}", Json(served), event_time))
        expected.append((f"grad-session-{s}", served, event_time))
    got = conn.execute("SELECT session_id,served,created_at FROM retrieval_events "
                       "WHERE id > %s ORDER BY id", (last,)).fetchall()
    require_fixture(got == expected, "graduation probes lack their expected session and served-entry pattern")
    conn.execute("INSERT INTO communities (id,label,size,cohesion,computed_at) "
                 "SELECT coalesce(max(id),0)+1,'deploy tooling',3,0.5,%s FROM communities", (now - 7200,))
    conn.execute("INSERT INTO meta (key,value) VALUES ('graph_digest',%s) "
                 "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                 (Json({"computed_at": 1791000000.25, "communities": 1}),))
    graph = conn.execute("SELECT id,label,size,cohesion,computed_at FROM communities WHERE id > %s",
                         (last_community,)).fetchall()
    digest = conn.execute("SELECT value FROM meta WHERE key='graph_digest'").fetchone()
    require_fixture(graph == [(last_community + 1, "deploy tooling", 3, 0.5, now - 7200)]
                    and digest == ({"computed_at": 1791000000.25, "communities": 1},),
                    "statistics community or graph digest is missing")


def seed(binary, dsn, home, docs, *, extra, fact, stats):
    with open_bank(dsn) as conn:
        existing = conn.execute("SELECT to_regclass('public.entries') IS NOT NULL").fetchone()[0]
        cutoff = conn.execute("SELECT coalesce(max(id),0) FROM entries").fetchone()[0] if existing else 0
    env = daemons.base_env(home, {
        "PSEUDOLIFE_MCP_DATABASE_URL": dsn,
        "PSEUDOLIFE_MCP_TOKEN": SEED_TOKEN,
        "PSEUDOLIFE_MCP_HOST": "127.0.0.1",
        "PSEUDOLIFE_RELEASE_CHECK": "0",
        "PSEUDOLIFE_MCP_AUTOSAVE_SECONDS": "86400",
        "PSEUDOLIFE_SESSION_REAP_SECONDS": "86400",
        **{key: os.environ[key] for key in ("PSEUDOLIFE_DAEMON_ONNX_DIR", "PSEUDOLIFE_DAEMON_ORT_THREADS")
           if key in os.environ},
    })
    env.pop("PL_HARNESS_SEED_DSN", None)
    env.pop("PL_HARNESS_SEED_RUST", None)
    port = daemons.free_port()
    candidate = daemons.rust_daemon(binary, home, port, env)
    try:
        candidate.start(wait_s=180)
        session = Session(port, SEED_TOKEN)
        try:
            if extra:
                write_extras(session, dsn, fact)
            else:
                write_base(session, port, docs)
        finally:
            session.close()
    finally:
        candidate.stop()
    with open_bank(dsn) as conn:
        if extra:
            finish_extras(conn, fact, stats, cutoff)
        else:
            finish_base(conn, docs, cutoff)


def main() -> int:
    if _DATABASE_TURN_MARKER.exists():
        from rust.harness_support.database_turn import require_turn

        require_turn(holder="standalone database harness", contain=True)
    if os.name == "nt":
        from rust.harness_support._database_turn_windows import contain_process_tree

        contain_process_tree()
    dsn = os.environ["PL_HARNESS_SEED_DSN"]
    binary = Path(os.environ["PL_HARNESS_SEED_RUST"]).resolve(strict=True)
    limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 150
    extra = "--mcp-extra" in sys.argv
    data = Path(os.environ["PSEUDOLIFE_MCP_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=data, prefix="native-seed-") as temp:
        home = daemons.make_home(Path(temp), "candidate", "memory:\n  dream:\n    enabled: false\n")
        docs = [] if extra else paragraphs(limit)
        seed(binary, dsn, home, docs, extra=extra, fact="--fact-extra" in sys.argv,
             stats="--stats-extra" in sys.argv)
    print("mcp extra seeded" if extra else f"stored {len(docs) + len(CRAFTED) + len(EPISODE_TEXTS)} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Graph ingestion gating (2026-07-02 review fix).

The dream is the lowest-quality producer (a 2B extractor) yet its output
went straight to live graph storage: any string became an entity, the edge
floor was 0.0, and re-assertion revived edges a human had removed. These
tests pin the write-time gate: junk names never become entities, the edge
floor drops type-violations, and human supersession is sticky against
agent re-assertion.
"""

from __future__ import annotations

import tempfile

import pytest

from pseudolife_memory.memory.graph_consolidation import junk_name_reason
from pseudolife_memory.utils.config import DreamConfig
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


# ── unit: write-time junk gate ────────────────────────────────────────────

def test_junk_name_reason_blocks_known_junk_classes():
    assert junk_name_reason("a<->b") == "concat-artifact"
    assert junk_name_reason("memory_recall->recall.py") == "concat-artifact"
    assert junk_name_reason("42") == "bare-number"
    assert junk_name_reason("done") == "status-word"
    assert junk_name_reason("  ") == "empty"


def test_junk_name_reason_allows_legitimate_names():
    # Short names are legitimate at write time (Go, uv) — they remain
    # review-queue material, judged by degree, not write-blocked.
    assert junk_name_reason("Go") is None
    assert junk_name_reason("PostgreSQL") is None
    assert junk_name_reason("RTX 4090") is None


def test_junk_name_reason_blocks_2026_07_02_cleanup_classes():
    # Every class below dominated the 612 hand-deleted entities of the
    # 2026-07-02 live-cortex cleanup; the gate must stop the re-supply.
    assert junk_name_reason("236 memories") == "count-prefix"
    assert junk_name_reason("5 type-violation junk edges") == "count-prefix"
    assert junk_name_reason("2026-07-02") == "bare-date"
    assert junk_name_reason("pseudolife_memory-20260702-194002.sql.gz") == "dump-file"
    assert junk_name_reason("data/backups/pseudolife_memory-20260624-200948.sql") == "dump-file"
    assert junk_name_reason("pseudolife-daemon:0.2.0-pre-gi") == "image-tag"
    assert junk_name_reason("docker compose -f ops/docker-compose.yml build x") == "command-string"
    assert junk_name_reason("python -m pseudolife_memory.web.devserver") == "command-string"
    assert junk_name_reason("LOCAL master = 8e2b992") == "hash-status"
    assert junk_name_reason("Action: accept-link") == "action-prefix"
    assert junk_name_reason(
        "deploy a schema change to the live pseudolife-mcp daemon") == "sentence"
    assert junk_name_reason("P3 SURFACE POLISH") == "status-shard"
    assert junk_name_reason("P1_roadmap_item") == "status-shard"


def test_junk_name_reason_new_rules_spare_legitimate_names():
    # Near-misses for each new rule that must stay storable.
    assert junk_name_reason("2026-07-02 review roadmap") is None    # dated title, not bare date
    assert junk_name_reason("arXiv:2606.22844") is None             # 2-part id, not an image tag
    assert junk_name_reason("3d-force-graph@1.73.6") is None        # versioned lib (@, not :)
    assert junk_name_reason("8-band continuum") is None             # hyphenated, not count-prefix
    assert junk_name_reason("docker compose") is None               # tool name, not a command line
    assert junk_name_reason("backup.ps1 off-disk mirror") is None   # short noun phrase
    assert junk_name_reason("Language Models Need Sleep") is None   # short paper name
    assert junk_name_reason(
        "Track A (graphify-derived recall hub-gating)") is None     # 5 tokens < sentence floor
    assert junk_name_reason("AllowTelemetry=0 at both HKLM Policies") is None  # '=' but no hash
    assert junk_name_reason("P2P protocol") is None                 # P<digit><letter>: no shard boundary


def test_dream_edge_floor_drops_type_violations_by_default():
    # Hard type-violations score 0.1125-0.175; the shipped floor must
    # exceed that (pre-fix it was 0.0 = write everything).
    assert DreamConfig().min_relation_confidence >= 0.2


# ── storage: revive semantics ─────────────────────────────────────────────

@pytest.fixture
def storage(pg_conn, pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage
    st = PostgresStorage(pg_url)
    try:
        yield st
    finally:
        st.close()


def test_upsert_edge_revive_false_keeps_superseded(storage):
    a = storage.ensure_entity("gate-src")
    b = storage.ensure_entity("gate-dst")
    storage.upsert_edge(a, "uses", b, confidence=0.8)
    assert storage.supersede_edge(a, "uses", b) is True

    storage.upsert_edge(a, "uses", b, confidence=0.6, revive=False)

    row = storage.conn.execute(
        "SELECT superseded_at FROM edges WHERE src_id=%s AND relation=%s "
        "AND dst_id=%s", (a, "uses", b)).fetchone()
    assert row[0] is not None, "agent re-assertion must not revive the edge"


def test_bless_edge_raises_confidence_and_marks_user(storage):
    # 2026-07-02 review fix 2: the missing "Keep" half of Prune. A blessed
    # edge climbs above the 0.6 dubious threshold and records the human call.
    a = storage.ensure_entity("bless-src")
    b = storage.ensure_entity("bless-dst")
    storage.upsert_edge(a, "uses", b, confidence=0.45, origin="agent")

    assert storage.bless_edge(a, "uses", b) is True

    row = storage.conn.execute(
        "SELECT confidence, origin FROM edges WHERE src_id=%s AND relation=%s "
        "AND dst_id=%s", (a, "uses", b)).fetchone()
    assert row[0] >= 0.8
    assert row[1] == "user"


def test_bless_edge_never_creates_or_revives(storage):
    a = storage.ensure_entity("bless-src2")
    b = storage.ensure_entity("bless-dst2")
    assert storage.bless_edge(a, "uses", b) is False, "no edge: nothing to bless"

    storage.upsert_edge(a, "uses", b, confidence=0.45, origin="agent")
    storage.supersede_edge(a, "uses", b)
    assert storage.bless_edge(a, "uses", b) is False, (
        "a superseded (human-removed) edge must not be blessable back to life")


def test_upsert_edge_default_still_revives(storage):
    a = storage.ensure_entity("gate-src2")
    b = storage.ensure_entity("gate-dst2")
    storage.upsert_edge(a, "uses", b, confidence=0.8)
    assert storage.supersede_edge(a, "uses", b) is True

    storage.upsert_edge(a, "uses", b, confidence=0.8)  # explicit re-assert

    row = storage.conn.execute(
        "SELECT superseded_at FROM edges WHERE src_id=%s AND relation=%s "
        "AND dst_id=%s", (a, "uses", b)).fetchone()
    assert row[0] is None, "an explicit (human) upsert still revives"


# ── service: the dream write path end-to-end ─────────────────────────────

@pytest.fixture()
def svc(pg_conn, pg_url):
    from pseudolife_memory.service import MemoryService

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = MemoryService(data_dir=d, database_url=pg_url)
        try:
            yield s
        finally:
            if s._storage is not None:
                s._storage.close()


def test_dream_relations_skip_junk_endpoints(svc):
    from pseudolife_memory import graph as G

    svc.stats()  # force _ensure_init so storage/graph exist
    n = svc._link_dream_relations([
        {"src": "a<->b", "relation": "uses", "dst": "postgres"},
        {"src": "gamma-svc", "relation": "uses", "dst": "42"},
    ])
    assert n == 0
    assert svc._storage.find_entity(G.norm_name("a<->b")) is None
    assert svc._storage.find_entity(G.norm_name("42")) is None


def test_service_bless_edge_clears_dubious_flag(svc):
    svc.graph_relate("gamma-tool", "uses", "delta-lib",
                     origin="agent", confidence=0.45)

    out = svc.graph_bless_edge("gamma-tool", "uses", "delta-lib")

    assert out["blessed"] is True
    nb = svc.graph_neighborhood("gamma-tool", depth=1)
    edge = next(e for e in nb["edges"]
                if e["relation"] == "uses" and e["dst"] == "delta-lib")
    assert edge["confidence"] >= 0.8
    assert edge["origin"] == "user"


def test_service_bless_edge_unknown_edge_or_entity(svc):
    svc.graph_relate("gamma-tool2", "uses", "delta-lib2")
    missing = svc.graph_bless_edge("gamma-tool2", "hosts", "delta-lib2")
    assert missing["blessed"] is False and missing["reason"] == "edge_not_found"

    unknown = svc.graph_bless_edge("no-such-entity-xyz", "uses", "delta-lib2")
    assert unknown["blessed"] is False and unknown["reason"] == "unknown_entity"


def test_dream_reassertion_does_not_revive_human_removal(svc):
    svc.graph_relate("alpha-svc", "uses", "beta-db")
    svc.graph_unrelate("alpha-svc", "uses", "beta-db")  # human: "wrong"

    svc._link_dream_relations([
        {"src": "alpha-svc", "relation": "uses", "dst": "beta-db"},
    ])

    nb = svc.graph_neighborhood("alpha-svc", depth=1)
    rels = {(e["relation"], e["dst"]) for e in nb.get("edges", [])}
    assert ("uses", "beta-db") not in rels, (
        "dream re-assertion revived an edge a human superseded")

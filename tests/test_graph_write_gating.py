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

from pseudolife_memory.memory.graph_consolidation import (
    junk_name_reason, variant_tokens, variant_conflict)
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


def test_junk_name_reason_blocks_metric_readings_and_lists():
    # 2026-07-11 curation classes: metric readings and captured enumerations
    assert junk_name_reason("stale 0.8") == "metric-reading"
    assert junk_name_reason("stale 0.0") == "metric-reading"
    assert junk_name_reason("stale_leak 0.7-0.8") == "metric-reading"
    assert junk_name_reason("data/, ops/.env, *.pt") == "list-artifact"


def test_junk_name_reason_spares_metric_and_list_near_misses():
    assert junk_name_reason("CUDA Toolkit 13.1") is None        # uppercase token
    assert junk_name_reason("Gemma 4 E4B") is None              # non-decimal tail
    assert junk_name_reason("User (jdoe, jdoe@example.com)") is None
    assert junk_name_reason("8-band continuum") is None


def test_junk_entities_flags_metric_readings_and_lists():
    from pseudolife_memory.memory.graph_consolidation import junk_entities
    ents = [{"id": 1, "display": "stale 0.8"},
            {"id": 2, "display": "data/, ops/.env, *.pt"}]
    out = junk_entities(ents, [], max_degree=1)
    assert {(j["display"], j["reason"]) for j in out} == {
        ("stale 0.8", "metric-reading"),
        ("data/, ops/.env, *.pt", "list-artifact")}
    # list-artifact is degree-agnostic (like concat-artifact); metric-reading
    # respects the degree cap
    out2 = junk_entities(ents, [], max_degree=-1)
    assert [j["reason"] for j in out2] == ["list-artifact"]


def test_junk_entities_flags_resolvable_compounds_only():
    from pseudolife_memory.memory.graph_consolidation import junk_entities
    ents = [{"id": 1, "display": "memory_lesson_search/world_search"},
            {"id": 2, "display": "pg+extractor"},
            {"id": 3, "display": "ops/backup.ps1"},       # extension-exempt
            {"id": 4, "display": "C++"}]                  # empty right side
    known = frozenset({"memory-lesson-search", "world-search", "pg",
                       "extractor", "ops", "backup-ps1"})
    out = junk_entities(ents, [], max_degree=1, known_norms=known)
    reasons = {j["display"]: j["reason"] for j in out}
    assert reasons.get("memory_lesson_search/world_search") == "compound-artifact"
    assert reasons.get("pg+extractor") == "compound-artifact"
    assert "ops/backup.ps1" not in reasons
    assert "C++" not in reasons
    # without known_norms (default) nothing is flagged as compound
    out2 = junk_entities(ents, [], max_degree=1)
    assert all(j["reason"] != "compound-artifact" for j in out2)


def test_dream_edge_floor_drops_type_violations_by_default():
    # Hard type-violations score 0.1125-0.175; the shipped floor must
    # exceed that (pre-fix it was 0.0 = write everything).
    assert DreamConfig().min_relation_confidence >= 0.2


# ── variant tokens & conflicts ────────────────────────────────────────────

def test_variant_tokens_extract_size_quant_version():
    assert variant_tokens("Gemma 4 E4B") == frozenset({"e4b"})
    toks = variant_tokens("gemma-4-26B_q4_0-it.gguf")
    assert "26b" in toks and "q4-0" in toks
    assert variant_tokens("pseudolife-daemon:0.2.0") == frozenset({"0.2.0"})
    assert variant_tokens("plain name") == frozenset()


def test_variant_conflict_blocks_cross_model_pairs():
    # the 9 merge proposals hand-rejected on 2026-07-11
    assert variant_conflict("Gemma-4-E4B-QAT (UD-Q4_K_XL)",
                            "gemma-4-E2B-it-qat-UD-Q4_K_XL")
    assert variant_conflict("gemma-E4B Q4_K_M", "Gemma-4-E4B-QAT (UD-Q4_K_XL)")
    assert variant_conflict("gemma-4-26B", "Gemma 4 E4B")
    assert variant_conflict("Qwen3.5-4B", "Qwen3.6-27B")
    # uppercase is the canonical GGUF quant spelling
    assert variant_conflict("gemma Q4_0", "gemma Q8_0")


def test_variant_conflict_allows_same_or_absent_variants():
    assert not variant_conflict("Gemma 4 E4B", "gemma-4-E4B-it base")
    assert not variant_conflict("update.ps1", "ops/update.ps1")
    assert not variant_conflict("Sonnet shim", "evals/sonnet_shim.py")
    # underscore vs hyphen quant forms are the SAME token (norm_name folds _ to -)
    assert not variant_conflict("UD-Q4_K_XL quant", "ud-q4-k-xl quant")
    # Q4 alone (quarter label) is NOT a variant token — it needs _K suffix
    assert not variant_conflict("Q4 2026 roadmap", "Q1 2027 roadmap")


def test_variant_tokens_quarter_labels_not_variants():
    # Quarter labels (Q1, Q4 standalone) are NOT variant tokens; only Q<digit>_K*
    assert variant_tokens("Q4 2026 roadmap") == frozenset()
    assert variant_tokens("Q1 2027 roadmap") == frozenset()
    # Q4_K forms ARE variants
    assert "q4-k" in variant_tokens("Q4_K 2026 quant")


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


def test_dream_edges_skip_generic_hubs(svc):
    # 2026-07-02 review fix 4: "memory_* related-to MCP"-style hub spokes carry
    # zero information; the dream must not mint edges touching generic hubs.
    svc.stats()
    n = svc._link_dream_relations([
        {"src": "hubgate-lib", "relation": "related-to", "dst": "MCP"},
        {"src": "master", "relation": "related-to", "dst": "hubgate-lib2"},
    ])
    assert n == 0
    assert svc._storage.find_entity("mcp") is None
    assert svc._storage.find_entity("master") is None


def test_dream_cross_project_edge_becomes_proposal(svc):
    # 2026-07-02 review fix 5: entities attributed to disjoint projects only
    # coexist in the shared bank — a dream edge between them goes to
    # edge_proposals for review, never straight into the live graph.
    svc.graph_relate("xproj-a-thing", "uses", "xproj-a-helper")
    svc.graph_relate("xproj-b-thing", "uses", "xproj-b-helper")
    svc.graph_assign_scope("xproj-a-thing", "proj-a")
    svc.graph_assign_scope("xproj-b-thing", "proj-b")

    n = svc._link_dream_relations([
        {"src": "xproj-a-thing", "relation": "uses", "dst": "xproj-b-thing"},
    ])

    assert n == 0
    nb = svc.graph_neighborhood("xproj-a-thing", depth=1)
    assert not any("xproj-b-thing" in (e["src"], e["dst"]) for e in nb["edges"])
    assert any(p["src"] == "xproj-a-thing" and p["dst"] == "xproj-b-thing"
               for p in svc._storage.pending_proposals())


def test_cross_project_untyped_relation_dropped(svc):
    # 2026-07-11: untyped (related-to fallback) cross-project pairs carry no
    # information — 4/4 such proposals were hand-rejected. Only a TYPED
    # relation across disjoint projects still earns a review proposal.
    svc.stats()
    with svc._lock:
        svc._resolve_or_create_entity("alpha-tool")
        svc._resolve_or_create_entity("beta-tool")
    svc.graph_assign_scope("alpha-tool", "proj-a")
    svc.graph_assign_scope("beta-tool", "proj-b")

    def _cross_count():
        return svc._storage.conn.execute(
            "SELECT count(*) FROM edge_proposals "
            "WHERE source = 'dream-cross-project'").fetchone()[0]

    # untyped fallback across disjoint scopes: dropped, no proposal
    n = svc._link_dream_relations([
        {"src": "alpha-tool", "relation": "correlates-with", "dst": "beta-tool"}])
    assert n == 0 and _cross_count() == 0
    # a TYPED relation across disjoint scopes still files a proposal
    n2 = svc._link_dream_relations([
        {"src": "alpha-tool", "relation": "uses", "dst": "beta-tool"}])
    assert n2 == 0 and _cross_count() == 1
    # the LITERAL "related-to" label resolves in the registry but is equally
    # information-free across disjoint scopes: dropped, no new proposal
    n3 = svc._link_dream_relations([
        {"src": "alpha-tool", "relation": "related-to", "dst": "beta-tool"}])
    assert n3 == 0 and _cross_count() == 1


def test_dream_untyped_low_confidence_edge_quarantined(svc):
    # 2026-07-19: untyped co-mention edges (related-to, conf 0.45) were the
    # dominant review-queue pollutant (~19/day straight into the live graph;
    # dubious count 34 -> 120 in four days). Below
    # dream.relation_quarantine_below they file an edge proposal for review
    # instead of a live edge; typed relations (0.70) are unaffected.
    svc.stats()
    with svc._lock:
        svc._resolve_or_create_entity("quar-a")
        svc._resolve_or_create_entity("quar-b")
    svc.graph_assign_scope("quar-a", "proj-q")
    svc.graph_assign_scope("quar-b", "proj-q")

    n = svc._link_dream_relations([
        {"src": "quar-a", "relation": "correlates-with", "dst": "quar-b"}])

    assert n == 0
    nb = svc.graph_neighborhood("quar-a", depth=1)
    assert not any("quar-b" in (e["src"], e["dst"]) for e in nb["edges"])
    quarantined = svc._storage.conn.execute(
        "SELECT count(*) FROM edge_proposals "
        "WHERE source = 'dream-low-confidence'").fetchone()[0]
    assert quarantined == 1


def test_dream_minted_entities_stamped_with_batch_scope(svc):
    # 2026-07-19: relation-endpoint entities minted by the dream carried no
    # project attribution — no fact traces, so the backfill can never scope
    # them (435 unattributed entities, all graph-only). Entities CREATED while
    # linking a dream batch are now stamped with the batch's entry sources,
    # scopes policy applied (case-fold + exclude + umbrella rollup).
    # Pre-existing entities are left untouched.
    svc.stats()
    svc.config.memory.scopes.exclude = ["es-stamp-meta"]
    svc.config.memory.scopes.rollup = {"es-stamp-proj": "es-stamp-family"}
    with svc._lock:
        pre = svc._resolve_or_create_entity("stamp-old")["id"]

    n = svc._link_dream_relations(
        [{"src": "stamp-old", "relation": "uses", "dst": "stamp-new"}],
        batch_sources={"ES-Stamp-Proj", "es-stamp-meta"})

    assert n == 1
    from pseudolife_memory.graph import norm_name
    st = svc._storage
    new_id = st.find_entity(norm_name("stamp-new"))["id"]
    srcs = {r["source"] for r in st.sources_for_entity(new_id)}
    assert srcs == {"es-stamp-proj", "es-stamp-family"}
    assert st.sources_for_entity(pre) == []


def test_dream_same_project_edge_still_written(svc):
    # A TYPED same-project edge passes both the cross-project gate and the
    # low-confidence quarantine (untyped related-to now quarantines instead —
    # see test_dream_untyped_low_confidence_edge_quarantined).
    svc.graph_relate("xproj-c-thing", "uses", "xproj-c-helper")
    svc.graph_assign_scope("xproj-c-thing", "proj-c")
    svc.graph_assign_scope("xproj-c-helper", "proj-c")

    n = svc._link_dream_relations([
        {"src": "xproj-c-thing", "relation": "uses", "dst": "xproj-c-helper"},
    ])
    assert n == 1


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


def test_dream_write_dedup_files_merge_proposal(svc):
    svc.stats()  # force init
    svc.graph_relate("epsilon-review.py", "part-of", "epsilon-core",
                     origin="user")
    n = svc._link_dream_relations([
        {"src": "epsilon review", "relation": "uses", "dst": "zeta-lib"},
    ])
    assert n == 1
    props = [p for p in svc._storage.pending_entity_proposals()
             if (p["reason"] or "").startswith("write-dedup:")
             and "epsilon" in p["reason"]]
    assert len(props) == 1
    assert {props[0]["entity"], props[0]["into"]} == {
        "epsilon review", "epsilon-review.py"}
    # re-run: entity now exists (created=False) -> nothing new filed
    svc._link_dream_relations([
        {"src": "epsilon review", "relation": "uses", "dst": "zeta-lib"},
    ])
    props2 = [p for p in svc._storage.pending_entity_proposals()
              if (p["reason"] or "").startswith("write-dedup:")
              and "epsilon" in p["reason"]]
    assert len(props2) == 1


def test_dream_write_dedup_respects_dismissed_and_disable(svc):
    svc.stats()
    svc.graph_relate("theta-store.py", "part-of", "theta-core", origin="user")
    svc._storage.dismiss_pair("theta-store", "theta-store-py")
    svc._link_dream_relations(
        [{"src": "theta store", "relation": "uses", "dst": "iota-lib"}])
    assert [p for p in svc._storage.pending_entity_proposals()
            if "theta" in (p["reason"] or "")] == []
    # disabled at 0: a fresh variant files nothing
    old = svc.config.memory.dream.write_dedup_min_jaccard
    svc.config.memory.dream.write_dedup_min_jaccard = 0.0
    try:
        svc._link_dream_relations(
            [{"src": "iota lib", "relation": "uses", "dst": "mu-lib"}])
        assert [p for p in svc._storage.pending_entity_proposals()
                if "iota" in (p["reason"] or "")] == []
    finally:
        svc.config.memory.dream.write_dedup_min_jaccard = old


def test_explicit_relate_never_files_write_dedup(svc):
    svc.stats()
    svc.graph_relate("kappa runner", "uses", "kappa-runner.py", origin="user")
    assert [p for p in svc._storage.pending_entity_proposals()
            if "kappa" in (p["reason"] or "")] == []


# ── dream alias-candidate post-pass (embedding coreference screen) ─────────

class _ClaimStub:
    """Extractor stub: emits one fixed claim per dream cycle."""

    def __init__(self, entity, attribute="deployment-status", value="production"):
        self._claim = dict(entity=entity, attribute=attribute, value=value,
                           confidence=0.8, origin="agent")

    def extract(self, texts, vocab):
        return [dict(self._claim)]


def _drain(svc, extractor):
    while svc.dream_run(extractor, limit=100)["pulled"]:
        pass


def _alias_props(svc):
    return [p for p in svc._storage.pending_entity_proposals()
            if (p["reason"] or "").startswith("dream-alias:")]


def test_dream_alias_candidate_files_semantic_merge_proposal(svc):
    """A dreamed paraphrase of an existing cortex entity (near-zero token
    overlap, so the Jaccard write-dedup can't see it) files a merge proposal
    for review."""
    svc.cortex_write("Pseudolife-MCP default extractor sidecar", "version",
                     "e4b", support="user")
    svc.store("sidecar deploy note", source="t")
    _drain(svc, _ClaimStub("production extractor sidecar"))
    props = _alias_props(svc)
    assert len(props) == 1
    assert {props[0]["entity"], props[0]["into"]} == {
        "production extractor sidecar",
        "Pseudolife-MCP default extractor sidecar"}
    # Same claim re-dreamed: entity is no longer new -> nothing re-filed.
    svc.store("sidecar deploy note again", source="t")
    _drain(svc, _ClaimStub("production extractor sidecar"))
    assert len(_alias_props(svc)) == 1


def test_dream_alias_candidate_ignores_unrelated_entities(svc):
    svc.cortex_write("Pseudolife-MCP default extractor sidecar", "version",
                     "e4b", support="user")
    svc.store("budget note", source="t")
    _drain(svc, _ClaimStub("quarterly budget report"))
    assert _alias_props(svc) == []


def test_dream_alias_candidate_respects_dismissed_and_disable(svc):
    from pseudolife_memory.graph import norm_name
    svc.cortex_write("Pseudolife-MCP default extractor sidecar", "version",
                     "e4b", support="user")
    svc.stats()
    svc._storage.dismiss_pair(*sorted((
        norm_name("production extractor sidecar"),
        norm_name("Pseudolife-MCP default extractor sidecar"))))
    svc.store("sidecar deploy note", source="t")
    _drain(svc, _ClaimStub("production extractor sidecar"))
    assert _alias_props(svc) == []
    # disabled at 0: a fresh paraphrase files nothing
    old = svc.config.memory.dream.alias_candidate_min_cosine
    svc.config.memory.dream.alias_candidate_min_cosine = 0.0
    try:
        svc.store("dream service note", source="t")
        svc.cortex_write("dream consolidation service", "status", "live",
                         support="user")
        _drain(svc, _ClaimStub("dream service"))
        assert _alias_props(svc) == []
    finally:
        svc.config.memory.dream.alias_candidate_min_cosine = old


def test_dream_alias_candidate_blocks_variant_conflict(svc):
    """E4B vs E2B names embed nearly identically but denote different models —
    the alias post-pass must not file a merge proposal for them."""
    svc.cortex_write("Gemma 4 E2B extractor sidecar", "version", "e2b",
                     support="user")
    svc.store("sidecar swap note", source="t")
    _drain(svc, _ClaimStub("Gemma 4 E4B extractor sidecar"))
    assert _alias_props(svc) == []


# ── slot-key folding at entity creation ──────────────────────────────────

def test_find_fact_slot_entity(storage):
    storage.conn.execute(
        "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm,"
        " value, status, confidence, asserted_at, last_confirmed)"
        " VALUES (%s,%s,%s,%s,%s,'current',0.9,1.0,1.0)",
        ("2026-07-11-known-facts-window", "delivered-components",
         "2026-07-11-known-facts-window", "delivered-components", "x"))
    assert storage.find_fact_slot_entity(
        "2026-07-11-known-facts-window-delivered-components"
    ) == "2026-07-11-known-facts-window"
    assert storage.find_fact_slot_entity("no-such-slot") is None


def test_resolve_or_create_folds_slot_key_to_owner(svc):
    svc.cortex_write("2026-07-11-known-facts-window", "delivered-components",
                     "gate+config+tests", support="user")
    svc.stats()
    with svc._lock:
        e = svc._resolve_or_create_entity(
            "2026-07-11-known-facts-window.delivered-components")
    assert e["display"] == "2026-07-11-known-facts-window"
    # non-slot dotted names are untouched
    with svc._lock:
        e2 = svc._resolve_or_create_entity("psycopg/transaction.py")
    assert e2["display"] == "psycopg/transaction.py"

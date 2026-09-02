"""TypeRetrieve (arXiv 2608.22752): in-scope CONSTRAINT facts are pinned
AHEAD of cosine ranking instead of competing on similarity (schema v35).

"In scope" is defined cheaply and precisely, with no second embedding pass:

- ``memory_search``'s cortex block (``service.cortex_search``): a constraint
  fact is in scope when its slot's ``entity_norm`` occurs as a
  separator-bounded run inside ``_norm_key(query)`` — the query NAMES the
  entity;
- ``memory_recall``: a constraint fact is in scope when its entity is a
  SEED of the walk (hop 0 — the entities the query itself resolved to);
  hop-discovered entities are context, not scope.

Pinned facts carry ``pinned: true`` so the reader can tell a pin from a
rank; an unlabelled bank is served byte-identically (no labels → no pins),
and ``memory.cortex.pin_constraints=false`` restores plain ranking.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.helpers import pg_reachable as _pg_reachable
from tests.helpers import reload_mcp_filemode
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

QUERY = "what is the payments-db host"


@pytest.fixture()
def svc(pristine_service):
    return pristine_service


# The cosine ordering between the host fact and the rule is a KNIFE EDGE
# (0.6285 vs 0.6273 locally; CI read 0.6291 vs 0.6293 and flipped —
# 2026-09-02), so no assertion below leans on it. The "higher-cosine
# non-constraint" the pin must beat is instead a fact whose value IS the
# query text: any embedder ranks it first by a wide margin (0.76 vs 0.63
# locally), and ``_GAP`` makes a future fixture edit fail loudly here
# rather than flap on CI (the test_cortex_bm25 knife-edge lesson).
_GAP = 0.08


def _seed(svc):
    svc.cortex_write("payments-db", "host", "db-prod-1", support="user")
    svc.cortex_write("payments-db", "faq", QUERY, support="user")   # the top-cosine fact
    svc.cortex_write("payments-db", "port", "5433", support="user")
    svc.cortex_write("payments-db", "volume-rule",
                     "Never run docker compose down -v against payments-db",
                     support="user")                      # auto → constraint
    svc.cortex_write("billing-svc", "deploy-rule",
                     "Never deploy billing-svc without a rollback tag",
                     support="user")                      # constraint, out of scope


def test_in_scope_constraint_outranks_a_higher_cosine_fact(svc):
    _seed(svc)
    out = svc.cortex_search(QUERY, top_k=5, min_score=0.0)["entries"]
    slots = [(e["entity"], e["attribute"]) for e in out]
    assert slots[0] == ("payments-db", "volume-rule")
    assert out[0]["pinned"] is True
    assert out[0]["distortion_tolerance"] == "constraint"
    faq = next(e for e in out if e["attribute"] == "faq")
    # The pin must have beaten cosine by a clear margin, or this test is
    # vacuous — and a fixture edit that narrows the margin fails HERE, named.
    assert faq["score"] - out[0]["score"] >= _GAP, (
        "knife-edge fixture: the top-cosine fact is not clearly above the "
        f"pin ({faq['score']} vs {out[0]['score']}); widen the gap")
    assert "pinned" not in faq


def test_out_of_scope_constraint_is_not_pinned(svc):
    _seed(svc)
    out = svc.cortex_search(QUERY, top_k=5, min_score=0.0)["entries"]
    billing = [e for e in out if e["entity"] == "billing-svc"]
    assert all("pinned" not in e for e in billing)
    assert out[0]["entity"] != "billing-svc"


def test_unlabelled_bank_is_served_byte_identically(svc):
    svc.cortex_write("payments-db", "host", "db-prod-1", support="user")
    svc.cortex_write("payments-db", "port", "5433", support="user")
    with_pins = svc.cortex_search(QUERY, top_k=5, min_score=0.0)
    svc.config.memory.cortex.pin_constraints = False
    try:
        without = svc.cortex_search(QUERY, top_k=5, min_score=0.0)
    finally:
        svc.config.memory.cortex.pin_constraints = True
    assert json.dumps(with_pins, sort_keys=True) == json.dumps(without, sort_keys=True)
    assert not any("pinned" in e for e in with_pins["entries"])


def test_knob_off_restores_plain_ranking(svc):
    _seed(svc)
    svc.config.memory.cortex.pin_constraints = False
    try:
        out = svc.cortex_search(QUERY, top_k=5, min_score=0.0)["entries"]
    finally:
        svc.config.memory.cortex.pin_constraints = True
    assert out[0]["attribute"] == "faq"          # plain cosine order
    assert not any("pinned" in e for e in out)


def test_pins_take_at_most_half_the_budget_best_cosine_first(svc):
    """Peer review major (2026-09-02): the first cut let >= k in-scope
    constraints displace the ENTIRE ranked block. Pins now get at most
    k // 2 slots, ordered by cosine (the least relevant rules are the ones
    dropped, not the newest), and the ranked facts keep the rest."""
    _seed(svc)
    for i in range(6):
        svc.cortex_write("payments-db", f"rule-{i}",
                         f"Never do thing number {i} to payments-db",
                         support="user")
    out = svc.cortex_search(QUERY, top_k=5, min_score=0.0)["entries"]
    assert len(out) == 5
    pinned = [e for e in out if e.get("pinned")]
    assert len(pinned) == 2
    assert out[:2] == pinned
    assert pinned[0]["score"] >= pinned[1]["score"]
    assert out[2]["attribute"] == "faq"           # the ranked answer survives
    assert not any(e.get("pinned") for e in out[2:])


def test_pins_respect_the_callers_relevance_floor(svc):
    """memory_search passes guard_min_score so weak facts are never
    asserted as canonical; a pin must clear the same floor — pinning is
    exemption from RANKING, not from relevance."""
    _seed(svc)
    base = svc.cortex_search(QUERY, top_k=5, min_score=0.0)["entries"]
    pin = next(e for e in base if e.get("pinned"))
    faq = next(e for e in base if e["attribute"] == "faq")
    assert faq["score"] - pin["score"] >= _GAP
    floor = (pin["score"] + faq["score"]) / 2
    out = svc.cortex_search(QUERY, top_k=5, min_score=floor)["entries"]
    assert not any(e.get("pinned") for e in out)
    assert out[0]["attribute"] == "faq"
    assert all(e["score"] >= floor for e in out)


def test_scope_test_is_separator_insensitive_and_word_bounded():
    from pseudolife_memory.service import _entity_in_query
    assert _entity_in_query("payments-db", "what is the payments db host")
    assert _entity_in_query("Payments DB", "the payments-db host?")
    assert not _entity_in_query("db", "what is the payments-database host")
    assert not _entity_in_query("payments-db", "what is the payments host")
    assert not _entity_in_query("", "anything")


def test_mcp_search_cortex_block_carries_labels_and_pins(tmp_path, monkeypatch):
    """The cortex block is a whitelist re-selection (chip 4.1's re_verify
    lesson): pinned / authority / distortion_tolerance must be re-selected
    or the most-used read surface never shows them."""
    mod = reload_mcp_filemode(tmp_path, monkeypatch)
    facts = [
        {"entity": "payments-db", "attribute": "volume-rule",
         "value": "Never run docker compose down -v", "origin": "user",
         "confidence": 0.9, "score": 0.21, "contested": False,
         "asserted_at": 1.0, "last_confirmed": 1.0, "age": "1d ago",
         "distortion_tolerance": "constraint", "authority": "directive",
         "pinned": True},
        {"entity": "payments-db", "attribute": "host", "value": "db-prod-1",
         "origin": "user", "confidence": 0.9, "score": 0.6,
         "contested": False, "asserted_at": 1.0, "last_confirmed": 1.0,
         "age": "1d ago"},
    ]
    monkeypatch.setattr(mod.service, "search", lambda **kw: {
        "query": kw.get("query", ""), "count": 0, "entries": [],
        "low_confidence": False})
    monkeypatch.setattr(mod.service, "cortex_search",
                        lambda *a, **k: {"entries": facts})
    block = mod.memory_search(query=QUERY)["cortex"]
    assert block[0]["pinned"] is True
    assert block[0]["distortion_tolerance"] == "constraint"
    assert block[0]["authority"] == "directive"
    assert "pinned" not in block[1]
    assert "distortion_tolerance" not in block[1] and "authority" not in block[1]


# ── memory_recall: seeds are the scope ────────────────────────────────────

@pytest.fixture(scope="module")
def recall_svc(tmp_path_factory):
    """One PG-backed service with a small graph: payments-db (seed) runs-on
    host-a (hop 1). Both carry six facts with the constraint written LAST,
    so record order alone would drop it behind the per-entity cap (5)."""
    if not _pg_reachable("postgresql://pseudolife:pseudolife@127.0.0.1:5433/postgres"):
        pytest.skip("bench Postgres not reachable")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path_factory.mktemp("pins"))
    svc.store("payments-db runs on host-a", source="bench")
    svc.graph_relate("payments-db", "runs-on", "host-a")
    for ent in ("payments-db", "host-a"):
        for i in range(5):
            svc.cortex_write(ent, f"attr-{i}", f"value {i} for {ent}",
                             support="user")
        svc.cortex_write(ent, "rule", f"Never reboot {ent} during a dream",
                         support="user")
    return svc


def test_seed_constraint_is_pinned_first_and_hop_constraint_is_not(recall_svc):
    out = recall_svc.recall("what does payments-db run on?")
    assert "payments-db" in out["seeds"]
    by_name = {e["entity"]: e["facts"] for e in out["entities"]}
    seed_facts = by_name["payments-db"]
    assert seed_facts[0]["attribute"] == "rule"
    assert seed_facts[0]["pinned"] is True
    assert seed_facts[0]["distortion_tolerance"] == "constraint"
    hop_facts = by_name["host-a"]
    assert hop_facts[-1]["attribute"] == "rule"         # left in record order
    assert not any("pinned" in f for f in hop_facts)


def test_mcp_recall_keeps_the_pinned_seed_constraint_under_the_fact_cap(
        recall_svc, monkeypatch):
    import pseudolife_memory.mcp_server as srv
    monkeypatch.setattr(srv, "service", recall_svc, raising=False)
    out = srv.memory_recall("what does payments-db run on?")
    by_name = {e["entity"]: e["facts"] for e in out["entities"]}
    seed = by_name["payments-db"]
    assert len(seed) <= srv._RECALL_MAX_FACTS_PER_ENTITY
    assert seed[0] == {"attribute": "rule",
                       "value": "Never reboot payments-db during a dream",
                       "distortion_tolerance": "constraint",
                       "authority": "directive", "pinned": True}
    hop = by_name["host-a"]
    assert all(f["attribute"] != "rule" for f in hop)   # capped away, unpinned

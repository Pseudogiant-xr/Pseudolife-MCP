"""Epistemic stance as a labelled claim field — Feature A of the
2026-08-12 stance+span-gate design (schema v29).

The dream pass is a compression step, and compression strips qualifiers:
"we'll probably move to Postgres 18" consolidates into a confident
canonical fact and the hedge is gone. arXiv:2608.06953 (pre-registered
replication) measured the fix: stance written as a LABELLED FIELD rather
than inline prose survives compression by ~15 points. Feature A gives the
extractor that field end to end:

- extractor claims may carry ``stance``: the note's own hedge words,
  near-verbatim, <= 48 chars, ONLY when the note hedges;
- the parse boundary whitelists it (the op-field lesson, 2026-07-31: a
  field missing from the whitelist silently disables the feature while
  the model emits it correctly);
- ``CortexRecord.stance`` stores it; the LATEST ASSERTING WRITE wins — a
  confirm or supersede with no stance clears the stored one (a confident
  restatement removes the hedge);
- serving surfaces it (``stance`` key present only when set);
- stance NEVER feeds confidence, ranking, or supersession decisions —
  it is reader metadata, steerable by note text like every model-emitted
  field (same class as claim ``origin``, 2026-08-09 review).

Scope v1: scalar dream path only (members keep their existing model);
no ``stance`` parameter on the MCP ``memory_fact_set`` surface.
"""
from __future__ import annotations

import tempfile

import pytest

from pseudolife_memory.service import MemoryService

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)
from tests.test_dream import _chat_payload, _stub_server


class _StubExtractor:
    """Returns a fixed claim list regardless of input (drives dream_run)."""

    def __init__(self, claims):
        self._claims = claims

    def extract(self, texts, vocab):
        return [dict(c) for c in self._claims]


@pytest.fixture()
def svc():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = MemoryService(data_dir=d)
        yield s


# ── parse boundary ────────────────────────────────────────────────────────

def test_openai_extractor_carries_stance_through_parse():
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    payload = _chat_payload([
        {"entity": "user", "attribute": "database plan",
         "value": "Postgres 18", "confidence": 0.6, "stance": "probably"},
        {"entity": "svc", "attribute": "port", "value": "8080",
         "confidence": 0.9},
    ])
    with _stub_server(lambda: (200, payload)) as base_url:
        claims = OpenAICompatExtractor(base_url, "m").extract(
            ["whatever"], vocab=[])
    assert claims[0]["stance"] == "probably"
    assert "stance" not in claims[1]


def test_stance_parse_normalizes_and_caps():
    """Whitespace-stripped, capped at 48 chars; blank or non-string stance
    degrades to absent — the parse boundary never propagates junk."""
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    payload = _chat_payload([
        {"entity": "a", "attribute": "b", "value": "v1",
         "confidence": 0.5, "stance": "  per the runbook  "},
        {"entity": "a", "attribute": "c", "value": "v2",
         "confidence": 0.5, "stance": "x" * 200},
        {"entity": "a", "attribute": "d", "value": "v3",
         "confidence": 0.5, "stance": "   "},
        {"entity": "a", "attribute": "e", "value": "v4",
         "confidence": 0.5, "stance": {"nested": "junk"}},
    ])
    with _stub_server(lambda: (200, payload)) as base_url:
        claims = OpenAICompatExtractor(base_url, "m").extract(
            ["whatever"], vocab=[])
    by_attr = {c["attribute"]: c for c in claims}
    assert by_attr["b"]["stance"] == "per the runbook"
    assert len(by_attr["c"]["stance"]) == 48
    assert "stance" not in by_attr["d"]
    assert "stance" not in by_attr["e"]


# ── write semantics: latest asserting write wins ──────────────────────────

def test_cortex_write_stores_stance(svc):
    svc.cortex_write("user", "database plan", "Postgres 18",
                     confidence=0.6, stance="probably")
    rec = svc.cortex_lookup("user", "database plan")
    assert rec is not None and rec["stance"] == "probably"


def test_confirm_without_stance_clears_it(svc):
    """A confident restatement removes the hedge: same value, no stance ->
    action 'confirmed' and the stored stance is gone."""
    svc.cortex_write("user", "database plan", "Postgres 18",
                     confidence=0.6, stance="probably")
    res = svc.cortex_write("user", "database plan", "Postgres 18",
                           confidence=0.9)
    assert res["action"] == "confirmed"
    rec = svc.cortex_lookup("user", "database plan")
    assert "stance" not in rec


def test_confirm_with_new_stance_replaces_it(svc):
    svc.cortex_write("user", "database plan", "Postgres 18",
                     confidence=0.6, stance="probably")
    res = svc.cortex_write("user", "database plan", "Postgres 18",
                           confidence=0.6, stance="still unconfirmed")
    assert res["action"] == "confirmed"
    rec = svc.cortex_lookup("user", "database plan")
    assert rec["stance"] == "still unconfirmed"


def test_supersede_carries_new_stance_old_record_keeps_its_own(svc):
    svc.cortex_write("user", "database plan", "Postgres 18",
                     confidence=0.6, stance="probably")
    res = svc.cortex_write("user", "database plan", "MySQL after all",
                           confidence=0.9, support="user")
    assert res["action"] == "superseded"
    rec = svc.cortex_lookup("user", "database plan")
    assert "stance" not in rec          # new value asserted plainly
    hist = svc.history("user", "database plan")
    old = [v for v in hist["versions"] if v["value"] == "Postgres 18"]
    assert old and old[0].get("stance") == "probably"


def test_stance_does_not_affect_confidence_or_supersession(svc):
    """Stance is reader metadata only: two writes differing solely in
    stance must produce identical confidence and identical routing."""
    svc.cortex_write("a", "x", "v1", confidence=0.7)
    plain = svc.cortex_lookup("a", "x")["confidence"]
    svc.cortex_write("b", "x", "v1", confidence=0.7, stance="apparently")
    hedged = svc.cortex_lookup("b", "x")["confidence"]
    assert plain == hedged


# ── dream loop passthrough ────────────────────────────────────────────────

def test_dream_claim_stance_lands_on_the_fact(svc):
    svc.store("we will probably move the plan to Postgres 18",
              source="pseudolife")
    res = svc.dream_run(_StubExtractor([
        {"entity": "user", "attribute": "database plan",
         "value": "Postgres 18", "confidence": 0.6, "origin": "agent",
         "stance": "probably", "source": 0},
    ]))
    assert res["inserted"] == 1
    rec = svc.cortex_lookup("user", "database plan")
    assert rec["stance"] == "probably"


# ── persistence mappings (no PG needed) ───────────────────────────────────

def test_record_to_row_and_hydrate_carry_stance():
    from pseudolife_memory.memory.cortex import CortexRecord
    from pseudolife_memory.storage.sync import _record_to_row

    rec = CortexRecord(entity="user", attribute="database plan",
                       value="Postgres 18", stance="probably")
    row = _record_to_row(rec)
    assert row["stance"] == "probably"
    # A pre-v29 row (no key at all) hydrates as None — legacy banks read
    # exactly as before.
    assert CortexRecord(entity="a", attribute="b", value="c").stance is None


def test_file_mode_save_load_round_trips_stance(tmp_path):
    import torch

    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.memory.slots import Slot

    store = CortexStore()
    store.write_fact(Slot(entity="user", attribute="database plan",
                          value="Postgres 18"),
                     torch.zeros(4), confidence=0.6, stance="probably")
    p = tmp_path / "cortex.pt"
    store.save(p)
    loaded = CortexStore()
    loaded.load(p)
    rec = loaded.lookup("user", "database plan")
    assert rec is not None and rec.stance == "probably"


# ── serving ───────────────────────────────────────────────────────────────

def test_serving_dict_includes_stance_only_when_set(svc):
    from pseudolife_memory.service import _cortex_record_to_dict

    svc.cortex_write("user", "database plan", "Postgres 18",
                     confidence=0.6, stance="probably")
    svc.cortex_write("svc", "port", "8080", confidence=0.9)
    hedged = svc._cortex.lookup("user", "database plan")
    plain = svc._cortex.lookup("svc", "port")
    assert _cortex_record_to_dict(hedged)["stance"] == "probably"
    assert "stance" not in _cortex_record_to_dict(plain)


# ── persistence in a live database (schema v29) ───────────────────────────
# PG-backed; skips without the bench server.


def test_stance_round_trips_through_storage(pg_url):  # noqa: F811
    from pseudolife_memory.storage.postgres import PostgresStorage

    storage = PostgresStorage(pg_url)
    row = {
        "entity": "user", "attribute": "database plan",
        "entity_norm": "user", "attribute_norm": "database plan",
        "value": "Postgres 18", "polarity": "+", "status": "current",
        "confidence": 0.6, "origin": "agent", "support": ["agent"],
        "provenance": [], "asserted_at": 1.0, "last_confirmed": 1.0,
        "supersedes_value": None, "superseded_by_value": None,
        "superseded_at": None, "embedding": None, "entity_id": None,
        "object_entity_id": None, "freshness_class": "evergreen",
        "kind": "scalar", "value_norm": None, "stance": "probably",
    }
    storage.upsert_fact(row)
    facts = [f for f in storage.load_facts()
             if f["attribute_norm"] == "database plan"]
    assert facts and facts[-1]["stance"] == "probably"


def test_stance_null_on_unhedged_insert(pg_url):  # noqa: F811
    """A row inserted without the key stores NULL — pre-v29 writer code and
    plainly asserted facts are indistinguishable, by design."""
    from pseudolife_memory.storage.postgres import PostgresStorage

    storage = PostgresStorage(pg_url)
    row = {
        "entity": "svc", "attribute": "port",
        "entity_norm": "svc", "attribute_norm": "port",
        "value": "8080", "polarity": "+", "status": "current",
        "confidence": 0.9, "origin": "agent", "support": ["agent"],
        "provenance": [], "asserted_at": 1.0, "last_confirmed": 1.0,
        "supersedes_value": None, "superseded_by_value": None,
        "superseded_at": None, "embedding": None, "entity_id": None,
        "object_entity_id": None, "freshness_class": "evergreen",
        "kind": "scalar", "value_norm": None,
    }
    storage.upsert_fact(row)
    facts = [f for f in storage.load_facts() if f["attribute_norm"] == "port"]
    assert facts and facts[-1]["stance"] is None

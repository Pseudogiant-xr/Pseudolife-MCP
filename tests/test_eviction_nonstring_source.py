"""A non-string source never blocks capacity eviction.

The Console's consolidate stores a raw JSON ``source`` as given, so a live
entry can carry a list or object source until the next restart reloads its
stored text. Eviction looked that source up in the weight table, and an
unhashable one raised ``TypeError: unhashable type`` inside every later
store into the full band (forced real-data run, daemon box, 2026-11-06:
Python failed 13 of ``console-consolidate-source``'s 15 writes, and Rust
answered 200 to the writes Python refused only this way). Rust weighs the
stored text, and no stored form of a non-string source names a weighted
tier, so it weighs 1.0.
"""
import time

import pytest
import torch

from pseudolife_memory.memory.miras.protocols import RetentionPolicy
from pseudolife_memory.memory.titans_memory import MemoryEntry

NON_STRING_SOURCES = [["user"], {"source": "user"}, [["a"], ["b", "c"]], True, 7, 3.5, None]


def _policy():
    return RetentionPolicy(weight_decay=0.0, decay_factor_on_contradiction=0.3,
                           eviction_score=lambda e, now: 0.0, name="t")


def _entry(source):
    return MemoryEntry(text="x", embedding=torch.zeros(4), source=source, timestamp=time.time())


@pytest.mark.parametrize("source", NON_STRING_SOURCES, ids=repr)
def test_a_non_string_source_weighs_like_an_unweighted_tier(source):
    policy, now = _policy(), time.time()
    assert policy.source_weighted_score(_entry(source), now) == \
        policy.source_weighted_score(_entry("unweighted"), now)


def test_a_weighted_string_source_keeps_its_weight():
    """The near miss: a real tier name still scales the score."""
    policy, now = _policy(), time.time()
    assert policy.source_weights["system"] != 1.0
    assert policy.source_weighted_score(_entry("system"), now) == policy.source_weights["system"]


def test_a_full_band_still_stores_after_a_list_source():
    from pseudolife_memory.memory.miras.band import build_band
    from pseudolife_memory.utils.config import MIRASBandSpec
    band = build_band(MIRASBandSpec(name="b", max_entries=2, retention_policy="balanced"),
                      embedding_dim=4, device="cpu")
    band.store(text="listed", embedding=torch.zeros(4), source=["user"])
    band.store(text="mapped", embedding=torch.zeros(4), source={"source": "user"})
    band.store(text="third", embedding=torch.zeros(4), source="user")  # evicts
    assert len(band.entries) == 2
    assert band.entries[-1].text == "third"


# The Console path end to end, on the bench PostgreSQL: the live entry takes
# the stored text of a raw source, as the Rust daemon's does
# (``correction_tools.rs``, ``pyjson::pg_text``), so filtered searches, the
# source list and eviction all see one string, before a restart and after.
from tests.pg_fixtures import pg_conn, pg_service, pg_url  # noqa: E402,F401


@pytest.mark.parametrize("raw, stored", [(["user"], "{user}"), ([["a"], ["b", "c"]], "{{a},{b,c}}"),
                                         (True, "true"), (7, "7")], ids=repr)
def test_a_consolidated_raw_source_lives_as_its_stored_text(pg_service, raw, stored):
    svc = pg_service
    svc.store("The raw source target note.", source="user")
    out = svc.consolidate(replaces=["The raw source target note."],
                          new_text="The raw source replacement note.", source=raw)
    assert out.get("superseded_count") == 1, out
    [live] = [e for b in svc._cms.bands for e in b.entries
              if e.text == "The raw source replacement note."]
    row = svc._storage.conn.execute("SELECT source FROM entries WHERE id = %s",
                                    (live.db_id,)).fetchone()
    assert (live.source, row[0]) == (stored, stored)
    # Each of these hashed the raw value and raised before.
    assert stored in {s["source"] for s in svc.list_sources()["sources"]}
    svc.search("raw source replacement", sources=["user"])
    svc._cms.bands[0].max_entries = len(svc._cms.bands[0].entries)
    svc.store("A later note into the full band.", source="user")


def test_a_string_consolidate_source_is_kept_as_given(pg_service):
    """The near miss: a string source is stored and kept unchanged."""
    svc = pg_service
    svc.store("The plain source target note.", source="user")
    svc.consolidate(replaces=["The plain source target note."],
                    new_text="The plain source replacement note.", source="{user}")
    [live] = [e for b in svc._cms.bands for e in b.entries
              if e.text == "The plain source replacement note."]
    assert live.source == "{user}"

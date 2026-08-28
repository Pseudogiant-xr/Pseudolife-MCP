"""Write-path inference: freshness_class comes from the entity's kind.

No model call on this path -- it is a dictionary lookup plus a pure function,
because it runs on every dream forever.

Also carries the two live-database halves of the same feature: the
``entity_kinds`` storage accessors the inference reads (schema v24), and the
end-to-end persistence of the ``freshness_class`` it writes (schema v23).
A column the writer never sets, or the hydrator never reads, is a column
that does not exist.
"""
from __future__ import annotations

import tempfile
import time

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def svc(pg_conn, pg_url):  # noqa: F811
    from pseudolife_memory.service import MemoryService
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = MemoryService(data_dir=d, database_url=pg_url)
        s._ensure_init()  # _kinds() below needs s._storage before any cortex_write
        try:
            yield s
        finally:
            if s._storage is not None:
                s._storage.close()


def _kinds(svc, **pairs):
    svc._storage.upsert_entity_kinds([
        {"entity_norm": k, "kind": v, "origin": "model",
         "confidence": 0.9, "decided_at": time.time()}
        for k, v in pairs.items()])
    svc._entity_kind_cache = None          # force reload


def test_system_entity_transient_attribute_infers_volatile(svc):
    _kinds(svc, daemon="system")
    svc.cortex_write("daemon", "schema-version", "24", support="action")
    assert svc.cortex_lookup("daemon", "schema-version")["freshness_class"] == "volatile"


def test_artifact_entity_same_attribute_stays_evergreen(svc):
    """The motivating pair, end to end through the real write path."""
    _kinds(svc, **{"0-9-0-release": "artifact"})
    svc.cortex_write("0-9-0-release", "schema-version", "v22", support="action")
    assert svc.cortex_lookup("0-9-0-release", "schema-version")[
        "freshness_class"] == "evergreen"


def test_explicit_class_beats_inference(svc):
    _kinds(svc, daemon="system")
    svc.cortex_write("daemon", "schema-version", "24",
                     support="action", freshness_class="evergreen")
    assert svc.cortex_lookup("daemon", "schema-version")["freshness_class"] == "evergreen"


def test_unknown_entity_defaults_evergreen(svc):
    """An unclassified entity must never start a fact decaying."""
    svc.cortex_write("never-seen", "deploy-status", "green", support="action")
    assert svc.cortex_lookup("never-seen", "deploy-status")["freshness_class"] == "evergreen"


def test_empty_kind_map_preserves_v23_behaviour(svc):
    """Until entity_kinds is populated, nothing changes."""
    svc.cortex_write("daemon", "deploy-status", "green", support="action")
    assert svc.cortex_lookup("daemon", "deploy-status")["freshness_class"] == "evergreen"


def test_entity_name_is_normalised_before_the_kind_lookup(svc):
    """Kinds are keyed by the same normalised key slots use, so a fact written
    as "Pseudolife Daemon" must find the kind stored as "pseudolife-daemon".
    Without normalisation every non-canonical spelling silently falls back to
    evergreen -- and nothing would go red."""
    _kinds(svc, **{"pseudolife-daemon": "system"})
    svc.cortex_write("Pseudolife Daemon", "deploy-status", "green", support="action")
    assert svc.cortex_lookup("Pseudolife Daemon", "deploy-status")[
        "freshness_class"] == "volatile"


# -- entity_kinds storage accessors (schema v24) ---------------------------


def _storage(pg_url):  # noqa: F811
    from pseudolife_memory.storage.postgres import PostgresStorage
    return PostgresStorage(pg_url)


def test_upsert_then_load_round_trip(pg_conn, pg_url):  # noqa: F811
    st = _storage(pg_url)
    try:
        n = st.upsert_entity_kinds([
            {"entity_norm": "daemon", "kind": "system",
             "origin": "model", "confidence": 0.9, "decided_at": time.time()},
            {"entity_norm": "0-9-0-release", "kind": "artifact",
             "origin": "rule", "confidence": 1.0, "decided_at": time.time()},
        ])
        assert n == 2
        assert st.load_entity_kinds() == {
            "daemon": "system", "0-9-0-release": "artifact"}
    finally:
        st.close()


def test_upsert_is_idempotent_and_updates_in_place(pg_conn, pg_url):  # noqa: F811
    st = _storage(pg_url)
    try:
        row = {"entity_norm": "daemon", "kind": "system",
               "origin": "model", "confidence": 0.9, "decided_at": time.time()}
        st.upsert_entity_kinds([row])
        st.upsert_entity_kinds([{**row, "kind": "concept", "origin": "user"}])
        assert st.load_entity_kinds() == {"daemon": "concept"}
        assert pg_conn.execute(
            "SELECT count(*) FROM entity_kinds").fetchone()[0] == 1
    finally:
        st.close()


# -- freshness_class persistence, end to end (schema v23) ------------------


def test_pre_v23_row_hydrates_as_evergreen(pg_conn):  # noqa: F811
    """The ALTER backfills a default, so facts written before this schema
    must read back as evergreen rather than None -- a null here would make
    ``normalize_class`` fall through to *volatile* and quietly decay the
    entire existing bank, which is the exact outcome the default avoids."""
    pg_conn.execute(
        "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, "
        "value, confidence, status, asserted_at, last_confirmed) "
        "VALUES ('legacy', 'attr', 'legacy', 'attr', 'val', 0.8, 'current', "
        "extract(epoch from now()), extract(epoch from now()))")
    pg_conn.commit()

    row = pg_conn.execute(
        "SELECT freshness_class FROM facts WHERE entity='legacy'").fetchone()
    assert row[0] == "evergreen"


def test_freshness_class_survives_a_write_read_round_trip(svc):
    """Exercise both halves through the service: the writer that sets the
    column and the hydrator that reads it back into a live CortexStore."""
    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.storage import sync

    svc.cortex_write("pseudolife-mcp", "extractor-prompt", "v2",
                     support="user", freshness_class="volatile")
    svc.cortex_write("pseudolife-mcp", "language", "python", support="user")

    rows = {r["entity"] + "/" + r["attribute"]: r for r in svc._storage.load_facts()}
    assert rows["pseudolife-mcp/extractor-prompt"]["freshness_class"] == "volatile"
    assert rows["pseudolife-mcp/language"]["freshness_class"] == "evergreen"

    c = CortexStore()
    sync.hydrate_cortex(c, svc._storage)
    assert c.lookup("pseudolife-mcp", "extractor-prompt").freshness_class == "volatile"
    assert c.lookup("pseudolife-mcp", "language").freshness_class == "evergreen"

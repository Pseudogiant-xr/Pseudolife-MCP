"""Write-path inference: freshness_class comes from the entity's kind.

No model call on this path -- it is a dictionary lookup plus a pure function,
because it runs on every dream forever.
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

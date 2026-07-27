"""Schema v23 — freshness class on personal cortex facts.

The world cortex has always decayed trust with age: a fact carries a
``freshness_class`` (evergreen / slow / volatile) and reads expose an
age-decayed ``effective_confidence`` plus a ``stale`` flag past 2xTTL. The
personal cortex had no equivalent, so a fact about transient state — the
``deployment-status: "Pending user go-ahead"`` on a slot whose deploy
finished hours earlier — kept full confidence indefinitely.

**Default is ``evergreen``, deliberately, and unlike the world cortex.**
World facts are external and rot by default; personal facts are mostly
durable ("this project is Python"). Defaulting to ``volatile`` would
silently re-rank an existing bank of hundreds of facts on an unmeasured
assumption. Evergreen means: no behaviour change until a writer marks a
fact as transient, which is the case that actually needs it.

Honest scope, recorded so nobody over-claims it later: this would NOT have
caught the 2026-07-26 v1/v2 extractor-prompt confusion on its own. That
fact was 10 days old; ``volatile`` has a 21-day TTL and only flags stale
past 2xTTL. Visible dates (v22 work) are what made that case legible.
Freshness earns its keep on facts a writer knows are transient.
"""

from __future__ import annotations

import tempfile
import time

import pytest
import torch

from pseudolife_memory.memory import freshness
from pseudolife_memory.memory.cortex import CortexRecord
from pseudolife_memory.storage import schema
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

DAY = 86400.0
EMB = torch.zeros(384)   # facts.embedding is vector(384) — PG enforces it


@pytest.fixture()
def svc(pg_conn, pg_url):  # noqa: F811
    from pseudolife_memory.service import MemoryService

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = MemoryService(data_dir=d, database_url=pg_url)
        try:
            yield s
        finally:
            if s._storage is not None:
                s._storage.close()


def test_schema_version_is_23():
    assert schema.SCHEMA_META_VERSION == 24


def test_facts_table_declares_freshness_class():
    ddl = schema.SCHEMA_SQL if hasattr(schema, "SCHEMA_SQL") else _ddl_text()
    facts_block = ddl.split("CREATE TABLE IF NOT EXISTS facts (", 1)[1].split(");", 1)[0]
    assert "freshness_class" in facts_block, (
        "facts table has no freshness_class column")
    assert "'evergreen'" in facts_block, (
        "personal facts must default to evergreen — defaulting to volatile "
        "would silently re-rank every existing fact")


def _ddl_text() -> str:
    from pathlib import Path
    return Path(schema.__file__).read_text(encoding="utf-8")


def test_new_record_defaults_to_evergreen():
    """No behaviour change for a bank that never sets the field."""
    rec = CortexRecord(entity="project", attribute="language", value="Python")
    assert rec.freshness_class == "evergreen"


def test_evergreen_never_decays_and_is_never_stale():
    old = time.time() - 5000 * DAY
    rec = CortexRecord(entity="project", attribute="language", value="Python",
                       confidence=0.9, asserted_at=old, last_confirmed=old)

    assert rec.effective_confidence() == pytest.approx(0.9)
    assert rec.is_stale() is False


def test_a_volatile_fact_loses_confidence_with_age():
    """The case this exists for: a fact about transient state."""
    now = time.time()
    fresh = CortexRecord(entity="deploy", attribute="status", value="pending",
                         confidence=0.9, asserted_at=now, last_confirmed=now,
                         freshness_class="volatile")
    aged = CortexRecord(entity="deploy", attribute="status", value="pending",
                        confidence=0.9, asserted_at=now - 21 * DAY,
                        last_confirmed=now - 21 * DAY,
                        freshness_class="volatile")

    assert fresh.effective_confidence(now) > aged.effective_confidence(now)
    assert aged.effective_confidence(now) == pytest.approx(0.9 * 0.4, abs=1e-6)


def test_a_volatile_fact_goes_stale_past_two_ttls():
    now = time.time()
    rec = CortexRecord(entity="deploy", attribute="status", value="pending",
                       confidence=0.9, asserted_at=now - 43 * DAY,
                       last_confirmed=now - 43 * DAY,
                       freshness_class="volatile")

    assert rec.is_stale(now) is True


def test_last_confirmed_is_the_decay_anchor_not_asserted_at():
    """Re-confirming a fact should restore its trust — otherwise a
    long-standing fact that is still true reads as rotten."""
    now = time.time()
    rec = CortexRecord(entity="deploy", attribute="status", value="green",
                       confidence=0.9, asserted_at=now - 100 * DAY,
                       last_confirmed=now, freshness_class="volatile")

    assert rec.effective_confidence(now) == pytest.approx(0.9)
    assert rec.is_stale(now) is False


def test_unknown_class_falls_back_to_evergreen_not_volatile():
    """`freshness.normalize_class` sends unknown values to *volatile* — right
    for world facts, which rot by default. On the personal cortex that would
    invert the whole design: a typo'd class would silently start a durable
    fact decaying. The write path must land on evergreen instead."""
    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.memory.slots import Slot

    assert freshness.normalize_class("nonsense") == "volatile"   # the helper

    store = CortexStore()
    store.write_fact(Slot("x", "y", "z"), EMB, freshness_class="nonsense")
    assert store.lookup("x", "y").freshness_class == "evergreen"


def test_a_valid_class_still_reaches_the_record():
    """Guards the obvious over-correction: falling back to evergreen must not
    swallow the classes a writer explicitly asked for."""
    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.memory.slots import Slot

    store = CortexStore()
    store.write_fact(Slot("a", "b", "c"), EMB, freshness_class="volatile")
    store.write_fact(Slot("d", "e", "f"), EMB, freshness_class="SLOW")
    assert store.lookup("a", "b").freshness_class == "volatile"
    assert store.lookup("d", "e").freshness_class == "slow"


def test_facts_column_exists_in_a_live_database(pg_conn):
    cols = {r[0] for r in pg_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='facts'").fetchall()}
    assert "freshness_class" in cols


def test_pre_v23_row_hydrates_as_evergreen(pg_conn):
    """The ALTER backfills a default, so facts written before this schema
    must read back as evergreen rather than None — a null here would make
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
    """A column the writer never sets, or the hydrator never reads, is a
    column that does not exist. Exercise both halves through the service."""
    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.memory.slots import Slot
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

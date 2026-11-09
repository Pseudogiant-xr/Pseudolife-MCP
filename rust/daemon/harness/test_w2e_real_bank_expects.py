"""w2e scenarios hold on a copy of a real bank, not only on an empty one.

The forced mirror run (mirror/forced.py) runs every w2e scenario on a copy
of a real, non-empty bank. There both daemons failed the same ``expect`` in
config-live-signal-retention, search-after-restart, console-facts-null-keys
and console-reinforce, because each assumed the scenario's rows were the
only rows in the bank: an expect counted a whole table, or a request named
entry id 1 (the seed's id on an empty bank only). In dream-rollback and
dream-rollback-persistence a fact's humanised ``age`` differed by two steps
because the two arms ran at different speeds on the larger bank (its tests:
test_w2e_lifecycle.py, beside the one-step age rule).

Each SQL test runs on a disposable bench database with the schema, some
pre-existing rows of the kind a real bank holds, and the scenario's own
rows: the old expect fails there, the scenario's current one holds, and
the state the expect guards against still fails it.
"""
import importlib
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))

ZERO_VECTOR = "array_fill(0, ARRAY[1024])::vector"


@pytest.fixture
def harness(monkeypatch):
    import pgdisposable as pg
    from rust.cli_harness.run_names import RunNames
    h = importlib.import_module("w2e")
    monkeypatch.setattr(pg, "NAMES", RunNames())
    return h


@pytest.fixture
def bank(monkeypatch, harness):
    import pgdisposable
    if not pgdisposable.LOGIN_FILE.exists():
        pytest.skip("no disposable-bank login here")
    import psycopg
    from pseudolife_memory.storage.schema import ensure_schema
    # An empty bank of its own, never a copy of a mirror's base.
    monkeypatch.delenv("PL_HARNESS_BASE_DATABASE", raising=False)
    pg = pgdisposable.Slice("w2e")
    name = pg.name("pl_cf_w2e_real_expects")
    dsn = pg.create(name)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            ensure_schema(conn)
            yield conn, dsn
    finally:
        pg.drop(name)


def holds(conn, query) -> bool:
    row = conn.execute(query).fetchone()
    return bool(row and row[0])


def expects(harness, scenario):
    """Every expect of a scenario's operations, in order."""
    return [q for op in harness.SCENARIOS[scenario]["ops"] for q in op.get("expect", ())]


def entry(conn, text, access_count=0, reinforcements=0, explicit=0):
    conn.execute(
        "INSERT INTO entries (band, text, embedding, ts, access_count, reinforcements, "
        f"explicit_reinforcements) VALUES ('flat', %s, {ZERO_VECTOR}, 1.0, %s, %s, %s)",
        (text, access_count, reinforcements, explicit))


def fact(conn, entity, attribute, value):
    conn.execute(
        "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, value, status, "
        "confidence, asserted_at, last_confirmed) "
        "VALUES (%s, %s, lower(%s), lower(%s), %s, 'current', 0.8, 1.0, 1.0)",
        (entity, attribute, entity, attribute, value))


def test_signal_retention_counts_only_the_fixture_signal(bank, harness):
    conn, _ = bank
    (query,) = [q for q in expects(harness, "config-live-signal-retention")
                if "outcome_signals" in q]
    # A real bank's own signals, recent and pending, survive the prune.
    conn.execute("INSERT INTO outcome_signals (task, outcome, created_at) "
                 "VALUES ('a real task', 'success', EXTRACT(EPOCH FROM CURRENT_TIMESTAMP))")
    assert not holds(conn, "SELECT count(*) = 0 FROM outcome_signals")
    assert holds(conn, query)
    # The near miss: the 100-day-old fixture signal still there fails it.
    conn.execute("INSERT INTO outcome_signals (task, outcome, created_at, consumed_at) "
                 "VALUES ('old fixture task', 'success', 1.0, 1.0)")
    assert not holds(conn, query)


def test_search_after_restart_counts_only_the_scenarios_entries(bank, harness):
    conn, _ = bank
    (query,) = [q for op in harness.SCENARIOS["search-after-restart"]["ops"]
                if op["op"] == "restart" for q in op["expect"]]
    texts = [op["args"]["text"] for op in harness.SCENARIOS["search-after-restart"]["ops"]
             if op.get("name") == "memory_store"]
    assert len(texts) == 2 and any("'" in t for t in texts)
    entry(conn, "A real entry read once.", access_count=1)
    entry(conn, "Another real entry read once.", access_count=1)
    entry(conn, texts[0], access_count=1)
    entry(conn, texts[1], access_count=1)
    assert not holds(conn, "SELECT count(*) = 2 FROM entries WHERE access_count = 1")
    assert holds(conn, query)
    # The near miss: a search whose access bump was lost
    # (w2d_search's search-no-access-bump) fails it.
    conn.execute("UPDATE entries SET access_count = 0 WHERE text = %s", (texts[1],))
    assert not holds(conn, query)


def test_null_key_writes_count_only_rows_they_could_land(bank, harness):
    conn, _ = bank
    queries = expects(harness, "console-facts-null-keys")
    assert len(queries) == 2
    fact(conn, "Real Service", "port", "8765")
    assert not holds(conn, "SELECT count(*) = 0 FROM facts")
    assert all(holds(conn, q) for q in queries)
    # The near misses: a null entity or attribute coerced to a string and
    # stored. Each request names the row it alone could have produced.
    for entity, attribute in (("", "field"), ("Console Null Key", "None")):
        fact(conn, entity, attribute, "valid")
        assert not any(holds(conn, q) for q in queries)
        conn.execute("DELETE FROM facts WHERE value = 'valid'")


def reinforce_ids(harness):
    """The ``entry_id`` of each checked /api/reinforce step."""
    return [op["body"]["entry_id"] for op in harness.SCENARIOS["console-reinforce"]["ops"]
            if op.get("path") == "/api/reinforce" and op.get("expect")]


def test_reinforce_by_unicode_digits_names_the_seeded_entry(bank, harness):
    """The Arabic-Indic step is ``int()``-parsed by both daemons. A literal
    "١" names entry 1, which on a real bank is not the seed's entry, so the
    target stayed unreinforced and both expects failed downstream."""
    conn, dsn = bank
    entry(conn, "A real entry that holds id 1.")
    entry(conn, "Console reinforcement target.")
    target = conn.execute("SELECT id FROM entries WHERE text = "
                          "'Console reinforcement target.'").fetchone()[0]
    assert target != 1
    # A rest body's string resolves exactly like this (w2e_episodes.run_rest).
    resolved = harness.resolve_args({"v": reinforce_ids(harness)[0]}, dsn)["v"]
    assert isinstance(resolved, str) and not resolved.isascii()
    assert int(resolved) == target


def test_reinforce_on_an_empty_bank_still_sends_arabic_indic_one(bank, harness):
    """The near miss: on the synthetic bank the request bytes are unchanged."""
    conn, dsn = bank
    entry(conn, "Console reinforcement target.")
    assert harness.resolve_args({"v": reinforce_ids(harness)[0]}, dsn)["v"] == "١"

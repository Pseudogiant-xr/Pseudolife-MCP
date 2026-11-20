"""Disposable resident rows for the clock-retry and writer-session probes."""
from copy import deepcopy

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from rust.harness_support.bank_fixture import initialize_bank


EPISODES = tuple(f"{index + 1:032x}" for index in range(3))
HLC_META_KEY = "coordination_hlc_highwater"
EPOCH_META_KEY = "writer_lease_epoch"

# These rows are the frozen startup seed's clock-retry case at 1bf44c714.
# The fixture has no entries and no vectors. Both probes load its canonical
# records, episode tree and resident metadata before exercising the clock.
_STAMP = dict(tx_time=1002.25, valid_time=999.25, writer_id="fixture-writer",
              session_id="fixture-session")
_CANONICAL = dict(_STAMP, polarity="+", status="current", confidence=0.7,
                  origin=None, support=[], provenance=[], asserted_at=0.0,
                  last_confirmed=0.0, supersedes_value=None, superseded_by_value=None,
                  superseded_at=None, embedding=None, version=1)
_FACT = dict(_CANONICAL, entity="Fixture", entity_norm="fixture", kind="scalar",
             value_norm=None, freshness_class="evergreen", stance=None, authority=None,
             distortion_tolerance=None, entity_id=None, object_entity_id=None)
_WORLD = dict(_CANONICAL, entity="External", entity_norm="external", attribute="release",
              attribute_norm="release", origin="source", support=["source"],
              source_url="", source_quote="", retrieved_at=0.0, freshness_class="volatile",
              content_hash=None, source_doc_id=None, hlc_logical=1)

_ROWS = {
    "episodes": tuple(
        dict(id=episode, title=f"Episode {index}", hint="startup fixture", started_at=1000.25 + index,
             ended_at=1010.25 if index == 0 else None, closed_by_new_start=index == 0,
             session_key=f"session-{index}", parent_id=EPISODES[1] if index == 2 else None)
        for index, episode in enumerate(EPISODES)
    ),
    "facts": (
        dict(_FACT, attribute="mode", attribute_norm="mode", value="old", status="superseded",
             superseded_by_value="new", superseded_at=1003.25, hlc_phys=100, hlc_logical=7),
        dict(_FACT, attribute="mode", attribute_norm="mode", value="new", confidence=0.8125,
             origin="user", support=["agent", "user"], provenance=list(EPISODES[1:]),
             asserted_at=1002.25, last_confirmed=1003.25, supersedes_value="old", hlc_phys=101,
             hlc_logical=0, stance="per fixture", authority="directive", distortion_tolerance="constraint"),
        dict(_FACT, attribute="tools", attribute_norm="tools", value="alpha", kind="member",
             value_norm="alpha", hlc_phys=100, hlc_logical=8),
        dict(_FACT, attribute="tools", attribute_norm="tools", value="beta", kind="member",
             value_norm="beta", origin="action", support=["action"], hlc_phys=100, hlc_logical=9),
        dict(_FACT, attribute="tools", attribute_norm="tools", value="retired", kind="member",
             value_norm="retired", status="removed", superseded_at=1004.25, hlc_phys=100, hlc_logical=10),
    ),
    "world_facts": (
        dict(_WORLD, value="old", status="superseded", superseded_by_value="new",
             superseded_at=1005.25, hlc_phys=101),
        dict(_WORLD, value="new", confidence=0.75, source_url="https://example.com/release",
             source_quote="Fixture release", retrieved_at=1005.25, content_hash="fixture-content",
             supersedes_value="old", hlc_phys=102),
    ),
    "lessons": (
        dict(_CANONICAL, entity="Fixture task", entity_norm="fixture-task", attribute="approach",
             attribute_norm="approach", value="check input", about="fixture-tool", polarity="-",
             outcome="correction", confidence=0.875, origin="agent", support=["agent"],
             provenance=["signal-1"], asserted_at=1006.25, last_confirmed=1007.25,
             entity_id=None, object_entity_id=None, hlc_phys=102, hlc_logical=6),
    ),
}
_META = {
    "active_session_pointer": {"session_id": "session-2", "ts": 1008.25},
    "episode_tombstones": {
        "swept": {"session_key": "session-old", "ended_at": 997.25, "title": "Swept"},
        "empty-title": {"session_key": "session-empty", "ended_at": None, "title": None},
        "ignored": {"title": "no session key"},
    },
    "deferred_empty_roots": {EPISODES[1]: 1009.25, "zero": None},
    "cortex_supersession_log": [
        dict(entity="Fixture", attribute="mode", old_value="old", new_value="new",
             confidence_delta=0.1125, timestamp=1003.25, decision="supersede", reason="newer_wins",
             writer_id=_STAMP["writer_id"], session_id=_STAMP["session_id"]),
    ],
    "cortex_dream_cursor": 1007.25,
}


def seed_bank(dsn, *, highwater):
    """Install the captured inputs in one transaction on a new fixture bank."""
    with psycopg.connect(dsn) as conn:
        initialize_bank(conn)
        for table, rows in _ROWS.items():
            columns = tuple(rows[0])
            statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier("public", table), sql.SQL(", ").join(map(sql.Identifier, columns)),
                sql.SQL(", ").join(sql.Placeholder() for _ in columns),
            )
            for row in rows:
                conn.execute(statement, [Jsonb(row[key]) if key in {"support", "provenance"} else row[key]
                                         for key in columns])
        for key, value in dict(_META, **{HLC_META_KEY: highwater}).items():
            conn.execute("INSERT INTO public.meta (key, value) VALUES (%s, %s)", (key, Jsonb(value)))


def with_meta(state, **values):
    """Build the expected full snapshot after replacing existing metadata."""
    expected = deepcopy(state)
    table = expected["rows"]["public.meta"]
    key, value = table["columns"].index("key"), table["columns"].index("value")
    observations = {row[key]: row for row in table["rows"]}
    for name, replacement in values.items():
        observations[name][value] = replacement
    return expected

"""SQL fixtures for dream cursor and rollback probes, with no product imports."""
from contextlib import contextmanager
import re
import time

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from rust.harness_support.database_guard import assert_disposable_database, refuse_production_database
from rust.harness_support.schema import ensure_schema
from dream_fixture_data import RECOVERY, RELATIONS


def check_target(dsn):
    name = conninfo_to_dict(dsn).get("dbname", "")
    refuse_production_database(name)
    if not re.fullmatch(r"pl_cf_(?:w[0-9][a-z]|http|pgs|prn|mir)_[a-z0-9_]{1,40}", name):
        raise ValueError("dream fixtures require a disposable harness database")
    return name


@contextmanager
def connection(dsn):
    name = check_target(dsn)
    with psycopg.connect(dsn) as conn:
        if assert_disposable_database(conn) != name:
            raise ValueError("dream fixture connection reached a different database")
        yield conn


def initialize(conn):
    """Reproduce the baseline constructor's fixture state, not its lifecycle."""
    ensure_schema(conn)
    for name, description, transitive, inverse in RELATIONS:
        conn.execute("INSERT INTO relations (name,description,transitive,inverse_of,builtin,created_at) "
                     "VALUES (%s,%s,%s,%s,TRUE,%s) ON CONFLICT (name) DO NOTHING",
                     (name, description, transitive, inverse, time.time()))
    conn.execute("INSERT INTO meta (key,value) VALUES ('writer_lease_epoch','1'::jsonb) "
                 "ON CONFLICT (key) DO UPDATE SET value=to_jsonb(COALESCE("
                 "CASE WHEN jsonb_typeof(meta.value)='number' "
                 "THEN (meta.value #>> '{}')::numeric::bigint END,0)+1)")


def seed(dsn, fixture):
    with connection(dsn) as conn:
        initialize(conn)
        for key, value in fixture.get("meta", {}).items():
            conn.execute("INSERT INTO meta(key,value) VALUES (%s,%s) "
                         "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (key, Jsonb(value)))
        for value in fixture.get("entries", []):
            entry = dict(band="flat", text="synthetic dream input", embedding=[0.0] * 1024,
                         surprise=0.5, ts=0.0, access_count=0, source="notes", superseded_at=None,
                         superseded_by_text=None, last_logical_turn=None, episode_id=None,
                         episode_title=None, tags=[], slots=[], authority=None,
                         distortion_tolerance=None, dream_state="pending")
            if set(value) - set(entry):
                raise ValueError("unknown dream fixture entry fields")
            entry.update(value)
            entry["embedding"] = "[" + ",".join(str(float(v)) for v in entry["embedding"]) + "]"
            for key in ("tags", "slots"):
                entry[key] = Jsonb(entry[key] if entry[key] is not None else [])
            conn.execute("INSERT INTO entries (band,text,embedding,surprise,ts,access_count,source,"
                         "superseded_at,superseded_by_text,last_logical_turn,episode_id,episode_title,"
                         "tags,slots,authority,distortion_tolerance,dream_state) "
                         "VALUES (%s,%s,%s::vector,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         tuple(entry.values()))


def recovery_state(dsn, name):
    """Apply the captured baseline's expected row changes to a template clone."""
    with connection(dsn) as conn:
        for update in RECOVERY[name]["updates"]:
            if update["table"] == "meta":
                conn.execute("INSERT INTO meta(key,value) VALUES (%s,%s) "
                             "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                             (update["key"], Jsonb(update["values"]["value"])))
            elif update["table"] == "entries" and set(update["values"]) == {"dream_state"}:
                conn.execute("UPDATE entries SET dream_state=%s WHERE id=%s",
                             (update["values"]["dream_state"], update["key"]))
            else:
                raise ValueError("unknown recovery fixture update")

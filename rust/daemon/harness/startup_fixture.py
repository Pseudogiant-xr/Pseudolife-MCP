"""Typed inputs for the startup-hydration cases from the frozen 2a2114b65 seed."""
import psycopg
from psycopg import sql

from rust.harness_support.bank_fixture import initialize_bank
from session_fixture import EPISODES, insert_rows, write_resident_rows


LOAD_CLOCK = 1000.0
FIDELITY_CASES = ("zero-timestamp-seating", "surprise-real-text-rounding",
                  "startup-tags-non-string-preservation")
_DIMENSION_FIXTURES = {
    "stale-entry-dims": ("entries",),
    "stale-cortex-dims": ("facts",),
    "stale-combined-dims": ("entries", "facts"),
    "world-lesson-dims-accepted": ("world_facts", "lessons"),
}
_HIGHWATERS = {
    "record-highwater": [101, 99],
    "malformed-highwater-bool": True,
    "malformed-highwater-short": [1],
    "malformed-highwater-negative": [-1, 0],
    "malformed-highwater-component": [1, True],
    "malformed-highwater-float": [1.5, 0],
}
_ENTRY = dict(source="agent", access_count=1, surprise=0.0, superseded_at=None,
              superseded_by_text=None, last_logical_turn=None, episode_id=None,
              episode_title=None, tags=[], slots=[], authority=None,
              distortion_tolerance=None, dream_state="pending")


def _basis(index, dimension=1024):
    values = [0.0] * dimension
    values[index] = 1.0
    return values


def entry_rows(case):
    if case in FIDELITY_CASES:
        times = (0.0, LOAD_CLOCK - 100) if case == "zero-timestamp-seating" else (LOAD_CLOCK,)
        surprises = (0.0,) * len(times)
        if case == "surprise-real-text-rounding":
            # Distinct cosines keep the comparison independent of top-k ties.
            heads = ((1.0, 0.0), (0.8, 0.6), (0.6, 0.8), (0.0, 1.0))
            vectors = [list(head) + [0.0] * 1022 for head in heads]
            times = (LOAD_CLOCK,) * len(vectors)
            surprises = (0.50005, 0.00005, 0.12325, 0.87505)
        else:
            vectors = [_basis(0) for _ in times]
        return [dict(_ENTRY, text=f"Fidelity entry {index}", embedding=vector,
                     ts=timestamp, surprise=surprise,
                     band="hot" if case == "zero-timestamp-seating" else "flat",
                     tags=["valid", 7, True] if case == "startup-tags-non-string-preservation" else [])
                for index, (vector, timestamp, surprise) in enumerate(zip(vectors, times, surprises))]

    # The deep-overflow golden seeds nine rows into a single band of two.
    return [dict(_ENTRY, text=f"Startup entry {index}", band="working",
                 embedding=_basis(index, 384 if index < 2 and case in
                                  {"stale-entry-dims", "stale-combined-dims"} else 1024),
                 surprise=0.125 + index / 16, ts=1000.25 + index, access_count=index,
                 superseded_at=1011.25 if index == 0 else None,
                 superseded_by_text="Startup entry 1" if index == 0 else None,
                 last_logical_turn=index, episode_id=EPISODES[2], episode_title="Episode 2",
                 tags=["startup", f"tag-{index}"], slots=[["Fixture", "entry", str(index), "+"]],
                 authority="observation" if index % 2 else None,
                 distortion_tolerance="episodic" if index % 2 else None,
                 dream_state="acknowledged" if index == 0 else "pending")
            for index in range(9 if case == "deep-overflow" else 5)]


def seed_bank(dsn, case):
    with psycopg.connect(dsn) as conn:
        initialize_bank(conn)
        if case == "empty":
            return
        for table in _DIMENSION_FIXTURES.get(case, ()):
            # The legacy vectors must survive insertion before the native
            # loader decides whether to refuse or accept them.
            conn.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN embedding TYPE vector").format(
                sql.Identifier("public", table)))
        insert_rows(conn, "entries", entry_rows(case))
        if case in FIDELITY_CASES:
            return
        cortex = 768 if case in {"stale-cortex-dims", "stale-combined-dims"} else 1024
        alternate = case == "world-lesson-dims-accepted"
        write_resident_rows(conn, highwater=_HIGHWATERS.get(case, [103, 2]), embeddings={
            "facts": {1: _basis(20, cortex), 2: _basis(21, cortex)},
            "world_facts": {1: _basis(22, 512 if alternate else 1024)},
            "lessons": {0: _basis(23, 256 if alternate else 1024)},
        })

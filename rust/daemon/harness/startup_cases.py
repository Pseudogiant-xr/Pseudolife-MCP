"""CPU-only startup hydration cells on independently cloned disposable banks.

The oracle uses sync.hydrate_cms and the required slot-store hydrators. Identity
metadata follows service.py:1308-1329; HLC input selection follows
service.py:1638-1664, before observe() adds the process's wall clock. Complete
storage.load_* rows preserve durable IDs, nullable fields and float32 vectors;
entry snapshots expose resident timestamps and tags after construction. The
zero-timestamp cell fixes both entry and seating clocks to 1000 seconds.
Dimension fault cells remove owned-column typmods before producer writes;
world/lesson vector lengths remain accepted by the source's startup guard.
This is startup evidence, not a claim that PG-HYDRATE is closed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
os.environ.setdefault("PL_HARNESS_SLICE", "pgs")
import daemons
import dbstate
import pgdisposable as pg
import psycopg
from psycopg import sql
from schema_cases import clock_diffs
from pseudolife_memory.storage.schema import assert_disposable_database

CUSTOM_CONFIG = """memory:
  embedding_dim: 1024
  miras:
    preset: custom
    bands:
      - {name: shallow, max_entries: 2, retention_policy: surprise_heavy}
      - {name: middle, max_entries: 2, retention_policy: surprise_heavy}
      - {name: deep, max_entries: 2, retention_policy: surprise_heavy}
"""
ZERO_TIME_CONFIG = """memory:
  embedding_dim: 1024
  miras:
    preset: custom
    bands:
      - {name: hot, max_entries: 1, retention_policy: balanced}
      - {name: deep, max_entries: 2, retention_policy: balanced}
"""
LOAD_CLOCK = 1000.0
FIDELITY_CASES = ("zero-timestamp-seating", "surprise-real-text-rounding",
                  "startup-tags-non-string-preservation")
CASES = (
    "empty", "seeded", "record-highwater", "preset-rename", "deep-overflow",
    "malformed-highwater-bool", "malformed-highwater-short",
    "malformed-highwater-negative", "malformed-highwater-component",
    "malformed-highwater-float", "required-loader-failure",
    "required-entry-loader-failure", "required-cortex-loader-failure",
    "required-world-loader-failure", "required-episode-loader-failure",
    "stale-entry-dims", "stale-cortex-dims", "stale-combined-dims",
    "world-lesson-dims-accepted", *FIDELITY_CASES,
)
EPISODES = tuple(f"{index + 1:032x}" for index in range(3))
REQUIRED_FAILURES = {
    "required-loader-failure": ("lessons", "about", "lesson", "lesson-loader-about"),
    "required-entry-loader-failure": ("entries", "text", "entry", "entry-loader-text"),
    "required-cortex-loader-failure": ("facts", "value", "cortex", "cortex-loader-value"),
    "required-world-loader-failure": ("world_facts", "source_quote", "world cortex", "world-loader-source-quote"),
    # hydrate_cms loads episodes within service.py's entry-hydration wrapper.
    "required-episode-loader-failure": ("episodes", "title", "entry", "episode-loader-title"),
}
STALE_FAILURES = {
    "stale-entry-dims": {"count": 2, "dims": [384]},
    "stale-cortex-dims": {"count": 2, "dims": [768]},
    "stale-combined-dims": {"count": 4, "dims": [384, 768]},
}
DIMENSION_FIXTURES = {
    "stale-entry-dims": ("entries",),
    "stale-cortex-dims": ("facts",),
    "stale-combined-dims": ("entries", "facts"),
    "world-lesson-dims-accepted": ("world_facts", "lessons"),
}
NORMALIZERS = {
    "error": "named coordination-highwater, five loader/column failures, and stale-dimension wording; exact typed count/dims retained",
    "restart.meta.writer_lease_epoch": "removed only for idempotence; increment checked separately",
    "relations.created_at": "new constructor rows only, checked against each arm's invocation window",
    "refusal.entries.band": "cosmetic seating repair precedes a later required-loader/HLC refusal; still exact in cross-arm DB diff",
}
MUTANTS = {
    "startup-skip-world": ("preset-rename", "/startup/world:"),
    "startup-hlc-zero": ("preset-rename", "/startup/hlc_highwater:"),
    "startup-drop-tombstones": ("preset-rename", "/startup/metadata/episode_tombstones/"),
    "startup-skip-seating": ("preset-rename", "/entries:"),
    "startup-skip-stamp": ("preset-rename", "/rows/public.entries/rows:"),
    "startup-skip-cortex-dims": ("stale-cortex-dims", "/ok:"),
    "startup-keep-zero-timestamp": ("zero-timestamp-seating", "/entries:"),
    "search-surprise-float32": ("surprise-real-text-rounding", "/search_entries:"),
    "startup-drop-non-string-tags": ("startup-tags-non-string-preservation", "/entries:"),
}


def plain(value):
    """Promote float32 components to JSON doubles without rounding or sorting."""
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if hasattr(value, "tolist"):
        return plain(value.tolist())
    return value


def seed(storage, case):
    """Shipped storage producers alone create fixture rows; never raw INSERTs."""
    if case == "empty":
        return
    import torch
    from pseudolife_memory.memory.cortex import CortexRecord, CortexStore
    from pseudolife_memory.memory.lessons import LessonRecord
    from pseudolife_memory.memory.titans_memory import MemoryEntry
    from pseudolife_memory.memory.world_cortex import WorldRecord
    from pseudolife_memory.storage import sync

    def vector(index, dimension=1024):
        if case == "clock-retry":
            return None
        result = torch.zeros(dimension, dtype=torch.float32, device="cpu")
        result[index] = 1.0
        return result

    if case in FIDELITY_CASES:
        surprises = (0.50005, 0.00005, 0.12325, 0.87505) if case == "surprise-real-text-rounding" else (0.0,)
        timestamps = (0.0, LOAD_CLOCK - 100) if case == "zero-timestamp-seating" else (LOAD_CLOCK,) * len(surprises)
        for index, timestamp in enumerate(timestamps):
            embedding = vector(0)
            if case == "surprise-real-text-rounding":
                # band.retrieve uses torch.topk, with no stable-order guarantee for ties.
                # Distinct cosines keep this cell about REAL surprise serialization.
                # https://docs.pytorch.org/docs/stable/generated/torch.topk.html
                embedding[0], embedding[1] = (
                    (1.0, 0.0), (0.8, 0.6), (0.6, 0.8), (0.0, 1.0),
                )[index]
            row = sync.entry_to_row(MemoryEntry(
                text=f"Fidelity entry {index}", embedding=embedding,
                timestamp=LOAD_CLOCK, access_count=1, source="agent",
                bank="hot" if case == "zero-timestamp-seating" else "flat",
                surprise_score=surprises[index] if len(surprises) > 1 else surprises[0],
                tags=["valid", 7, True] if case == "startup-tags-non-string-preservation" else [],
            ))
            # insert_entry accepts zero directly; MemoryEntry construction would replace it.
            row["ts"] = timestamp
            storage.insert_entry(row)
        return

    for index, ended in enumerate((1010.25, None, None)):
        storage.upsert_episode({
            "id": EPISODES[index], "title": f"Episode {index}",
            "hint": "startup fixture", "started_at": 1000.25 + index,
            "ended_at": ended, "closed_by_new_start": index == 0,
            "session_key": f"session-{index}",
            "parent_id": EPISODES[1] if index == 2 else None,
        })
    count = 0 if case == "clock-retry" else 9 if case == "deep-overflow" else 5
    for index in range(count):
        dimension = (384 if index < 2 and case in {"stale-entry-dims", "stale-combined-dims"}
                     else 1024)
        entry = MemoryEntry(
            text=f"Startup entry {index}", embedding=vector(index, dimension),
            surprise_score=0.125 + index / 16, timestamp=1000.25 + index,
            access_count=index, source="agent", bank="working",
            superseded_at=1011.25 if index == 0 else None,
            superseded_by_text="Startup entry 1" if index == 0 else None,
            last_logical_turn=index, episode_id=EPISODES[2],
            episode_title="Episode 2", tags=["startup", f"tag-{index}"],
            slots=[("Fixture", "entry", str(index), "+")],
            authority="observation" if index % 2 else None,
            distortion_tolerance="episodic" if index % 2 else None,
            dream_state="acknowledged" if index == 0 else "pending",
        )
        storage.insert_entry(sync.entry_to_row(entry))

    stamp = dict(tx_time=1002.25, valid_time=999.25,
                 writer_id="fixture-writer", session_id="fixture-session")
    cortex_dimension = 768 if case in {"stale-cortex-dims", "stale-combined-dims"} else 1024
    facts = [
        CortexRecord("Fixture", "mode", "old", status="superseded",
                     superseded_by_value="new", superseded_at=1003.25,
                     hlc_phys=100, hlc_logical=7, **stamp),
        CortexRecord("Fixture", "mode", "new", confidence=0.8125,
                     supersedes_value="old", embedding=vector(20, cortex_dimension),
                     support={"user", "agent"}, provenance={EPISODES[1], EPISODES[2]},
                     asserted_at=1002.25, last_confirmed=1003.25,
                     hlc_phys=101, hlc_logical=0, stance="per fixture",
                     authority="directive", distortion_tolerance="constraint", **stamp),
        CortexRecord("Fixture", "tools", "alpha", kind="member",
                     embedding=vector(21, cortex_dimension), hlc_phys=100, hlc_logical=8, **stamp),
        CortexRecord("Fixture", "tools", "beta", kind="member",
                     support={"action"}, hlc_phys=100, hlc_logical=9, **stamp),
        CortexRecord("Fixture", "tools", "retired", kind="member", status="removed",
                     superseded_at=1004.25, hlc_phys=100, hlc_logical=10, **stamp),
    ]
    storage.replace_slot_facts({record.key for record in facts},
                               [sync._record_to_row(record) for record in facts])
    world = [
        WorldRecord("External", "release", "old", status="superseded",
                    superseded_by_value="new", superseded_at=1005.25,
                    hlc_phys=101, hlc_logical=1, **stamp),
        WorldRecord("External", "release", "new", confidence=0.75,
                    source_url="https://example.com/release", source_quote="Fixture release",
                    retrieved_at=1005.25, content_hash="fixture-content",
                    embedding=vector(22, 512 if case == "world-lesson-dims-accepted" else 1024),
                    supersedes_value="old",
                    hlc_phys=102, hlc_logical=1, **stamp),
    ]
    storage.replace_slot_world_facts({record.key for record in world},
                                     [sync._world_record_to_row(record) for record in world])
    lessons = [
        LessonRecord("Fixture task", "approach", "check input", about="fixture-tool",
                     confidence=0.875, outcome="correction", polarity="-",
                     origin="agent", support={"agent"}, provenance={"signal-1"},
                     embedding=vector(23, 256 if case == "world-lesson-dims-accepted" else 1024),
                     asserted_at=1006.25, last_confirmed=1007.25,
                     hlc_phys=102, hlc_logical=6, **stamp),
    ]
    storage.replace_slot_lessons({record.key for record in lessons},
                                 [sync._lesson_record_to_row(record) for record in lessons])
    storage.meta_set("active_session_pointer", {"session_id": "session-2", "ts": 1008.25})
    storage.meta_set("episode_tombstones", {
        "swept": {"session_key": "session-old", "ended_at": 997.25, "title": "Swept"},
        "empty-title": {"session_key": "session-empty", "ended_at": None, "title": None},
        "ignored": {"title": "no session key"},
    })
    storage.meta_set("deferred_empty_roots", {EPISODES[1]: 1009.25, "zero": None})
    log = CortexStore()
    log._log(facts[0], facts[1].value, facts[1].confidence, 1003.25,
             "supersede", "newer_wins", writer_id=stamp["writer_id"], session_id=stamp["session_id"])
    storage.meta_set("cortex_supersession_log", log.supersession_log)
    storage.meta_set("cortex_dream_cursor", 1007.25)
    durable = {
        "malformed-highwater-bool": True,
        "malformed-highwater-short": [1],
        "malformed-highwater-negative": [-1, 0],
        "malformed-highwater-component": [1, True],
        "malformed-highwater-float": [1.5, 0],
    }.get(case, [103, 2] if case != "record-highwater" else [101, 99])
    storage.meta_set("coordination_hlc_highwater", durable)


def python_hydrate(storage, config_path, resident_service=None):
    import torch
    from pseudolife_memory.memory.cms import ContinuumMemorySystem
    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.memory.lessons import LessonStore
    from pseudolife_memory.memory.world_cortex import WorldCortexStore
    from pseudolife_memory.storage import sync
    from pseudolife_memory.service import MemoryService
    from pseudolife_memory.utils.config import load_config

    torch.set_num_threads(1)
    config = resident_service.config if resident_service is not None else load_config(config_path)
    dimension = resident_service._embedder.embedding_dim if resident_service is not None else 1024
    config.memory.embedding_dim = dimension  # service aligns this to the embedder.
    cms = ContinuumMemorySystem(config.memory, storage=storage)
    if resident_service is not None:
        resident_service._cms = cms
    try:
        clock = (patch("time.time", return_value=LOAD_CLOCK)
                 if os.environ.get("PL_PGS_CASE") == "zero-timestamp-seating" else nullcontext())
        with clock:
            sync.hydrate_cms(cms, storage)
    except Exception as error:
        raise RuntimeError(f"entry hydration failed: {error}") from error
    cortex, world, lessons = CortexStore(), WorldCortexStore(), LessonStore()
    try:
        sync.hydrate_cortex(cortex, storage)
    except Exception as error:
        raise RuntimeError(f"cortex hydration failed: {error}") from error
    # service.py:1142-1210 and :1584: the real guard runs after cortex and
    # before world/lessons. A stub supplies only its cheap state inputs.
    resident = resident_service or SimpleNamespace(
        _cms=cms, _cortex=cortex, _embedder=SimpleNamespace(embedding_dim=dimension),
        config=config, data_dir=Path(config_path).parent)
    resident._cortex = cortex
    MemoryService._refuse_on_stale_hydrated_dims(resident)
    for label, hydrate, store in (
        ("world cortex", sync.hydrate_world_cortex, world),
        ("lesson", sync.hydrate_lessons, lessons),
    ):
        try:
            hydrate(store, storage)
        except Exception as error:
            raise RuntimeError(f"{label} hydration failed: {error}") from error
    resident._world, resident._lessons = world, lessons

    # service.py:1308-1329: preserve its truthiness/default conversions.
    pointer = storage.get_meta("active_session_pointer")
    active = ([str(pointer["session_id"]), float(pointer.get("ts") or 0.0)]
              if isinstance(pointer, dict) and pointer.get("session_id") else None)
    raw = storage.get_meta("episode_tombstones")
    tombstones = {
        str(key): [str(value["session_key"]), float(value.get("ended_at") or 0.0),
                   str(value.get("title") or "")]
        for key, value in (raw.items() if isinstance(raw, dict) else ())
        if isinstance(value, dict) and value.get("session_key")
    }
    raw = storage.get_meta("deferred_empty_roots")
    deferred = {str(key): float(value or 0.0)
                for key, value in (raw.items() if isinstance(raw, dict) else ())}
    entries = storage.load_entries()
    residents = {entry.db_id: entry for band in cms.bands for entry in band.entries}
    seating = {entry.db_id: band.name for band in cms.bands for entry in band.entries}
    for row in entries:
        row["band"] = seating[row["id"]]
        row["ts"] = residents[row["id"]].timestamp
        row["tags"] = residents[row["id"]].tags
    snapshot = plain({"startup": {
        "episodes": storage.load_episodes(), "current_episode": cms.episodes.current_id,
        "cortex": storage.load_facts(), "world": storage.load_world_facts(),
        "lessons": storage.load_lessons(), "metadata": {
            "active_session": active, "episode_tombstones": tombstones,
            "deferred_empty_roots": deferred,
            "cortex_supersession_log": cortex.supersession_log,
            "cortex_dream_cursor": cortex.dream_cursor,
        }, "hlc_highwater": [0, 0], "hlc_reseed_pending": True,
    }, "entries": entries})
    if os.environ.get("PL_PGS_CASE") in {"surprise-real-text-rounding", "startup-tags-non-string-preservation"}:
        from pseudolife_memory.service import _entry_to_dict
        query = torch.zeros(dimension)
        query[0] = 1.0
        hits = cms.retrieve(query, top_k=12, min_score=0.0, bm25=False, rerank=False,
                            timeline=False, disable_recency_boost=True, count_access=False,
                            tags=["valid"] if os.environ["PL_PGS_CASE"] == "startup-tags-non-string-preservation" else None)
        snapshot["search_entries"] = [_entry_to_dict(entry, score)
                                      for entry, score in zip(hits.entries, hits.scores)]
    if resident_service is not None:
        resident_service._clock_retry_snapshot = snapshot
        return snapshot
    from pseudolife_memory.memory.hlc import HybridLogicalClock
    resident._world, resident._lessons, resident._storage = world, lessons, storage
    resident._hlc = HybridLogicalClock(now_ms=lambda: 100)
    resident._hlc_reseed_pending = True
    try:
        # This late source failure retains the fully loaded stores; it is
        # outside service.py's _abandon_partial_init/backoff boundary.
        MemoryService._reseed_hlc(resident)
    except Exception as error:
        return {"ok": False, "error": str(error), "retained": snapshot}
    snapshot["startup"]["hlc_highwater"] = [resident._hlc._phys, resident._hlc._logical]
    snapshot["startup"]["hlc_reseed_pending"] = resident._hlc_reseed_pending
    return {"ok": True, **snapshot}


def oracle_cell(seed_case=None):
    from pseudolife_memory.storage.postgres import PostgresStorage
    storage = None
    try:
        storage = PostgresStorage(os.environ["PL_PGS_STARTUP_DSN"])
        if seed_case:
            if seed_case in DIMENSION_FIXTURES:
                # Fault injection on this cell's empty owned template only:
                # remove the typmod so shipped producer APIs can write legacy
                # vector lengths. The additive constructor permits vector.
                for table in DIMENSION_FIXTURES[seed_case]:
                    assert_disposable_database(storage.conn)
                    storage.conn.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN embedding TYPE vector").format(
                        sql.Identifier(table)))
            seed(storage, seed_case)
            return
        try:
            result = python_hydrate(storage, os.environ["PL_PGS_CONFIG"])
        except Exception as error:
            result = {"ok": False, "error": str(error)}
            details = source_stale_dims(str(error))
            if details is not None:
                result["stale_dims"] = details
    except Exception as error:
        if seed_case:
            raise
        result = {"ok": False, "error": str(error)}
    finally:
        if storage is not None:
            storage.close()
    Path(os.environ["PL_PGS_STARTUP_OUT"]).write_text(
        json.dumps(result, allow_nan=False), encoding="utf-8")


def invoke(binary, kind, dsn, home, output, seed_case=None, mutant=None, case=None):
    env = daemons.base_env(home, {
        "PL_PGS_STARTUP_DSN": dsn, "PL_PGS_STARTUP_OUT": str(output),
        "PL_PGS_CONFIG": str(home / "data" / "config.yaml"),
        "PL_HARNESS_SLICE": "pgs", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
        "PL_PGS_CASE": case or seed_case or "",
    })
    # Prevent an unrelated harness invocation leaking a bank or output path.
    for key in list(env):
        if key.startswith("PL_") and key not in {
            "PL_PGS_STARTUP_DSN", "PL_PGS_STARTUP_OUT", "PL_PGS_CONFIG", "PL_HARNESS_SLICE",
            "PL_PGS_CASE",
        }:
            env.pop(key)
    if kind == "python":
        argv = [sys.executable, str(Path(__file__).resolve()), "--oracle-cell"]
        if seed_case:
            argv += ["--seed-cell", seed_case]
    else:
        argv = [str(binary), "--exact", "startup::tests::db_startup_inputs_dump", "--nocapture"]
        if mutant:
            env["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
    start = time.time()
    run = subprocess.run(argv, cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    window = [start, time.time()]
    if run.returncode:
        raise RuntimeError(f"{kind} startup cell exit {run.returncode}: "
                           f"{run.stdout[-1500:]} {run.stderr[-1500:]}")
    if kind == "rust" and "1 passed" not in run.stdout:
        raise RuntimeError("Rust startup probe did not run exactly one passing test")
    if seed_case:
        return None, window
    result = json.loads(output.read_text(encoding="utf-8"))
    if type(result.get("ok")) is not bool:
        raise RuntimeError(f"{kind} startup probe omitted its boolean ok field")
    if not result["ok"] and set(result) not in (
            {"ok", "error"}, {"ok", "error", "stale_dims"}, {"ok", "error", "retained"}):
        raise RuntimeError(f"{kind} failed startup exposed partial resident state")
    retained = result.get("retained")
    if not result["ok"] and retained is not None and (
            not isinstance(retained, dict) or set(retained) != {"startup", "entries"}
            or retained.get("startup", {}).get("hlc_reseed_pending") is not True):
        raise RuntimeError(f"{kind} retained an incomplete startup or a resolved clock failure")
    return result, window


def source_stale_dims(error):
    """Retain the real service guard's count and dimensions, never its path."""
    match = re.search(r"Refusing to serve: (\d+) hydrated row\(s\).*? are embedded at "
                      r"(\d+(?:, \d+)*) dims, but the live embedder .*? produces 1024-d vectors", error)
    if match is None:
        return None
    return {"count": int(match[1]), "dims": [int(value) for value in match[2].split(", ")]}


def normalized_result(result):
    result = json.loads(json.dumps(result))
    if not result["ok"]:
        error = result["error"]
        details = result.get("stale_dims")
        if (isinstance(details, dict) and set(details) == {"count", "dims"}
                and type(details["count"]) is int and details["count"] > 0
                and type(details["dims"]) is list and details["dims"]
                and all(type(value) is int and value > 0 for value in details["dims"])
                and details["dims"] == sorted(set(details["dims"]))
                and f'{details["count"]} hydrated row(s)' in error):
            result["error"] = "stale-hydrated-dimensions"
        elif "invalid coordination clock high-water mark" in error:
            result["error"] = "coordination-highwater"
        else:
            for table, column, loader, category in REQUIRED_FAILURES.values():
                if f"{loader} hydration failed" in error and f'column "{column}"' in error:
                    result["error"] = category
                    break
    return result


def without_epoch(state):
    state = json.loads(json.dumps(state))
    table = state["rows"].get("public.meta")
    if table:
        index = table["columns"].index("key")
        table["rows"] = [row for row in table["rows"] if row[index] != "writer_lease_epoch"]
    return state


def without_refusal_bookkeeping(state):
    state = without_epoch(state)
    table = state["rows"].get("public.entries")
    if table:
        index = table["columns"].index("band")
        for row in table["rows"]:
            row[index] = "<pre-refusal seating>"
    return state


def epoch(state):
    table = state["rows"]["public.meta"]
    key, value = table["columns"].index("key"), table["columns"].index("value")
    return next(row[value] for row in table["rows"] if row[key] == "writer_lease_epoch")


def cell(binary, number, case, result, mutant=None):
    names = [f"{pg.PREFIX}{number + index}" for index in range(3)]
    created = []
    try:
        with tempfile.TemporaryDirectory(prefix="pl-pgs-startup-") as scratch:
            root = Path(scratch)
            config = (ZERO_TIME_CONFIG if case == "zero-timestamp-seating" else
                      CUSTOM_CONFIG if case in {"preset-rename", "deep-overflow"} else None)
            homes = [daemons.make_home(root, kind, config) for kind in ("seed", "python", "rust")]
            template = pg.create(names[0])
            created.append(names[0])
            invoke(binary, "python", template, homes[0], root / "seed.json", seed_case=case)
            if case in REQUIRED_FAILURES:
                table, column, _, _ = REQUIRED_FAILURES[case]
                with psycopg.connect(template, autocommit=True) as conn:
                    assert_disposable_database(conn)
                    conn.execute(sql.SQL("ALTER TABLE {} DROP COLUMN {}").format(
                        sql.Identifier(table), sql.Identifier(column)))
            before = dbstate.dump(template)
            dsns = []
            for name in names[1:]:
                dsns.append(pg.create(name, template=names[0]))
                created.append(name)
            initial = None
            result["stages"] = []
            for restart in range(2):
                outputs, states, windows = [], [], []
                for kind, dsn, home in zip(("python", "rust"), dsns, homes[1:]):
                    output, window = invoke(binary, kind, dsn, home, root / f"{kind}-{restart}.json", mutant=mutant, case=case)
                    outputs.append(output)
                    states.append(dbstate.dump(dsn))  # immediately after each arm
                    windows.append(window)
                expected_ok = not (case.startswith("malformed-highwater")
                                   or case in REQUIRED_FAILURES or case in STALE_FAILURES)
                expected_error = ("stale-hydrated-dimensions" if case in STALE_FAILURES
                                  else REQUIRED_FAILURES[case][3] if case in REQUIRED_FAILURES
                                  else "coordination-highwater")
                semantic_diffs = dbstate.diff(*(normalized_result(output) for output in outputs))
                for kind, output, state in zip(("python", "rust"), outputs, states):
                    if output["ok"] != expected_ok:
                        semantic_diffs.append(f"{kind}: expected ok={expected_ok}")
                    elif not expected_ok and normalized_result(output)["error"] != expected_error:
                        semantic_diffs.append(f"{kind}: wrong refusal category")
                    if case in STALE_FAILURES:
                        semantic_diffs.extend(dbstate.diff(
                            STALE_FAILURES[case], output.get("stale_dims"), f"/{kind}/expected_stale_dims"))
                    if output["ok"] and case == "zero-timestamp-seating":
                        semantic_diffs.extend(dbstate.diff(
                            [("hot", LOAD_CLOCK), ("deep", LOAD_CLOCK - 100)],
                            [(row["band"], row["ts"]) for row in output["entries"]],
                            f"/{kind}/zero_timestamp_seats"))
                        table = state["rows"]["public.entries"]
                        columns = table["columns"]
                        durable = sorted(table["rows"], key=lambda row: row[columns.index("id")])
                        semantic_diffs.extend(dbstate.diff(
                            [("hot", 0.0), ("deep", LOAD_CLOCK - 100)],
                            [(row[columns.index("band")], row[columns.index("ts")]) for row in durable],
                            f"/{kind}/durable_zero_timestamp_seats"))
                    if output["ok"] and case == "surprise-real-text-rounding":
                        semantic_diffs.extend(dbstate.diff(
                            [0.5, 0.0001, 0.1232, 0.875],
                            [row["surprise_score"] for row in output.get("search_entries", [])],
                            f"/{kind}/surprise_scores"))
                    if output["ok"] and case == "startup-tags-non-string-preservation":
                        semantic_diffs.extend(dbstate.diff(
                            json.dumps([["valid", 7, True]]),
                            json.dumps([row["tags"] for row in output["entries"]]),
                            f"/{kind}/resident_tags"))
                rules = {("relations", "created_at"): "clock"}
                stage = {
                    "restart": restart, "snapshots": dict(zip(("python", "rust"), outputs)),
                    "snapshot_diffs": semantic_diffs,
                    "db_diffs": dbstate.diff(*(dbstate.normalize(state, rules, before) for state in states)),
                    "clock_diffs": [difference for state, window in zip(states, windows)
                                    for difference in clock_diffs(state, before, window)],
                    "windows": dict(zip(("python", "rust"), windows)),
                }
                if initial is not None:
                    stage["idempotence_diffs"] = [
                        difference for old, new in zip(initial[1], states)
                        for difference in dbstate.diff(without_epoch(old), without_epoch(new))
                    ] + [difference for old, new in zip(initial[0], outputs)
                         for difference in dbstate.diff(normalized_result(old), normalized_result(new))]
                else:
                    initial = (outputs, states)
                for index, state in enumerate(states):
                    prior = before if restart == 0 else initial[1][index]
                    old_epoch, new_epoch = epoch(prior), epoch(state)
                    if type(old_epoch) is not int or type(new_epoch) is not int or new_epoch != old_epoch + 1:
                        stage.setdefault("epoch_diffs", []).append(f"arm {index}: constructor epoch did not increment once")
                    if not expected_ok:
                        unchanged = dbstate.diff(without_refusal_bookkeeping(prior), without_refusal_bookkeeping(state))
                        stage.setdefault("refusal_state_diffs", []).extend(unchanged)
                result["stages"].append(stage)
    finally:
        cleanup_errors = []
        for name in reversed(created):
            try:
                pg.drop(name)
            except Exception as error:
                cleanup_errors.append(f"{name}: {type(error).__name__}")
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors


def failed(result):
    return bool(result.get("error") or result.get("cleanup_errors") or any(
        value for stage in result.get("stages", []) for key, value in stage.items()
        if key.endswith("_diffs")))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rust-test-bin", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--only", nargs="+", choices=CASES)
    parser.add_argument("--mutants", action="store_true")
    golden_mode = parser.add_mutually_exclusive_group()
    golden_mode.add_argument("--record-goldens", action="store_true")
    golden_mode.add_argument("--check-goldens", action="store_true")
    parser.add_argument("--oracle-cell", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--seed-cell", choices=CASES, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if pg.SLICE != "pgs":
        parser.error("startup cells require PL_HARNESS_SLICE=pgs")
    if args.oracle_cell:
        oracle_cell(args.seed_cell)
        return 0
    if args.rust_test_bin is None or args.out is None:
        parser.error("--rust-test-bin and --out are required")
    if args.record_goldens and args.only:
        parser.error("--record-goldens requires the complete startup matrix")
    binary = args.rust_test_bin.resolve()
    if not binary.is_file():
        parser.error("--rust-test-bin must name the built daemon Cargo test executable")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO).decode().strip(),
               "normalizers": NORMALIZERS, "cells": []}
    number = time.time_ns()
    golden_path = HERE / "goldens" / "startup-hydration.json"
    goldens = json.loads(golden_path.read_text()) if args.check_goldens else None
    for index, case in enumerate(args.only or CASES):
        result = {"case": case}
        payload["cells"].append(result)
        try:
            cell(binary, number + index * 3, case, result)
            if goldens and not failed(result):
                for stage_index, stage in enumerate(result["stages"]):
                    stage["golden_diffs"] = dbstate.diff(
                        goldens["cases"][case][stage_index], normalized_result(stage["snapshots"]["rust"]))
        except Exception as error:
            result["error"] = str(error)
        args.out.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(f"startup {case}: {'FAIL' if failed(result) else '0 diffs'}", flush=True)
    bad = any(failed(result) for result in payload["cells"])
    if args.mutants and not bad:
        payload["mutants"] = {}
        for index, (mutant, (mutant_case, wanted_path)) in enumerate(MUTANTS.items()):
            result = {"case": mutant_case}
            try:
                cell(binary, number + 1000 + index * 3, mutant_case, result, mutant=mutant)
            except Exception as error:
                result["error"] = str(error)
            stages = result.get("stages", [])
            # An infrastructure failure is never proof of a source mutant.
            infrastructure_failed = bool(result.get("error") or result.get("cleanup_errors") or any(
                stage.get("clock_diffs") or stage.get("epoch_diffs") for stage in stages))
            differences = [difference for stage in stages for key in ("snapshot_diffs", "db_diffs")
                           for difference in stage[key]]
            caught = not infrastructure_failed and len(stages) == 2 and any(
                difference.startswith(wanted_path) for difference in differences)
            payload["mutants"][mutant] = {
                "caught": caught, "expected_path": wanted_path,
                "differences": differences,
                **({"error": result["error"]} if result.get("error") else {}),
                **({"cleanup_errors": result["cleanup_errors"]} if result.get("cleanup_errors") else {}),
            }
            print(f"startup mutant {mutant}: {'caught' if caught else 'SURVIVED'}", flush=True)
            args.out.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        bad = any(not value["caught"] for value in payload["mutants"].values())
    if args.record_goldens and not bad:
        golden_path.write_text(json.dumps({"normalizers": NORMALIZERS, "oracle_head": payload["head"],
            "cases": {result["case"]: [normalized_result(s["snapshots"]["python"]) for s in result["stages"]]
                      for result in payload["cells"]}}, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return int(bad)


if __name__ == "__main__":
    raise SystemExit(main())

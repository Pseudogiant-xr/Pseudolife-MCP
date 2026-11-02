"""CLI-MIGRATE-FLAT: offline migration, including its physical backup pair.

The candidate directory must also contain the matching pseudolife-daemon.
Only UUID directory names, verified backup timestamps and physical archive
encoding are normalized; entry rows and migration plan hashes remain exact.
"""

import atexit
import base64
import json
import hashlib
import os
from pathlib import Path
import re

from .. import normalize
from ..core import Case
from . import _bank, backup

DB = _bank.name("pl_cf_migrate_flat")
CREATED = False
MARKER = "flat_band_migration_v1"


@atexit.register
def cleanup():
    if CREATED:
        _bank.drop(DB)


def setup(config, rows=True):
    def prepare(arm):
        global CREATED
        _bank.create(DB)
        CREATED = True
        with _bank.connect(DB) as conn:
            if rows:
                conn.execute("INSERT INTO episodes (id,title,started_at) VALUES ('ep','session',42)")
                for index, band in enumerate(("working", "micro", "instant", "fast", "medium", "slow", "archival", "forever", "orphan")):
                    conn.execute("INSERT INTO entries (band,text,embedding,ts,episode_id,superseded_at) "
                                 "VALUES (%s,'same text',%s::vector,%s,'ep',%s)",
                                 (band, "[" + ",".join(["0.1"] * 1024) + "]", index, 42 if index == 0 else None))
            conn.commit()
        bank = arm.home / "bank"
        bank.mkdir()
        original = bank / "config.yaml"
        original.write_bytes(config.encode())
        os.utime(original, (backup.FIXED, backup.FIXED))
        arm.state["before"] = _bank.dump(DB)
    return prepare


def after(arm, obs):
    assert obs["exit"] == 0, "migration must succeed, not match a shared refusal"
    current = _bank.dump(DB)
    before = arm.state["before"]
    original_entries = [json.loads(row) for row in before["tables"]["entries"]]
    entries = [json.loads(row) for row in current["tables"]["entries"]]
    assert len(entries) == len(original_entries)
    assert all(entry["band"] == "flat" for entry in entries)
    assert [{k: v for k, v in row.items() if k != "band"} for row in entries] == [
        {k: v for k, v in row.items() if k != "band"} for row in original_entries]
    for table in before["tables"]:
        if table not in ("entries", "meta"):
            assert current["tables"][table] == before["tables"][table], table
    assert current["sequences"] == before["sequences"]
    obs["db"] = current


def refused(arm, obs):
    writer = arm.state.get("writer")
    if writer is not None:
        writer.close()
    assert obs["exit"] == 1
    assert _bank.dump(DB) == arm.state["before"]
    obs["db"] = arm.state["before"]


def held_writer(arm):
    setup("memory: {miras: {preset: continuum}}\n")(arm)
    writer = _bank.connect(DB, autocommit=True)
    writer.execute("SELECT pg_advisory_lock(hashtextextended('pseudolife-bank-writer',0))")
    arm.state["writer"] = writer


def pending(replaced):
    def prepare(arm):
        import yaml
        from pseudolife_memory.migrate_flat_cli import flat_config
        original = "memory: {miras: {preset: continuum}}\n"
        target = yaml.safe_dump(flat_config(yaml.safe_load(original), 9)[0], sort_keys=False)
        setup(target if replaced else original)(arm)
        marker = {"version": 1, "complete": False, "entries": 9, "capacity": 5250,
                  "original_sha256": hashlib.sha256(original.encode()).hexdigest(),
                  "target_sha256": hashlib.sha256(target.encode()).hexdigest(),
                  "backup": {"dump": "bank.sql.gz", "state": "state.tgz"}}
        with _bank.connect(DB) as conn:
            conn.execute("UPDATE entries SET band='flat'")
            conn.execute("INSERT INTO meta(key,value) VALUES (%s,%s::jsonb)", (MARKER, json.dumps(marker)))
            conn.commit()
        arm.state["before"] = _bank.dump(DB)
    return prepare


@normalize.rule("flat-migration-files")
def files(obs):
    pattern = re.compile(r"(?:^|/)bank/backups/migrate-flat-([0-9a-f]{32})(?:/|$)")
    identifiers = {match[1] for name in obs["files"] if (match := pattern.search(name))}
    if len(identifiers) > 1:
        raise ValueError("migration unexpectedly created more than one backup directory")
    identifier = next(iter(identifiers), None)
    if identifier:
        for field in ("files", "modes"):
            if field in obs:
                obs[field] = {name.replace(identifier, "<migration-id>"): value for name, value in obs[field].items()}
        for field in ("stdout", "stderr"):
            obs[field] = base64.b64encode(base64.b64decode(obs[field]).replace(identifier.encode(), b"<migration-id>")).decode()
    for row_index, text in enumerate(obs.get("db", {}).get("tables", {}).get("meta", [])):
        row = json.loads(text)
        if row["key"] != MARKER:
            continue
        for field in ("dump", "state"):
            path = row["value"]["backup"][field]
            if identifier:
                path = path.replace(identifier, "<migration-id>")
            row["value"]["backup"][field] = backup._stamp(path.encode(), obs["window"]).decode()
        obs["db"]["tables"]["meta"][row_index] = json.dumps(row, sort_keys=True)
    backup.backup_files(obs)


def cases():
    from pseudolife_memory.backup_cli import _find_pg_dump
    pg_dump = _find_pg_dump()
    if pg_dump is None:
        raise RuntimeError("migrate_flat parity needs PostgreSQL client tools")
    env = {"PSEUDOLIFE_MCP_DATABASE_URL": _bank.url(DB),
           "PATH": str(pg_dump.parent) + os.pathsep + os.environ.get("PATH", "")}
    argv = ["migrate-flat", "--apply", "--data-dir", "{HOME}/bank"]
    return [
        Case("help", ["migrate-flat", "--help"]),
        Case("no-apply", ["migrate-flat", "--data-dir", "{HOME}/bank"]),
        Case("missing-data-dir", ["migrate-flat", "--apply"]),
        Case("writer-held", argv, env=env, setup=held_writer, after=refused),
        *[Case("refuse-" + name, argv, env=env, setup=setup(config), after=refused)
          for name, config in (
              ("timestamp", "date: 2026-11-02\n"),
              ("binary", "blob: !!binary YQ==\n"),
              ("integer", "large: 9223372036854775808\n"),
              ("alias", "a: &x [1, 2]\nb: *x\n"),
              ("preset", "memory: {miras: {preset: typo}}\n"),
              ("policy", "memory: {miras: {preset: custom, bands: [{name: hot, retention_policy: typo}]}}\n"),
          )],
        *[Case("resume-" + state, argv, env=env, setup=pending(replaced), after=after)
          for state, replaced in (("before-replace", False), ("after-replace", True))],
        *[Case("collapse-" + preset, argv, env=env,
               setup=setup("unrelated: yes\nmemory:\n  miras:\n    preset: " + preset + "\n"),
               after=after, rules=("flat-migration-files",), timeout=120,
               real_programs=("pg_dump",), expect_output=True)
          for preset in ("continuum", "titans", "moneta", "yaad", "memora", "flat")],
        Case("custom-capacity", argv, env=env,
             setup=setup("memory:\n  miras:\n    preset: custom\n    bands:\n    - {name: hot, max_entries: 7000, retention_policy: recency_heavy}\n    - {name: deep, max_entries: 9000, retention_policy: surprise_heavy}\n"),
             after=after, rules=("flat-migration-files",), timeout=120,
             real_programs=("pg_dump",), expect_output=True),
        Case("flat-noop", argv, env=env,
             setup=setup("memory: {miras: {preset: flat}}\n", rows=False),
             after=after, timeout=60, real_programs=("pg_dump",), expect_output=True),
    ]

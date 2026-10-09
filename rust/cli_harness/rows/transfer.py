"""CLI-TRANSFER: ``pseudolife-mcp export`` / ``import`` on an explicit DSN.

Export cases read one seeded source bank (both arms export the same bank; its
dump after each arm proves the export left it untouched). Import cases load
an archive written by the oracle's own ``perform_export`` (or a hand-varied
copy, the same edits ``tests/test_transfer_cli.py`` makes) into a fresh
target bank recreated by the oracle's ``ensure_schema`` before each arm, and
compare both arms' full dumps.

Where the candidate defers by design, or fails with its named PostgreSQL
diagnostic where the oracle prints a traceback, the case opts into an
``expect-*`` rule: the oracle arm's streams are replaced by the expected
native line (after checking the oracle's own shape), its files by the
pre-run snapshot and, for deferrals, its database by the pre-run dump. The
candidate is then compared against that expectation: deferring must change
nothing.
"""

from __future__ import annotations

import atexit
import base64
import datetime
import io
import json
import re
import time
import zipfile

from .. import core, normalize
from ..mutants import Mutant
from . import _bank

ROW = "transfer"
SRC = "pl_cf_w1c_transfer_src"
TRICKY = "pl_cf_w1c_transfer_tricky"
TGT = "pl_cf_w1c_transfer_tgt"
INF = "pl_cf_w1c_transfer_inf"
DEEP = "pl_cf_w1c_transfer_deep"
NYC = "pl_cf_w1c_transfer_nyc"
NYC_EMPTY = "pl_cf_w1c_transfer_nyc_empty"
_CREATED: set[str] = set()
_ARCHIVES: dict[str, bytes] = {}
_EOL = "\r\n" if core.WINDOWS else "\n"
_SEP = "\\" if core.WINDOWS else "/"


def _drop_all() -> None:
    for name in sorted(_CREATED):
        try:
            _bank.drop(name)
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass


atexit.register(_drop_all)


def _create(name: str, schema: bool = True) -> None:
    import psycopg  # noqa: PLC0415
    _CREATED.add(name)
    # _bank.drop terminates every backend on the database; an autovacuum
    # worker it may not signal fails that call. Wait for the worker instead.
    for attempt in range(40):
        try:
            _bank.create(name, schema=schema)
            return
        except psycopg.errors.InsufficientPrivilege:
            if attempt == 39:
                raise
            time.sleep(0.5)


def _wait_idle(name: str) -> None:
    """The guard counts client backends: wait for closed setup sessions to go."""
    with _bank._admin() as conn:  # noqa: SLF001 - same module family
        for _ in range(200):
            n = conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = %s "
                "AND backend_type = 'client backend'", (name,)).fetchone()[0]
            if n == 0:
                return
            time.sleep(0.05)
    raise RuntimeError(f"{name}: setup sessions did not close")


def _seed(name: str) -> None:
    from tests.test_transfer_cli import _seed_bank  # noqa: PLC0415 (oracle checkout)
    with _bank.connect(name) as conn:
        _seed_bank(conn)


_TRICKY_SQL = r"""
INSERT INTO episodes (id, title, hint, started_at, ended_at, session_key) VALUES
 ('ep-f1', E'quote " back \\ tab \t nl \n cr \r ctl \x01 del \x7f', NULL,
  'Infinity', '-Infinity', E'astral \U0001F600 sep   nbsp   é'),
 ('ep-f2', 'big', 'hint', 1e15, 1e16, NULL),
 ('ep-f3', 'small', '', 0.0001, 0.00001, NULL),
 ('ep-f4', 'neg zero', NULL, '-0', 5e-324, NULL),
 ('ep-f5', 'nan', NULL, 'NaN', 123456789.123456789, NULL),
 ('ep-f6', 'max', NULL, 1.7976931348623157e308, -2.2250738585072014e-308, NULL);
INSERT INTO entity_kinds (entity_norm, kind, origin, confidence, decided_at) VALUES
 ('k-nan', 'x', 'y', 'NaN', 1.5),
 ('k-inf', 'x', 'y', '-Infinity', 2.5),
 ('k-denorm', 'x', 'y', 1e-45, 3.5),
 ('k-max', 'x', 'y', 3.4028235e38, 4.5),
 ('k-tenth', 'x', 'y', 0.1, 5.5),
 ('k-negzero', 'x', 'y', '-0', 6.5),
 ('k-int', 'x', 'y', 16777217, 7.5),
 ('k-null', 'x', 'y', NULL, 8.5);
INSERT INTO store_decisions (store, entity_norm, attribute_norm, action,
  decided_by, reason, record, decided_at) VALUES
 ('lesson', 'e', 'a', 'retire', NULL, NULL,
  '{"zeta": 1, "a": [1.0, 1.50, 1e-7, 1E16, 12345678901234567890123, -0,
    0.1000000000000000055511151231257827, 1e400, -1.5e-400],
    "": true, "nested": {"k": null, "s": "é \"\\\/\b\f\n\r\t\u0001"},
    "emoji": "😀", "empty": {}, "list": []}', 1.0),
 ('world', 'e', 'b', 'restore', 'me', 'why', '"just a string"', 2.0),
 ('world', 'e', 'c', 'restore', 'me', 'why', '[]', 3.0),
 ('world', 'e', 'd', 'restore', 'me', 'why', '12.5000', 4.0);
INSERT INTO chronicle_events (occurred_at, recorded_at, actor, actor_norm,
  description, description_norm) VALUES
 ('2026-08-30T12:00:00.123+00', 200.0, 'a', 'a', 'micro', 'micro'),
 ('0044-03-15 12:00:00.000001+00', 201.0, 'b', 'b', 'old', 'old'),
 ('9999-12-31 23:59:59.999999+00', 202.0, 'c', 'c', 'late', 'late'),
 ('2026-01-01 00:00:00-05', 203.0, 'd', 'd', 'offset', 'offset');
INSERT INTO meta (key, value) VALUES
 ('tricky_meta', '{"zz": [1, 2.0], "a": "é", "bb": null}'),
 ('sampleext_schema_version', '"v34-sampleext"'),
 ('coordination_bank_id', '"11111111-1111-4111-8111-111111111111"'),
 ('writer_lease_epoch', '7');
UPDATE outcome_signals SET used_ids =
 '{"credited": [1], "unmatched": [], "served_elsewhere": []}';
INSERT INTO dismissed_pairs (a_norm, b_norm, dismissed_at)
 SELECT 'bulk-a-' || g, 'bulk-b-' || g, g * 0.25 FROM generate_series(1, 1200) g;
"""


def _seed_tricky(name: str) -> None:
    _seed(name)
    with _bank.connect(name) as conn:
        conn.execute(_TRICKY_SQL)
        conn.commit()


def _sources() -> None:
    """Both source banks and the oracle's archives, built once per process."""
    if _ARCHIVES:
        return
    import tempfile  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from pseudolife_memory.transfer_cli import perform_export  # noqa: PLC0415
    _create(SRC)
    _seed(SRC)
    _create(TRICKY)
    _seed_tricky(TRICKY)
    _create(INF)
    _create(DEEP)
    _create(NYC)
    _seed(NYC)
    _create(NYC_EMPTY)
    with _bank.connect(INF) as conn:
        conn.execute("INSERT INTO chronicle_events (occurred_at, recorded_at, actor, "
                     "actor_norm, description, description_norm) VALUES "
                     "('infinity', 1.0, 'a', 'a', 'never', 'never')")
        conn.commit()
    with _bank.connect(DEEP) as conn:
        conn.execute("INSERT INTO store_decisions (store, entity_norm, attribute_norm, "
                     "action, record, decided_at) VALUES ('lesson', 'e', 'a', 'retire', "
                     "%s::jsonb, 1.0)", ("[" * 250 + "]" * 250,))
        conn.commit()
    with _bank._admin() as conn:  # noqa: SLF001
        for name in (NYC, NYC_EMPTY):
            conn.execute(f'ALTER DATABASE "{name}" SET timezone = \'America/New_York\'')
    with tempfile.TemporaryDirectory() as tmp:
        for key, name in (("current", SRC), ("tricky", TRICKY)):
            path = Path(tmp) / f"{key}.zip"
            perform_export(_bank.url(name), path)
            _ARCHIVES[key] = path.read_bytes()
    for key, mutate in _VARIANTS.items():
        _ARCHIVES[key] = _rewrite(_ARCHIVES["current"], mutate)
    for name in (SRC, TRICKY, INF, DEEP, NYC, NYC_EMPTY):
        _wait_idle(name)


def _rewrite(blob: bytes, mutate) -> bytes:
    """tests/test_transfer_cli.py::_rewrite_zip on bytes."""
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        blobs = {n: zf.read(n) for n in zf.namelist()}
    mutate(blobs)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in blobs.items():
            zf.writestr(name, data)
    return out.getvalue()


def _manifest_edit(**changes):
    def mutate(blobs):
        manifest = json.loads(blobs["manifest.json"])
        for key, value in changes.items():
            if value is _DROP:
                manifest.pop(key, None)
            else:
                manifest[key] = value
        blobs["manifest.json"] = json.dumps(manifest).encode()
    return mutate


_DROP = object()


def _without_v39(schema_version):
    def mutate(blobs):
        blobs.pop("memory_trace_invalidations.jsonl")
        manifest = json.loads(blobs["manifest.json"])
        manifest["schema_version"] = schema_version
        manifest["counts"].pop("memory_trace_invalidations", None)
        blobs["manifest.json"] = json.dumps(manifest).encode()
    return mutate


def _empty_v39(blobs):
    blobs["memory_trace_invalidations.jsonl"] = b""
    manifest = json.loads(blobs["manifest.json"])
    manifest["counts"]["memory_trace_invalidations"] = 0
    blobs["manifest.json"] = json.dumps(manifest).encode()


def _edit_first(member, **changes):
    def mutate(blobs):
        lines = blobs[member].decode().split("\n")
        rec = json.loads(lines[0])
        rec.update(changes)
        lines[0] = json.dumps(rec)
        blobs[member] = "\n".join(lines).encode()
    return mutate


def _append_meta(key, value):
    def mutate(blobs):
        row = json.dumps({"key": key, "value": value})
        blobs["meta.jsonl"] = blobs["meta.jsonl"] + (row + "\n").encode()
    return mutate


def _reverse_relations(blobs):
    lines = [ln for ln in blobs["relations.jsonl"].decode().split("\n") if ln]
    blobs["relations.jsonl"] = "\n".join(reversed(lines)).encode()


def _drop_column(member, column, first_only=False):
    def mutate(blobs):
        lines = []
        for index, line in enumerate(blobs[member].decode().split("\n")):
            if not line:
                continue
            rec = json.loads(line)
            if not first_only or index == 0:
                rec.pop(column, None)
            lines.append(json.dumps(rec))
        blobs[member] = "\n".join(lines).encode()
    return mutate


def _no_manifest(blobs):
    blobs.pop("manifest.json")


def _bad_evidence(blobs):
    blobs["edge_evidence.jsonl"] = b'{"edge_id": 99, "entry_id": 1}\n'


_VARIANTS = {
    "no-v39-schema-38": _without_v39(38),
    "no-v39-schema-37": _without_v39(37),
    "legacy-37": _manifest_edit(schema_version=37),
    "empty-v39": _empty_v39,
    "flag": _append_meta("curation_listing_spelling_v2", {"copied": 2, "at": 160.0}),
    "inject-ext": _append_meta("sampleext_schema_version", "v34-sampleext"),
    "inject-bank-id": _append_meta("coordination_bank_id",
                                   "11111111-1111-4111-8111-111111111111"),
    "unknown-column": _edit_first("entries.jsonl", from_the_future=1),
    "unknown-meta-column": _edit_first("meta.jsonl", meta_extra=1),
    "reversed-relations": _reverse_relations,
    "dropped-column": _drop_column("entries.jsonl", "explicit_reinforcements"),
    "mixed-columns": _drop_column("entries.jsonl", "explicit_reinforcements", first_only=True),
    "nonfinite": _edit_first("entries.jsonl", surprise=float("nan"), ts=float("inf")),
    "dim-512": _manifest_edit(embedding_dim=512),
    "dim-string": _manifest_edit(embedding_dim="512"),
    "format-9": _manifest_edit(format_version=9),
    "format-string": _manifest_edit(format_version="1"),
    "format-missing": _manifest_edit(format_version=_DROP),
    "format-float": _manifest_edit(format_version=1.0),
    "no-manifest": _no_manifest,
    "bad-evidence": _bad_evidence,
    "string-in-float": _edit_first("episodes.jsonl", started_at="not-a-float"),
    "float4-overflow": _edit_first("entries.jsonl", surprise=1e39),
    "huge-int-float": _edit_first("entries.jsonl", ts=2 ** 64),
    "bad-vector": _edit_first("entries.jsonl", embedding="[1,2]"),
}


# ------------------------------------------------------------------ rules


def _window_utc(text: str, window) -> bool:
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError:
        return False
    return (moment.utcoffset() == datetime.timedelta(0)
            and window[0] - 1 <= moment.timestamp() <= window[1] + 1)


_CREATED_AT = re.compile(rb'"created_at": "([^"]+)"')
_VERSION = re.compile(rb'"pseudolife_version": ("[0-9]+(?:\.[0-9]+)+[^"]*"|null)')


@normalize.rule("transfer-zip")
def transfer_zip(obs: dict) -> None:
    """Archives compare as member names, order and decompressed contents (the
    ZIP container bytes are free). Python's own zipfile reads each archive,
    CRCs included. In manifest.json only ``created_at`` (validated as UTC
    inside the arm's window) and ``pseudolife_version`` (the installed
    distribution's version vs the native crate's) are replaced."""
    for rel, value in list(obs["files"].items()):
        if not rel.endswith(".zip") or not value.startswith("file:"):
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(value[5:]))) as zf:
                members = [[info.filename, zf.read(info)] for info in zf.infolist()]
        except zipfile.BadZipFile:
            continue
        rendered = []
        for name, blob in members:
            if name == "manifest.json":
                match = _CREATED_AT.search(blob)
                if match and _window_utc(match.group(1).decode(), obs["window"]):
                    blob = blob[:match.start(1)] + b"<created_at>" + blob[match.end(1):]
                blob = _VERSION.sub(b'"pseudolife_version": "<version>"', blob, count=1)
            try:
                rendered.append([name, blob.decode("utf-8")])
            except UnicodeDecodeError:
                # Unambiguous: undecodable bytes never equal any text member.
                rendered.append([name, {"base64": base64.b64encode(blob).decode()}])
        canonical = json.dumps(rendered, ensure_ascii=False, indent=0).encode()
        obs["files"][rel] = "file:" + base64.b64encode(canonical).decode()


_DEFAULT_NAME = re.compile(rb"pseudolife-export-([0-9]{8}-[0-9]{6})\.zip")


def _local_stamp_in_window(stamp: str, window) -> bool:
    return any(time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) == stamp
               for t in range(int(window[0]) - 1, int(window[1]) + 2))


@normalize.rule("transfer-default-name")
def transfer_default_name(obs: dict) -> None:
    """The default ``--out`` name's local timestamp, validated against the
    arm's window, in stdout and in the file paths."""
    def swap(data: bytes) -> bytes:
        def one(match: re.Match) -> bytes:
            if _local_stamp_in_window(match.group(1).decode(), obs["window"]):
                return b"pseudolife-export-<ts>.zip"
            return match.group(0)
        return _DEFAULT_NAME.sub(one, data)

    obs["stdout"] = base64.b64encode(swap(base64.b64decode(obs["stdout"]))).decode()
    files: dict = {}
    for rel, value in obs["files"].items():
        new_rel = swap(rel.encode()).decode()
        while new_rel in files:
            new_rel += " <normalized-collision>"
        files[new_rel] = value
    obs["files"] = files


def _expect(line: str, keep_db: bool, oracle_shape):
    """Each arm's database is compared with its own pre-run dump: a seed
    written through the oracle's writers stamps wall-clock times, so the two
    arms' banks differ before either runs. A deferring candidate must leave
    its own bank exactly as it was."""
    def apply(obs: dict) -> None:
        if obs.get("arm") != "python":
            if not keep_db and obs.get("db") == obs.get("db_before"):
                obs["db"] = "<unchanged>"
            return
        stderr = base64.b64decode(obs["stderr"]).decode("utf-8", "replace")
        if not oracle_shape(obs, stderr):
            return
        obs["exit"] = 1
        obs["stdout"] = ""
        obs["stderr"] = base64.b64encode((line + _EOL).encode()).decode()
        obs["files"] = obs["setup_files"]
        if not keep_db:
            obs["db"] = "<unchanged>"
    return apply


def _traceback(obs, stderr):
    return obs["exit"] == 1 and "Traceback (most recent call last)" in stderr


def _ran(obs, stderr):
    return True


_GENERIC = "pseudolife-stdio: mode '{}' is deferred in this candidate"
normalize.rule("expect-export-deferred")(
    _expect(_GENERIC.format("export"), False, _traceback))
normalize.rule("expect-export-deferred-any")(
    _expect(_GENERIC.format("export"), False, _ran))
normalize.rule("expect-import-deferred")(
    _expect(_GENERIC.format("import"), False, _ran))
normalize.rule("expect-import-schema-deferred")(_expect(
    "pseudolife-stdio: import into a bank not at this build's schema (55) is "
    "deferred in this candidate (needs native ensure_schema)", False, _ran))
_NO_DATABASE = "no database configured — set PSEUDOLIFE_MCP_DATABASE_URL"
# Host-independent: an oracle with pg0 refuses the unreadable marker with a
# traceback, one without pg0 never attaches and prints the no-database
# refusal. The candidate defers by name either way.
normalize.rule("expect-import-lite-deferred")(_expect(
    "pseudolife-stdio: mode 'import' on the embedded lite tier is deferred in this "
    "candidate (needs native embedded_pg)", False,
    lambda obs, stderr: (_traceback(obs, stderr) and "PG_VERSION" in stderr)
    or (obs["exit"] == 1 and stderr.startswith(_NO_DATABASE))))
normalize.rule("expect-import-native-failure")(_expect(
    "pseudolife-stdio: import failed (native-pg-diagnostics); nothing was committed",
    True, lambda obs, stderr: _traceback(obs, stderr)
    and "psycopg.errors.ForeignKeyViolation" in stderr))


# ------------------------------------------------------------------ cases


def _dsn(name: str) -> dict:
    return {"PSEUDOLIFE_MCP_DATABASE_URL": _bank.url(name)}


def _export_setup(arm: core.Arm) -> None:
    _sources()
    _wait_idle(SRC)
    _wait_idle(TRICKY)
    arm.state["files"] = core.snapshot(arm.home)


def _export_after(bank: str):
    def after(arm: core.Arm, obs: dict) -> None:
        obs["arm"] = arm.name
        obs["setup_files"] = arm.state["files"]
        obs["db"] = _bank.dump(bank)
        obs["db_before"] = arm.state["db_before"]
    return after


def _import_setup(archive: str, seed=None, holder=False, schema=True):
    def setup(arm: core.Arm) -> None:
        _sources()
        _create(TGT, schema=schema)
        if seed:
            with _bank.connect(TGT) as conn:
                seed(conn)
                conn.commit()
        arm.state["db_before"] = _bank.dump(TGT)
        _wait_idle(TGT)
        (arm.cwd / "bank.zip").write_bytes(_ARCHIVES[archive])
        if holder == "locked":
            # An open transaction holding ACCESS SHARE on entries: the
            # oracle's ensure_schema ALTER waits 5 s and fails.
            conn = _bank.connect(TGT)
            conn.execute("SELECT 1 FROM entries LIMIT 1")
            arm.state["holder"] = conn
        elif holder:
            arm.state["holder"] = _bank.connect(TGT, autocommit=True)
        arm.state["files"] = core.snapshot(arm.home)
    return setup


def _import_after(arm: core.Arm, obs: dict) -> None:
    holder = arm.state.pop("holder", None)
    if holder is not None:
        holder.rollback()
        holder.close()
    obs["arm"] = arm.name
    obs["setup_files"] = arm.state["files"]
    obs["db_before"] = arm.state["db_before"]
    obs["db"] = _bank.dump(TGT)


def _seed_target(conn) -> None:
    from tests.test_transfer_cli import _seed_bank  # noqa: PLC0415
    _seed_bank(conn)


def _seed_null_stamps(conn) -> None:
    """A non-empty schema-55 bank holding an edge as the daemon writes it
    (``PostgresStorage.upsert_edge``: tx_time, valid_time, writer_id NULL),
    which the oracle's ensure_schema backfills before refusing the import."""
    from pseudolife_memory.storage.postgres import PostgresStorage  # noqa: PLC0415
    from tests.test_transfer_cli import _seed_bank  # noqa: PLC0415
    _seed_bank(conn)
    conn.commit()
    storage = PostgresStorage(_bank.url(TGT))
    try:
        storage.upsert_edge(2, "uses", 1, origin="agent")
    finally:
        storage.close()
    row = conn.execute("SELECT count(*) FROM edges WHERE tx_time IS NULL").fetchone()
    assert row[0] == 1, "the daemon writer no longer leaves NULL stamps"


def _seed_relation(conn) -> None:
    conn.execute(
        "INSERT INTO relations (name, description, transitive, builtin, created_at) "
        "VALUES ('uses', 'target copy', FALSE, TRUE, 1.0)")


def _seed_flag(conn) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES "
                 "('curation_listing_spelling_v2', '{\"copied\": 0, \"at\": 900.0}')")


def _seed_bank_id(conn) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES ('coordination_bank_id', "
                 "'\"22222222-2222-4222-8222-222222222222\"')")


def _schema_54(conn) -> None:
    conn.execute("UPDATE meta SET value = '54' WHERE key = 'schema_version'")


def _lite_marker(arm: core.Arm) -> None:
    pgdata = arm.home / "lite" / "embedded_pg"
    pgdata.mkdir(parents=True)
    # Unreadable on purpose: an oracle with pg0 refuses before starting anything.
    (pgdata / "PG_VERSION").write_text("not-a-version\n")
    arm.state["files"] = core.snapshot(arm.home)
    arm.state["db_before"] = None


def _lite_after(arm: core.Arm, obs: dict) -> None:
    obs["arm"] = arm.name
    obs["setup_files"] = arm.state["files"]
    obs["db_before"] = None


def _export(cid, argv, bank=SRC, rules=("transfer-zip",), note="", existing=(),
            dumped=True):
    def setup(arm: core.Arm) -> None:
        for name in existing:
            (arm.cwd / name).write_bytes(b"not yet an archive")
        _export_setup(arm)
        if dumped:
            arm.state["db_before"] = _bank.dump(bank)
            _wait_idle(bank)

    after = _export_after(bank) if dumped else _lite_after
    return core.Case(cid, ["export", *argv], env=_dsn(bank), setup=setup, after=after,
                     rules=rules, timeout=120, note=note)


def _import(cid, archive, argv=(), rules=(), note="", **setup):
    return core.Case(cid, ["import", "bank.zip", *argv], env=_dsn(TGT),
                     setup=_import_setup(archive, **setup), after=_import_after,
                     rules=rules, timeout=120, note=note)


def cases() -> list[core.Case]:
    zip_rules = ("transfer-zip",)
    return [
        core.Case("export-help", ["export", "--help"], note="argparse help at COLUMNS=80"),
        core.Case("import-help", ["import", "--help"], note="argparse help at COLUMNS=80"),
        core.Case("export-no-database", ["export"],
                  note="no DSN, no lite bank: the no-database refusal"),
        core.Case("import-no-database", ["import", "x.zip", "--data-dir", "{HOME}" + _SEP + "none"],
                  note="no DSN, explicit data dir without a bank"),
        core.Case("import-lite-defers", ["import", "x.zip", "--data-dir", "{HOME}" + _SEP + "lite"],
                  setup=_lite_marker, after=_lite_after,
                  rules=("expect-import-lite-deferred",),
                  note="an embedded bank marker: native deferral, nothing touched"),
        _export("export-seeded", ["--out", _SEP.join(["{CWD}", "nested", "dir", "bank.zip"])],
                note="every exported table, created parent dirs, absolute --out"),
        _export("export-relative-out", ["--data-dir", "ignored", "--out", "rel.zip"],
                note="relative --out printed as given; --data-dir ignored with a DSN"),
        _export("export-default-out", [], rules=("transfer-default-name", "transfer-zip"),
                note="./pseudolife-export-<local ts>.zip"),
        _export("export-tricky", ["--out", "t.zip"], bank=TRICKY,
                note="float4/float8 repr incl. NaN/inf/-0/1e16, jsonb re-serialisation, "
                     "timestamptz isoformat, control and astral text, meta skip keys, "
                     "used_ids dropped"),
        _export("export-overwrites", ["--out", "bank.zip"], existing=("bank.zip",),
                note="an existing archive is replaced"),
        _export("export-connection-defers", ["--out", _SEP.join(["{CWD}", "d", "x.zip"])], bank=TGT + "_absent",
                rules=("expect-export-deferred",), dumped=False,
                note="unreachable database: native generic deferral, no directory made"),
        _import("import-seeded", "current"),
        _import("import-tricky", "tricky", note="tricky values round-trip through import"),
        _import("import-nonempty-refused", "current", seed=_seed_target),
        _import("import-other-connection-refused", "current", holder=True),
        _import("import-force-with-holder", "current", argv=("--force",), holder=True),
        _import("import-force-before-path", "current", argv=("--data-dir", "x", "--force"),
                note="option order and an ignored --data-dir"),
        _import("import-unknown-column", "unknown-column"),
        _import("import-unknown-meta-column", "unknown-meta-column"),
        _import("import-reversed-relations", "reversed-relations"),
        _import("import-dropped-column", "dropped-column"),
        _import("import-mixed-columns", "mixed-columns",
                note="two column sets in one batch: grouped like executemany"),
        _import("import-nonfinite-floats", "nonfinite"),
        _import("import-relation-conflict", "current", seed=_seed_relation,
                note="ON CONFLICT DO NOTHING counts and the deferred inverse UPDATE"),
        _import("import-flag-cleared", "current", seed=_seed_flag),
        _import("import-flag-from-export", "flag", seed=_seed_flag),
        _import("import-injected-ext-marker", "inject-ext"),
        _import("import-injected-bank-id", "inject-bank-id", seed=_seed_bank_id),
        _import("import-no-v39-backfill", "no-v39-schema-38",
                note="member absent: the backfill runs, its count prints last"),
        _import("import-legacy-entries", "no-v39-schema-37",
                note="schema < 38: dream_state NULL"),
        _import("import-legacy-with-v39", "legacy-37"),
        _import("import-empty-v39", "empty-v39"),
        _import("import-dim-mismatch", "dim-512"),
        _import("import-dim-string", "dim-string"),
        _import("import-format-9", "format-9"),
        _import("import-format-string", "format-string"),
        _import("import-format-missing", "format-missing"),
        _import("import-format-float", "format-float"),
        _import("import-no-manifest", "no-manifest"),
        _import("import-schema-54-defers", "current", seed=_schema_54,
                rules=("expect-import-schema-deferred",)),
        _import("import-blank-database-defers", "current", schema=False,
                rules=("expect-import-schema-deferred",)),
        _import("import-fk-violation-fails", "bad-evidence",
                rules=("expect-import-native-failure",),
                note="a SQL error inside the transaction: rolled back by both arms"),
        # Review round 1 (2026-10-09): deferrals that must change nothing.
        _export("export-part-exists-defers", ["--out", "bank.zip"],
                existing=("bank.zip.part",), rules=("expect-export-deferred-any",),
                note="a .part this run did not create is never touched"),
        _export("export-infinity-defers", ["--out", _SEP.join(["{CWD}", "n", "x.zip"])],
                bank=INF, rules=("expect-export-deferred",),
                note="an 'infinity' timestamptz: deferred before any directory is made"),
        _export("export-deep-jsonb-defers", ["--out", _SEP.join(["{CWD}", "n", "x.zip"])],
                bank=DEEP, rules=("expect-export-deferred-any",),
                note="jsonb nested past the ported depth: deferred before any effect"),
        _export("export-non-utc-defers", ["--out", _SEP.join(["{CWD}", "n", "x.zip"])],
                bank=NYC, rules=("expect-export-deferred-any",),
                note="a non-UTC session with a non-null timestamptz"),
        _export("export-non-utc-empty", ["--out", "x.zip"], bank=NYC_EMPTY,
                note="a non-UTC session without timestamptz values is answered"),
        _import("import-null-stamps-defers", "current", seed=_seed_null_stamps,
                rules=("expect-import-deferred",),
                note="ensure_schema would backfill daemon-written stamps before refusing"),
        _import("import-force-locked-holder-defers", "current", argv=("--force",),
                holder="locked", rules=("expect-import-deferred",),
                note="a holder lock ensure_schema's DDL cannot take within 5 s"),
        _import("import-string-in-float-defers", "string-in-float",
                rules=("expect-import-deferred",)),
        _import("import-float4-overflow-defers", "float4-overflow",
                rules=("expect-import-deferred",)),
        _import("import-bad-vector-defers", "bad-vector", rules=("expect-import-deferred",)),
        _import("import-huge-int-float", "huge-int-float",
                note="an int past int8 into float8 is typed numeric, as psycopg types it"),
    ]


MUTANTS = [
    Mutant("transfer-float-window", ROW, "shim/src/cli/transfer/json.rs",
           "if !(-4..16).contains(&power) {", "if !(-4..15).contains(&power) {",
           ("export-tricky",)),
    Mutant("transfer-meta-skip", ROW, "shim/src/cli/transfer.rs",
           '"active_session_pointer",', '"active_session_pointer_x",', ("export-seeded",)),
    Mutant("transfer-nonempty-wording", ROW, "shim/src/cli/transfer/import.rs",
           "import only fills a fresh bank", "import only fills an empty bank",
           ("import-nonempty-refused",)),
    Mutant("transfer-entry-sequence", ROW, "shim/src/cli/transfer/import.rs",
           'if highwater == "0" {', 'if highwater != "-1" {', ("import-seeded",)),
    Mutant("transfer-refusal-exit", ROW, "shim/src/cli/transfer.rs",
           "            1,\n        ),\n        Outcome::SchemaDeferred",
           "            2,\n        ),\n        Outcome::SchemaDeferred", ("import-format-9",)),
    Mutant("transfer-drop-inverses", ROW, "shim/src/cli/transfer/import.rs",
           "    send(client, &inverses).await?;", "    let _ = &inverses;",
           ("import-reversed-relations",)),
    Mutant("transfer-order-flip", ROW, "shim/src/cli/transfer/export.rs",
           '"DECLARE {cursor} CURSOR FOR SELECT * FROM {} ORDER BY 1"',
           '"DECLARE {cursor} CURSOR FOR SELECT * FROM {} ORDER BY 1 DESC"',
           ("export-seeded",)),
    # Review round 1 (2026-10-09): each guard below is load-bearing.
    Mutant("transfer-null-stamps", ROW, "shim/src/cli/transfer/import.rs",
           'if unstamped.as_deref() != Some("f") {', 'if unstamped.as_deref() == Some("x") {',
           ("import-null-stamps-defers",)),
    Mutant("transfer-probe-share", ROW, "shim/src/cli/transfer/import.rs",
           'IN ACCESS EXCLUSIVE MODE"', 'IN ACCESS SHARE MODE"',
           ("import-force-locked-holder-defers",)),
    Mutant("transfer-int-type", ROW, "shim/src/cli/transfer/import.rs",
           '_ => "numeric",', '_ => "int8",', ("import-huge-int-float",)),
    Mutant("transfer-float4-loads", ROW, "shim/src/cli/transfer/import.rs",
           "fn float4_loads(value: f64) -> bool {",
           "fn float4_loads(value: f64) -> bool {\n    if value.is_finite() {\n"
           "        return true;\n    }", ("import-float4-overflow-defers",)),
    Mutant("transfer-numeric-strings", ROW, "shim/src/cli/transfer/import.rs",
           'matches!(udt, "text" | "uuid" | "vector" | "timestamptz")',
           'matches!(udt, "text" | "uuid" | "vector" | "timestamptz" | "float8")',
           ("import-string-in-float-defers",)),
    Mutant("transfer-cast-check", ROW, "shim/src/cli/transfer/import.rs",
           "if let Pass::Dry(client) = pass", "if let Pass::Real(client) = pass",
           ("import-bad-vector-defers",)),
    Mutant("transfer-part-guard", ROW, "shim/src/cli/transfer/export.rs",
           'match fs::symlink_metadata(format!("{display}.part")) {',
           'match fs::symlink_metadata(format!("{display}.part.absent")) {',
           ("export-part-exists-defers",)),
    Mutant("transfer-timestamp-range", ROW, "shim/src/cli/transfer/export.rs",
           "IS NOT NULL AND NOT \\", "IS NULL AND NOT \\", ("export-infinity-defers",)),
    Mutant("transfer-jsonb-precheck", ROW, "shim/src/cli/transfer/export.rs",
           ".any(|text| json::loads(text).is_none())", ".any(|text| text.is_empty())",
           ("export-deep-jsonb-defers",)),
    Mutant("transfer-utc-precheck", ROW, "shim/src/cli/transfer/export.rs",
           "Kind::Timestamptz if rendering.utc && rendering.iso => format!(",
           "Kind::Timestamptz if rendering.iso => format!(", ("export-non-utc-defers",)),
]

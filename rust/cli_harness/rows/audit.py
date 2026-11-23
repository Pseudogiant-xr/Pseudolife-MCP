"""CLI-AUDIT: ``pseudolife-mcp board-audit`` verify, export, redact and stats.

Read actions share one captured bank checked after each arm. Redactions
receive separate template copies. Archive cases project JSON lines from those banks,
with deliberate edits for malformed or invalid histories.
"""

from __future__ import annotations

import atexit
import base64
import json
import os
import re
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from functools import cache, partial
from pathlib import Path

from .. import core, normalize
from . import _bank
from rust.harness_support.audit_oracle import GENESIS_HASH, audit_hash, canonical_payload, verify_audit_chain
from rust.harness_support.database_guard import (
    assert_disposable_database, fixture_database_pattern, refuse_production_database,
)

DAY = 86400
T0 = 1_700_000_000.0
_NO_AUDIT_LOG = "this bank has no audit log yet (schema older than v42); start a v42 daemon once to create it"
_FIXTURE = Path(__file__).with_suffix(".fixtures.json")
_STATS_REFERENCES = Path(__file__).with_name("audit.stats.references.json")
_STATS_CORPUS = Path(__file__).resolve().parents[2] / "shim/src/cli/board_audit/testdata/stats.json"
_ARCHIVE_HASH_TOKEN = "<archive-head-hash>"
_ARCHIVE_CLOCK_TOKEN = "<archive-head-created-at>"


def _shift_rich_clock(rows, anchor):
    """Move the last three sends and their agent activity to the run's clock."""
    events = rows["coordination_events"]
    # The frozen seeder advanced its clock by 7.25 s after the last fixed
    # send, then took the later of that tick and the wall clock.
    last_fixed = max(row["created_at"] for row in events if row["created_at"] < anchor)
    shift = max(last_fixed + 7.25, time.time()) - anchor
    for table, columns in (
        ("coordination_agents", ("created_at", "last_activity")),
        ("coordination_messages", ("created_at", "expires_at")),
    ):
        for row in rows[table]:
            for column in columns:
                if row[column] >= anchor:
                    row[column] += shift
    previous = GENESIS_HASH
    for row in events:
        if row["created_at"] < anchor:
            previous = row["hash"]
            continue
        row["created_at"] += shift
        if row["event"] == "send":
            payload = json.loads(row["payload"])
            payload["expires_at"] += shift
            row["payload"] = canonical_payload(payload)
        row["prev_hash"] = previous
        row["hash"] = previous = audit_hash(previous, row)


def _seed_captured(conn, name, kind):
    from rust.harness_support.fixture_rows import checked_fixture_insert  # noqa: PLC0415

    # Captured at 88787b0e2 on the committed schema, with deterministic
    # synthetic UUIDs, credentials and salts. Only rows and sequences changed
    # by the old seeders are stored. The schema plan supplies the rest.
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    variant = fixture["variants"][kind]
    rows = variant["rows"]
    events = rows["coordination_events"]
    events.sort(key=lambda row: row["seq"])
    # Check stored payload text, links, prune anchor and body commitments
    # before changing any timestamps or rebuilding the linked hashes.
    report = verify_audit_chain(events)
    if not report["ok"]:
        raise ValueError(f"{kind} audit fixture has an invalid chain: {report}")
    if kind == "rich":
        _shift_rich_clock(rows, fixture["clock_anchor"])

    with conn.transaction():
        _assert_audit_database(conn, name)
        for table, records in rows.items():
            statement, params = checked_fixture_insert(conn, table, records)
            conn.execute(statement, params)
        for sequence, (value, called) in variant["sequences"].items():
            conn.execute("SELECT setval(%s::regclass, %s, %s)", ("public." + sequence, value, called))
    return variant["ids"]


# --- disposable banks --------------------------------------------------------

PREFIX = "pl_cf_w1c_audit_"
_IDS: dict[str, dict] = {}
_BASELINE: dict[str, dict] = {}
_ARCHIVES: dict[str, bytes] = {}
_CREATED: set[str] = set()


def _name(kind: str) -> str:
    return _bank.name(PREFIX + kind)


def _drop_all() -> None:
    for name in sorted(_CREATED):
        try:
            _drop(name)
        except Exception:  # noqa: BLE001 (best-effort cleanup at exit)
            pass


atexit.register(_drop_all)


def _drop(name: str) -> None:
    """``_bank.drop``, waiting out an autovacuum worker on the database: the
    test login may not terminate one (the redactions' VACUUM/ANALYZE and
    row churn can start one), and it finishes on its own."""
    import psycopg  # noqa: PLC0415
    for attempt in range(60):
        try:
            _bank.drop(name)
            return
        except (psycopg.errors.InsufficientPrivilege, psycopg.errors.ObjectInUse):
            if attempt == 59:
                raise
            time.sleep(0.5)


def _require_audit_name(name: str) -> None:
    _bank.NAMES.require_owned(name)
    if not name.startswith(PREFIX):
        raise ValueError(f"expected a disposable audit database, got {name!r}")
    refuse_production_database(name)


def _assert_audit_database(conn, name: str) -> None:
    _require_audit_name(name)
    if assert_disposable_database(conn) != name:
        raise ValueError(f"audit fixture connection did not reach {name!r}")


def _copy(source: str, target: str) -> None:
    _require_audit_name(target)
    with _bank.connect(source, autocommit=True) as conn:
        _assert_audit_database(conn, source)
    _drop(target)
    _CREATED.add(target)
    with _bank._admin() as conn:
        conn.execute(f'CREATE DATABASE "{target}" TEMPLATE "{source}"')


def ensure(kind: str) -> str:
    """The seeded bank of this kind (created once per harness process)."""
    name = _name(kind)
    _require_audit_name(name)
    if name in _BASELINE:
        return name
    _CREATED.add(name)
    if kind == "nolog":
        _bank.create(name, schema=False)
    elif kind == "empty":
        _bank.create(name)
    elif kind == "tampered":
        _copy(ensure("rich"), name)
        with _bank.connect(name, autocommit=True) as conn:
            _assert_audit_database(conn, name)
            conn.execute("UPDATE coordination_events SET task='elsewhere' WHERE seq=2")
    elif kind == "prev49":
        _copy(ensure("rich"), name)
        with _bank.connect(name, autocommit=True) as conn:
            _assert_audit_database(conn, name)
            conn.execute("DROP TABLE coordination_wakes")
    elif kind == "prev46":
        # A restored v42-v45 bank read before any v46 daemon started, made
        # as test_board_audit_cli.test_a_bank_before_v46_... makes it.
        _copy(ensure("legacy"), name)
        with _bank.connect(name, autocommit=True) as conn:
            _assert_audit_database(conn, name)
            conn.execute("ALTER TABLE coordination_events DROP COLUMN body")
    else:
        _bank.create(name)
        with _bank.connect(name, autocommit=True) as conn:
            _IDS[kind] = seed_stats(conn) if kind == "stats" else _seed_captured(conn, name, kind)
    _BASELINE[name] = _bank.dump(name)
    return name


def ids(kind: str) -> dict:
    ensure(kind)
    return _IDS[kind]


_EXPORT_FIELDS = (
    "seq", "event", "actor", "principal", "agent_id", "recipient_agent_id", "project",
    "task", "message_id", "payload", "created_at", "hlc", "prev_hash", "hash",
)
_BODY_FIELDS = ("body", "body_salt")


def _export_line(values) -> bytes:
    row = dict(zip(_EXPORT_FIELDS + _BODY_FIELDS, values, strict=True))
    row["payload"] = json.loads(row["payload"])
    return (json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")


def _stored_audit_rows(conn):
    conn.execute("SET LOCAL extra_float_digits = 3")
    has_body, = conn.execute(
        "SELECT EXISTS (SELECT FROM pg_attribute "
        "WHERE attrelid = 'coordination_events'::regclass "
        "AND attname = 'body' AND NOT attisdropped)"
    ).fetchone()
    body = "body,body_salt" if has_body else "NULL::text,NULL::text"
    columns = ",".join(_EXPORT_FIELDS)
    return conn.execute(f"SELECT {columns},{body} FROM coordination_events ORDER BY seq")


def export_bytes(kind: str) -> bytes:
    """Project a seeded bank into the archive format independently of the CLI."""
    _bank.NAMES.require_process()
    if kind not in _ARCHIVES:
        with _bank.connect(ensure(kind)) as conn:
            _ARCHIVES[kind] = b"".join(_export_line(row) for row in _stored_audit_rows(conn))
    return _ARCHIVES[kind]


def export_rows(kind: str) -> list[dict]:
    return [json.loads(line) for line in export_bytes(kind).decode("utf-8").splitlines()]


def head(kind: str, seq: int | None = None, digest: str | None = None) -> str:
    last = export_rows(kind)[-1]
    return f"{last['seq'] if seq is None else seq}:{last['hash'] if digest is None else digest}"


def _lines(rows) -> bytes:
    # As the oracle tests rewrite an export: json.dumps per row.
    return "".join(json.dumps(row) + "\n" for row in rows).encode("utf-8")


def _send_index(rows, message_id) -> int:
    return next(i for i, r in enumerate(rows) if r["event"] == "send"
                and r["message_id"] == message_id)


def archive(variant: str) -> bytes:
    """An export of a seeded bank, unchanged or edited one way."""
    if variant == "surrogate-before-link":
        row = export_rows("rich")[0]
        row["prev_hash"] = "1" * 64
        row["payload"] = {"odd": "\ud800"}
        return _lines([row])
    if variant == "head-19-digits":
        # Lift an oracle-produced retention-anchored chain to a BIGINT head.
        rows = export_rows("pruned")
        shift = 1000000000000000000 - rows[-1]["seq"]
        previous = rows[0]["prev_hash"]
        for row in rows:
            row["seq"] += shift
            row["prev_hash"] = previous
            if row["event"] == "audit_prune":
                row["payload"]["through_seq"] += shift
            row["hash"] = previous = audit_hash(previous, row)
        assert verify_audit_chain(rows, expect_head=(rows[-1]["seq"], previous))["ok"]
        return _lines(rows)
    if variant.startswith("reserved-number-key-"):
        # Keep one complete exported row, hashed with an ordinary
        # payload key that happens to match serde's internal number transport.
        row = export_rows("rich")[0]
        row["payload"]["$serde_json::private::Number"] = "1"
        row["hash"] = audit_hash(row["prev_hash"], row)
        raw = _lines([row])
        if variant == "reserved-number-key-single":
            return raw
        key = (b'"\\u0024serde_json::private::\\u004eumber"'
               if variant == "reserved-number-key-escaped" else
               b'"$serde_json::private::Number"')
        return raw.replace(b'"$serde_json::private::Number": "1"',
                           b'"$serde_json::private::Number": "1", ' + key + b': "1"', 1)
    if variant == "pruned":
        return export_bytes("pruned")
    if variant == "legacy-v45":
        return _lines({k: v for k, v in row.items() if k != "body"}
                      for row in export_rows("legacy"))
    raw = export_bytes("rich")
    if variant == "rich":
        return raw
    if variant in ("broken-before-decode", "decode-before-broken"):
        rows = export_rows("rich")[:1]
        rows[0]["task"] = "rewritten"
        gap = b" " * 9000 if variant == "broken-before-decode" else b""
        return _lines(rows) + gap + b"\xff\n"
    first = raw.split(b"\n", 1)[0] + b"\n"
    digits = b"9" * 4301
    if variant == "duplicate-truncated":
        # The repeated key's object never closes: Python says "not JSON".
        return first + b'{"seq":1,"seq":2\n'
    if variant == "duplicate-then-long-int":
        # The long integer is refused while scanning, before the close.
        return first + b'{"a":1,"a":2,"n":' + digits + b"}\n"
    if variant == "duplicate-nested-then-long-int":
        # The inner object closes, and its repeated key is refused, first.
        return first + b'{"a":{"x":1,"x":2},"n":' + digits + b"}\n"
    if variant == "rich-crlf":
        return raw.replace(b"\n", b"\r\n")
    if variant == "rich-cr-blank":
        return b"\r \n\x1c\n" + raw.replace(b"\n", b"\r\r")
    rows = export_rows("rich")
    target = _send_index(rows, ids("rich")["to_redact"])
    if variant == "stripped":
        return _lines({k: v for k, v in row.items() if k not in ("body", "body_salt")}
                      for row in rows)
    if variant == "blanked":
        rows[target]["body"] = rows[target]["body_salt"] = None
    elif variant == "edited-body":
        rows[target]["body"] = "another body"
    elif variant == "resalted":
        rows[target]["body_salt"] = "0" * 32
    elif variant == "salt-only":
        rows[target]["body"] = None
    elif variant == "wrong-type":
        rows[target]["body"] = 7
    elif variant == "wrong-type-cr":
        rows[target]["body_salt"] = 7
        return b"\r\r \r\n" + _lines(rows).replace(b"\n", b"\r")
    elif variant == "seq-string":
        rows[3]["seq"] = str(rows[3]["seq"])
    elif variant == "duplicate":
        text = _lines(rows).decode("utf-8").splitlines()
        body = json.dumps(rows[target]["body"])
        text[target] = text[target].replace(f'"body": {body}',
                                            f'"body": "EVIL", "body": {body}', 1)
        return ("\n".join(text) + "\n").encode("utf-8")
    elif variant == "payload-edit":
        rows[3]["payload"]["fields"]["status"] = "rewritten"
    elif variant == "late-garbage":
        rows[3]["payload"]["fields"]["status"] = "rewritten"
        return _lines(rows) + b"not JSON\n"
    elif variant == "gap":
        del rows[5]
    elif variant == "link":
        rows[6]["prev_hash"] = "f" * 64
    elif variant == "genesis":
        rows[0]["prev_hash"] = "1" * 64
    elif variant == "unanchored":
        rows = rows[4:]
    elif variant == "redact-kept-body":
        # The redacted send's body written back after its redaction.
        redacted = _send_index(rows, ids("rich")["redacted"])
        rows[redacted]["body"] = "pasted by mistake"
    else:
        raise KeyError(variant)
    return _lines(rows)


# --- named rules ---------------------------------------------------------------

@normalize.rule("audit-redact-clock")
def audit_redact_clock(obs: dict) -> None:
    """A redaction's own event is stamped with the arm's wall clock, which
    is hashed. Validated, then tokenized: the newest event must be a redact
    whose created_at lies inside this arm's run window and whose hash is the
    harness's ``audit_hash`` of that row; its created_at and hash become
    tokens. Every printed hash reference must be this arm's own validated
    hash before it is replaced: stdout's ``redact_hash`` and ``expect_head``
    (when stdout carries the result), the stderr ``--expect-head SEQ:HASH``
    note, and no other 64-hex string or literal token anywhere. The live
    message row's ``expires_at``, when a redaction's ``LEAST(expires_at,
    now)`` moved it to exactly that clock, is tokenized the same way."""
    db = obs.get("db")
    if not isinstance(db, dict):
        return
    rows = db["tables"].get("coordination_events", [])
    parsed = [json.loads(text) for text in rows]
    if not parsed:
        return
    newest = max(range(len(parsed)), key=lambda i: parsed[i]["seq"])
    row = parsed[newest]
    start, end = obs["window"]
    if (row["event"] != "redact" or not start - 1 <= row["created_at"] <= end + 1
            or audit_hash(row["prev_hash"], row) != row["hash"]):
        return
    digest, head = row["hash"], f"{row['seq']}:{row['hash']}"
    stdout, stderr = base64.b64decode(obs["stdout"]), base64.b64decode(obs["stderr"])
    if b"<redact-hash>" in stdout + stderr:
        return
    if stdout.strip():
        try:
            result = json.loads(stdout)
        except ValueError:
            return
        if result.get("redact_hash") != digest or result.get("expect_head") != head:
            return
    if f"--expect-head {head}`".encode() not in stderr:
        return
    if any(found.decode() != digest for found in re.findall(rb"[0-9a-f]{64}", stdout + stderr)):
        return
    clock = row["created_at"]
    row["created_at"] = "<redact-clock>"
    row["hash"] = "<redact-hash>"
    rows[newest] = json.dumps(row)
    messages = db["tables"].get("coordination_messages", [])
    for index, text in enumerate(messages):
        message = json.loads(text)
        if message["message_id"] == row["message_id"] and message["expires_at"] == clock:
            message["expires_at"] = "<redact-clock>"
            messages[index] = json.dumps(message)
    for field, data in (("stdout", stdout), ("stderr", stderr)):
        obs[field] = base64.b64encode(data.replace(digest.encode(), b"<redact-hash>")).decode()


_TRAILER = re.compile(rb"Exception ignored in: <_io\.TextIOWrapper name='<stdout>'"
                      rb"[^\r\n]*\r?\n(?:BrokenPipeError|OSError): [^\r\n]*\r?\n\Z")


@normalize.rule("audit-stdout-closed-trailer")
def audit_stdout_closed_trailer(obs: dict) -> None:
    """Declared substitution ``audit-stdout-closed-trailer``: after a
    committed redaction whose result line stdout refused, CPython's
    interpreter-shutdown flush fails, prints an ignored-exception trailer
    and exits 120. The native CLI keeps exit 120 and prints no synthetic
    trailer. Only that exact trailer, with exit 120, is removed. No live
    comparison starts the frozen CLI any more, but `--record` still records
    it, and replaying such a recording needs this rule; it retires with that
    path."""
    stderr = base64.b64decode(obs["stderr"])
    if obs["exit"] == 120 and _TRAILER.search(stderr):
        obs["stderr"] = base64.b64encode(_TRAILER.sub(b"", stderr)).decode()


@normalize.rule("audit-stats-clock")
def stats_clock(obs):
    """Validate runtime fields before substituting only clock/version tokens.

    Every metric, source, key order, numeric spelling and file byte remains
    compared. Fixed time windows are never replaced.
    """
    if obs.get("arm") == "reference":
        # The expected observation already contains these exact clock tokens.
        return
    import tomllib
    native_version = tomllib.loads((Path(__file__).resolve().parents[2] / "shim" / "Cargo.toml").read_text())["package"]["version"]
    defaults = obs.get("stats_defaults", (False, False))
    if defaults is False:
        defaults = (False, False)
    def line(raw):
        try:
            report = json.loads(raw)
        except (ValueError, UnicodeError):
            return raw
        if not isinstance(report, dict) or report.get("report") != "board-audit stats":
            return raw
        assert report["shape"] == 1
        if obs["arm"] == "rust":
            assert report["version"] == native_version
        else:
            # The isolated oracle can resolve a different installed wheel than
            # the harness process (the same free field as transfer manifests).
            assert re.fullmatch(r"[0-9]+(?:\.[0-9]+)+[a-zA-Z0-9.+!-]*", report["version"])
        start, end = obs["window"]
        assert start - 0.001 <= report["generated_at"] <= end + 0.001
        window = report["window"]
        if defaults[1]:
            assert start <= window["until"] <= end
        if defaults[0]:
            assert window["since"] == window["until"] - 86400
        raw = re.sub(rb'("generated_at":\s*)[-+0-9.eE]+', rb'\1"<generated_at>"', raw, count=1)
        raw = re.sub(rb'("version":\s*)"[^"\r\n]*"', rb'\1"<version>"', raw, count=1)
        if defaults[1]:
            raw = re.sub(rb'("until":\s*)[-+0-9.eE]+', rb'\1"<until>"', raw, count=1)
        if defaults[0] and defaults[1]:
            raw = re.sub(rb'("since":\s*)[-+0-9.eE]+', rb'\1"<since>"', raw, count=1)
        return raw
    def content(raw):
        return b"".join(line(part) for part in raw.splitlines(keepends=True))
    obs["stdout"] = base64.b64encode(content(base64.b64decode(obs["stdout"]))).decode()
    for name, value in list(obs.get("files", {}).items()):
        if value.startswith("file:"):
            obs["files"][name] = "file:" + base64.b64encode(content(base64.b64decode(value[5:]))).decode()


# --- cases -----------------------------------------------------------------------

SEP = "\\" if core.WINDOWS else "/"
GUARDS = {
    # Never the daemon container or a lite bank on the host running this.
    "PSEUDOLIFE_DAEMON_EXEC": "1",
    "PSEUDOLIFE_DOCKER": "{HOME}/no-docker",
    "PSEUDOLIFE_MCP_DATA_DIR": "{HOME}/no-bank",
    "PSEUDOLIFE_MCP_DATABASE_URL": None,
    # No-bank paths re-run in the daemon container through docker
    # (daemon_exec): a PATH inside the home finds none (core's preflight).
    "PATH": "{HOME}" + SEP + "no-bin",
}
PROGRAMS = ("docker",)


def _home(rel: str) -> str:
    return "{HOME}" + SEP + rel.replace("/", SEP)


class _Lazy:
    """An argv item resolved in setup (seeded ids and heads exist only then)."""

    def __init__(self, make):
        self.make = make


def _resolve_argv(case, original):
    case.argv = [item.make() if isinstance(item, _Lazy) else item for item in original]


def _stash(arm) -> None:
    arm.state["before"] = core.snapshot(arm.home)


def _record(arm, obs) -> None:
    obs["arm"] = arm.name
    obs["before"] = arm.state.get("before", {})


def _files(arm, files) -> None:
    for rel, data in (files or {}).items():
        path = arm.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data() if callable(data) else data)


def _bank_verify_reference(case, kind, home):
    """Verify the seeded bank with the frozen chain rules before launching the CLI."""
    core._reset(home)
    arm = core.Arm("reference", home, home / "cwd")
    arm.cwd.mkdir()
    case.setup(arm)
    newline = b"\r\n" if core.WINDOWS else b"\n"
    if kind == "nolog":
        code, stdout = 2, b""
        stderr = f"board-audit: {_NO_AUDIT_LOG}".encode("utf-8") + newline
    else:
        expect = None
        if case.argv[2:]:
            seq, digest = case.argv[3].split(":", 1)
            expect = (int(seq), digest)
        with _bank.connect(arm.state["name"]) as conn:
            # Bank verification hashes the stored payload text. Archive
            # projection decodes it and can change its spelling.
            rows = (dict(zip(_EXPORT_FIELDS + _BODY_FIELDS, row, strict=True))
                    for row in _stored_audit_rows(conn))
            report = verify_audit_chain(rows, expect_head=expect)
        code, stderr = int(not report["ok"]), b""
        stdout = json.dumps(report).encode("utf-8") + newline
    return {"home": str(home), "exit": code, "stdout": _encoded(stdout), "stderr": _encoded(stderr),
            "files": arm.state["before"], "modes": core.modes(home), "db": "unchanged"}


# The frozen CLI's `verify` against a bank whose report stdout refuses,
# captured from the frozen arm of the live audit row at master ffbad904b on
# 23 November 2026: CPython 3.11.17 on the Linux dev box, 3.11.9 on the
# Windows dev VM in ordinary and 8.3 short TEMP alike. The frozen `verify`
# printed its report and returned 0; CPython's shutdown flush to the closed
# stdout then failed, printed this ignored-exception trailer and exited 120.
# The native CLI refuses instead (exit 2 and a diagnostic), so the case
# stays one of the row's known differences.
_FROZEN_CLOSED_STDOUT_VERIFY = {
    "linux": ("Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\n"
              "BrokenPipeError: [Errno 32] Broken pipe\n"),
    "windows": ("Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\r\n"
                "OSError: [Errno 22] Invalid argument\r\n"),
}


def _frozen_closed_stdout_verify_reference(case, kind, home):
    """The frozen capture above, for a seed whose report the frozen CLI printed."""
    verified = _bank_verify_reference(case, kind, home)
    if verified["exit"] != 0 or not core.decode(verified, "stdout"):
        raise AssertionError(f"{case.id}: the frozen capture is of a bank that verifies and prints its "
                             f"report; the {kind} seed now verifies with exit {verified['exit']} and "
                             f"{len(core.decode(verified, 'stdout'))} bytes of report")
    stderr = _FROZEN_CLOSED_STDOUT_VERIFY[core.PLATFORM].encode("utf-8")
    return dict(verified, exit=120, stdout=_encoded(b""), stderr=_encoded(stderr))


def frozen_closed_stdout_verify_case(case_id, kind):
    case = read_case(case_id, kind, ["verify"], stdout_closed=True)
    case.reference = partial(_frozen_closed_stdout_verify_reference, case, kind)
    return case


def bank_verify_case(case_id, kind, *, expect_head=None, env=None):
    argv = ["verify"]
    if expect_head is not None:
        argv.extend(["--expect-head", expect_head])
    case = read_case(case_id, kind, argv, env=env)
    case.reference = partial(_bank_verify_reference, case, kind)
    return case


def read_case(case_id, kind, argv, *, files=None, rules=(), stdout_closed=False,
              env=None, note="", platforms=("windows", "linux"), prepare=None):
    """A read action against one seeded bank, which must stay unchanged."""
    original = ["board-audit", *argv]

    def setup(arm):
        name = ensure(kind)
        _files(arm, files)
        if prepare is not None:
            prepare(arm)
        _resolve_argv(case, original)
        arm.state["name"] = name
        _stash(arm)

    def after(arm, obs):
        _record(arm, obs)
        dump = _bank.dump(arm.state["name"])
        obs["db"] = "unchanged" if dump == _BASELINE[arm.state["name"]] else dump

    environment = {**GUARDS, "PSEUDOLIFE_MCP_DATABASE_URL": _bank.url(_name(kind))}
    environment.update(env or {})
    case = core.Case(case_id, list(original), env=environment, setup=setup,
                     after=after, rules=rules, stdout_closed=stdout_closed, note=note,
                     platforms=platforms, programs=PROGRAMS)
    return case


def _filter_export(data, *, project=None, task=None, agent=None, since=None, until=None):
    """Select seeded rows while preserving their JSON lines and sequence order."""
    lines = []
    for line in data.splitlines(keepends=True):
        row = json.loads(line)
        if ((project is not None and row["project"] != project)
                or (task is not None and row["task"] != task)
                or (agent is not None and agent not in (row["agent_id"], row["recipient_agent_id"]))
                or (since is not None and row["created_at"] < since)
                or (until is not None and row["created_at"] >= until)):
            continue
        lines.append(line)
    return b"".join(lines)


def _bank_export_reference(case, kind, filters, recorded, home):
    core._reset(home)
    arm = core.Arm("reference", home, home / "cwd")
    arm.cwd.mkdir()
    case.setup(arm)
    want = {"home": str(home), "files": arm.state["before"],
            "modes": core.modes(home), "db": "unchanged"}
    if recorded:
        # These stdout refusals have frozen-Python goldens on both platforms.
        golden = _audit_golden(core.PLATFORM)
        if case.id in (golden.get("recorded_from_candidate") or {}):
            raise ValueError(f"{case.id}: live export refusal requires a frozen Python recording")
        frozen = golden["cases"][case.id]
        return dict(want, exit=frozen["exit"], stdout=frozen["stdout"], stderr=frozen["stderr"])
    data = export_bytes(kind)
    if filters:
        resolved = {}
        for key, value in filters.items():
            if isinstance(value, _Lazy):
                value = value.make()
            if isinstance(value, datetime):
                value, = _audit_timestamps((value.isoformat(),))
            resolved[key] = value
        data = _filter_export(data, **resolved)
    stdout = data.replace(b"\n", b"\r\n") if core.WINDOWS else data
    return dict(want, exit=0, stdout=_encoded(stdout), stderr="")


def bank_export_case(case_id, kind, argv=(), *, recorded=False, **filters):
    """Use a stored-row projection or a recorded refusal for export to stdout."""
    case = read_case(case_id, kind, ["export", *argv])
    case.reference = partial(_bank_export_reference, case, kind, filters, recorded)
    return case


def _export_observation(arm, code, stdout=b"", diagnostic=""):
    newline = b"\r\n" if core.WINDOWS else b"\n"
    stderr = diagnostic.encode("utf-8") + newline if diagnostic else b""
    return {"home": str(arm.home), "exit": code,
            "stdout": _encoded(stdout.replace(b"\n", newline)), "stderr": _encoded(stderr),
            "files": core.snapshot(arm.home), "modes": core.modes(arm.home), "db": "unchanged"}


def _frozen_create_refusal(path):
    # The long Windows path can fail at the path limit or at the component
    # limit. Use the frozen exporter's exclusive open on the disposable path
    # so the reference follows the host's actual policy.
    try:
        with path.open("x", encoding="utf-8", newline="\n"):
            pass
    except OSError as error:
        return f"board-audit: cannot create {path}: {error.strerror}"
    raise AssertionError(f"invalid export path was successfully opened: {path}")


def _export_file_reference(case, out, task, diagnostic, leftover, home):
    core._reset(home)
    arm = core.Arm("reference", home, home / "cwd")
    arm.cwd.mkdir()
    case.setup(arm)
    path = home / out
    diagnostic = diagnostic(path) if callable(diagnostic) else diagnostic.format(out=path)
    if not diagnostic:
        data = export_bytes("rich")
        with path.open("xb") as stream:
            stream.write(_filter_export(data, task=task) if task is not None else data)
    elif leftover:
        # The frozen exporter opens the destination before resolving --agent.
        # A lookup refusal leaves its empty file with ordinary umask bits.
        path.touch(exist_ok=False)
    return _export_observation(arm, 2 if diagnostic else 0, diagnostic=diagnostic)


def export_file_case(case_id, out, *, task=None, agent=None, diagnostic="", leftover=False, **options):
    argv = ["export"]
    if agent is not None:
        argv.extend(["--agent", agent])
    argv.extend(["--out", _home(out)])
    if task is not None:
        argv.extend(["--task", task])
    case = read_case(case_id, "rich", argv, **options)
    case.reference = partial(_export_file_reference, case, out, task, diagnostic, leftover)
    return case


def _export_stdout_failure_reference(case, kind, diagnostic, home):
    core._reset(home)
    arm = core.Arm("reference", home, home / "cwd")
    arm.cwd.mkdir()
    case.setup(arm)
    stdout = b""
    if kind == "invalid_payload":
        with _bank.connect(arm.state["name"]) as conn:
            rows = list(_stored_audit_rows(conn))
            if len(rows) != 2:
                raise AssertionError("the invalid-payload seed must hold exactly two audit rows, "
                                     f"found {len(rows)}")
            prefix, tail = rows
            try:
                json.loads(tail[_EXPORT_FIELDS.index("payload")])
            except json.JSONDecodeError:
                pass
            else:
                raise AssertionError("the second audit payload must be malformed")
            stdout = _export_line(prefix)
    return _export_observation(arm, 2, stdout, diagnostic)


def export_stdout_failure_case(case_id, kind, diagnostic, *, stdout_closed=False):
    case = read_case(case_id, kind, ["export"], stdout_closed=stdout_closed)
    case.reference = partial(_export_stdout_failure_reference, case, kind, diagnostic)
    return case


@cache
def _file_references(platform):
    path = Path(__file__).with_name(f"audit.file.{platform}.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _file_reference(case, reference, home):
    """Recreate the frozen observation from the case's own inputs."""
    core._reset(home)
    arm = core.Arm("reference", home, home / "cwd")
    arm.cwd.mkdir()
    case.setup(arm)
    code, stdout, stderr = reference
    if _ARCHIVE_HASH_TOKEN in stdout:
        # The fixture archive carries the run's head, including hashes
        # recomputed at seed time. Resolve it before launching the CLI.
        lines = (home / "archive.jsonl").read_text(encoding="utf-8").splitlines()
        tail = json.loads(next(line for line in reversed(lines) if line.strip()))
        stdout = stdout.replace(_ARCHIVE_HASH_TOKEN, tail["hash"]).replace(
            _ARCHIVE_CLOCK_TOKEN, json.dumps(float(tail["created_at"])))
    return {
        "home": str(home), "exit": code,
        "stdout": _encoded(stdout.encode("utf-8")), "stderr": _encoded(stderr.encode("utf-8")),
        "files": arm.state["before"], "modes": core.modes(home),
    }


def file_case(case_id, argv, *, files=None, rules=(), stdout_closed=False, env=None,
              platforms=("windows", "linux"), note="", timeout=30.0):
    """A file-only action (no bank in the environment)."""
    original = ["board-audit", *argv]

    def setup(arm):
        _files(arm, files)
        _resolve_argv(case, original)
        _stash(arm)

    case = core.Case(case_id, list(original), env={**GUARDS, **(env or {})}, setup=setup, after=_record,
                     rules=rules, stdout_closed=stdout_closed, platforms=platforms, note=note,
                     programs=PROGRAMS, timeout=timeout)
    if (reference := _file_references(core.PLATFORM).get(case_id)) is not None:
        case.reference = partial(_file_reference, case, reference)
    return case


def _no_bank_reference(case, home):
    newline = "\r\n" if core.WINDOWS else "\n"
    # Frozen daemon_exec.no_bank_message("board-audit").
    stderr = ("board-audit: no bank found: set PSEUDOLIFE_MCP_DATABASE_URL to the bank's database URL, or run "
              "where the lite tier's data dir holds one (Docker tier: start the daemon, or run "
              "`docker exec -it pseudolife-mcp-daemon pseudolife-mcp board-audit ...` on its host)" + newline)
    return _file_reference(case, (2, "", stderr), home)


def audit_no_bank_case(case_id, argv):
    case = file_case(case_id, argv)
    case.reference = partial(_no_bank_reference, case)
    return case


def archive_case(case_id, variant, *extra, rules=(), stdout_closed=False):
    return file_case(case_id, ["verify", "--input", _home("archive.jsonl"), *extra],
                     files={"archive.jsonl": lambda: archive(variant)}, rules=rules,
                     stdout_closed=stdout_closed)


REDACT_DB = _bank.name(PREFIX + "rd")


# Transcribed from pseudolife_memory/board_audit_cli.py at
# 3ac81bcdcb3a6d64a769b40057c02b1c35458225 (_redact and _bank). Each
# refusal case declares its outcome; these tables do not decide what to reject.
_REDACT_REFUSALS = {
    "invalid_message_id": "--message-id must be one message id as the audit log shows it",
    "invalid_reason": "--reason must be 1-240 characters on one line, with no control, "
                      "format or separator characters",
    "secret_like_body": "the reason looks like it holds a credential, and the reason is "
                        "kept in the log for good; describe the mistake without repeating it",
    "message_not_found": "no record of this message id: an unknown id, or one whose send "
                         "event audit retention removed and whose live copy is gone too",
    "body_in_hashed_payload": "this message was sent before schema v46, when the body was "
                              "part of the hashed payload; removing it would break the "
                              "chain, so it stays until audit retention removes the event, "
                              "and its live copy is already gone",
    "already_redacted": "this message's body is already redacted",
}
_REDACT_UNAVAILABLE = {
    "busy": "the board is busy: a row or the audit chain stayed locked for "
            "more than 5 s. Nothing was changed; retry",
    "nolog": _NO_AUDIT_LOG,
    "prev46": "this bank's audit log predates schema v46, so no body in it can be redacted: "
              "every send body is part of the hashed payload. Start a v46 daemon once to add "
              "the body column; bodies sent before it stay unredactable",
}


def _redact_prefix_matches(bank, prefix):
    """The seed's message IDs a redact prefix can name: live messages and
    retained send events (other audit events never resolve a prefix)."""
    tables = _BASELINE[bank]["tables"]
    messages = {json.loads(text)["message_id"] for text in tables["coordination_messages"]}
    for text in tables["coordination_events"]:
        row = json.loads(text)
        if row["event"] == "send":
            messages.add(row["message_id"])
    return sorted(message for message in messages if message is not None and message.startswith(prefix))


def _distinguishing_prefixes(matches):
    # The frozen rule (`distinguishing_prefixes` in
    # pseudolife_memory/storage/coordination.py at 3ac81bc): the first eight
    # matches in ID order, each cut to the shortest length that tells every
    # adjacent pair of them apart, never under eight characters.
    listed = sorted(matches)[:8]
    shared = max(len(os.path.commonprefix(pair)) for pair in zip(listed, listed[1:]))
    cut = max(8, shared + 1)
    return [message[:cut] for message in listed]


def _seed_payload(text, where):
    """A seeded send payload as the JSON object the references read."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as error:
        raise AssertionError(f"{where}: the send payload is not JSON ({error})") from None
    if not isinstance(payload, dict):
        raise AssertionError(f"{where}: the send payload is not a JSON object")
    return payload


def _has_lone_surrogate(value):
    # json.loads joins a valid surrogate pair into one code point, so any
    # surrogate left in a string is a lone one.
    if isinstance(value, str):
        return any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    if isinstance(value, dict):
        return any(_has_lone_surrogate(key) or _has_lone_surrogate(item) for key, item in value.items())
    return isinstance(value, list) and any(map(_has_lone_surrogate, value))


def _has_nonfinite(value):
    if isinstance(value, float):
        return value != value or value in (float("inf"), float("-inf"))
    if isinstance(value, dict):
        return any(map(_has_nonfinite, value.values()))
    return isinstance(value, list) and any(map(_has_nonfinite, value))


# The payload property a case is named for; a reseed without it fails.
_SEED_PROPERTIES = {
    "lone-surrogate": ("a lone surrogate in the send payload", _has_lone_surrogate),
    "nonfinite-fanout": ("a non-finite number in a fan-out send payload with a string request_id",
                         lambda payload: _has_nonfinite(payload) and "fanout" in payload
                         and isinstance(payload.get("request_id"), str)),
}


def _assert_refusal_seed(seed, value, refusal):
    """Fail loudly when the seed no longer refuses the way a case declares.

    ``message_not_found`` needs no live message or send event that the value
    names (by prefix when it is 8-31 lowercase hex, else exactly);
    ``body_in_hashed_payload`` needs the named message's first send to carry
    no ``text_commitment`` and no live text to remain."""
    tables = _BASELINE[seed]["tables"]
    sends = sorted((row for row in map(json.loads, tables["coordination_events"]) if row["event"] == "send"),
                   key=lambda row: row["seq"])
    live = list(map(json.loads, tables["coordination_messages"]))
    prefix = 8 <= len(value) < 32 and all(c in "0123456789abcdef" for c in value)
    named = _redact_prefix_matches(seed, value) if prefix else [
        value for row in [*sends, *live] if row["message_id"] == value][:1]
    where = f"{seed} message {value}"
    if refusal == "message_not_found":
        if named:
            raise AssertionError(f"{where}: a not-found refusal needs no seeded message, found {named}")
        return
    if len(named) != 1:
        raise AssertionError(f"{where}: a hashed-payload refusal needs one named message, found {len(named)}")
    first = next((row for row in sends if row["message_id"] == named[0]), None)
    texts = [row["text"] for row in live if row["message_id"] == named[0]]
    if first is None or "text_commitment" in _seed_payload(first["payload"], where) or any(
            t is not None for t in texts):
        raise AssertionError(f"{where}: a hashed-payload refusal needs a pre-v46 send and no live text")


def _redact_refusal_reference(case, source, original, refusal, home, *, read_only=False):
    """A refusal leaves the candidate's template copy equal to the seed bank."""
    started = time.time()
    bank = ensure(source)
    _resolve_argv(case, original)
    core._reset(home)
    (home / "cwd").mkdir()
    newline = b"\r\n" if core.WINDOWS else b"\n"
    if refusal in _REDACT_REFUSALS or refusal == "ambiguous_message_id":
        code = 1
        result = {"ok": False, "message_id": case.argv[3], "reason": refusal}
        stdout = json.dumps(result).encode("ascii") + newline
        diagnostic = _REDACT_REFUSALS.get(refusal, refusal)
        if refusal in ("message_not_found", "body_in_hashed_payload"):
            _assert_refusal_seed(bank, case.argv[3], refusal)
        if refusal == "ambiguous_message_id":
            # The matches, their order and the distinguishing length come
            # from the original seed, not from either arm.
            matches = _redact_prefix_matches(bank, case.argv[3])
            if len(matches) < 2:
                raise AssertionError(f"{case.id}: an ambiguous prefix needs two or more seeded "
                                     f"matches, found {len(matches)}")
            diagnostic += ": " + ", ".join(_distinguishing_prefixes(matches))
    else:
        code, stdout = 2, b""
        diagnostic = _REDACT_UNAVAILABLE[refusal]
    stderr = f"board-audit: {diagnostic}".encode("utf-8") + newline
    return {"home": str(home), "window": [started, time.time()], "exit": code,
            "stdout": _encoded(stdout), "stderr": _encoded(stderr), "files": core.snapshot(home),
            "modes": core.modes(home), "db": "unchanged" if read_only else _BASELINE[bank]}


def _prepare_redaction_bank(bank, statements):
    if not statements:
        return
    with _bank.connect(bank, autocommit=True) as conn:
        _assert_audit_database(conn, bank)
        for statement in statements:
            conn.execute(statement.replace("{DB}", bank))


def redact_case(case_id, source, message, reason, *, rules=("audit-redact-clock",),
                hold=False, prepare=(), stdout_closed=False, note="", refusal=None):
    """A redaction on this arm's own TEMPLATE copy of a seeded bank;
    ``prepare`` statements run on that copy first (test instruments only)."""
    original = ["board-audit", "redact", "--message-id",
                message if isinstance(message, _Lazy) or isinstance(message, str)
                else _Lazy(message), "--reason", reason]

    def setup(arm):
        _copy(ensure(source), REDACT_DB)
        _prepare_redaction_bank(REDACT_DB, prepare)
        _resolve_argv(case, original)
        if hold:
            import psycopg  # noqa: PLC0415
            holder = psycopg.connect(_bank.url(REDACT_DB))
            holder.execute("SET search_path TO public")
            holder.execute("SELECT 1 FROM coordination_messages WHERE message_id=%s "
                           "FOR UPDATE", (case.argv[3],))
            arm.state["holder"] = holder
        _stash(arm)

    def after(arm, obs):
        holder = arm.state.pop("holder", None)
        if holder is not None:
            holder.rollback()
            holder.close()
        _record(arm, obs)
        obs["db"] = _bank.dump(REDACT_DB)

    case = core.Case(case_id, list(original),
                     env={**GUARDS, "PSEUDOLIFE_MCP_DATABASE_URL": _bank.url(REDACT_DB)},
                     setup=setup, after=after, rules=rules, timeout=60,
                     stdout_closed=stdout_closed, note=note, programs=PROGRAMS)
    if refusal is not None:
        case.reference = partial(_redact_refusal_reference, case, source, original, refusal)
    return case


# Test instrument for the vacuum-skip path (the test login owns every table
# and the database, so Postgres never skips a step for it, `VACUUM
# pg_catalog.pg_statistic` included): ANALYZE evaluates an expression
# index on each sampled row, and this expression raises a WARNING there,
# which `_vacuum` collects exactly as it collects a permission skip. The
# database default also hides warnings, as a role or database setting can.
VACUUM_WARNING = (
    "CREATE FUNCTION audit_test_warning(text) RETURNS text IMMUTABLE LANGUAGE plpgsql "
    "AS $$BEGIN RAISE WARNING 'audit test warning'; RETURN $1; END$$",
    "CREATE INDEX audit_test_warning_idx ON coordination_messages "
    "(audit_test_warning(message_id))",
    'ALTER DATABASE "{DB}" SET client_min_messages = error',
)


def _rich(key, index=None, cut=None):
    def make():
        value = ids("rich")[key]
        value = value[index] if index is not None else value
        return value[:cut] if cut else value
    return _Lazy(make)


def _fixture_message_prefix(prefix):
    def make():
        matches = _redact_prefix_matches(ensure("rich"), prefix)
        if len(matches) != 1:
            raise AssertionError(f"the rich seed must hold exactly one message for prefix {prefix}, "
                                 f"found {len(matches)}")
        return matches[0]

    return _Lazy(make)


def _abbreviated_redact_case():
    original = ["board-audit", "redact", "--m", "x", "--r", "why"]
    case = read_case("redact-abbreviated", "rich", original[1:])
    case.reference = partial(_redact_refusal_reference, case, "rich", original,
                             "message_not_found", read_only=True)
    return case


_REDACT_REFERENCE_DB = _bank.name(PREFIX + "rd_ref")


def _assert_redaction_seed(seed, message_id, audit_copy, siblings, seed_property=None):
    """Fail loudly when the seed no longer has the shape a case declares.

    ``audit_copy`` and ``siblings`` are declared per case; this checks them
    against the original seed's rows: removed needs one v46 send (its payload
    carries ``text_commitment``) that still holds a body or salt; kept needs
    one pre-v46 send and live text; gone needs no send event and one live
    row. The envelope the projection copies must be non-null. ``siblings``
    must equal the seed's other sends of one fan-out (the frozen
    ``_burst_siblings``: same sender and ``request_id``, in ``seq`` order).
    ``seed_property`` names the payload property a case is named for
    (``_SEED_PROPERTIES``)."""
    tables = _BASELINE[seed]["tables"]
    events = [json.loads(text) for text in tables["coordination_events"]]
    sends = [row for row in events if row["event"] == "send" and row["message_id"] == message_id]
    live = [row for row in map(json.loads, tables["coordination_messages"])
            if row["message_id"] == message_id]
    where = f"{seed} message {message_id}"
    if audit_copy == "gone":
        if sends or len(live) != 1:
            raise AssertionError(f"{where}: a gone redaction needs no send event and one live row, "
                                 f"found {len(sends)} and {len(live)}")
        if live[0]["text"] is None and live[0]["fingerprint"] == "redacted":
            raise AssertionError(f"{where}: a gone redaction needs a live row not already redacted")
        envelope = live[0]["sender_agent_id"], live[0]["project"], live[0]["task"]
        found = []
    else:
        if len(sends) != 1:
            raise AssertionError(f"{where}: expected exactly one send event, found {len(sends)}")
        send, = sends
        payload = _seed_payload(send["payload"], where)
        if seed_property is not None:
            description, holds = _SEED_PROPERTIES[seed_property]
            if not holds(payload):
                raise AssertionError(f"{where}: this case is named for {description}, "
                                     "which the seed no longer has")
        committed = "text_commitment" in payload
        if audit_copy == "removed" and not (committed and len(live) == 1 and (
                send["body"] is not None or send["body_salt"] is not None)):
            raise AssertionError(f"{where}: a removed redaction needs a v46 send with a stored body "
                                 "or salt and one live row")
        if audit_copy == "kept" and (committed or len(live) != 1 or live[0]["text"] is None):
            raise AssertionError(f"{where}: a kept redaction needs a pre-v46 send and live text")
        envelope = send["agent_id"], send["project"], send["task"]
        found = []
        request = payload.get("request_id")
        if "fanout" in payload and request is not None:
            found = [row["message_id"] for row in sorted(events, key=lambda row: row["seq"])
                     if row["event"] == "send" and row["message_id"] != message_id
                     and row["agent_id"] == send["agent_id"]
                     and _seed_payload(row["payload"], f"{seed} send {row['seq']}").get("request_id")
                     == request]
    if None in envelope:
        raise AssertionError(f"{where}: the sender, project and task must be non-null")
    if found != list(siblings):
        raise AssertionError(f"{where}: declared fan-out siblings {list(siblings)} differ from "
                             f"the seed's {found}")


def _project_redaction(bank, message_id, reason, now, audit_copy, siblings):
    """Apply the fixture's known redaction to a separate, guarded bank copy."""
    from rust.harness_support.fixture_rows import checked_fixture_insert  # noqa: PLC0415

    with _bank.connect(bank) as conn:
        _assert_audit_database(conn, bank)
        if audit_copy == "gone":
            seq = None
            live = conn.execute(
                "SELECT text,sender_agent_id,recipient_agent_id,project,task "
                "FROM coordination_messages WHERE message_id=%s", (message_id,)).fetchone()
            if live is None:
                raise AssertionError(f"{bank}: no live row for the gone message {message_id}")
            live_text, agent, recipient, project, task = live
        else:
            sends = conn.execute(
                "SELECT seq,agent_id,recipient_agent_id,project,task FROM coordination_events "
                "WHERE event='send' AND message_id=%s", (message_id,)).fetchall()
            if len(sends) != 1:
                raise AssertionError(f"{bank}: expected one send event for {message_id}, found {len(sends)}")
            (seq, agent, recipient, project, task), = sends
            live = conn.execute("SELECT text FROM coordination_messages WHERE message_id=%s",
                                (message_id,)).fetchone()
            if live is None:
                raise AssertionError(f"{bank}: no live row for {message_id}")
            live_text, = live
        head = conn.execute("SELECT seq,hash FROM coordination_events ORDER BY seq DESC LIMIT 1").fetchone()
        if head is None:
            raise AssertionError(f"{bank}: the reference copy has no audit event to chain the redaction to")
        head_seq, head_hash = head
        event = dict(seq=head_seq + 1, event="redact", actor="operator", principal="",
                     agent_id=agent, recipient_agent_id=recipient, project=project, task=task,
                     message_id=message_id, hlc="", created_at=now, prev_hash=head_hash,
                     payload=canonical_payload({"message_id": message_id, "seq": seq,
                                                "reason": reason, "audit_copy": audit_copy}),
                     body=None, body_salt=None)
        event["hash"] = audit_hash(head_hash, event)
        if audit_copy == "removed":
            conn.execute("UPDATE coordination_events SET body=NULL,body_salt=NULL WHERE seq=%s", (seq,))
        conn.execute("UPDATE coordination_messages SET text=NULL,fingerprint='redacted',"
                     "expires_at=LEAST(expires_at,%s) WHERE message_id=%s", (now, message_id))
        insert, values = checked_fixture_insert(conn, "coordination_events", [event])
        conn.execute(insert, values)

    redact_seq = event["seq"]
    digest = event["hash"]
    report = {"ok": True, "message_id": message_id, "seq": seq, "redact_seq": redact_seq,
              "redact_hash": digest, "expect_head": f"{redact_seq}:{digest}",
              "live_body_cleared": live_text is not None, "audit_copy": audit_copy}
    if audit_copy != "gone":
        report["other_copies"] = siblings
    return report


def _redact_fixture_warnings(bank, instrument):
    # Run the CLI's clean-up statements on the reference copy, so PostgreSQL
    # in this environment supplies the warnings, their count and order. With
    # the ANALYZE instrument prepared, its warning must be among them; without
    # it the clean-up must be complete, so a test login that starts skipping
    # a step fails here instead of moving the expected result.
    warnings = []
    with _bank.connect(bank, autocommit=True) as conn:
        _assert_audit_database(conn, bank)

        def note(diag):
            if diag.severity_nonlocalized == "WARNING":
                warnings.append(diag.message_primary or "")

        conn.add_notice_handler(note)
        conn.execute("SET client_min_messages TO warning")
        conn.execute("VACUUM (ANALYZE) coordination_events, coordination_messages")
        conn.execute("VACUUM pg_catalog.pg_statistic")
    if instrument and "audit test warning" not in warnings:
        raise AssertionError("the redaction reference's ANALYZE instrument produced no warning")
    if not instrument and warnings:
        raise AssertionError("the test login skipped clean-up: " + "; ".join(warnings))
    return warnings


def _redact_projected_reference(source, message, reason, audit_copy, siblings, prepare, stdout_closed, home,
                                *, seed_property=None):
    started = time.time()
    seed = ensure(source)
    _copy(seed, _REDACT_REFERENCE_DB)
    _prepare_redaction_bank(_REDACT_REFERENCE_DB, prepare)
    message_id = message.make() if isinstance(message, _Lazy) else message
    siblings = [other.make() for other in siblings]
    _assert_redaction_seed(seed, message_id, audit_copy, siblings, seed_property)
    report = _project_redaction(_REDACT_REFERENCE_DB, message_id, reason, started, audit_copy, siblings)
    instrumented = prepare == VACUUM_WARNING
    warnings = _redact_fixture_warnings(_REDACT_REFERENCE_DB, instrumented)
    # Only the ANALYZE instrument may leave the clean-up incomplete (checked
    # above), so the flag follows the case's declaration: literally True
    # without it, False with it.
    report["vacuumed"] = not instrumented
    # The notes, their order and the result key order are transcribed from
    # `_redact` in pseudolife_memory/board_audit_cli.py at
    # 3ac81bcdcb3a6d64a769b40057c02b1c35458225.
    notes = []
    if audit_copy == "kept":
        notes.append("the live copy is blanked and out of delivery, but this message "
                     "was sent before schema v46: the audit log keeps its body until audit "
                     "retention removes the send event")
    if siblings:
        notes.append(f"this message was one of {len(siblings) + 1} copies "
                     "of one send to a project or the whole board; each keeps its own copy of the "
                     "body until it is redacted too: " + " ".join(siblings))
    if audit_copy == "gone":
        notes.append("audit retention had already removed this message's send event, "
                     "so the audit log held no copy of it; its live request fingerprint (and any "
                     "live text) is blanked")
    if warnings:
        notes.append("the redaction is committed, but Postgres skipped part of the "
                     "clean-up (" + "; ".join(warnings) + "), so the old row versions or the "
                     "statistics may still hold the body; run `VACUUM (ANALYZE) coordination_events, "
                     "coordination_messages` and `VACUUM pg_statistic` as the tables' owner or a superuser")
    notes.append("record this head outside the bank; `pseudolife-mcp board-audit "
                 f"verify --expect-head {report['expect_head']}` later shows the redaction record is still there")

    core._reset(home)
    (home / "cwd").mkdir()
    newline = b"\r\n" if core.WINDOWS else b"\n"
    stdout = b"" if stdout_closed else json.dumps(report).encode("ascii") + newline
    stderr = b"".join(f"board-audit: {note}".encode("utf-8") + newline for note in notes)
    return {"home": str(home), "window": [started, time.time()], "exit": 120 if stdout_closed else 0,
            "stdout": _encoded(stdout), "stderr": _encoded(stderr),
            "files": core.snapshot(home), "modes": core.modes(home), "db": _bank.dump(_REDACT_REFERENCE_DB)}


def redact_projected_case(case_id, source, message, reason, *, resolved_id=None, audit_copy="removed",
                          siblings=(), prepare=(), stdout_closed=False, rules=("audit-redact-clock",),
                          seed_property=None):
    if prepare not in ((), VACUUM_WARNING):
        # The reference would read any other preparation as no instrument.
        raise ValueError(f"{case_id}: a projected redaction's only preparation is VACUUM_WARNING")
    case = redact_case(case_id, source, message, reason, prepare=prepare, rules=rules, stdout_closed=stdout_closed)
    case.reference = partial(_redact_projected_reference, source, resolved_id or message, reason,
                             audit_copy, siblings, prepare, stdout_closed, seed_property=seed_property)
    return case


def redact_removed_case(case_id, key, reason, *, cut=None):
    """A direct v46 send with a full fixture ID for the reference."""
    return redact_projected_case(case_id, "rich", _rich(key, cut=cut), reason, resolved_id=_rich(key))


def stats_events():
    """Synthetic export rows for telemetry; deliberately not a verified chain."""
    a, b = "a" * 32, "b" * 32
    def event(seq, kind, when, agent=a, payload=None, **extra):
        return dict(seq=seq, event=kind, actor="agent", principal="alice" if agent == a else "bob",
                    agent_id=agent, recipient_agent_id=None, project="fixture", task="stats",
                    message_id=None, payload=payload or {}, created_at=float(when), hlc="",
                    prev_hash="0" * 64, hash="1" * 64, body=None, body_salt=None, **extra)
    rows = []
    def add(kind, when, agent=a, payload=None, **extra):
        row = event(len(rows)+1, kind, when, agent, payload)
        row.update(extra)
        rows.append(row)
    def park(reason, expiry, before=None):
        return {"fields": {"park_reason": reason, "park_expires": expiry},
                "before": {"park_reason": before, "park_expires": None}}
    add("update", 900, b, park("before_window", 1200))
    add("attach", 1000, payload={"renewed": False})
    add("attach", 1100, b, {"renewed": False})
    add("attach", 1150, b, {"renewed": True})
    add("update", 1300, b, park("blocked", 2400, "before_window"))
    add("update", 1310, b, {"fields": {"park_reason": "needs_resource"}, "before": {}})
    for n, decision in enumerate(("rung", "nudged", "hinted", "withheld", "no_path", "capped", "not_needed", "future")):
        add("send", 1400+n*10, message_id=f"m{n}", recipient_agent_id=b,
            payload={"wake": decision, "private": "body/path must not reach the report"})
        if n % 2 == 0:
            add("read", 1400+n*10+(2.25 if n == 0 else n), b,
                {"path": "pull"}, message_id=f"m{n}", recipient_agent_id=b)
    add("update", 1500, b, {"fields": {"park_reason": None}, "before": {}})
    add("update", 1600, payload=park("expires", 1650))
    add("update", 1700, payload=park("revived", 1900, "expires"))
    add("update", 1800, payload={"fields": {"park_reason": None}, "before": {}})
    add("update", 2000, payload=park("open", 6000))
    add("detach", 3000, b)
    add("prune", 4000, payload={"agent_ids": [a]})
    add("read", 5000, b, message_id="m1")  # exclusive boundary: still unread
    return rows


def seed_stats(conn):
    """Seed the fresh stats bank with a deterministic chain and historical wakes."""
    if not fixture_database_pattern("w1c").fullmatch(assert_disposable_database(conn)):
        raise ValueError("stats seed needs a disposable w1c database")
    events = stats_events()
    b = "b" * 32
    events.extend([
        dict(events[0], seq=100, event="woke", created_at=1410.0, agent_id=b, payload={}),
        dict(events[0], seq=101, event="read", created_at=1411.0, agent_id=b, payload={"path":"delivery"}),
        dict(events[0], seq=102, event="update", created_at=1420.0, agent_id=b, actor="daemon", payload={}),
    ])
    ordered = sorted(events, key=lambda row: (row["created_at"], row["seq"]))
    previous = GENESIS_HASH
    for seq, row in enumerate(ordered, start=1):
        row.update(seq=seq, payload=canonical_payload(row["payload"]), prev_hash=previous)
        row["hash"] = previous = audit_hash(previous, row)
    wakes = [
        ("rung", 1400.0, 1405.0),
        ("nudged", 1410.0, None),
        ("rung", 1450.0, 5000.0),
        ("rung", 900.0, 1000.0),
        ("hinted", 1460.0, 1470.0),
    ]
    with conn.transaction(), conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO coordination_events "
            "(seq,event,actor,principal,agent_id,recipient_agent_id,project,task,"
            "message_id,payload,created_at,hlc,prev_hash,hash) "
            "VALUES (%(seq)s,%(event)s,%(actor)s,%(principal)s,%(agent_id)s,"
            "%(recipient_agent_id)s,%(project)s,%(task)s,%(message_id)s,%(payload)s,"
            "%(created_at)s,%(hlc)s,%(prev_hash)s,%(hash)s)",
            ordered,
        )
        cursor.executemany(
            "INSERT INTO coordination_wakes "
            "(recipient_agent_id,sender_agent_id,message_id,decision,ring_at,created_at,served_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            [(b, "a" * 32, f"m{n}", decision, created, created, served)
             for n, (decision, created, served) in enumerate(wakes)],
        )
    return {}


def stats_durations():
    return b'not json\n[]\n' + b"".join(json.dumps(record).encode()+b"\n" for record in [
        {"ended":"1970-01-01T01:06:40Z", "seconds":2.25,
         "queued_at":"1970-01-01T01:00:00Z", "started_at":"1970-01-01T01:00:02.35Z"},
        {"ended":"1970-01-01T01:06:40Z", "seconds":2.35},
        {"ended":"1970-01-01T01:06:40Z", "seconds":True},
        {"ended":"1970-01-01T01:23:20Z", "seconds":999},
        {"ended":"bad", "seconds":5},
    ])


@cache
def _audit_golden(platform):
    path = Path(__file__).resolve().parents[1] / "goldens" / f"audit.{platform}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _recorded_cases(platform):
    return _audit_golden(platform)["cases"]


def _recorded_reference(case_id, home):
    want = _recorded_cases(core.PLATFORM)[case_id]
    if core.WINDOWS:
        return want
    # Goldens use a fixed umask. Live ordinary files and directories follow
    # the caller's umask, while the private export stays owner-only.
    core._reset(home)
    (home / "cwd").mkdir()
    (home / "file").touch()
    modes = core.modes(home)
    ordinary = {"0o755": modes["cwd"], "0o644": modes["file"]}
    return dict(want, modes={name: ordinary.get(mode, mode) for name, mode in want["modes"].items()})


@cache
def _stats_references():
    # The captured fields extend the report shapes in the committed Rust
    # corpus. They use the frozen inputs and oracle at 0c5de1488.
    fixture = json.loads(_STATS_REFERENCES.read_text(encoding="utf-8"))
    corpus = json.loads(_STATS_CORPUS.read_text(encoding="utf-8"))
    return fixture, {case["name"]: case["report"] for case in corpus}


def _encoded(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _audit_timestamps(dates):
    if sys.platform == "linux" and "TZDIR" in os.environ:
        # The CLI arm inherits TZ but drops TZDIR. Resolve under the same
        # inputs without changing the harness process's timezone.
        env = {"TZ": os.environ["TZ"]} if "TZ" in os.environ else {}
        code = (
            "import sys\n"
            "from datetime import datetime\n"
            "print(*(datetime.fromisoformat(date).timestamp() for date in sys.argv[1:]))"
        )
        timestamps = subprocess.check_output(
            [sys.executable, "-I", "-c", code, *dates], env=env, text=True, timeout=5)
        return tuple(map(float, timestamps.split()))
    return tuple(datetime.fromisoformat(date).timestamp() for date in dates)


def _stats_report(case, report_id):
    reference, corpus = _stats_references()
    shape = reference["reports"][report_id]
    report = dict(corpus[shape["corpus"]], version="<version>", generated_at="<generated_at>")
    report.update(shape["fields"])
    if case.id == "stats-local-system-zone":
        dates = [
            case.argv[case.argv.index(flag) + 1]
            for flag in ("--since", "--until")
        ]
        since, until = _audit_timestamps(dates)
        report["window"] = {"since": since, "until": until, "hours": round((until - since) / 3600, 3)}
    return (json.dumps(report, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")


def _stats_reference(case, home):
    """Build the expected observation from setup inputs and reference bytes."""
    core._reset(home)
    arm = core.Arm("reference", home, home / "cwd")
    arm.cwd.mkdir()
    case.setup(arm)
    want = {
        "arm": arm.name, "home": str(home), "exit": 0,
        "stdout": "", "stderr": "", "files": core.snapshot(home), "modes": core.modes(home),
    }
    if case.id.startswith("stats-bank-"):
        want["db"] = "unchanged"
    fixture, _ = _stats_references()
    if case.id in fixture["fixed"]:
        fixed = fixture["fixed"][case.id]
        want["exit"] = fixed["exit"]
        stream = "stderr" if want["exit"] else "stdout"
        want[stream] = _encoded(fixed[core.PLATFORM].encode("utf-8"))
    elif case.id == "stats-out-and-append":
        line = _stats_report(case, "stats-export-metrics")
        old_log = base64.b64decode(want["files"]["log.jsonl"][5:])
        want["files"].update({"out.jsonl": "file:" + _encoded(line),
                              "log.jsonl": "file:" + _encoded(old_log + line)})
        if not core.WINDOWS:
            # Both the seeded log and the new output use the caller's umask.
            want["modes"]["out.jsonl"] = want["modes"]["log.jsonl"]
    else:
        line = _stats_report(case, case.id)
        want["stdout"] = _encoded(line.replace(b"\n", b"\r\n") if core.WINDOWS else line)
    return want


def _fixed_stats_case(case_id, argv):
    case = file_case(case_id, argv)
    case.reference = partial(_stats_reference, case)
    return case


def stats_cases():
    common = ["stats", "--since", "1000", "--until", "5000", "--input", _home("stats.jsonl"),
              "--durations", _home("durations.jsonl")]
    files = {"stats.jsonl": lambda: _lines(stats_events()), "durations.jsonl": stats_durations}
    out = [
        file_case("stats-export-metrics", common, files=files, rules=("audit-stats-clock",)),
        file_case("stats-out-and-append", [*common,"--out",_home("out.jsonl"),"--append",_home("log.jsonl")],
                  files={**files,"log.jsonl":b"existing\n"}, rules=("audit-stats-clock",)),
        file_case("stats-out-exists", [*common,"--out",_home("out.jsonl")], files={"out.jsonl":b"keep"}),
        file_case("stats-invalid-window", ["stats","--since","5000","--until","1000"]),
        file_case("stats-stdout-closed", common, files=files, stdout_closed=True),
        file_case("stats-out-missing-parent", [*common,"--out",_home("missing/out.jsonl")], files=files),
        file_case("stats-append-missing-parent", [*common,"--append",_home("missing/log.jsonl")], files=files),
        file_case("stats-input-malformed", [*common], files={"stats.jsonl":b"{}\n"}),
        file_case("stats-input-duplicate", common, files={"stats.jsonl":b'{"seq":1,"seq":2}\n'}),
        file_case("stats-input-utf8", common, files={"stats.jsonl":b"\xff\n"}),
        file_case("stats-empty-default-window", ["stats","--input",_home("empty.jsonl")],
                  files={"empty.jsonl":b""}, rules=("audit-stats-clock",)),
        read_case("stats-bank-precision", "stats", ["stats","--since","1000","--until","5000"],
                  rules=("audit-stats-clock",)),
        read_case("stats-bank-rich", "rich", ["stats","--since",str(T0),"--until",str(T0+DAY*2)],
                  rules=("audit-stats-clock",)),
        read_case("stats-bank-no-wakes", "prev49", ["stats","--since",str(T0),"--until",str(T0+DAY*2)],
                  rules=("audit-stats-clock",)),
        read_case("stats-bank-nolog", "nolog", ["stats","--since","1000","--until","5000"]),
    ]
    # An empty export makes the parsed local epoch itself observable in window.since.
    for label, zone, since, until in [
        ("ny-fold","America/New_York","2026-11-01T01:30:00","2026-11-02T00:00:00Z"),
        ("ny-gap","America/New_York","2026-03-08T02:30:00","2026-03-09T00:00:00Z"),
        ("lord-howe-fold","Australia/Lord_Howe","2026-04-05T01:45:00","2026-04-06T00:00:00Z"),
        ("lord-howe-gap","Australia/Lord_Howe","2026-10-04T02:15:00","2026-10-05T00:00:00Z"),
        ("apia-gap","Pacific/Apia","2011-12-30T12:00:00","2012-01-01T00:00:00Z"),
        ("date-only","UTC","2026-10-11","2026-10-12T00:00:00Z"),
    ]:
        out.append(file_case("stats-local-"+label,["stats","--input",_home("empty.jsonl"),"--since",since,"--until",until],
                   files={"empty.jsonl":b""},env={"TZ":zone},platforms=("linux",),rules=("audit-stats-clock",)))
        record = {"ended":since,"seconds":2.35,"queued_at":since,"started_at":until}
        out.append(file_case("stats-durations-"+label,["stats","--input",_home("empty.jsonl"),"--since","0","--until","2000000000",
                             "--durations",_home("durations.jsonl")],
                   files={"empty.jsonl":b"","durations.jsonl":json.dumps(record).encode()},
                   env={"TZ":zone},platforms=("linux",),rules=("audit-stats-clock",)))
    out.append(file_case("stats-local-system-zone",["stats","--input",_home("empty.jsonl"),"--since","2026-10-11T12:00:00",
                          "--until","2026-10-13T00:00:00Z"], files={"empty.jsonl":b""},rules=("audit-stats-clock",)))
    for case in out:
        case.golden = case.id == "stats-invalid-window"  # Fixed diagnostic; reports retain live clock validation.
        original_after = case.after
        defaults = ("--since" not in case.argv, "--until" not in case.argv)
        successful = "audit-stats-clock" in case.rules
        def after(arm,obs,original_after=original_after,defaults=defaults,successful=successful):
            original_after(arm,obs)
            assert obs["exit"] == (0 if successful else 2), "stats case did not reach its intended result"
            obs["stats_defaults"] = defaults
        case.after = after
        case.reference = (partial(_recorded_reference, case.id) if case.golden else
                          partial(_stats_reference, case))
    return out


_AUDIT_DOCKER = r"""import json, os, sys
from pathlib import Path
root = Path(os.environ['HOME'])
argv = sys.argv[1:]
with (root/'docker.jsonl').open('a',encoding='utf-8') as out:
    out.write(json.dumps(argv)+'\n')
if argv[0] == 'inspect':
    print('true' if argv[2] == '{{.State.Running}}' else os.environ['FIXTURE_ENGINE'])
    raise SystemExit(0)
(root/'docker.stdin').write_bytes(sys.stdin.buffer.read())
sys.stdout.buffer.write(b'{"container":"fixture"}\n')
sys.stderr.buffer.write(b'fixture stderr\n')
raise SystemExit(int(os.environ['FIXTURE_EXIT']))
"""


def transport_cases():
    import shlex
    out = []
    for engine in ("python", "rust", "<no value>"):
        for action in ("verify", "redact", "export", "stats"):
            for exit_code in ((0, 23) if action in ("export", "stats") else (23,)):
                suffix = "cmd" if core.WINDOWS else "sh"
                tail = {"verify":["--expect-head","12:"+"a"*64],
                        "redact":["--message-id","fixture-id","--reason","wrong paste"],
                        "export":["--task","fixture task"],
                        "stats":["--since","1000.0","--until","5000.0","--durations","/dev/stdin"]}[action]
                argv = ["board-audit",action,*tail]
                if action == "export":
                    argv += ["--out",_home("out.jsonl")]
                if action == "stats":
                    argv = ["board-audit","stats","--since","1000.0","--until","5000.0",
                            "--durations",_home("durations.jsonl"),"--out",_home("out.jsonl"),"--append",_home("log.jsonl")]
                def setup(arm,suffix=suffix):
                    script = arm.home/"docker_fixture.py"
                    script.write_text(_AUDIT_DOCKER,encoding="utf-8")
                    wrapper = (f'@echo off\r\n"{sys.executable}" "{script}" %*\r\nexit /b %errorlevel%\r\n' if core.WINDOWS else
                               f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n')
                    path = arm.home/("docker_fixture."+suffix)
                    path.write_text(wrapper,encoding="utf-8",newline="")
                    if not core.WINDOWS:
                        path.chmod(0o700)
                    (arm.home/"durations.jsonl").write_bytes(b'{"seconds":2.25}\n')
                    arm.state["inputs"] = {p.name:p.read_bytes() for p in (script,path,arm.home/"durations.jsonl")}
                    _stash(arm)
                def after(arm,obs,engine=engine,action=action,tail=tail,exit_code=exit_code):
                    calls = [json.loads(line) for line in (arm.home/"docker.jsonl").read_text().splitlines()]
                    expected = [["inspect","-f","{{.State.Running}}","pseudolife-mcp-daemon"],
                                ["inspect","-f",'{{index .Config.Labels "org.pseudolife.engine"}}',"pseudolife-mcp-daemon"],
                                ["exec","-i","-e","PSEUDOLIFE_DAEMON_EXEC=1","pseudolife-mcp-daemon",
                                 *(["pseudolife-mcp"] if engine == "rust" else ["python","-m","pseudolife_memory.cli"]),
                                 "board-audit",action,*tail]]
                    assert calls == expected, calls
                    assert obs["exit"] == exit_code
                    assert (arm.home/"docker.stdin").read_bytes() == (b'{"seconds":2.25}\n' if action == "stats" else b"")
                    if action in ("export","stats"):
                        destination = arm.home/"out.jsonl"
                        assert destination.exists() == (exit_code == 0)
                        if exit_code == 0:
                            assert destination.read_bytes() == b'{"container":"fixture"}\n'
                            if action == "stats":
                                assert (arm.home/"log.jsonl").read_bytes() == destination.read_bytes()
                            elif not core.WINDOWS:
                                assert stat.S_IMODE(destination.stat().st_mode) == 0o600, (
                                    "transport export must be owner-only (mode 0600)")
                    # Wrapper paths include the interpreter, so remove only the
                    # fixture files after asserting their original bytes.
                    for name,data in arm.state["inputs"].items():
                        assert (arm.home/name).read_bytes() == data
                        (arm.home/name).unlink()
                    obs["files"] = core.snapshot(arm.home)
                    _record(arm,obs)
                env = {**GUARDS,"PSEUDOLIFE_DAEMON_EXEC":None,
                       "PSEUDOLIFE_DOCKER":"{HOME}/docker_fixture."+suffix,
                       "FIXTURE_ENGINE":engine,"FIXTURE_EXIT":str(exit_code)}
                case_id = f"transport-{engine.replace('<no value>', 'unlabelled')}-{action}-{exit_code}"
                case = core.Case(case_id, argv, env=env, setup=setup, after=after, programs=PROGRAMS,
                                 reference=partial(_recorded_reference, case_id))
                out.append(case)
    return out


def cases() -> list[core.Case]:
    rich_head = _Lazy(lambda: head("rich"))
    out = [
        # The old 100,000-key scan exceeded five seconds on Linux (2026-10-12),
        # so Linux keeps that budget as the regression guard. On hosted Windows
        # the Python oracle alone exceeded it (batch run 38198996412), so
        # Windows uses the harness default.
        file_case("input-wide-object", ["verify", "--input", _home("wide.jsonl")],
                  files={"wide.jsonl": lambda: json.dumps({f"key-{n}": None for n in range(100000)}).encode("utf-8")},
                  timeout=30.0 if os.name == "nt" else 5.0),
        *[bank_export_case(f"export-iso-{name}", "rich", ["--since", value], since=since)
          for name, value, since in [
            ("week-date", "2026-W41-1", datetime(2026, 10, 5)),
            ("week-datetime", "2026-W41-6T12:00:00", datetime(2026, 10, 10, 12)),
            ("hour", "2026-10-11T12", datetime(2026, 10, 11, 12)),
            ("compact-offset", "2026-10-11T12:00:00+0100", datetime(2026, 10, 11, 11, tzinfo=timezone.utc)),
            ("hour-offset", "2026-10-11T12:00:00+01", datetime(2026, 10, 11, 11, tzinfo=timezone.utc)),
            ("comma-fraction", "2026-10-11T12:00:00,5+00:00",
             datetime(2026, 10, 11, 12, microsecond=500_000, tzinfo=timezone.utc)),
            ("compact-hour", "20261011T12", datetime(2026, 10, 11, 12)),
            ("second-offset", "2026-10-11T12:00:00-00:00:30",
             datetime(2026, 10, 11, 12, second=30, tzinfo=timezone.utc)),
        ]],
        # The pre-fix 100 MiB newline scan exceeded 10s on Linux (2026-10-12).
        file_case("input-long-line", ["verify", "--input", _home("long.jsonl")],
                  files={"long.jsonl": lambda: b" " * (100 * 1024 * 1024) + b"\n"},
                  timeout=10.0),
        file_case("input-windows-interior-drive", ["verify", "--input", r"subdir\C:\archive.jsonl"],
                  platforms=("windows",), note="Windows: pending CI"),
        file_case("input-windows-verbatim", ["verify", "--input", "\\\\?\\" + _home("./missing//archive.jsonl")],
                  platforms=("windows",), note="Windows: pending CI"),
        archive_case("input-surrogate-before-link", "surrogate-before-link"),
        file_case("root-delimiter", ["--", "verify"]),
        file_case("short-help-joined-tail", ["verify", "-hello=x"]),
        file_case("input-head-nondecimal-digit", ["verify", "--expect-head", "²:" + "a" * 64]),
        file_case("input-head-digit-limit", ["verify", "--expect-head", "0" * 4301 + "1:" + "a" * 64]),
        file_case("input-windows-leading-dot", ["verify", "--input", "./missing//archive.jsonl/"],
                  platforms=("windows",), note="Windows: pending CI"),
        export_file_case("export-output-loop", "loop",
                         diagnostic="board-audit: cannot create {out}: File exists",
                         platforms=("linux",), prepare=lambda arm: (arm.home / "loop").symlink_to("loop")),
        export_file_case("export-windows-long-name", "x" * 260,
                         diagnostic=_frozen_create_refusal,
                         platforms=("windows",), note="Windows: pending CI"),
        audit_no_bank_case("export-local-minute-no-bank", ["export", "--since", "2026-10-10T12:00"]),
        export_stdout_failure_case(
            "export-invalid-payload-prefix", "invalid_payload",
            "board-audit failed; the database error is not shown because it can "
            "include the connection string. Check PSEUDOLIFE_MCP_DATABASE_URL.",
        ),
        # verify --input: every event family the seeded chain carries
        archive_case("input-rich", "rich"),
        archive_case("input-rich-expect-head", "rich", "--expect-head", rich_head),
        file_case("input-expect-head-first", ["verify", "--expect-head", rich_head, "--input",
                                              _home("archive.jsonl")],
                  files={"archive.jsonl": lambda: archive("rich")}),
        archive_case("input-head-mismatch", "rich", "--expect-head",
                     _Lazy(lambda: head("rich", digest="0" * 64))),
        archive_case("input-head-missing", "rich", "--expect-head",
                     _Lazy(lambda: head("rich", seq=9999))),
        archive_case("input-head-19-digits", "rich", "--expect-head",
                     _Lazy(lambda: head("rich", seq=1000000000000000000))),
        archive_case("input-head-i64-max", "rich", "--expect-head",
                     _Lazy(lambda: head("rich", seq=9223372036854775807))),
        archive_case("input-head-19-digits-matches", "head-19-digits", "--expect-head",
                     _Lazy(lambda: "1000000000000000000:" +
                           json.loads(archive("head-19-digits").splitlines()[-1])["hash"])),
        archive_case("input-head-earlier", "rich", "--expect-head",
                     _Lazy(lambda: f"3:{export_rows('rich')[2]['hash']}")),
        archive_case("input-rich-crlf", "rich-crlf"),
        archive_case("input-broken-before-decode", "broken-before-decode"),
        archive_case("input-decode-before-broken", "decode-before-broken"),
        archive_case("input-rich-cr-blank", "rich-cr-blank"),
        archive_case("input-pruned-start-cut", "pruned"),
        archive_case("input-pruned-head-pruned", "pruned", "--expect-head", "1:" + "a" * 64),
        archive_case("input-legacy-v45", "legacy-v45"),
        archive_case("input-stripped", "stripped"),
        archive_case("input-blanked", "blanked"),
        archive_case("input-edited-body", "edited-body"),
        archive_case("input-resalted", "resalted"),
        archive_case("input-salt-only", "salt-only"),
        archive_case("input-wrong-type", "wrong-type"),
        archive_case("input-wrong-type-cr", "wrong-type-cr"),
        archive_case("input-seq-string", "seq-string"),
        archive_case("input-duplicate", "duplicate"),
        file_case("audit-reserved-number-key-duplicates",
                  ["verify", "--input", _home("archive.jsonl")],
                  files={"archive.jsonl": b'{"payload":{"$serde_json::private::Number":"1",'
                         b'"$serde_json::private::Number":"1"}}\n'}),
        archive_case("audit-reserved-number-key-duplicates-exported",
                     "reserved-number-key-duplicates"),
        archive_case("audit-reserved-number-key-duplicates-escaped",
                     "reserved-number-key-escaped"),
        archive_case("audit-reserved-number-key-single", "reserved-number-key-single"),
        archive_case("input-payload-edit", "payload-edit"),
        archive_case("input-late-garbage", "late-garbage"),
        archive_case("input-gap", "gap"),
        archive_case("input-link", "link"),
        archive_case("input-genesis", "genesis"),
        archive_case("input-unanchored", "unanchored"),
        archive_case("input-redact-kept-body", "redact-kept-body"),
        file_case("input-empty", ["verify", "--input", _home("archive.jsonl")],
                  files={"archive.jsonl": b""}),
        file_case("input-missing", ["verify", "--input", _home("missing.jsonl")]),
        file_case("input-pathlib", ["verify", "--input", _home("./missing//archive.jsonl/")]),
        file_case("input-directory", ["verify", "--input", "{HOME}"]),
        file_case("input-windows-slashes", ["verify", "--input", "./missing//archive.jsonl/"],
                  platforms=("windows",)),
        file_case("input-joined", ["verify", "--input=" + _home("archive.jsonl")],
                  files={"archive.jsonl": b""}),
        file_case("input-abbreviated", ["verify", "--in", _home("archive.jsonl")],
                  files={"archive.jsonl": b""}),
        file_case("input-repeated", ["verify", "--input", "missing", "--input", _home("archive.jsonl")],
                  files={"archive.jsonl": b""}),
        file_case("input-missing-value", ["verify", "--input"]),
        file_case("input-negative-unicode", ["verify", "--input", "-١.٥"], files={"-١.٥": b""}),
        file_case("input-unknown-with-space", ["verify", "--input", "--unknown name"], files={"--unknown name": b""}),
        file_case("input-zero-padded-head", ["verify", "--input", _home("archive.jsonl"), "--expect-head", "٠٠١:" + "a" * 64], files={"archive.jsonl": b""}),
        file_case("input-option-as-value", ["verify", "--input", "--unknown"]),
        file_case("input-bad-head", ["verify", "--expect-head=bad"]),
        file_case("input-unknown-option", ["verify", "--unknown=x"]),
        file_case("input-unrecognized", ["verify", "extra", "--unknown", "x"]),
        file_case("redact-required", ["redact"]),
        _abbreviated_redact_case(),
        file_case("export-bad-time", ["export", "--since", "NaN"]),
        *[file_case(f"export-bad-numeric-{name}", ["export", "--since=" + value])
          for name, value in [("inf", "inf"), ("negative-inf", "-Infinity"), ("overflow", "1e9999")]],
        file_case("export-invalid-nonnumeric", ["export", "--since", "not-a-time"]),
        audit_no_bank_case("export-iso-overwritten", ["export", "--since", "2026-W41-1", "--since", "0"]),
        file_case("audit-missing-action", []),
        file_case("audit-invalid-action", ["other"]),
        file_case("audit-root-unknown", ["--unknown", "verify"]),
        file_case("audit-root-help", ["--help"]),
        file_case("export-help", ["export", "--he"]),
        file_case("redact-help", ["redact", "-hello"]),
        _fixed_stats_case("stats-help", ["stats", "--help"]),
        _fixed_stats_case("stats-invalid-option", ["stats", "--unknown"]),
        file_case("help-explicit-value", ["verify", "--help=x"]),
        bank_export_case("export-repeated-abbreviated", "rich", ["--ta=t2", "--task", "t1"], task="t1"),
        bank_export_case("export-negative-time", "rich", ["--since", "-5"], since=-5),
        bank_export_case("export-underscore-time", "rich", ["--since=١_٧٠٠_٠٠٠_٠٠٠"], since=T0),
        bank_export_case("export-payload-domain", "payload"),
        bank_verify_case("bank-verify-payload-domain", "payload"),
        bank_verify_case("bank-verify-surrogate-send", "surrogate_send"),
        bank_export_case("export-surrogate-send", "surrogate_send"),
        redact_projected_case("redact-surrogate-send", "surrogate_send",
                              _Lazy(lambda: ids("surrogate_send")["message"]), "old paste",
                              seed_property="lone-surrogate"),
        redact_projected_case("redact-nonfinite-fanout", "payload_send",
                              _Lazy(lambda: ids("payload_send")["message"]), "old paste",
                              seed_property="nonfinite-fanout"),
        redact_case("redact-payload-domain", "payload", _Lazy(lambda: ids("payload")["legacy"]),
                    "old paste", refusal="body_in_hashed_payload"),
        file_case("input-payload-domain", ["verify", "--input", _home("archive.jsonl")],
                  files={"archive.jsonl": lambda: export_bytes("payload")}),
        # verify against the bank
        bank_verify_case("bank-verify-rich", "rich"),
        bank_verify_case("bank-verify-expect-head", "rich", expect_head=rich_head),
        bank_verify_case("bank-verify-head-mismatch", "rich",
                         expect_head=_Lazy(lambda: head("rich", digest="0" * 64))),
        bank_verify_case("bank-verify-head-missing", "rich",
                         expect_head=_Lazy(lambda: head("rich", seq=9999))),
        bank_verify_case("bank-verify-pruned", "pruned"),
        bank_verify_case("bank-verify-head-pruned", "pruned", expect_head="2:" + "b" * 64),
        bank_verify_case("bank-verify-empty", "empty"),
        bank_verify_case("bank-verify-nolog", "nolog"),
        bank_verify_case("bank-verify-tampered", "tampered"),
        bank_verify_case("bank-verify-legacy", "legacy"),
        # export
        bank_export_case("export-all", "rich"),
        bank_export_case("export-task", "rich", ["--task", "t1"], task="t1"),
        bank_export_case("export-project-since", "rich", ["--project", "p", "--since", "1700000050"],
                         project="p", since=T0 + 50),
        bank_export_case("export-until-fraction", "rich", ["--until", "1700000044.5"], until=T0 + 44.5),
        bank_export_case("export-since-iso", "rich",
                         ["--since", "2023-11-14T22:14:20+00:00", "--until", "2023-11-14T23:15:00+01:00"],
                         since=T0 + 60, until=T0 + 100),
        bank_export_case("export-since-iso-fraction", "rich",
                         ["--since", "2023-11-14T22:14:20.500000+00:00", "--until",
                          "2023-11-14T23:15:00.000001+01:00"], since=T0 + 60.5, until=T0 + 100.000001),
        bank_export_case("export-since-iso-long-fraction", "rich",
                         ["--since", "2023-11-14T22:14:20.123456789+00:00"], since=T0 + 60.123456),
        bank_export_case("export-until-iso-epoch-boundary", "fraction",
                         ["--until", "1970-01-01T00:00:59.999999+00:01"], until=-0.000001),
        bank_export_case("export-agent-full", "rich", ["--agent", _rich("b")], agent=_rich("b")),
        bank_export_case("export-agent-prefix", "rich", ["--agent", _rich("a", cut=8)], agent=_rich("a")),
        bank_export_case("export-agent-literal", "rich", ["--agent", "alice"], agent="alice"),
        bank_export_case("export-agent-unknown", "rich", ["--agent", "ffffffff0"], recorded=True),
        export_file_case("export-agent-unknown-out", "out.jsonl", agent="ffffffff0", leftover=True,
                         diagnostic="board-audit: no agent id starts with ffffffff0"),
        export_file_case("export-windows-invalid-name", "bad<name.jsonl",
                         diagnostic="board-audit: cannot create {out}: Invalid argument",
                         note="Windows: pending CI", platforms=("windows",)),
        export_file_case("export-out", "out.jsonl"),
        export_file_case("export-out-filtered", "out.jsonl", task="t3"),
        export_file_case("export-out-exists", "out.jsonl", files={"out.jsonl": b"keep"},
                         diagnostic="board-audit: {out} exists; export never replaces a file"),
        bank_export_case("export-pruned", "pruned"),
        bank_export_case("export-legacy", "legacy"),
        bank_export_case("export-empty", "empty"),
        bank_export_case("export-nolog", "nolog", recorded=True),
        export_stdout_failure_case("export-stdout-closed", "rich",
                                   "board-audit: the output was closed before the export finished",
                                   stdout_closed=True),
        # redact, each arm on its own copy
        redact_removed_case("redact-removed", "to_redact", "wrong paste ✓"),
        redact_removed_case("redact-prefix", "to_redact", "wrong paste", cut=10),
        redact_projected_case("redact-fanout-siblings", "rich", _rich("hello", index=0),
                              "sent to everyone by mistake", siblings=(_rich("hello", index=1),)),
        redact_projected_case("redact-kept-v45", "rich", _rich("v45"), "pasted by mistake", audit_copy="kept"),
        redact_projected_case("redact-gone", "pruned", _Lazy(lambda: ids("pruned")["old"]),
                              "pasted by mistake", audit_copy="gone"),
        redact_case("redact-legacy-refused", "rich", _rich("legacy"), "too old", refusal="body_in_hashed_payload"),
        redact_case("redact-already", "rich", _rich("redacted"), "again", refusal="already_redacted"),
        redact_case("redact-unknown", "rich", "0" * 32, "unknown", refusal="message_not_found"),
        redact_case("redact-unknown-prefix", "rich", "fffffffff", "unknown", refusal="message_not_found"),
        redact_case("redact-invalid-id", "rich", "bad.id", "why", refusal="invalid_message_id"),
        redact_case("redact-reason-control", "rich", _rich("to_redact"), "bell\x07", refusal="invalid_reason"),
        redact_case("redact-reason-long", "rich", _rich("to_redact"), "x" * 241, refusal="invalid_reason"),
        redact_case("redact-reason-blank", "rich", _rich("to_redact"), "  ", refusal="invalid_reason"),
        redact_case("redact-reason-secret", "rich", _rich("to_redact"),
                    "token=" + "q7Hd2kLm9Pz4" + "Rt6Wv8Xy1Bc3", refusal="secret_like_body"),
        redact_case("redact-busy", "rich", _rich("to_redact"), "wrong paste", hold=True, refusal="busy"),
        redact_case("redact-nolog", "nolog", "0" * 32, "why", refusal="nolog"),
        # Historical case ids remain stable across native implementation.
        *stats_cases(),
        *transport_cases(),
        file_case("verify-help", ["verify", "--help"]),
        bank_export_case("export-joined-option", "rich", ["--task=t1"], task="t1"),
        bank_export_case("export-float-spelling", "rich", ["--since", "1.7e9"], since=T0),
        # Empty-chain verification pins native DSN options without random head bytes.
        bank_verify_case("defer-dsn-option", "empty",
                         env={"PSEUDOLIFE_MCP_DATABASE_URL": _bank.url(_name("empty"))
                              + "?application_name=audit"}),
        export_file_case("export-out-missing-dir-before-agent", "missing/out.jsonl", agent="ffffffff0",
                         diagnostic="board-audit: cannot create {out}: No such file or directory"),
        audit_no_bank_case("no-bank", ["verify"]),
        file_case("input-not-utf8", ["verify", "--input", _home("archive.jsonl")],
                  files={"archive.jsonl": b'{"seq": 1}\n\xff\n'}),
        file_case("input-partial-utf8-after-cr", ["verify", "--input", _home("archive.jsonl")],
                  files={"archive.jsonl": b"{}\r\xf0"}),
        file_case("input-split-utf8", ["verify", "--input", _home("archive.jsonl")],
                  files={"archive.jsonl": b" " * 8191 + "雪\n".encode("utf-8")}),
        file_case("input-not-json", ["verify", "--input", _home("archive.jsonl")],
                  files={"archive.jsonl": b"not JSON\n"}),
        # review round 1 (O1): a repeated key is reported when its object closes
        archive_case("input-duplicate-nested-then-long-int", "duplicate-nested-then-long-int"),
        archive_case("input-duplicate-truncated", "duplicate-truncated"),
        archive_case("input-duplicate-then-long-int", "duplicate-then-long-int"),
        # pathlib reprints a drive-relative spelling natively
        file_case("input-drive-relative",
                  ["verify", "--input", "C:.\\missing-audit-archive.jsonl"],
                  platforms=("windows",)),
        # (O2) a stdout that refuses the report
        frozen_closed_stdout_verify_case("defer-bank-verify-stdout-closed", "rich"),
        redact_projected_case("redact-stdout-closed", "rich", _rich("to_redact"), "wrong paste",
                              rules=("audit-redact-clock", "audit-stdout-closed-trailer"),
                              stdout_closed=True),
        # (O4) coverage: LEAST(expires_at, now), ambiguous prefixes, pre-v46,
        # the vacuum-skip path with warnings hidden by default
        redact_removed_case("redact-future-expiry", "future", "wrong paste"),
        bank_export_case("export-agent-ambiguous", "rich", ["--agent", "ab12cd34"], recorded=True),
        export_file_case(
            "export-agent-ambiguous-out", "out.jsonl", agent="ab12cd34ef5", leftover=True,
            diagnostic="board-audit: --agent ab12cd34ef5 matches several ids: "
                       "ab12cd34ef56, ab12cd34ef57; give a longer prefix",
        ),
        bank_export_case("export-agent-twin", "rich", ["--agent", "ab12cd34ef57"],
                         agent="ab12cd34ef57000000000000000000a2"),
        redact_case("redact-ambiguous-prefix", "rich", "cd34ef56", "wrong paste",
                    refusal="ambiguous_message_id"),
        redact_projected_case("redact-twin-prefix", "rich", "cd34ef56ab13", "wrong paste",
                              resolved_id=_fixture_message_prefix("cd34ef56ab13")),
        bank_verify_case("bank-verify-prev46", "prev46"),
        bank_export_case("export-prev46", "prev46"),
        redact_case("redact-prev46", "prev46", _Lazy(lambda: ids("legacy")["legacy"]), "why", refusal="prev46"),
        redact_projected_case("redact-vacuum-warning", "rich", _rich("to_redact"), "wrong paste",
                              prepare=VACUUM_WARNING),
    ]
    return out

"""CLI-AUDIT: ``pseudolife-mcp board-audit`` (board_audit_cli.py).

Banks are seeded through the oracle's own writers: every audit row comes from
``CoordinationStore`` (the board's write path) on a fixed test clock, or from
the oracle test suite's helpers (``tests.test_coordination_audit._legacy_send``
/ ``_sent_by_v45``). Nothing here inserts an audit row by hand; the one
tampered bank changes a row's ``task`` exactly as
``test_board_audit_cli.test_verify_prints_the_head_and_fails_on_tampering``
does. Agent ids, credentials, message ids and body salts stay random, so the
two arms of a case read one seeded bank (read actions, checked unchanged after
each arm) or each get a ``CREATE DATABASE ... TEMPLATE`` copy of it (redact).
Archives are the oracle's own ``board-audit export --out`` of those banks,
edited the way the oracle tests edit them.
"""

from __future__ import annotations

import atexit
import base64
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .. import core, normalize
from ..mutants import Mutant
from . import _bank

DAY = 86400
# Fixed seed clock: far enough in the past that a redaction's
# ``expires_at=LEAST(expires_at, now)`` never moves a seeded row.
T0 = 1_700_000_000.0


class _Storage:
    """The same minimal storage the oracle tests hand ``CoordinationStore``."""

    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def _txn(self):
        with self.conn.transaction():
            yield


def _store(conn):
    from pseudolife_memory.storage.coordination import CoordinationStore  # noqa: PLC0415
    now = [T0]
    store = CoordinationStore(_Storage(conn), clock=lambda: now[0])
    store.test_time = now
    return store


def _creds(agent, principal="alice"):
    return principal, agent["agent_id"], agent["credential"]


def seed_rich(conn) -> dict:
    """Many event families on one chain; returns ids the cases address."""
    import pytest  # noqa: PLC0415  (oracle test helpers take a monkeypatch)
    from tests.test_coordination_audit import _legacy_send, _sent_by_v45  # noqa: PLC0415
    store = _store(conn)
    tick = store.test_time

    def later(seconds=7.25):
        tick[0] += seconds

    a = store.register("alice", label="claude-code", project="p", task="t1",
                       status="starting ✓", wake_enabled=True)
    later()
    b = store.register("alice", label="codex", project="p", task="t2", wake_enabled=True)
    later()
    c = store.register("bob", project="q", task="t3", name="third")
    later()
    store.update(*_creds(a), status="suite=queued #3", expect=600)
    later()
    store.update(*_creds(b), park_reason="needs_resource", park_needs="the gpu",
                 status="waiting for the gpu")
    later()
    attached = store.attach(*_creds(b), attachment_id="live", wake_enabled=True)
    # Attached too, so the fan-out to "all" below lands as two copies.
    attached_a = store.attach(*_creds(a), attachment_id="live-a")
    later()
    direct = store.send(*_creds(a), to=b["agent_id"], text="from t1 naïve ✓ \"quoted\"",
                        request_id="r1")
    later(0.5)
    store.send(*_creds(a), to=b["agent_id"], text="the gpu is free", request_id="r2",
               clears="the gpu")
    later()
    burst = store.send(*_creds(a), to="project:p", text="to the whole project",
                       request_id="burst")
    later()
    hello = store.send(*_creds(c, "bob"), to="all", text="hello everyone",
                       request_id="hello", urgent=True)
    later()
    page = store.receive(*_creds(b))
    later()
    for message in page["messages"]:
        store.ack(*_creds(b), message_id=message["message_id"])
    store.detach(*_creds(b), attachment_id="live", generation=attached["generation"])
    store.detach(*_creds(a), attachment_id="live-a", generation=attached_a["generation"])
    later()
    store.acquire_lease(*_creds(a), name="gpu", ttl=300, purpose="bench")
    later()
    store.acquire_lease(*_creds(b), name="gpu", ttl=120, expect=900)
    later()
    store.release_lease(*_creds(a), name="gpu")
    later()
    store.acquire_lease(*_creds(c, "bob"), name="suite", ttl=600)
    store.break_lease("suite")
    later()
    store.grant_conductor("p", a["agent_id"], hold=3600)
    later()
    store.woke(b["agent_id"], "alice")
    later()
    store.update(*_creds(a), park_reason=None, status="done for now")
    later()
    redacted = store.send(*_creds(c, "bob"), to=a["agent_id"], text="pasted by mistake",
                          request_id="oops")["message_id"]
    later()
    store.redact(redacted, "wrong paste")
    later()
    # A day on: live bodies expire (``expire``), leases lapse (``lease_expire``).
    tick[0] += DAY + 1
    store.prune()
    later()
    legacy = _legacy_send(store, a, c)
    later()
    with pytest.MonkeyPatch.context() as patch:
        v45 = _sent_by_v45(patch, store, a, c, "sent by a v45 daemon")["message_id"]
    later()
    to_redact = store.send(*_creds(a), to=c["agent_id"], text="redact me later",
                           request_id="later")["message_id"]
    later()
    # Sent at seeding's wall clock, so it expires a day after any run and a
    # redaction's LEAST(expires_at, now) moves it to the arm's own clock.
    # (The store caps expiry at a day after its clock.) Later rows keep it.
    tick[0] = max(tick[0], time.time())
    future = store.send(*_creds(a), to=c["agent_id"], text="lives past the run",
                        request_id="future")["message_id"]
    later()
    # Ids sharing a prefix, so prefix resolution is ambiguous. uuid4 is the
    # writers' only source of ids; the rows are still the store's own.
    with _chosen_uuids(AGENT_TWINS):
        twins = [store.register("carol", label="twin")["agent_id"] for _ in AGENT_TWINS]
    with _chosen_uuids(MESSAGE_TWINS):
        twin_mail = [store.send(*_creds(a), to=c["agent_id"], text=f"twin {n}",
                                request_id=f"twin-{n}")["message_id"]
                     for n in range(len(MESSAGE_TWINS))]
    if twins != AGENT_TWINS or twin_mail != MESSAGE_TWINS:
        raise RuntimeError(f"chosen ids not used: {twins} {twin_mail}")
    later()
    store.update(*_creds(c, "bob"), status="after the prune")
    return {"a": a["agent_id"], "b": b["agent_id"], "c": c["agent_id"],
            "direct": direct["message_id"], "legacy": legacy, "v45": v45,
            "redacted": redacted, "to_redact": to_redact, "future": future,
            "burst": [r["message_id"] for r in burst["receipts"]],
            "hello": [r["message_id"] for r in hello["receipts"]]}


AGENT_TWINS = ["ab12cd34ef56000000000000000000a1", "ab12cd34ef57000000000000000000a2"]
MESSAGE_TWINS = ["cd34ef56ab1200000000000000000001", "cd34ef56ab1300000000000000000002"]


@contextmanager
def _chosen_uuids(hexes):
    """``uuid.uuid4`` returns these, in order, then the real thing again."""
    import uuid  # noqa: PLC0415
    queue = [uuid.UUID(value) for value in hexes]
    real = uuid.uuid4
    uuid.uuid4 = lambda: queue.pop(0) if queue else real()
    try:
        yield
    finally:
        uuid.uuid4 = real


def seed_pruned(conn) -> dict:
    """A chain whose oldest rows audit retention removed (an ``audit_prune``
    cut the log now starts after)."""
    store = _store(conn)
    tick = store.test_time
    a = store.register("alice", project="p")
    b = store.register("alice", project="p")
    old = store.send(*_creds(a), to=b["agent_id"], text="old mail", request_id="old")
    tick[0] += 3 * DAY
    store.update(*_creds(a), status="still here")
    store.prune(audit_retention_days=1)
    tick[0] += 60
    store.update(*_creds(b), status="after the cut")
    return {"a": a["agent_id"], "b": b["agent_id"], "old": old["message_id"]}


def seed_legacy(conn) -> dict:
    """Only sends from before v46 (bodies inside the hashed payload), so an
    export without ``body`` fields still verifies."""
    from tests.test_coordination_audit import _legacy_send  # noqa: PLC0415
    store = _store(conn)
    a = store.register("alice")
    b = store.register("alice")
    message = _legacy_send(store, a, b, text="an old body ✓")
    store.test_time[0] += 30
    store.update(*_creds(a), status="later")
    return {"legacy": message}


def seed_payload_domain(conn) -> dict:
    """Historical payload shapes, appended and hashed by the oracle writer."""
    store = _store(conn)
    with store.storage._txn():
        event = store._event("register", {
            "nan": float("nan"), "positive": float("inf"), "negative": -float("inf"),
            "integer": 10 ** 200, "deep": json.loads("[" * 150 + "0" + "]" * 150),
            "ordered": {"雪": 1, "😀": 2, "a": 3},
        })
        legacy = store._event("send", {"text": "old body", "odd": float("nan")},
                              message_id="ef12ab34cd5600000000000000000001")
        surrogate = store._event("register", {})
        surrogate["payload"] = r'{"odd":"\ud800","text":"ascii 雪"}'
        store._append([event, legacy, surrogate], T0)
    return {"legacy": legacy["message_id"]}


def seed_payload_send(conn, *, surrogate=False) -> dict:
    """A real send whose stored payload uses Python's historical JSON domain."""
    store = _store(conn)
    a = store.register("alice", project="p")
    b = store.register("alice", project="p")
    store.attach(*_creds(b), attachment_id="historical")
    event = store._event

    def historical_event(kind, payload, **kwargs):
        result = event(kind, payload, **kwargs)
        if kind == "send":
            payload = dict(payload, odd="\ud800" if surrogate else float("nan"))
            result["payload"] = json.dumps(payload, ensure_ascii=True, sort_keys=True,
                                           separators=(",", ":"))
        return result

    store._event = historical_event
    sent = store.send(*_creds(a), to=b["agent_id"] if surrogate else "project:p",
                      text="old send", request_id="historical")
    message = sent["message_id"] if surrogate else sent["receipts"][0]["message_id"]
    return {"message": message}


def seed_invalid_payload(conn) -> dict:
    """An old stored payload which export cannot decode."""
    store = _store(conn)
    with store.storage._txn():
        event = store._event("register", {})
        event["payload"] = "{"
        store._append([store._event("register", {}), event], T0)
    return {}


def seed_fraction_boundary(conn) -> dict:
    """One oracle-written event between two differently rounded ISO limits."""
    store = _store(conn)
    store.test_time[0] = -1.0000000000287557e-6
    store.register("alice", project="p")
    return {}


# --- disposable banks --------------------------------------------------------

PREFIX = "pl_cf_w1c_audit_"
SEEDERS = {"rich": seed_rich, "pruned": seed_pruned, "legacy": seed_legacy,
           "fraction": seed_fraction_boundary, "payload": seed_payload_domain,
           "payload_send": seed_payload_send,
           "invalid_payload": seed_invalid_payload,
           "surrogate_send": lambda conn: seed_payload_send(conn, surrogate=True),
           "stats": lambda conn: seed_stats(conn)}
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


def _copy(source: str, target: str) -> None:
    _bank.url(target)  # validates the name
    _drop(target)
    _CREATED.add(target)
    with _bank._admin() as conn:
        conn.execute(f'CREATE DATABASE "{target}" TEMPLATE "{source}"')


def ensure(kind: str) -> str:
    """The seeded bank of this kind (created once per harness process)."""
    name = _name(kind)
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
            conn.execute("UPDATE coordination_events SET task='elsewhere' WHERE seq=2")
    elif kind == "prev49":
        _copy(ensure("rich"), name)
        with _bank.connect(name, autocommit=True) as conn:
            conn.execute("DROP TABLE coordination_wakes")
    elif kind == "prev46":
        # A restored v42-v45 bank read before any v46 daemon started, made
        # as test_board_audit_cli.test_a_bank_before_v46_... makes it.
        _copy(ensure("legacy"), name)
        with _bank.connect(name, autocommit=True) as conn:
            conn.execute("ALTER TABLE coordination_events DROP COLUMN body")
    else:
        _bank.create(name)
        with _bank.connect(name, autocommit=True) as conn:
            _IDS[kind] = SEEDERS[kind](conn)
    _BASELINE[name] = _bank.dump(name)
    return name


def ids(kind: str) -> dict:
    ensure(kind)
    return _IDS[kind]


def export_bytes(kind: str) -> bytes:
    """The oracle's own ``board-audit export --out`` of a seeded bank."""
    _bank.NAMES.require_process()
    if kind not in _ARCHIVES:
        from pseudolife_memory.board_audit_cli import main  # noqa: PLC0415
        name = ensure(kind)
        with tempfile.TemporaryDirectory() as scratch:
            target = Path(scratch) / "export.jsonl"
            previous = os.environ.get("PSEUDOLIFE_MCP_DATABASE_URL")
            os.environ["PSEUDOLIFE_MCP_DATABASE_URL"] = _bank.url(name)
            try:
                if main(["export", "--out", str(target)]) != 0:
                    raise RuntimeError(f"oracle export of {name} failed")
            finally:
                if previous is None:
                    os.environ.pop("PSEUDOLIFE_MCP_DATABASE_URL", None)
                else:
                    os.environ["PSEUDOLIFE_MCP_DATABASE_URL"] = previous
            _ARCHIVES[kind] = target.read_bytes()
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
        from pseudolife_memory.storage.coordination import (  # noqa: PLC0415
            audit_hash, verify_audit_chain)
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
        from pseudolife_memory.storage.coordination import audit_hash  # noqa: PLC0415
        # Keep one complete exported row, hashed by the oracle with an ordinary
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

DEFERRAL = ("board-audit: this path is deferred; native board-audit covers canonical "
            "verify, export and redact\n")


def _native(text: str) -> bytes:
    return (text.replace("\n", "\r\n") if core.WINDOWS else text).encode("utf-8")


@normalize.rule("rust-deferral")
def rust_deferral(obs: dict) -> None:
    """A declared deferral case: the candidate must print the deferral line,
    exit 1 and change nothing. The oracle arm still runs, but its exit and
    streams are replaced by that expectation and its files by the home as
    setup left it; the database is compared as observed (unchanged)."""
    if obs.get("arm") != "python":
        return
    obs["exit"] = 1
    obs["stdout"] = ""
    obs["stderr"] = base64.b64encode(_native(DEFERRAL)).decode()
    obs["files"] = dict(obs.get("before", {}))


@normalize.rule("audit-redact-clock")
def audit_redact_clock(obs: dict) -> None:
    """A redaction's own event is stamped with the arm's wall clock, which
    is hashed. Validated, then tokenized: the newest event must be a redact
    whose created_at lies inside this arm's run window and whose hash is the
    oracle's ``audit_hash`` of that row; its created_at and hash become
    tokens. Every printed hash reference must be this arm's own validated
    hash before it is replaced: stdout's ``redact_hash`` and ``expect_head``
    (when stdout carries the result), the stderr ``--expect-head SEQ:HASH``
    note, and no other 64-hex string or literal token anywhere. The live
    message row's ``expires_at``, when a redaction's ``LEAST(expires_at,
    now)`` moved it to exactly that clock, is tokenized the same way."""
    from pseudolife_memory.storage.coordination import audit_hash  # noqa: PLC0415
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
    trailer. Only that exact trailer, with exit 120, is removed."""
    stderr = base64.b64decode(obs["stderr"])
    if obs["exit"] == 120 and _TRAILER.search(stderr):
        obs["stderr"] = base64.b64encode(_TRAILER.sub(b"", stderr)).decode()


@normalize.rule("audit-stats-clock")
def stats_clock(obs):
    """Validate runtime fields before substituting only clock/version tokens.

    Every metric, source, key order, numeric spelling and file byte remains
    compared. Fixed time windows are never replaced.
    """
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
    return case


def archive_case(case_id, variant, *extra, rules=(), stdout_closed=False):
    return file_case(case_id, ["verify", "--input", _home("archive.jsonl"), *extra],
                     files={"archive.jsonl": lambda: archive(variant)}, rules=rules,
                     stdout_closed=stdout_closed)


REDACT_DB = _bank.name(PREFIX + "rd")


def redact_case(case_id, source, message, reason, *, rules=("audit-redact-clock",),
                hold=False, prepare=(), stdout_closed=False, note=""):
    """A redaction on this arm's own TEMPLATE copy of a seeded bank;
    ``prepare`` statements run on that copy first (test instruments only)."""
    original = ["board-audit", "redact", "--message-id",
                message if isinstance(message, _Lazy) or isinstance(message, str)
                else _Lazy(message), "--reason", reason]

    def setup(arm):
        _copy(ensure(source), REDACT_DB)
        if prepare:
            with _bank.connect(REDACT_DB, autocommit=True) as conn:
                for statement in prepare:
                    conn.execute(statement.replace("{DB}", REDACT_DB))
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
    return case


# Test instrument for the vacuum-skip path (the test login owns every table,
# so Postgres never skips a step for it): ANALYZE evaluates an expression
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


def _pruned(key):
    return _Lazy(lambda: ids("pruned")[key])


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
    """Oracle-chained events, plus a controlled historical wakes-table fixture."""
    store = _store(conn)
    events = stats_events()
    b = "b" * 32
    events.extend([
        dict(events[0], seq=100, event="woke", created_at=1410.0, agent_id=b, payload={}),
        dict(events[0], seq=101, event="read", created_at=1411.0, agent_id=b, payload={"path":"delivery"}),
        dict(events[0], seq=102, event="update", created_at=1420.0, agent_id=b, actor="daemon", payload={}),
    ])
    for row in sorted(events,key=lambda r:(r["created_at"],r["seq"])):
        event = store._event(row["event"],row["payload"],actor=row["actor"],principal=row["principal"],
                             agent_id=row["agent_id"],recipient=row["recipient_agent_id"],project="fixture",
                             task="stats",message_id=row["message_id"])
        with store.storage._txn():
            store._append([event],row["created_at"])
    for n,(decision,created,served) in enumerate([
        ("rung",1400.0,1405.0),("nudged",1410.0,None),("rung",1450.0,5000.0),
        ("rung",900.0,1000.0),("hinted",1460.0,1470.0),
    ]):
        conn.execute("INSERT INTO coordination_wakes (recipient_agent_id,sender_agent_id,message_id,decision,ring_at,created_at,served_at) "
                     "VALUES (%s,%s,%s,%s,%s,%s,%s)", (b,"a"*32,f"m{n}",decision,created,created,served))
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


def stats_fixture():
    """Generate deterministic unit-test goldens by executing the Python oracle."""
    from pseudolife_memory.board_audit_stats import compute_stats
    events = stats_events()
    integer_events = [dict(e,created_at=int(e["created_at"])) for e in events]
    durations = []
    for line in stats_durations().splitlines():
        try:
            durations.append(json.loads(line))
        except ValueError:
            pass
    b = "b"*32
    wakes = [{"recipient_agent_id":b,"decision":d,"created_at":c,"served_at":s}
             for d,c,s in [("rung",1400.0,1401.0),("nudged",1410.0,None),("rung",1450.0,5000.0)]]
    result = []
    for name, rows, rings, records in [("empty",[],[],[]),("telemetry",events,wakes,durations),
                                       ("integer-times",integer_events,wakes,durations)]:
        report = compute_stats(rows,rings,records,since=1000.0,until=5000.0,now=5000.5,
                               version="fixture",sources={"events":"export","wakes":"fixture","durations":"file"})
        result.append(dict(name=name,events=rows,wakes=rings,durations=records,report=report,
                           line=json.dumps(report,ensure_ascii=True,separators=(",",":"))))
    return result


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
    import sys
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
                case = core.Case(f"transport-{engine.replace('<no value>', 'unlabelled')}-{action}-{exit_code}",
                                 argv,env=env,setup=setup,after=after,programs=PROGRAMS)
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
        *[read_case(f"export-iso-{name}", "rich", ["export", "--since", value]) for name, value in [
            ("week-date", "2026-W41-1"),
            ("week-datetime", "2026-W41-6T12:00:00"),
            ("hour", "2026-10-11T12"),
            ("compact-offset", "2026-10-11T12:00:00+0100"),
            ("hour-offset", "2026-10-11T12:00:00+01"),
            ("comma-fraction", "2026-10-11T12:00:00,5+00:00"),
            ("compact-hour", "20261011T12"),
            ("second-offset", "2026-10-11T12:00:00-00:00:30"),
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
        read_case("export-output-loop", "rich", ["export", "--out", _home("loop")],
                  platforms=("linux",), prepare=lambda arm: (arm.home / "loop").symlink_to("loop")),
        read_case("export-windows-long-name", "rich", ["export", "--out", _home("x" * 260)],
                  platforms=("windows",), note="Windows: pending CI"),
        file_case("export-local-minute-no-bank", ["export", "--since", "2026-10-10T12:00"]),
        read_case("export-invalid-payload-prefix", "invalid_payload", ["export"]),
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
        read_case("redact-abbreviated", "rich", ["redact", "--m", "x", "--r", "why"]),
        file_case("export-bad-time", ["export", "--since", "NaN"]),
        *[file_case(f"export-bad-numeric-{name}", ["export", "--since=" + value])
          for name, value in [("inf", "inf"), ("negative-inf", "-Infinity"), ("overflow", "1e9999")]],
        file_case("export-invalid-nonnumeric", ["export", "--since", "not-a-time"]),
        file_case("export-iso-overwritten", ["export", "--since", "2026-W41-1", "--since", "0"]),
        file_case("audit-missing-action", []),
        file_case("audit-invalid-action", ["other"]),
        file_case("audit-root-unknown", ["--unknown", "verify"]),
        file_case("audit-root-help", ["--help"]),
        file_case("export-help", ["export", "--he"]),
        file_case("redact-help", ["redact", "-hello"]),
        file_case("stats-help", ["stats", "--help"]),
        file_case("stats-invalid-option", ["stats", "--unknown"]),
        file_case("help-explicit-value", ["verify", "--help=x"]),
        read_case("export-repeated-abbreviated", "rich", ["export", "--ta=t2", "--task", "t1"]),
        read_case("export-negative-time", "rich", ["export", "--since", "-5"]),
        read_case("export-underscore-time", "rich", ["export", "--since=١_٧٠٠_٠٠٠_٠٠٠"]),
        read_case("export-payload-domain", "payload", ["export"]),
        read_case("export-create-before-payload", "invalid_payload",
                  ["export", "--out", _home("missing/out.jsonl")]),
        read_case("export-invalid-payload-out", "invalid_payload",
                  ["export", "--out", _home("out.jsonl")]),
        read_case("bank-verify-payload-domain", "payload", ["verify"]),
        read_case("bank-verify-surrogate-send", "surrogate_send", ["verify"]),
        read_case("export-surrogate-send", "surrogate_send", ["export"]),
        redact_case("redact-surrogate-send", "surrogate_send", _Lazy(lambda: ids("surrogate_send")["message"]), "old paste"),
        redact_case("redact-nonfinite-fanout", "payload_send", _Lazy(lambda: ids("payload_send")["message"]), "old paste"),
        redact_case("redact-payload-domain", "payload", _Lazy(lambda: ids("payload")["legacy"]), "old paste"),
        file_case("input-payload-domain", ["verify", "--input", _home("archive.jsonl")],
                  files={"archive.jsonl": lambda: export_bytes("payload")}),
        # verify against the bank
        read_case("bank-verify-rich", "rich", ["verify"]),
        read_case("bank-verify-expect-head", "rich", ["verify", "--expect-head", rich_head]),
        read_case("bank-verify-head-mismatch", "rich",
                  ["verify", "--expect-head", _Lazy(lambda: head("rich", digest="0" * 64))]),
        read_case("bank-verify-head-missing", "rich",
                  ["verify", "--expect-head", _Lazy(lambda: head("rich", seq=9999))]),
        read_case("bank-verify-pruned", "pruned", ["verify"]),
        read_case("bank-verify-head-pruned", "pruned",
                  ["verify", "--expect-head", "2:" + "b" * 64]),
        read_case("bank-verify-empty", "empty", ["verify"]),
        read_case("bank-verify-nolog", "nolog", ["verify"]),
        read_case("bank-verify-tampered", "tampered", ["verify"]),
        read_case("bank-verify-legacy", "legacy", ["verify"]),
        # export
        read_case("export-all", "rich", ["export"]),
        read_case("export-task", "rich", ["export", "--task", "t1"]),
        read_case("export-project-since", "rich",
                  ["export", "--project", "p", "--since", "1700000050"]),
        read_case("export-until-fraction", "rich", ["export", "--until", "1700000044.5"]),
        read_case("export-since-iso", "rich",
                  ["export", "--since", "2023-11-14T22:14:20+00:00", "--until",
                   "2023-11-14T23:15:00+01:00"]),
        read_case("export-since-iso-fraction", "rich",
                  ["export", "--since", "2023-11-14T22:14:20.500000+00:00", "--until",
                   "2023-11-14T23:15:00.000001+01:00"]),
        read_case("export-since-iso-long-fraction", "rich",
                  ["export", "--since", "2023-11-14T22:14:20.123456789+00:00"]),
        read_case("export-until-iso-epoch-boundary", "fraction",
                  ["export", "--until", "1970-01-01T00:00:59.999999+00:01"]),
        read_case("export-agent-full", "rich", ["export", "--agent", _rich("b")]),
        read_case("export-agent-prefix", "rich", ["export", "--agent", _rich("a", cut=8)]),
        read_case("export-agent-literal", "rich", ["export", "--agent", "alice"]),
        read_case("export-agent-unknown", "rich", ["export", "--agent", "ffffffff0"]),
        read_case("export-agent-unknown-out", "rich",
                  ["export", "--agent", "ffffffff0", "--out", _home("out.jsonl")]),
        read_case("export-windows-invalid-name", "rich", ["export", "--out", _home("bad<name.jsonl")],
                  note="Windows: pending CI", platforms=("windows",)),
        read_case("export-out", "rich", ["export", "--out", _home("out.jsonl")]),
        read_case("export-out-filtered", "rich",
                  ["export", "--out", _home("out.jsonl"), "--task", "t3"]),
        read_case("export-out-exists", "rich", ["export", "--out", _home("out.jsonl")],
                  files={"out.jsonl": b"keep"}),
        read_case("export-pruned", "pruned", ["export"]),
        read_case("export-legacy", "legacy", ["export"]),
        read_case("export-empty", "empty", ["export"]),
        read_case("export-nolog", "nolog", ["export"]),
        read_case("export-stdout-closed", "rich", ["export"], stdout_closed=True),
        # redact, each arm on its own copy
        redact_case("redact-removed", "rich", _rich("to_redact"), "wrong paste ✓"),
        redact_case("redact-prefix", "rich", _rich("to_redact", cut=10), "wrong paste"),
        redact_case("redact-fanout-siblings", "rich", _rich("hello", index=0),
                    "sent to everyone by mistake"),
        redact_case("redact-kept-v45", "rich", _rich("v45"), "pasted by mistake"),
        redact_case("redact-gone", "pruned", _pruned("old"), "pasted by mistake"),
        redact_case("redact-legacy-refused", "rich", _rich("legacy"), "too old"),
        redact_case("redact-already", "rich", _rich("redacted"), "again"),
        redact_case("redact-unknown", "rich", "0" * 32, "unknown"),
        redact_case("redact-unknown-prefix", "rich", "fffffffff", "unknown"),
        redact_case("redact-invalid-id", "rich", "bad.id", "why"),
        redact_case("redact-reason-control", "rich", _rich("to_redact"), "bell\x07"),
        redact_case("redact-reason-long", "rich", _rich("to_redact"), "x" * 241),
        redact_case("redact-reason-blank", "rich", _rich("to_redact"), "  "),
        redact_case("redact-reason-secret", "rich", _rich("to_redact"),
                    "token=" + "q7Hd2kLm9Pz4" + "Rt6Wv8Xy1Bc3"),
        redact_case("redact-busy", "rich", _rich("to_redact"), "wrong paste", hold=True),
        redact_case("redact-nolog", "nolog", "0" * 32, "why"),
        # declared deferrals: the candidate defers and changes nothing
        *stats_cases(),
        *transport_cases(),
        file_case("verify-help", ["verify", "--help"]),
        read_case("export-joined-option", "rich", ["export", "--task=t1"]),
        read_case("export-float-spelling", "rich", ["export", "--since", "1.7e9"]),
        read_case("defer-dsn-option", "rich", ["verify"], rules=("rust-deferral",),
                  env={"PSEUDOLIFE_MCP_DATABASE_URL": _bank.url(_name("rich"))
                       + "?application_name=audit"}),
        read_case("export-out-missing-dir", "rich",
                  ["export", "--out", _home("missing/out.jsonl")]),
        read_case("export-out-missing-dir-before-agent", "rich",
                  ["export", "--agent", "ffffffff0", "--out", _home("missing/out.jsonl")]),
        file_case("no-bank", ["verify"]),
        file_case("input-not-utf8", ["verify", "--input", _home("archive.jsonl")],
                  files={"archive.jsonl": b'{"seq": 1}\n\xff\n'}),
        file_case("input-partial-utf8-after-cr", ["verify", "--input", _home("archive.jsonl")],
                  files={"archive.jsonl": b"{}\r\xf0"}),
        file_case("input-split-utf8", ["verify", "--input", _home("archive.jsonl")],
                  files={"archive.jsonl": b" " * 8191 + "雪\n".encode("utf-8")}),
        file_case("input-not-json", ["verify", "--input", _home("archive.jsonl")],
                  files={"archive.jsonl": b"not JSON\n"}),
        redact_case("defer-reason-format-char", "rich", _rich("to_redact"),
                    "zero​width", rules=("rust-deferral",)),
        # review round 1 (O1): a repeated key is reported when its object closes
        archive_case("input-duplicate-nested-then-long-int", "duplicate-nested-then-long-int"),
        archive_case("input-duplicate-truncated", "duplicate-truncated"),
        archive_case("input-duplicate-then-long-int", "duplicate-then-long-int"),
        # pathlib reprints a drive-relative spelling natively
        file_case("input-drive-relative",
                  ["verify", "--input", "C:.\\missing-audit-archive.jsonl"],
                  platforms=("windows",)),
        # (O2) a stdout that refuses the report
        archive_case("defer-input-stdout-closed", "rich", rules=("rust-deferral",),
                     stdout_closed=True),
        read_case("defer-bank-verify-stdout-closed", "rich", ["verify"],
                  rules=("rust-deferral",), stdout_closed=True),
        redact_case("redact-stdout-closed", "rich", _rich("to_redact"), "wrong paste",
                    rules=("audit-redact-clock", "audit-stdout-closed-trailer"),
                    stdout_closed=True),
        redact_case("defer-redact-refusal-stdout-closed", "rich", _rich("redacted"), "again",
                    rules=("rust-deferral",), stdout_closed=True),
        # (O4) coverage: LEAST(expires_at, now), ambiguous prefixes, pre-v46,
        # the vacuum-skip path with warnings hidden by default
        redact_case("redact-future-expiry", "rich", _rich("future"), "wrong paste"),
        read_case("export-agent-ambiguous", "rich", ["export", "--agent", "ab12cd34"]),
        read_case("export-agent-ambiguous-out", "rich",
                  ["export", "--agent", "ab12cd34ef5", "--out", _home("out.jsonl")]),
        read_case("export-agent-twin", "rich", ["export", "--agent", "ab12cd34ef57"]),
        redact_case("redact-ambiguous-prefix", "rich", "cd34ef56", "wrong paste"),
        redact_case("redact-twin-prefix", "rich", "cd34ef56ab13", "wrong paste"),
        read_case("bank-verify-prev46", "prev46", ["verify"]),
        read_case("export-prev46", "prev46", ["export"]),
        redact_case("redact-prev46", "prev46", _Lazy(lambda: ids("legacy")["legacy"]), "why"),
        redact_case("redact-vacuum-warning", "rich", _rich("to_redact"), "wrong paste",
                    prepare=VACUUM_WARNING),
    ]
    return out


MUTANTS = [
    Mutant("audit-iso-conversion-admitted", "audit", "shim/src/cli/board_audit/args.rs",
           "None => iso(value),", "None => iso(value).or(Some(0.0)),",
           ("export-invalid-nonnumeric",)),
    Mutant("audit-repeat-first-wins", "audit", "shim/src/cli/board_audit/args.rs",
           "slot.1 = value;", "let _ = value;", ("input-repeated", "export-repeated-abbreviated")),
    Mutant("audit-create-message", "audit", "shim/src/cli/board_audit/bank.rs",
           "cannot create {out}: {text}", "cannot write {out}: {text}", ("export-out-missing-dir",)),
    Mutant("audit-read-message", "audit", "shim/src/cli/board_audit/mod.rs",
           "board-audit: cannot read {input}", "board-audit: unreadable {input}", ("input-directory",)),
    Mutant("audit-payload-depth-capped", "audit", "shim/src/cli/board_audit/codec.rs",
           "EXPORT_DEPTH: usize = 988;", "EXPORT_DEPTH: usize = 128;", ("export-payload-domain",)),
    Mutant("audit-decode-one-byte", "audit", "shim/src/cli/board_audit/mod.rs",
           "self.at + 8192", "self.at + 1", ("input-decode-before-broken",)),
    Mutant("audit-body-reason-swap", "audit", "shim/src/cli/board_audit/chain.rs",
           '"body_missing"\n            } else {\n                "body_not_exported"',
           '"body_not_exported"\n            } else {\n                "body_missing"',
           ("input-stripped", "input-blanked")),
    Mutant("audit-broken-exit-3", "audit", "shim/src/cli/board_audit/mod.rs",
           "Ok(Some(broken)) => return report(broken, 1),",
           "Ok(Some(broken)) => return report(broken, 3),",
           ("input-gap", "input-link")),
    Mutant("audit-export-agent-filter-dropped", "audit", "shim/src/cli/board_audit/bank.rs",
           "    if let Some(agent) = &filters.agent {",
           "    if let Some(agent) = &None::<String> {",
           ("export-agent-full", "export-agent-prefix")),
    Mutant("audit-export-order-flipped", "audit", "shim/src/cli/board_audit/bank.rs",
           "FROM coordination_events{filter} ORDER BY seq\"",
           "FROM coordination_events{filter} ORDER BY seq DESC\"",
           ("export-all", "bank-verify-rich")),
    Mutant("audit-redact-body-kept", "audit", "shim/src/cli/board_audit/redact.rs",
           "UPDATE coordination_events SET body=NULL,body_salt=NULL WHERE",
           "UPDATE coordination_events SET body=body,body_salt=NULL WHERE",
           ("redact-removed",)),
    Mutant("audit-exists-token", "audit", "shim/src/cli/board_audit/mod.rs",
           "exists; export never replaces a file", "exists; export never overwrites a file",
           ("export-out-exists",)),
    Mutant("audit-start-cut-dropped", "audit", "shim/src/cli/board_audit/chain.rs",
           '("through_seq".into(), integer(cut.through_seq)),', "",
           ("input-pruned-start-cut", "bank-verify-pruned")),
    Mutant("audit-siblings-dropped", "audit", "shim/src/cli/board_audit/redact.rs",
           "            siblings.push(row.try_get::<_, String>(0)?);",
           "            let _ = row;", ("redact-fanout-siblings",)),
    # review round 1
    Mutant("audit-duplicate-eager", "audit", "shim/src/cli/pyjson.rs",
           "                dict.push((key, value));\n            }",
           "                dict.push((key, value));\n            }\n"
           "            if self.unique && repeated { self.duplicate = true; return Err(Refused::NotJson); }",
           ("input-duplicate-truncated", "input-duplicate-then-long-int")),
    Mutant("audit-verify-stdout-closed-exit-1", "audit", "shim/src/cli/board_audit/mod.rs",
           "        Err(()) => deferred(),", "        Err(()) => 1,",
           ("defer-input-stdout-closed", "defer-bank-verify-stdout-closed")),
    Mutant("audit-redact-stdout-closed-exit-1", "audit", "shim/src/cli/board_audit/redact.rs",
           "    } else {\n        120\n    };", "    } else {\n        1\n    };",
           ("redact-stdout-closed",)),
    Mutant("audit-printed-hash-token", "audit", "shim/src/cli/board_audit/redact.rs",
           'let expect_head = format!("{}:{}", redacted.redact_seq, redacted.redact_hash);',
           'let expect_head = format!("{}:<redact-hash>", redacted.redact_seq);',
           ("redact-removed",)),
    Mutant("audit-least-dropped", "audit", "shim/src/cli/board_audit/redact.rs",
           "        cleared = text.is_some();\n        tx.execute(\n            \"UPDATE "
           "coordination_messages SET text=NULL,fingerprint='redacted',expires_at=LEAST("
           "expires_at,$1) WHERE message_id=$2\",",
           "        cleared = text.is_some();\n        tx.execute(\n            \"UPDATE "
           "coordination_messages SET text=NULL,fingerprint='redacted',expires_at=expires_at"
           "+0*$1 WHERE message_id=$2\",",
           ("redact-future-expiry",)),
    Mutant("audit-vacuum-warnings-dropped", "audit", "shim/src/cli/board_audit/redact.rs",
           "Some(tokio_postgres::error::Severity::Warning)",
           "Some(tokio_postgres::error::Severity::Notice)", ("redact-vacuum-warning",)),
]

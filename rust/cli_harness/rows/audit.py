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
    store.grant_delegate("p", a["agent_id"], hold=3600)
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


# --- disposable banks --------------------------------------------------------

PREFIX = "pl_cf_w1c_audit_"
SEEDERS = {"rich": seed_rich, "pruned": seed_pruned, "legacy": seed_legacy}
_IDS: dict[str, dict] = {}
_BASELINE: dict[str, dict] = {}
_ARCHIVES: dict[str, bytes] = {}
_CREATED: set[str] = set()


def _name(kind: str) -> str:
    return PREFIX + kind


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
    if variant == "head-19-digits":
        from pseudolife_memory.storage.coordination import audit_hash  # noqa: PLC0415
        row = export_rows("rich")[0]
        row["seq"] = 1000000000000000000
        row["hash"] = audit_hash(row["prev_hash"], row)
        return _lines([row])
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
              env=None, note=""):
    """A read action against one seeded bank, which must stay unchanged."""
    original = ["board-audit", *argv]

    def setup(arm):
        name = ensure(kind)
        _files(arm, files)
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
                     programs=PROGRAMS)
    return case


def file_case(case_id, argv, *, files=None, rules=(), stdout_closed=False,
              platforms=("windows", "linux"), note=""):
    """A file-only action (no bank in the environment)."""
    original = ["board-audit", *argv]

    def setup(arm):
        _files(arm, files)
        _resolve_argv(case, original)
        _stash(arm)

    case = core.Case(case_id, list(original), env=dict(GUARDS), setup=setup, after=_record,
                     rules=rules, stdout_closed=stdout_closed, platforms=platforms, note=note,
                     programs=PROGRAMS)
    return case


def archive_case(case_id, variant, *extra, rules=(), stdout_closed=False):
    return file_case(case_id, ["verify", "--input", _home("archive.jsonl"), *extra],
                     files={"archive.jsonl": lambda: archive(variant)}, rules=rules,
                     stdout_closed=stdout_closed)


REDACT_DB = PREFIX + "rd"


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


def cases() -> list[core.Case]:
    rich_head = _Lazy(lambda: head("rich"))
    out = [
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
                           json.loads(archive("head-19-digits"))["hash"])),
        archive_case("input-head-earlier", "rich", "--expect-head",
                     _Lazy(lambda: f"3:{export_rows('rich')[2]['hash']}")),
        archive_case("input-rich-crlf", "rich-crlf"),
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
        read_case("export-agent-full", "rich", ["export", "--agent", _rich("b")]),
        read_case("export-agent-prefix", "rich", ["export", "--agent", _rich("a", cut=8)]),
        read_case("export-agent-literal", "rich", ["export", "--agent", "alice"]),
        read_case("export-agent-unknown", "rich", ["export", "--agent", "ffffffff0"]),
        read_case("export-agent-unknown-out", "rich",
                  ["export", "--agent", "ffffffff0", "--out", _home("out.jsonl")]),
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
        read_case("defer-stats", "rich", ["stats"], rules=("rust-deferral",)),
        read_case("defer-help", "rich", ["verify", "--help"], rules=("rust-deferral",)),
        read_case("defer-joined-option", "rich", ["export", "--task=t1"],
                  rules=("rust-deferral",)),
        read_case("defer-float-spelling", "rich", ["export", "--since", "1.7e9"],
                  rules=("rust-deferral",)),
        read_case("defer-dsn-option", "rich", ["verify"], rules=("rust-deferral",),
                  env={"PSEUDOLIFE_MCP_DATABASE_URL": _bank.url(_name("rich"))
                       + "?application_name=audit"}),
        read_case("defer-out-missing-dir", "rich",
                  ["export", "--out", _home("missing/out.jsonl")], rules=("rust-deferral",)),
        file_case("defer-no-bank", ["verify"], rules=("rust-deferral",)),
        file_case("defer-input-not-utf8", ["verify", "--input", _home("archive.jsonl")],
                  files={"archive.jsonl": b'{"seq": 1}\n\xff\n'}, rules=("rust-deferral",)),
        file_case("defer-input-not-json", ["verify", "--input", _home("archive.jsonl")],
                  files={"archive.jsonl": b"not JSON\n"}, rules=("rust-deferral",)),
        redact_case("defer-reason-format-char", "rich", _rich("to_redact"),
                    "zero​width", rules=("rust-deferral",)),
        # review round 1 (O1): a repeated key is reported when its object closes
        archive_case("input-duplicate-nested-then-long-int", "duplicate-nested-then-long-int"),
        archive_case("defer-input-duplicate-truncated", "duplicate-truncated",
                     rules=("rust-deferral",)),
        archive_case("defer-input-duplicate-then-long-int", "duplicate-then-long-int",
                     rules=("rust-deferral",)),
        # (C3) pathlib reprints a drive-relative spelling: defer it
        file_case("defer-input-drive-relative",
                  ["verify", "--input", "C:.\\missing-audit-archive.jsonl"],
                  rules=("rust-deferral",), platforms=("windows",)),
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
    Mutant("audit-duplicate-eager", "audit", "shim/src/cli/board_audit/archive.rs",
           "            repeated |= !keys.insert(key);",
           "            if !keys.insert(key) {\n                self.0.set(true);\n"
           "                return Err(de::Error::custom(\"duplicate decoded key\"));\n"
           "            }",
           ("defer-input-duplicate-truncated", "defer-input-duplicate-then-long-int")),
    Mutant("audit-drive-relative-admitted", "audit", "shim/src/cli/board_audit/args.rs",
           "    if cfg!(windows) && value.contains(':') {",
           "    if cfg!(windows) && value.contains(':') && false {",
           ("defer-input-drive-relative",)),
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

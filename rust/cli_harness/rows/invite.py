"""CLI-PAIRING (invite): ``pseudolife-mcp invite`` (invite_cli.py) over an
explicit ``PSEUDOLIFE_MCP_DATABASE_URL``.

Banks are disposable (``_bank``): one seeded bank per kind, filled once by
the oracle's own writers (``principal_store.create_invite`` / ``redeem`` /
``revoke``, and the meta row as ``tests/test_invite_cli.py``'s ``bank``
fixture inserts it). Read-only cases share the seeded bank and check it
unchanged after each arm; a case that may write gets, per arm, a fresh bank
whose seeded rows are copied verbatim from the seed (DML only: this row runs
no reap, TRUNCATE, DROP or DDL of its own, and calls
``assert_disposable_database`` before every write).

Every arm runs with no tailscale or docker CLI reachable (``PATH`` inside the home,
the Program Files default moved into the home) and a data dir that holds no
``config.yaml``, so the oracle's ``exposed_url``, Docker path and
``_listed_note`` read nothing from this host. The fixture daemon serves
``/health`` with the disposable bank's real fingerprint (the daemon's
``_bank_fingerprint``: the first 16 hex of the SHA-256 of the bank id).

Named rules: ``invite-written`` (a written row's clocks inside the arm's own
window, the stored code hash equal to the SHA-256 of the code that arm
printed, and the printed expiry equal to the stored one) and
``invite-deferral`` (a declared deferral case's expectation).
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import json
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import core, normalize
from ..mutants import Mutant
from . import _bank

PREFIX = "pl_cf_w1c_pairing_"
ARM_DB = _bank.name(PREFIX + "arm")
BANK_ID = "fixture-invite-bank"
FINGERPRINT = hashlib.sha256(BANK_ID.encode()).hexdigest()[:16]
OTHER_FINGERPRINT = "0123456789abcdef"
SEP = "\\" if core.WINDOWS else "/"
DEFERRAL = "pseudolife-stdio: mode 'invite' is deferred in this candidate\n"


# ── seeds ───────────────────────────────────────────────────────────────────

def _guarded(conn) -> None:
    from pseudolife_memory.storage.schema import assert_disposable_database  # noqa: PLC0415
    assert_disposable_database(conn)


def _meta(conn) -> None:
    from pseudolife_memory.storage.coordination import BANK_ID_META_KEY  # noqa: PLC0415
    _guarded(conn)
    conn.execute("INSERT INTO meta (key, value) VALUES (%s, to_jsonb(%s::text))",
                 (BANK_ID_META_KEY, BANK_ID))


def seed_states(conn, name: str) -> None:
    """One principal in each state ``describe_rows`` names."""
    from pseudolife_memory import principal_store as store  # noqa: PLC0415
    from pseudolife_memory.principals import secret_sha256  # noqa: PLC0415
    _meta(conn)
    store.create_invite(conn, "alpha", ttl_seconds=900, replace=False)
    bravo = store.create_invite(conn, "bravo", tier="core", ttl_seconds=900, replace=False)
    if store.redeem(_bank.url(name), secret_sha256(bravo["code"]),
                    secret_sha256("seed-token-bravo")) is None:
        raise RuntimeError("seed: bravo was not paired")
    store.create_invite(conn, "charlie", ttl_seconds=900, replace=False)
    store.revoke(conn, "charlie")
    store.create_invite(conn, "delta", board=False, ttl_seconds=-60, replace=False)
    store.create_invite(conn, "a-rather-long-principal-name.x", tier="full", ttl_seconds=3600,
                        replace=False)


SEEDERS = {
    "states": seed_states,
    "empty": lambda conn, name: _meta(conn),
    "nometa": lambda conn, name: None,
}
_CREATED: set[str] = set()
_BASELINE: dict[str, dict] = {}


def _drop_all() -> None:
    for name in sorted(_CREATED):
        try:
            _bank.drop(name)
        except Exception:  # noqa: BLE001 (best-effort cleanup at exit)
            pass


atexit.register(_drop_all)


def _create(name: str, schema: bool = True) -> None:
    """``_bank.create``, waiting out an autovacuum worker on the database:
    the test login may not terminate one, and it finishes on its own (as
    the audit row's ``_drop`` waits)."""
    import psycopg  # noqa: PLC0415
    for attempt in range(60):
        try:
            _bank.create(name, schema=schema)
            return
        except (psycopg.errors.InsufficientPrivilege, psycopg.errors.ObjectInUse):
            if attempt == 59:
                raise
            time.sleep(0.5)


def ensure(kind: str) -> str:
    """The seeded bank of this kind (created once per harness process)."""
    name = _bank.name(PREFIX + kind)
    if name in _BASELINE:
        return name
    _CREATED.add(name)
    if kind == "noschema":
        _create(name, schema=False)
    else:
        _create(name)
        with _bank.connect(name, autocommit=True) as conn:
            SEEDERS[kind](conn, name)
    _BASELINE[name] = _bank.dump(name)
    return name


def fresh_copy(kind: str) -> None:
    """A fresh ``ARM_DB`` holding the seed's meta row and principals,
    copied verbatim (each arm writes into its own)."""
    seed = ensure(kind)
    _CREATED.add(ARM_DB)
    _create(ARM_DB, schema=kind != "noschema")
    if kind == "noschema":
        return
    with _bank.connect(seed, autocommit=True) as source:
        meta = source.execute(
            "SELECT key, value FROM meta WHERE key = 'coordination_bank_id'").fetchall()
        columns = [r[0] for r in source.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
            "AND table_name='principals' ORDER BY ordinal_position")]
        rows = source.execute(f"SELECT {', '.join(columns)} FROM principals").fetchall()
    from psycopg.types.json import Jsonb  # noqa: PLC0415
    with _bank.connect(ARM_DB, autocommit=True) as target:
        _guarded(target)
        for key, value in meta:
            target.execute("INSERT INTO meta (key, value) VALUES (%s, %s)", (key, Jsonb(value)))
        marks = ", ".join(["%s"] * len(columns))
        for row in rows:
            target.execute(f"INSERT INTO principals ({', '.join(columns)}) VALUES ({marks})", row)
        # The clock witness (test instrument, after the seed rows): a BEFORE
        # trigger notes the database clock as each arm's write reaches the
        # row, so the rule can check a written time against that moment
        # instead of the arm's whole run window. The CLIs never read it.
        _guarded(target)
        target.execute(f"CREATE TABLE {WITNESS} (principal text, at double precision)")
        target.execute(
            f"CREATE FUNCTION {WITNESS}_note() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN "
            f"INSERT INTO {WITNESS} VALUES (NEW.principal, "
            "EXTRACT(EPOCH FROM clock_timestamp())::double precision); RETURN NEW; END$$")
        target.execute(f"CREATE TRIGGER {WITNESS}_note BEFORE INSERT OR UPDATE ON principals "
                       f"FOR EACH ROW EXECUTE FUNCTION {WITNESS}_note()")


WITNESS = "pl_cf_clock"


def _principals(dump: dict) -> list[dict]:
    return [json.loads(text) for text in dump["tables"].get("principals", [])]


# ── the fixture daemon ──────────────────────────────────────────────────────

def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Daemon:
    """``/health`` on 127.0.0.1:<port>, answering ``health`` (a dict, or raw
    bytes) with ``status``; every request is recorded."""

    def __init__(self, port: int, health, status: int = 200):
        self.url = f"http://127.0.0.1:{port}"
        self._seen: list[dict] = []
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                daemon._seen.append({"method": "GET", "target": self.path, "headers": {},
                                     "body": ""})
                if self.path != "/health":
                    body, code = b'{"error": "not_found"}', 404
                else:
                    body = health if isinstance(health, bytes) else json.dumps(health).encode()
                    code = status
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass

        self.server = Server(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def requests(self) -> list:
        return list(self._seen)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def health(**over) -> dict:
    payload = {"status": "ok", "version": "0.0.0-fixture", "schema": 55, "auth": True,
               "bank": FINGERPRINT}
    payload.update(over)
    return payload


# ── the environment ─────────────────────────────────────────────────────────

# The oracle looks up tailscale (expose's exposed_url) and docker
# (docker_available): PATH stays inside the home, and core's preflight proves
# neither resolves outside it.
PROGRAMS = ("tailscale", "docker")
NO_BIN = "{HOME}" + SEP + "no-bin"


def env(dsn_kind: str | None = "arm", **extra) -> dict:
    out = {
        "PATH": NO_BIN,
        "ProgramW6432": "{HOME}" + SEP + "no-programs",
        "ProgramFiles": "{HOME}" + SEP + "no-programs",
        "PSEUDOLIFE_MCP_DATA_DIR": "{HOME}" + SEP + "no-bank",
        "PSEUDOLIFE_MCP_DATABASE_URL": None,
        "PSEUDOLIFE_MCP_TOKENS": None,
        "PSEUDOLIFE_MCP_TIER_MAP": None,
        "PSEUDOLIFE_MCP_PORT": None,
        "HOMEDRIVE": None,
        "HOMEPATH": None,
    }
    if dsn_kind == "arm":
        out["PSEUDOLIFE_MCP_DATABASE_URL"] = _bank.url(ARM_DB)
    elif dsn_kind is not None:
        out["PSEUDOLIFE_MCP_DATABASE_URL"] = _bank.url(_bank.name(PREFIX + dsn_kind))
    out.update(extra)
    return out


def refused_login_url() -> str:
    """The test login's own server and database, a wrong password: the
    server refuses the login and nothing else is touched."""
    good = _bank.url(ARM_DB)
    user = good.split("://", 1)[1].split(":", 1)[0]
    return f"postgresql://{user}:not-the-password@{good.rsplit('@', 1)[1]}"


# ── named rules ─────────────────────────────────────────────────────────────

_CODE = re.compile(rb"\b([0-9A-HJKMNP-TV-Z]{4})-([0-9A-HJKMNP-TV-Z]{4})-([0-9A-HJKMNP-TV-Z]{4})\b")


def _when(epoch) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch)) if epoch else "-"


# How far a written clock may precede the witness trigger's own reading of
# the same statement (the trigger runs right after the row's expressions).
SLACK = 0.05


@normalize.rule("invite-written")
def invite_written(obs: dict) -> None:
    """A row this arm wrote, checked against the clock witness: the
    database clock a BEFORE trigger read as that write reached the row
    (``pl_cf_clock``, itself inside the arm's run window). A changed
    ``created_at`` / ``revoked_at`` becomes ``<clock>`` only when it lies in
    ``(at - SLACK, at]``; a changed ``code_expires_at`` becomes
    ``<clock+ttl>`` only when ``expires - ttl`` does, so a code lifetime
    shortened by any noticeable amount stays visible. A changed
    ``code_hash`` becomes ``<printed-code-hash>`` only when it is the
    SHA-256 of the one code this arm printed; that code becomes ``<code>``,
    and the printed expiry becomes ``<expires>`` only when it is spelled
    exactly as the oracle spells the stored value (JSON: ``repr`` of the
    float; text: ``(%Y-%m-%d %H:%M UTC)`` of its ``gmtime``). The witness
    table is then dropped from the observation."""
    db = obs.get("db")
    if not isinstance(db, dict):
        return
    start, end = obs["window"]
    ttl = obs.get("ttl") or 0
    witness: dict[str, float] = {}
    for text in db["tables"].pop(WITNESS, []):
        note = json.loads(text)
        if start - 1 <= note["at"] <= end + 1:
            witness[note["principal"]] = max(note["at"], witness.get(note["principal"], 0))
    seed = {row["principal"]: row for row in obs.get("seed_rows", [])}
    stdout = base64.b64decode(obs["stdout"])
    codes = {m.group(0) for m in _CODE.finditer(stdout)}
    printed = next(iter(codes)) if len(codes) == 1 else None
    printed_hash = (hashlib.sha256(printed.replace(b"-", b"")).hexdigest()
                    if printed else None)
    rows = _principals(db)
    for row in rows:
        before = seed.get(row["principal"], {})
        at = witness.get(row["principal"])
        expiry = None
        for column in ("created_at", "code_expires_at", "revoked_at"):
            value = row.get(column)
            if value is None or before.get(column) == value or at is None:
                continue
            if column == "code_expires_at":
                if ttl and at - SLACK < value - ttl <= at:
                    expiry = value
                    row[column] = "<clock+ttl>"
            elif at - SLACK < value <= at:
                row[column] = "<clock>"
        if row.get("code_hash") and row["code_hash"] != before.get("code_hash") \
                and row["code_hash"] == printed_hash:
            row["code_hash"] = "<printed-code-hash>"
            text = stdout.replace(printed, b"<code>")
            if expiry is not None:
                text = text.replace(b'"expires_at": ' + repr(expiry).encode() + b",",
                                    b'"expires_at": "<expires>",')
                text = text.replace(f"({_when(expiry)})".encode(), b"(<expires>)")
            obs["stdout"] = base64.b64encode(text).decode()
    db["tables"]["principals"] = sorted(json.dumps(row, sort_keys=True) for row in rows)


_RECURSION = re.compile(rb"\ATraceback \(most recent call last\):\r?\n.*"
                        rb"\nRecursionError: maximum recursion depth exceeded[^\r\n]*\r?\n\Z",
                        re.DOTALL)


@normalize.rule("invite-recursion-traceback")
def invite_recursion_traceback(obs: dict) -> None:
    """Declared: a ``/health`` nested past CPython's recursion limit kills
    the oracle with an uncaught ``RecursionError`` (exit 1, a traceback
    naming its own source files); the candidate prints its deferral line,
    exit 1. Only that traceback with exit 1 maps; the request both sent and
    the unchanged bank stay compared."""
    if obs.get("arm") != "python":
        return
    stderr = base64.b64decode(obs["stderr"])
    if obs["exit"] == 1 and not base64.b64decode(obs["stdout"]) and _RECURSION.match(stderr):
        native = DEFERRAL.replace("\n", "\r\n") if core.WINDOWS else DEFERRAL
        obs["stderr"] = base64.b64encode(native.encode()).decode()


@normalize.rule("invite-deferral")
def invite_deferral(obs: dict) -> None:
    """A declared deferral case: the candidate must print the dispatcher's
    deferral line, exit 1, send no request and change nothing. The oracle
    arm still runs (on its own bank, with no tool or real bank reachable);
    its observation is replaced by that expectation."""
    if obs.get("arm") != "python":
        return
    obs["exit"] = 1
    obs["stdout"] = ""
    native = DEFERRAL.replace("\n", "\r\n") if core.WINDOWS else DEFERRAL
    obs["stderr"] = base64.b64encode(native.encode()).decode()
    obs["files"] = dict(obs.get("before", {}))
    obs["modes"] = dict(obs.get("before_modes", {}))
    if "requests" in obs:
        obs["requests"] = []
    obs["db"] = "unchanged"


# ── cases ───────────────────────────────────────────────────────────────────

def _stash(arm) -> None:
    arm.state["before"] = core.snapshot(arm.home)
    arm.state["before_modes"] = core.modes(arm.home)


def _record(arm, obs, argv=()) -> None:
    obs["arm"] = arm.name
    obs["before"] = arm.state.get("before", {})
    obs["before_modes"] = arm.state.get("before_modes", {})
    if "--port" in argv:
        obs["argv_port"] = argv[argv.index("--port") + 1]


_PORT_FORMS = ("port {}", "127.0.0.1:{}", "localhost:{}")


@normalize.rule("invite-port")
def invite_port(obs: dict) -> None:
    """The ``--port`` a case passes is a free port drawn when the row loads,
    so it differs between the harness process that recorded a golden and
    the one replaying it. Each arm records the exact value it was given
    (``argv_port``); only ``port <p>``, ``127.0.0.1:<p>`` and
    ``localhost:<p>`` with that value become ``<port>`` in the streams."""
    port = obs.get("argv_port")
    if not port:
        return
    for field in ("stdout", "stderr"):
        data = base64.b64decode(obs[field])
        for form in _PORT_FORMS:
            data = re.sub(re.escape(form.format(port)).encode() + rb"(?![0-9])",
                          form.format("<port>").encode(), data)
        obs[field] = base64.b64encode(data).decode()


def _daemon(port: int, payload, status: int = 200):
    return lambda: Daemon(port, payload, status)


def read_case(case_id, kind, argv, *, daemon=None, extra_env=None, rules=(), files=None):
    """A case that must not write: the shared seeded bank, checked unchanged."""
    def setup(arm):
        ensure(kind)
        for rel, data in (files or {}).items():
            path = arm.home / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        _stash(arm)

    def after(arm, obs):
        _record(arm, obs, argv)
        name = _bank.name(PREFIX + kind)
        dump = _bank.dump(name)
        obs["db"] = "unchanged" if dump == _BASELINE[name] else dump

    return core.Case(case_id, ["invite", *argv], env=env(kind, **(extra_env or {})),
                     setup=setup, after=after, rules=(*rules, "invite-port"), daemon=daemon,
                     timeout=60, programs=PROGRAMS)


def write_case(case_id, kind, argv, *, daemon=None, extra_env=None, ttl=900,
               rules=("invite-written",), files=None):
    """A case that may write: this arm's own fresh copy of the seeded bank."""
    def setup(arm):
        fresh_copy(kind)
        arm.state["seed"] = _bank.dump(ARM_DB)
        for rel, data in (files or {}).items():
            path = arm.home / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        _stash(arm)

    def after(arm, obs):
        _record(arm, obs, argv)
        dump = _bank.dump(ARM_DB)
        obs["ttl"] = ttl
        obs["seed_rows"] = _principals(arm.state["seed"])
        obs["db"] = "unchanged" if dump == arm.state["seed"] else dump

    return core.Case(case_id, ["invite", *argv], env=env("arm", **(extra_env or {})),
                     setup=setup, after=after, rules=(*rules, "invite-port"), daemon=daemon,
                     timeout=60, programs=PROGRAMS)


# Cases whose observation shows the seeded principals: the oracle's writers
# stamp them with the wall clock and draw their codes and tokens at random in
# every harness process, so these run live only (rows/invite.md "Hosted CI
# and goldens"). The others replay as goldens.
LIVE_ONLY = frozenset({
    "list", "list-json", "list-bank", "invite-new", "invite-new-json",
    "invite-new-placeholder", "invite-new-placeholder-json", "invite-empty-url",
    "invite-options", "invite-board", "invite-minutes-half-even", "invite-minutes-up",
    "invite-day", "reinvite-pending", "reinvite-expired-tier-json",
    "reinvite-expired-board", "paired-replace", "revoked-reinvite-json",
    "invite-bank-match", "invite-health-int-at-limit", "invite-env-port", "revoke",
    "revoke-json", "revoke-again",
    # Its report is the daemon's auth value nested to the limit and
    # pretty-printed: 2.6 MB of indentation, compared live only.
    "health-nested-at-limit-json"})


def cases() -> list[core.Case]:
    out = _cases()
    missing = LIVE_ONLY - {case.id for case in out}
    if missing:
        raise ValueError(f"LIVE_ONLY names no case: {sorted(missing)}")
    for case in out:
        case.golden = case.id not in LIVE_ONLY
    return out


def _cases() -> list[core.Case]:
    out: list[core.Case] = []
    add = out.append

    # Usage and refusals before the database.
    add(read_case("usage-no-mode", "states", []))
    add(read_case("usage-two-modes-json", "states", ["--list", "--revoke", "alpha", "--json"]))
    add(read_case("usage-empty-name", "states", [""]))
    add(read_case("usage-bad-name", "states", ["Laptop"]))
    add(read_case("usage-bad-name-json", "states", ["a/b", "--json"]))
    add(read_case("usage-long-name", "states", ["a" * 65]))
    add(read_case("usage-bad-revoke", "states", ["--revoke", "..", "--yes"]))
    add(read_case("refused-reserved", "states", ["daemon"]))
    add(read_case("refused-reserved-json", "states", ["maintainer", "--json"]))
    add(read_case("refused-env-token", "states", ["laptop", "--url", "http://h:1"],
                  extra_env={"PSEUDOLIFE_MCP_TOKENS": "tok-1:Laptop, tok-2:other"}))
    add(read_case("refused-env-tier", "states", ["laptop", "--json"],
                  extra_env={"PSEUDOLIFE_MCP_TIER_MAP": "laptop:core"}))
    add(read_case("revoke-not-interactive", "states", ["--revoke", "alpha"]))
    add(read_case("version-check", "states", ["--version-check"]))

    # The daemon check (an invite only).
    port = free_port()
    add(read_case("daemon-unreachable", "states", ["laptop", "--port", str(port)]))
    for case_id, payload, status in (
            ("daemon-degraded", health(status="degraded"), 503),
            ("daemon-no-auth-json", health(auth=False), 200),
            ("daemon-auth-absent", {k: v for k, v in health().items() if k != "auth"}, 200),
            ("daemon-old-schema", health(schema=52), 200),
            ("daemon-schema-null-json", health(schema=None), 200),
            ("daemon-schema-text", health(schema="55"), 200),
            ("daemon-schema-bool", health(schema=True), 200),
            ("daemon-bank-null-json", health(bank=None), 200),
            ("daemon-not-json", b"<html>not the daemon</html>", 200),
            ("daemon-list-body", b"[1, 2]", 200)):
        port = free_port()
        argv = ["laptop", "--port", str(port), "--url", "http://h:1"]
        if case_id.endswith("-json"):
            argv.append("--json")
        add(read_case(case_id, "states", argv, daemon=_daemon(port, payload, status)))
    port = free_port()
    add(read_case("daemon-other-bank", "states", ["laptop", "--port", str(port), "--url", "u"],
                  daemon=_daemon(port, health(bank=OTHER_FINGERPRINT))))
    port = free_port()
    add(read_case("daemon-other-bank-json", "nometa",
                  ["laptop", "--port", str(port), "--url", "u", "--json"],
                  daemon=_daemon(port, health())))
    port = free_port()
    add(read_case("no-principals-table", "noschema",
                  ["laptop", "--port", str(port), "--url", "u"], daemon=_daemon(port, health())))

    # /health values only CPython's reader holds: lone surrogates, nesting
    # up to its measured recursion limit, integers past 4300 digits.
    def nested(depth: int, **over) -> bytes:
        fields = {k: v for k, v in health(**over).items() if k != "auth"}
        return json.dumps(fields)[:-1].encode() + b', "auth": ' + \
            b"[" * depth + b"]" * depth + b"}"

    for case_id, payload, json_mode, rules in (
            ("health-surrogate-schema", health(schema="\ud800x"), False, ()),
            ("health-surrogate-schema-json", health(schema="\ud800x"), True, ()),
            ("health-surrogate-bank", health(bank="b\udc00"), False, ()),
            ("health-surrogate-bank-json", health(bank="b\udc00"), True, ()),
            ("health-surrogate-in-list-json", health(schema=["\ud800", {"\udc00": 1}]), True, ()),
            # 984 nested in the object: 985 containers, the invite limit.
            ("health-nested-at-limit", nested(984), False, ()),
            ("health-nested-at-limit-json", nested(984), True, ()),
            ("health-nested-past-limit", nested(985), False, ("invite-recursion-traceback",)),
            ("health-int-past-limit", json.dumps(health())[:-1].encode()
             + b', "x": 1' + b"0" * 4300 + b"}", False, ())):
        port = free_port()
        argv = ["laptop", "--port", str(port), "--url", "u"] + (["--json"] if json_mode else [])
        add(read_case(case_id, "states", argv, daemon=_daemon(port, payload), rules=rules))

    # `target = args.name or args.revoke`: an empty --revoke is a target.
    add(read_case("usage-empty-revoke-with-list", "states", ["--list", "--revoke", ""]))
    add(read_case("usage-empty-name-and-revoke-json", "states",
                  ["", "--list", "--revoke", "", "--json"]))

    # List.
    add(read_case("list", "states", ["--list"]))
    add(read_case("list-json", "states", ["--list", "--json"]))
    add(read_case("list-bank", "states", ["--list", "--bank", FINGERPRINT]))
    add(read_case("list-other-bank-json", "states", ["--list", "--bank", OTHER_FINGERPRINT,
                                                     "--json"]))
    add(read_case("list-empty", "empty", ["--list"]))
    add(read_case("list-empty-json", "empty", ["--json", "--list"]))
    add(read_case("list-no-table", "noschema", ["--list"]))
    add(read_case("list-no-meta-bank", "nometa", ["--list", "--bank", FINGERPRINT]))

    # Writes.
    def inviting(case_id, kind, argv, ttl=900, payload=None, **options):
        port = free_port()
        add(write_case(case_id, kind, [*argv, "--port", str(port)], ttl=ttl,
                       daemon=_daemon(port, payload or health()), **options))

    inviting("invite-new", "states", ["laptop", "--url", "http://100.64.0.2:8765"])
    inviting("invite-new-json", "states", ["laptop", "--url", "http://100.64.0.2:8765", "--json"])
    inviting("invite-new-placeholder", "states", ["laptop"])
    inviting("invite-new-placeholder-json", "states", ["laptop", "--json"])
    inviting("invite-empty-url", "states", ["laptop", "--url", ""])
    inviting("invite-options", "states",
             ["--tier", "core", "--no-board", "--expires", "2h", "laptop", "--url", "u"], ttl=7200)
    inviting("invite-board", "states", ["laptop", "--board", "--expires", "90", "--url", "u",
                                        "--json"], ttl=5400)
    inviting("invite-minutes-half-even", "states", ["laptop", "--expires", "150s", "--url", "u"],
             ttl=150)
    inviting("invite-minutes-up", "states", ["laptop", "--expires", "91S", "--url", "u"], ttl=91)
    inviting("invite-day", "states", ["laptop", "--expires", "1d", "--url", "u"], ttl=86400)
    inviting("reinvite-pending", "states", ["alpha", "--url", "u"])
    inviting("reinvite-expired-tier-json", "states", ["delta", "--tier", "full", "--url", "u",
                                                     "--json"])
    inviting("reinvite-expired-board", "states", ["delta", "--board", "--url", "u"])
    inviting("paired-needs-replace", "states", ["bravo", "--url", "u"])
    inviting("paired-needs-replace-json", "states", ["bravo", "--url", "u", "--json"])
    inviting("paired-replace", "states", ["bravo", "--replace", "--url", "u"])
    inviting("revoked-reinvite-json", "states", ["charlie", "--url", "u", "--json"])
    inviting("invite-bank-match", "states", ["laptop", "--bank", FINGERPRINT, "--url", "u"])
    inviting("invite-bank-other", "states", ["laptop", "--bank", OTHER_FINGERPRINT, "--url", "u"])
    inviting("invite-empty-bank", "empty", ["zulu.1", "--url", "u", "--json"])
    inviting("invite-health-int-at-limit", "states", ["laptop", "--url", "u", "--json"],
             payload=json.dumps(health())[:-1].encode() + b', "x": -' + b"9" * 4300 + b"}")
    port = free_port()
    add(write_case("invite-env-port", "states", ["laptop", "--url", "u"],
                   extra_env={"PSEUDOLIFE_MCP_PORT": str(port)},
                   daemon=_daemon(port, health())))
    add(write_case("revoke", "states", ["--revoke", "alpha", "--yes"]))
    add(write_case("revoke-json", "states", ["--revoke", "bravo", "--yes", "--json"]))
    add(write_case("revoke-again", "states", ["--revoke", "charlie", "--yes"]))
    add(write_case("revoke-missing", "states", ["--revoke", "nobody", "--yes"]))
    add(write_case("revoke-reserved-name", "states", ["--revoke", "default", "--yes", "--json"]))
    add(write_case("revoke-other-bank", "states", ["--revoke", "alpha", "--yes", "--bank",
                                                   OTHER_FINGERPRINT]))

    # Declared deferrals: before any effect, whatever the oracle does.
    deferred = ("invite-deferral",)

    def deferral(case_id, argv, kind="states", **options):
        port = free_port()
        add(write_case(case_id, kind, [*argv, "--port", str(port)], rules=deferred,
                       daemon=_daemon(port, health()), **options))

    deferral("defer-no-dsn", ["laptop", "--url", "u"],
             extra_env={"PSEUDOLIFE_MCP_DATABASE_URL": None})
    # The oracle asks /health first, then fails to log in (exit 1); the
    # candidate opens the session first, so it defers before any request.
    deferral("defer-refused-login", ["laptop", "--url", "u"],
             extra_env={"PSEUDOLIFE_MCP_DATABASE_URL": refused_login_url()})
    deferral("defer-list-no-dsn", ["--list"], extra_env={"PSEUDOLIFE_MCP_DATABASE_URL": ""})
    tailscale = "tailscale.exe" if core.WINDOWS else "tailscale"
    deferral("defer-tailscale-present", ["laptop"],
             extra_env={"PATH": "{HOME}" + SEP + "tools"},
             files={"tools/" + tailscale: b"not a program\n"})
    deferral("defer-config-yaml", ["laptop", "--url", "u"],
             extra_env={"PSEUDOLIFE_MCP_DATA_DIR": "{HOME}" + SEP + "data"},
             files={"data/config.yaml": b"coordination:\n  allowed_principals: [laptop]\n"})
    deferral("defer-malformed-token-map", ["laptop", "--url", "u"],
             extra_env={"PSEUDOLIFE_MCP_TOKENS": "no-separator"})
    deferral("defer-help", ["--help"])
    deferral("defer-expires-too-short", ["laptop", "--expires", "30s", "--url", "u"])
    deferral("defer-unknown-tier", ["laptop", "--tier", "writer", "--url", "u"])
    deferral("defer-repeated-option", ["laptop", "--url", "u", "--url", "v"])
    deferral("defer-equals-spelling", ["laptop", "--url=u"])
    deferral("defer-env-port-spelling", ["laptop", "--url", "u"],
             extra_env={"PSEUDOLIFE_MCP_PORT": " 8765"})
    return out


MUTANTS = [
    # A code lifetime 0.6 s short: inside the arm's run window, outside the
    # clock witness's slack.
    Mutant("invite-ttl-shortened", "invite", "shim/src/cli/pairing/invite_db.rs",
           "VALUES ($1, $2, $3, $4, {NOW} + $5, {NOW})",
           "VALUES ($1, $2, $3, $4, {NOW} + $5 - 0.6, {NOW})",
           cases=("invite-new", "invite-new-json")),
    Mutant("invite-revoke-dropped", "invite", "shim/src/cli/pairing/invite_db.rs",
           "SET revoked_at = COALESCE(revoked_at, {NOW}), ",
           "SET revoked_at = revoked_at, ", cases=("revoke", "revoke-json")),
    Mutant("invite-list-order", "invite", "shim/src/cli/pairing/invite_db.rs",
           "FROM public.principals ORDER BY principal\"",
           "FROM public.principals ORDER BY principal DESC\"", cases=("list", "list-json")),
    Mutant("invite-refusal-exit", "invite", "shim/src/cli/pairing/invite.rs",
           "return Ok(report.fail_units(EXIT_REFUSED, refusal));",
           "return Ok(report.fail_units(EXIT_FAILED, refusal));",
           cases=("daemon-unreachable", "daemon-no-auth-json")),
    Mutant("invite-message-token", "invite", "shim/src/cli/pairing/invite.rs",
           "single use, expires in {} min",
           "single-use, expires in {} min", cases=("invite-new",)),
    Mutant("invite-keep-tier-lost", "invite", "shim/src/cli/pairing/invite_db.rs",
           "(Some(_), None) => \", tier = $4\",",
           "(Some(_), None) => \", tier = $4, board = TRUE\",",
           cases=("reinvite-expired-tier-json",)),
    Mutant("invite-expired-is-pending", "invite", "shim/src/cli/pairing/invite_db.rs",
           "expires.is_some_and(|expires| expires > now)",
           "expires.is_some()", cases=("list",)),
]

"""CLI-MAINTAINER: ``pseudolife-mcp maintainer`` (maintainer_cli.py).

Each arm copies an owned disposable bank. The seeded rows and the Console
effects selected by the waiting cases were captured from the frozen writer.
The CLI still creates the real code and polls its actual durable state.
"""

from __future__ import annotations

import atexit
import base64
import contextlib
import hashlib
import json
import os
import re
import time
from pathlib import Path

from rust.harness_support.database_guard import assert_disposable_database, refuse_production_database
from rust.harness_support.fixture_rows import insert_fixture_rows

from .. import core, normalize
from . import _bank

PREFIX = "pl_cf_w1c_maint_"
ARM_DB = _bank.name(PREFIX + "arm")
# The fixed clock used to capture the seed rows and date the session fixtures.
T0 = 1782864000.0
# The waiting cases' redemption clock: after every seed, before any run.
HOOK_T = T0 + 3600.0

# Chosen credential ids (16 bytes each, unpadded base64url).
IDS = {
    "pending": "-pendingKeyAAAAAAAAAAA",   # a pending bootstrap key; '-' leads
    "laptop": "-dashKeyAAAAAAAAAAAAAA",    # active (bootstrap, host confirm)
    "phone": "phoneKey_quarantineAAA",     # active, quarantined; '_' inside
    "spare": "cancelXdRevokedAAAAAAA",     # revoked (cancelled by laptop)
}
def _session(sid: str, issuer: str, method: str) -> str:
    """A v59 Console session row (test instrument): ``issuer`` is a passkey
    credential id for a passkey sign-in, a token hash for a link one."""
    token = hashlib.sha256(("cli-harness-browser-" + sid).encode()).hexdigest()
    return ("INSERT INTO console_sessions (session_id, token_hash, principal, source, issuer, "
            f"method, label, created_at, last_used_at, expires_at) VALUES ('{sid}', '{token}', "
            f"'default', 'environment', '{issuer}', '{method}', 'Seeded {sid}', {T0}, NULL, "
            f"{T0 + 90 * 86400.0})")


# Signed-in browsers on the rich bank: the laptop key's and the phone key's
# passkey sign-ins, and a link sign-in, which no key revocation touches.
SESSIONS = (_session("sess-laptop", IDS["laptop"], "passkey"),
            _session("sess-phone", IDS["phone"], "passkey"),
            _session("sess-link", "0" * 64, "link"))


_FIXTURE = Path(__file__).with_name("maintainer.fixtures.json")
_SEED_TABLES = ("meta", "maintainer_nonces", "maintainer_passkeys",
                "maintainer_bootstrap", "coordination_events")
_SEED_SHAPES = {
    "fresh": {"meta": 1},
    "stale": {"meta": 1, "maintainer_bootstrap": 1},
    "pending": {table: 1 for table in _SEED_TABLES},
    "rich": {"meta": 1, "maintainer_nonces": 10, "maintainer_passkeys": 5,
             "maintainer_bootstrap": 1, "coordination_events": 7},
}


def _fixture():
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _require_owned_bank(name):
    _bank.NAMES.require_owned(name)
    if not name.startswith(PREFIX):
        raise ValueError("maintainer fixture requires an owned maintainer bank")


def _require_bank(conn, name):
    _require_owned_bank(name)
    if assert_disposable_database(conn) != name:
        raise ValueError("server reported a different database from the owned maintainer fixture")


def _validate_rows(rows, expected):
    if {table: len(records) for table, records in rows.items()} != expected:
        raise ValueError("incomplete or unexpected maintainer fixture rows")


def _seed(conn, name, kind):
    _require_bank(conn, name)
    rows = _fixture()["seeds"][kind]
    _validate_rows(rows, _SEED_SHAPES[kind])
    if rows["meta"][0]["key"] != "maintainer_secret_v1":
        raise ValueError("maintainer fixture is missing its secret row")
    with conn.transaction():
        for table in _SEED_TABLES:
            if table in rows:
                insert_fixture_rows(conn, table, rows[table])


_BASELINE: dict[str, dict] = {}
_CREATED: set[str] = set()


def _name(kind: str) -> str:
    return _bank.name(PREFIX + kind)


def _drop(name: str, *, admin=None) -> None:
    """``_bank.drop``, waiting out an autovacuum worker the test login may
    not end (it finishes on its own)."""
    import psycopg  # noqa: PLC0415
    for attempt in range(60):
        try:
            _bank.drop(name, admin=admin)
            return
        except (psycopg.errors.InsufficientPrivilege, psycopg.errors.ObjectInUse):
            if attempt == 59:
                raise
            time.sleep(0.5)


def _drop_all() -> None:
    for name in sorted(_CREATED):
        with contextlib.suppress(Exception):
            _drop(name)


atexit.register(_drop_all)


def _copy(source: str, target: str) -> None:
    _require_owned_bank(source)
    _require_owned_bank(target)
    refuse_production_database(source)
    refuse_production_database(target)
    with _bank._admin() as conn:
        if assert_disposable_database(conn) != "postgres":
            raise ValueError("maintainer fixture copy requires the test admin database")
        _drop(target, admin=conn)
        _CREATED.add(target)
        conn.execute(f'CREATE DATABASE "{target}" TEMPLATE "{source}"')


def ensure(kind: str) -> str:
    """The seeded bank of this kind (created once per harness process)."""
    name = _name(kind)
    if name in _BASELINE:
        return name
    _CREATED.add(name)
    if kind == "notables":
        _bank.create(name, schema=False)
    else:
        _bank.create(name)
        if kind in _SEED_SHAPES:
            with _bank.connect(name, autocommit=True) as conn:
                _seed(conn, name, kind)
    _BASELINE[name] = _bank.dump(name)
    return name


def changes(baseline: dict, dump: dict) -> dict:
    """A post-state as what differs from the arm's starting bank: the
    starting rows' digest (both arms start from one seed, and seeding is
    deterministic, so the digest is equal unless the seed changed), every
    table whose rows changed (all its rows), any table that disappeared, and
    every other dump section (sequences, columns, constraints, indexes) that
    changed, in full. Equal for two arms exactly when their full dumps are,
    given equal starting banks; a golden then holds only what each case
    changed."""
    seed = hashlib.sha256(json.dumps(baseline["tables"], sort_keys=True).encode()).hexdigest()
    out: dict = {"seed": seed}
    for section in sorted(set(baseline) | set(dump)):
        before, after = baseline.get(section), dump.get(section)
        if section == "tables":
            out["tables"] = {name: rows for name, rows in sorted(after.items())
                             if before.get(name) != rows}
            gone = sorted(set(before) - set(after))
            if gone:
                out["tables_dropped"] = gone
        elif before != after:
            out[section] = after
    return out


def _secret(dump) -> str | None:
    for text in dump["tables"].get("meta", []):
        row = json.loads(text)
        if row.get("key") == "maintainer_secret_v1":
            return row["value"]
    return None


# --- named rules ---------------------------------------------------------------

def _native(text: str) -> bytes:
    return (text.replace("\n", "\r\n") if core.WINDOWS else text).encode("utf-8")


_CODE_LINE = re.compile(rb"One-time enrolment code: ([A-Z2-7]{10})(\r?\n)")


def _in(value, window, shift=0.0) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and window[0] - 1 <= value - shift <= window[1] + 1)


@normalize.rule("maintainer-host-write")
def maintainer_host_write(obs: dict) -> None:
    """What the CLI writes from its own clock and randomness, validated then
    tokenized (seeded and harness-written values stay exact):

    - ``maintainer_key`` events by the operator (empty principal, agent,
      project, task and hlc) whose ``created_at`` lies in this arm's run
      window and whose ``hash`` matches the harness's ``audit_hash`` of the row
      over its ``prev_hash``: ``created_at`` and ``hash`` become tokens, as
      does any later row's ``prev_hash`` naming that hash;
    - a passkey's ``active_from`` / ``revoked_at`` inside the window;
    - a bootstrap row whose ``expires_at`` is the window plus BOOTSTRAP_TTL
      and whose ``code_hash`` is sha256 of the one 10-symbol base32 code
      stdout printed: ``expires_at``, ``code_hash`` and the printed code.
      When stdout is empty (a refused stdout: the code was never shown),
      the one such unused row's 64-hex ``code_hash`` and ``expires_at``;
    - the secret, when it differs from the seed's and is 64 lowercase hex.
    """
    from rust.harness_support.audit_oracle import audit_hash  # noqa: PLC0415
    db = obs.get("db")
    if not isinstance(db, dict):
        return
    window = obs["window"]
    tables = db["tables"]
    events = [json.loads(text) for text in tables.get("coordination_events", [])]
    hashes: dict[str, str] = {}
    for row in sorted(events, key=lambda r: r["seq"]):
        if (row["event"] == "maintainer_key" and row["actor"] == "operator"
                and row["principal"] == "" and row["agent_id"] == ""
                and row["recipient_agent_id"] is None and row["message_id"] is None
                and row["project"] == "" and row["task"] == "" and row["hlc"] == ""
                and _in(row["created_at"], window)
                and audit_hash(row["prev_hash"], row) == row["hash"]):
            hashes[row["hash"]] = f"<host-hash-{len(hashes) + 1}>"
            row["created_at"] = "<clock>"
    for row in events:
        row["hash"] = hashes.get(row["hash"], row["hash"])
        row["prev_hash"] = hashes.get(row["prev_hash"], row["prev_hash"])
    if events:
        tables["coordination_events"] = sorted(json.dumps(r, sort_keys=True) for r in events)
    keys = [json.loads(text) for text in tables.get("maintainer_passkeys", [])]
    for row in keys:
        for field in ("active_from", "revoked_at"):
            if _in(row[field], window):
                row[field] = "<clock>"
    if keys:
        tables["maintainer_passkeys"] = sorted(json.dumps(r, sort_keys=True) for r in keys)
    # v59: a revoked key's Console sign-ins end on the database clock.
    sessions = [json.loads(text) for text in tables.get("console_sessions", [])]
    for row in sessions:
        if _in(row["revoked_at"], window):
            row["revoked_at"] = "<clock>"
    if sessions:
        tables["console_sessions"] = sorted(json.dumps(r, sort_keys=True) for r in sessions)
    stdout = base64.b64decode(obs["stdout"])
    printed = _CODE_LINE.findall(stdout)
    codes = [json.loads(text) for text in tables.get("maintainer_bootstrap", [])]
    unshown = [row for row in codes
               if not stdout and row["used_at"] is None and _in(row["expires_at"], window, 600.0)
               and re.fullmatch(r"[0-9a-f]{64}", row["code_hash"])]
    for row in codes:
        if (len(printed) == 1 and _in(row["expires_at"], window, 600.0)
                and row["code_hash"] == hashlib.sha256(printed[0][0]).hexdigest()):
            row["expires_at"] = "<clock+600>"
            row["code_hash"] = "<code-hash>"
            obs["stdout"] = base64.b64encode(
                _CODE_LINE.sub(rb"One-time enrolment code: <code>\2", stdout)).decode()
        elif len(unshown) == 1 and row is unshown[0]:
            row["expires_at"] = "<clock+600>"
            row["code_hash"] = "<unshown-code-hash>"
    if codes:
        tables["maintainer_bootstrap"] = sorted(json.dumps(r, sort_keys=True) for r in codes)
    meta = [json.loads(text) for text in tables.get("meta", [])]
    for row in meta:
        value = row["value"]
        if (row["key"] == "maintainer_secret_v1" and value != obs.get("seed_secret")
                and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)):
            row["value"] = "<rotated-secret>"
    if meta:
        tables["meta"] = sorted(json.dumps(r, sort_keys=True) for r in meta)


_TRAILER = re.compile(rb"Exception ignored in: <_io\.TextIOWrapper name='<stdout>'"
                      rb"[^\r\n]*\r?\n(?:BrokenPipeError|OSError): [^\r\n]*\r?\n\Z")


@normalize.rule("maintainer-terminated-class")
def maintainer_terminated_class(obs: dict) -> None:
    """Declared oracle race: when the waiting connection's backend is
    terminated, psycopg names the server's FATAL (``AdminShutdown``) only if
    libpq read that message before the socket closed; otherwise it raises
    ``OperationalError``. The maintainer host printed ``AdminShutdown``,
    hosted Windows CI ``OperationalError`` (2026-10-10, run 38007737431).
    The native client always reads the FATAL. Only the oracle arm's exact
    ``OperationalError`` line, at exit 2, becomes ``AdminShutdown``; the
    native arm is not rewritten, so it must still name the FATAL."""
    if obs.get("arm") != "python" or obs["exit"] != 2:
        return
    stderr = base64.b64decode(obs["stderr"])
    lost = b"error: OperationalError; check PSEUDOLIFE_MCP_DATABASE_URL"
    if stderr.rstrip(b"\r\n") == lost:
        named = stderr.replace(b"OperationalError", b"AdminShutdown", 1)
        obs["stderr"] = base64.b64encode(named).decode()


@normalize.rule("maintainer-stdout-closed-trailer")
def maintainer_stdout_closed_trailer(obs: dict) -> None:
    """Declared substitution: after a committed change whose report stdout
    refused, CPython's interpreter-shutdown flush fails, prints an
    ignored-exception trailer and exits 120. The native CLI keeps exit 120
    and prints no synthetic trailer. Only that exact trailer, with exit 120,
    is removed."""
    stderr = base64.b64decode(obs["stderr"])
    if obs["exit"] == 120 and _TRAILER.search(stderr):
        obs["stderr"] = base64.b64encode(_TRAILER.sub(b"", stderr)).decode()




# --- the waiting form's harness side ---------------------------------------------

def _live_code(conn, proc) -> str | None:
    """The CLI's committed code row (its hash), or None if it exited first."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and proc.poll() is None:
        row = conn.execute("SELECT code_hash FROM maintainer_bootstrap "
                           "WHERE used_at IS NULL").fetchone()
        if row:
            return row[0]
        time.sleep(0.05)
    return None


def redeem_during(arm, proc) -> None:
    with _bank.connect(ARM_DB, autocommit=True) as conn:
        _require_bank(conn, ARM_DB)
        stored = _live_code(conn, proc)
        if stored is None:
            return
        # Both waiting cases copy the fresh bank. Its only captured row is
        # the fixture secret, so the CLI's new code is the only bootstrap row.
        capture = _fixture()["hooks"]["redeem"]
        nonce, key, event = ("maintainer_nonces", "maintainer_passkeys", "coordination_events")
        rows, status = capture["rows"], capture["status"]
        _validate_rows(rows, {nonce: 1, key: 1, event: 1})
        if status != {"credential_id": rows[key][0]["credential_id"],
                      "failed_attempts": 0, "used_at": HOOK_T}:
            raise ValueError("invalid maintainer redemption fixture")
        # The waiting CLI publishes the code before the hook runs. Keep its
        # actual hash and expiry while replaying the captured Console effects.
        with conn.transaction():
            insert_fixture_rows(conn, nonce, rows[nonce])
            conn.execute("LOCK TABLE maintainer_passkeys IN SHARE ROW EXCLUSIVE MODE")
            insert_fixture_rows(conn, key, rows[key])
            changed = conn.execute(
                "UPDATE maintainer_bootstrap SET used_at=%s,credential_id=%s "
                "WHERE code_hash=%s AND used_at IS NULL AND expires_at>=%s AND failed_attempts=0",
                (status["used_at"], status["credential_id"], stored, HOOK_T),
            ).rowcount
            if changed != 1:
                raise RuntimeError("maintainer fixture did not redeem the waiting code")
            insert_fixture_rows(conn, event, rows[event])


def burn_during(arm, proc) -> None:
    with _bank.connect(ARM_DB, autocommit=True) as conn:
        _require_bank(conn, ARM_DB)
        stored = _live_code(conn, proc)
        if stored is None:
            return
        # As in redeem_during, the waiting command started on a fresh copy.
        capture = _fixture()["hooks"]["burn"]
        if capture != {"rows": {}, "status": {"credential_id": None,
                                                "failed_attempts": 5, "used_at": None}}:
            raise ValueError("invalid maintainer burn fixture")
        # Wrong guesses commit separately and leave no spent nonce. Only the
        # last committed increment makes the waiting command report a burn.
        for attempt in range(capture["status"]["failed_attempts"]):
            changed = conn.execute(
                "UPDATE maintainer_bootstrap SET failed_attempts=failed_attempts+1 "
                "WHERE code_hash=%s AND used_at IS NULL AND expires_at>=%s AND failed_attempts=%s",
                (stored, HOOK_T, attempt),
            ).rowcount
            if changed != 1:
                raise RuntimeError("maintainer fixture did not burn the waiting code")


def terminate_during(arm, proc) -> None:
    """End the waiting CLI's backend from the server side (the oracle then
    reports psycopg's class for SQLSTATE 57P01). Only this login's other
    sessions on this arm's own disposable copy."""
    _require_owned_bank(ARM_DB)
    with _bank.connect(ARM_DB, autocommit=True) as conn:
        if assert_disposable_database(conn) != ARM_DB:
            raise ValueError("server reported a different database from the owned maintainer fixture")
        if _live_code(conn, proc) is None:
            return
        # Past the first poll, so the CLI is between polls.
        time.sleep(0.5)
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                     "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                     "AND usename = current_user")


def lock_during(arm, proc) -> None:
    """Hold the code table while the CLI waits: its next poll meets the
    session's 5 s lock timeout (SQLSTATE 55P03)."""
    import psycopg  # noqa: PLC0415
    with _bank.connect(ARM_DB, autocommit=True) as conn:
        _require_bank(conn, ARM_DB)
        if _live_code(conn, proc) is None:
            return
        with conn.transaction():
            conn.execute("LOCK TABLE maintainer_bootstrap IN ACCESS EXCLUSIVE MODE")
            deadline = time.monotonic() + 30
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            raise psycopg.Rollback


def refuse_at_commit(table: str, errcode: str) -> tuple[str, ...]:
    """Test instrument: a deferred constraint trigger that fails the change's
    COMMIT with ``errcode``, after every statement before it succeeded."""
    return (
        "CREATE FUNCTION harness_refuse_at_commit() RETURNS trigger LANGUAGE plpgsql AS "
        f"$$BEGIN RAISE EXCEPTION 'refused at commit' USING ERRCODE = '{errcode}'; END$$",
        "CREATE CONSTRAINT TRIGGER harness_refuse_at_commit AFTER INSERT OR UPDATE ON "
        f"{table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION harness_refuse_at_commit()",
    )


# --- cases -----------------------------------------------------------------------

GUARDS = {
    # Never the daemon container or a lite bank on the host running this.
    "PSEUDOLIFE_DAEMON_EXEC": "1",
    "PSEUDOLIFE_DOCKER": "{HOME}/no-docker",
    "PSEUDOLIFE_MCP_DATA_DIR": "{HOME}/no-bank",
    "PSEUDOLIFE_MCP_DATABASE_URL": None,
}
WRITES = ("maintainer-host-write",)


class _DatabaseUrl(str):
    """Keep the child environment value intact while hiding it in case reprs."""

    def __repr__(self) -> str:
        return "'<database URL withheld>'"


def case(case_id, kind, argv, *, rules=WRITES, during=None, env=None, dsn=True,
         stdout_closed=False, skip_if=None, prepare=(), note="", programs=(), golden=True):
    """``maintainer <argv>`` on this arm's own copy of a seeded bank;
    ``prepare`` statements run on that copy first (test instruments)."""

    def setup(arm):
        _copy(ensure(kind), ARM_DB)
        if prepare:
            with _bank.connect(ARM_DB, autocommit=True) as conn:
                _require_bank(conn, ARM_DB)
                for statement in prepare:
                    conn.execute(statement)
        arm.state["prepared"] = _bank.dump(ARM_DB) if prepare else _BASELINE[_name(kind)]

    def after(arm, obs):
        obs["arm"] = arm.name
        dump = _bank.dump(ARM_DB)
        baseline = arm.state["prepared"]
        obs["seed_secret"] = _secret(baseline)
        obs["db"] = "unchanged" if dump == baseline else changes(baseline, dump)

    environment = dict(GUARDS)
    if dsn:
        environment["PSEUDOLIFE_MCP_DATABASE_URL"] = _bank.url(ARM_DB)
    environment.update(env or {})
    key = "PSEUDOLIFE_MCP_DATABASE_URL"
    if environment.get(key) is not None:
        environment[key] = _DatabaseUrl(environment[key])
    return core.Case(case_id, ["maintainer", *argv], env=environment, setup=setup,
                     during=during, after=after, rules=rules, timeout=60,
                     stdout_closed=stdout_closed, skip_if=skip_if, note=note,
                     programs=programs, golden=golden)


LISTING = WRITES
CLOSED = WRITES + ("maintainer-stdout-closed-trailer",)


def defer(case_id, kind, argv, **kwargs):
    """Retain historical case IDs while testing their native answers."""
    return case(case_id, kind, argv, **kwargs)


def cases() -> list[core.Case]:
    pending, laptop = IDS["pending"], IDS["laptop"]
    from . import lease_transport
    transport = []
    for engine in ('rust', 'python', '<no value>'):
        env = {'PSEUDOLIFE_DOCKER': '{HOME}/fake_docker.' + ('cmd' if core.WINDOWS else 'sh'),
               'PSEUDOLIFE_MCP_DATABASE_URL': None, 'PSEUDOLIFE_DAEMON_EXEC': None,
               'FIXTURE_DOCKER_ENGINE': engine}
        transport.append(core.Case('container-' + engine, ['maintainer', 'list'], env=env,
                                   stdin=b'container-input\x00\xff\n',
                                   setup=lease_transport._docker_setup,
                                   after=lease_transport._docker_after('list', [], engine, 'maintainer')))
    return transport + [
        # list
        case("list-empty", "empty", ["list"]),
        case("list-pending", "pending", ["list"], rules=LISTING),
        case("list-rich", "rich", ["list"], rules=LISTING),
        case("list-read-error", "rich", ["list"], prepare=(
            "ALTER TABLE maintainer_passkeys RENAME COLUMN label TO hidden_label",)),
        case("list-column-type-error", "rich", ["list"], prepare=(
            "ALTER TABLE maintainer_passkeys ALTER COLUMN label TYPE jsonb USING to_jsonb(label)",)),
        # the bank has no maintainer tables (schema before v54)
        case("no-tables-list", "notables", ["list"]),
        case("no-tables-enrol-code", "notables", ["enrol-code", "--no-wait"]),
        case("no-tables-reset", "notables", ["reset"]),
        # enrol-code
        case("enrol-code-no-wait", "fresh", ["enrol-code", "--no-wait"]),
        case("enrol-code-no-secret", "empty", ["enrol-code", "--no-wait"]),
        case("enrol-code-replaces-unused", "stale", ["enrol-code", "--no-wait"]),
        case("enrol-code-refused-pending", "pending", ["enrol-code", "--no-wait"]),
        case("enrol-code-refused-active", "rich", ["enrol-code"]),
        case("enrol-code-wait-redeemed", "fresh", ["enrol-code"], during=redeem_during),
        case("enrol-code-wait-burned", "fresh", ["enrol-code"], during=burn_during),
        # confirm
        case("confirm-dash-prefix", "pending", ["confirm", pending[:9]]),
        case("confirm-six", "pending", ["confirm", pending[:6]]),
        case("confirm-full-id", "pending", ["confirm", pending]),
        case("confirm-not-pending", "rich", ["confirm", laptop[:10]]),
        case("confirm-short", "pending", ["confirm", "abc"]),
        case("confirm-unknown", "pending", ["confirm", "zzzzzzzz"]),
        # revoke
        case("revoke-active", "rich", ["revoke", laptop[:8]]),
        case("revoke-quarantined-underscore", "rich", ["revoke", IDS["phone"][:12]]),
        case("revoke-pending", "pending", ["revoke", pending[:8]]),
        case("revoke-already-revoked", "rich", ["revoke", IDS["spare"][:8]]),
        case("revoke-ambiguous", "rich", ["revoke", "twinAB"]),
        case("revoke-twin-unique", "rich", ["revoke", "twinAB2"]),
        # '_' is a LIKE wildcard: unescaped, this would match cancelXd...
        case("revoke-underscore-wildcard", "rich", ["revoke", "cancel_dRev"]),
        case("revoke-odd-percent", "rich", ["revoke", "a%b_cd"]),
        case("revoke-odd-space", "rich", ["revoke", "abc def"]),
        case("revoke-empty", "rich", ["revoke", ""]),
        case("revoke-non-ascii", "rich", ["revoke", "twinABé1"]),
        # v59: a host revoke ends that key's passkey sign-ins only; reset ends
        # every passkey's; link sign-ins stay. Live only: added after the
        # goldens were recorded (see maintainer.md).
        case("revoke-ends-the-keys-sign-ins", "rich", ["revoke", laptop[:8]],
             prepare=SESSIONS, golden=False),
        case("revoke-already-revoked-ends-nothing", "rich", ["revoke", IDS["spare"][:8]],
             prepare=SESSIONS, golden=False),
        # reset
        case("reset-without-yes", "rich", ["reset"]),
        case("reset-rich", "rich", ["reset", "--yes"]),
        case("reset-pending", "pending", ["reset", "--yes"]),
        case("reset-empty", "empty", ["reset", "--yes"]),
        case("reset-ends-every-passkey-sign-in", "rich", ["reset", "--yes"],
             prepare=SESSIONS, golden=False),
        # a COMMIT that fails after every statement succeeded: Python's error
        # path with psycopg's class for the SQLSTATE (exact, then the class
        # prefix fallback), never a deferral
        case("commit-refused-confirm", "pending", ["confirm", pending[:9]],
             prepare=refuse_at_commit("maintainer_passkeys", "P0001")),
        case("commit-refused-reset", "rich", ["reset", "--yes"],
             prepare=refuse_at_commit("maintainer_passkeys", "XX999")),
        case("commit-refused-enrol-code", "fresh", ["enrol-code", "--no-wait"],
             prepare=refuse_at_commit("maintainer_bootstrap", "57P01")),
        # the bank fails while the committed code waits
        case("enrol-code-wait-terminated", "fresh", ["enrol-code"], during=terminate_during,
             rules=(*WRITES, "maintainer-terminated-class")),
        case("enrol-code-wait-lock-timeout", "fresh", ["enrol-code"], during=lock_during),
        # a refused stdout after a committed change
        case("confirm-stdout-closed", "pending", ["confirm", pending[:9]], stdout_closed=True,
             rules=CLOSED),
        case("enrol-code-no-wait-stdout-closed", "fresh", ["enrol-code", "--no-wait"],
             stdout_closed=True, rules=CLOSED),
        case("enrol-code-wait-stdout-closed", "fresh", ["enrol-code"], stdout_closed=True,
             rules=CLOSED),
        # Historical argument, environment and stdout regressions.
        defer("defer-list-stdout-closed", "rich", ["list"], stdout_closed=True),
        defer("defer-list-tz-set", "rich", ["list"], env={"TZ": "UTC"}),
        defer("defer-help", "rich", ["--help"]),
        defer("defer-list-help", "rich", ["list", "--help"]),
        defer("defer-enrol-poll", "fresh", ["enrol-code", "--poll", "0.01", "--no-wait"]),
        defer("defer-confirm-double-dash", "pending", ["confirm", "--", pending[:9]]),
        defer("defer-reset-repeated-yes", "rich", ["reset", "--yes", "--yes"]),
        # No DSN: the oracle would re-run in the daemon container through
        # docker (daemon_exec), which GUARDS already block; a PATH inside the
        # home finds none either (core's preflight).
        defer("defer-no-dsn", "rich", ["list"], dsn=False,
              env={"PATH": "{HOME}" + os.sep + "no-bin"}, programs=("docker",)),
        defer("defer-dsn-option", "rich", ["list"], env={
            "PSEUDOLIFE_MCP_DATABASE_URL": _bank.url(ARM_DB) + "?application_name=x"}),
    ]

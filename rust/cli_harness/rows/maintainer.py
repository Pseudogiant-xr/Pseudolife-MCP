"""CLI-MAINTAINER: ``pseudolife-mcp maintainer`` (maintainer_cli.py).

Banks are seeded through the oracle's own writers only: ``MaintainerStore``
issues challenges, ``bootstrap_code`` admits the first key, the Console's
enrolment (``enrol_bootstrap``, ``approve_enrol`` + ``enrol_approved``),
``cancel`` and the sign-count flag run on the test suite's software
authenticator (``tests.maintainer_authenticator``), and the host's
``confirm`` activates the first key. Credential ids are chosen (the
authenticator's id is its own random bytes in the tests; here fixed bytes,
so prefixes are known), and the seed clock is fixed per harness process.
Each kind is seeded once; every arm of every case gets its own ``CREATE
DATABASE ... TEMPLATE`` copy, so both arms start from identical rows,
including the random secret, codes and nonces seeding wrote.

The waiting ``enrol-code`` cases act from the harness while the CLI waits
(``Case.during``): once the CLI's code row exists, the oracle's
``enrol_bootstrap`` redeems it, or five wrong guesses through it burn it.
The plaintext code is only on the CLI's stdout, which the harness reads
after exit, so the redemption runs with ``storage.maintainer.code_hash``
answering the stored hash (the one seam replaced; every row is still the
oracle's), a fixed clock and a fixed challenge nonce.
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
import types
from contextlib import contextmanager
from unittest import mock

from .. import core, normalize
from ..mutants import Mutant
from . import _bank

PREFIX = "pl_cf_w1c_maint_"
ARM_DB = PREFIX + "arm"
# Seed clock: two hours before this harness process started, whole minute.
# Recent on purpose: Windows shows list times only in the current year.
T0 = float(int(time.time()) // 60 * 60 - 7200)
# The waiting cases' redemption clock: after every seed, before any run.
HOOK_T = T0 + 3600.0

# Chosen credential ids (16 bytes each, unpadded base64url).
IDS = {
    "pending": "-pendingKeyAAAAAAAAAAA",   # a pending bootstrap key; '-' leads
    "laptop": "-dashKeyAAAAAAAAAAAAAA",    # active (bootstrap, host confirm)
    "phone": "phoneKey_quarantineAAA",     # active, quarantined; '_' inside
    "twin1": "twinAB1keyDAAAAAAAAAAA",     # twin1 and twin2 share 'twinAB'
    "twin2": "twinAB2keyEAAAAAAAAAAA",
    "spare": "cancelXdRevokedAAAAAAA",     # revoked (cancelled by laptop)
    "desk": "deskKeyHookAAAAAAAAAAA",      # redeemed by the waiting case
}
LABELS = {"pending": "laptop", "laptop": "laptop", "phone": "Tom's phone ☎",
          "twin1": 'it\'s "twin"', "twin2": "twin two", "spare": "spare",
          "desk": "desk key"}
WRONG_CODE = "WRONGCODE2"


class _Storage:
    """The minimal storage the oracle store runs on (as the CLI's own)."""

    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def _txn(self):
        with self.conn.transaction():
            yield


def _store(conn, clock):
    from pseudolife_memory.storage.maintainer import MaintainerStore  # noqa: PLC0415
    from tests.maintainer_authenticator import ORIGIN, RP_ID  # noqa: PLC0415
    return MaintainerStore(_Storage(conn), rp_id=RP_ID, origin=ORIGIN, clock=clock)


_AUTHS: dict = {}


def auth(name: str):
    """One software authenticator per chosen id, shared by every arm."""
    if name not in _AUTHS:
        from tests.maintainer_authenticator import SoftAuthenticator  # noqa: PLC0415
        # A nonzero count makes every assertion advance it, so a stale one
        # is a regression the store flags.
        made = SoftAuthenticator(sign_count=1)
        made.credential_id = base64.urlsafe_b64decode(IDS[name] + "==")
        if made.id != IDS[name]:
            raise RuntimeError(f"{name}: id {IDS[name]} is not canonical base64url")
        _AUTHS[name] = made
    return _AUTHS[name]


def _challenge(payload):
    from pseudolife_memory.storage.maintainer import challenge_bytes  # noqa: PLC0415
    return challenge_bytes(payload)


def _bootstrap(store, name):
    """The first key, as the Console redeems a host code."""
    code = store.bootstrap_code()
    payload, mac = store.issue("enrol-bootstrap", {"label": LABELS[name]})
    return store.enrol_bootstrap(payload, mac, auth(name).register(_challenge(payload)), code)


def _add(store, signer, name):
    """Another key: ``signer`` approves the label, then the new key registers."""
    payload, mac = store.issue("enrol-approve", {"label": LABELS[name]})
    approved = store.approve_enrol(payload, mac, auth(signer).assertion(_challenge(payload)))
    attestation = auth(name).register(_challenge(approved["payload"]))
    return store.enrol_approved(approved["payload"], approved["mac"], attestation)


def _clock():
    now = [T0]

    def tick():
        now[0] += 60.0
        return now[0]
    return now, tick


def seed_fresh(conn):
    """A daemon that has issued one challenge: the secret exists, no keys."""
    now, _ = _clock()
    _store(conn, lambda: now[0]).issue("enrol-bootstrap", {"label": "unused"})


def seed_stale(conn):
    """As fresh, plus an unused host code nobody redeemed."""
    now, _ = _clock()
    store = _store(conn, lambda: now[0])
    store.issue("enrol-bootstrap", {"label": "unused"})
    store.bootstrap_code()


def seed_pending(conn):
    now, _ = _clock()
    _bootstrap(_store(conn, lambda: now[0]), "pending")


def seed_rich(conn):
    from pseudolife_memory.storage.maintainer import MaintainerError  # noqa: PLC0415
    now, tick = _clock()
    store = _store(conn, lambda: now[0])
    _bootstrap(store, "laptop")
    tick()
    store.confirm(IDS["laptop"][:8])
    for name in ("phone", "twin1", "twin2", "spare"):
        tick()
        _add(store, "laptop", name)
    tick()
    payload, mac = store.issue("cancel", {"credential_id": IDS["spare"]})
    store.cancel(payload, mac, auth("laptop").assertion(_challenge(payload)))
    tick()
    # A stale sign count: refused, and the key is flagged for review.
    payload, mac = store.issue("enrol-approve", {"label": "stale"})
    stale = auth("laptop").assertion(_challenge(payload), sign_count=auth("laptop").sign_count)
    try:
        store.approve_enrol(payload, mac, stale)
    except MaintainerError:
        pass
    else:
        raise RuntimeError("the stale assertion was accepted")


SEEDERS = {"fresh": seed_fresh, "stale": seed_stale, "pending": seed_pending,
           "rich": seed_rich}
_BASELINE: dict[str, dict] = {}
_CREATED: set[str] = set()


def _name(kind: str) -> str:
    return PREFIX + kind


def _drop(name: str) -> None:
    """``_bank.drop``, waiting out an autovacuum worker the test login may
    not end (it finishes on its own)."""
    import psycopg  # noqa: PLC0415
    for attempt in range(60):
        try:
            _bank.drop(name)
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
    from pseudolife_memory.storage.schema import assert_disposable_database  # noqa: PLC0415
    _bank.url(target)  # validates the name: pl_cf_ only
    _drop(target)
    _CREATED.add(target)
    with _bank._admin() as conn:
        assert_disposable_database(conn)
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
        if kind in SEEDERS:
            with _bank.connect(name, autocommit=True) as conn:
                SEEDERS[kind](conn)
    _BASELINE[name] = _bank.dump(name)
    return name


def _secret(dump) -> str | None:
    for text in dump["tables"].get("meta", []):
        row = json.loads(text)
        if row.get("key") == "maintainer_secret_v1":
            return row["value"]
    return None


# --- named rules ---------------------------------------------------------------

DEFERRAL = "pseudolife-stdio: mode 'maintainer' is deferred in this candidate\n"


def _native(text: str) -> bytes:
    return (text.replace("\n", "\r\n") if core.WINDOWS else text).encode("utf-8")


@normalize.rule("maintainer-deferral")
def maintainer_deferral(obs: dict) -> None:
    """A declared deferral: the candidate must print the dispatcher's
    deferral line, exit 1 and change nothing. The oracle arm still runs (on
    its own copy), but its exit, streams and bank are replaced by that
    expectation: no output, and a bank equal to the seed."""
    if obs.get("arm") != "python":
        return
    obs["exit"] = 1
    obs["stdout"] = ""
    obs["stderr"] = base64.b64encode(_native(DEFERRAL)).decode()
    obs["db"] = "unchanged"


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
      window and whose ``hash`` is the oracle's ``audit_hash`` of the row
      over its ``prev_hash``: ``created_at`` and ``hash`` become tokens, as
      does any later row's ``prev_hash`` naming that hash;
    - a passkey's ``active_from`` / ``revoked_at`` inside the window;
    - a bootstrap row whose ``expires_at`` is the window plus BOOTSTRAP_TTL
      and whose ``code_hash`` is sha256 of the one 10-symbol base32 code
      stdout printed: ``expires_at``, ``code_hash`` and the printed code;
    - the secret, when it differs from the seed's and is 64 lowercase hex.
    """
    from pseudolife_memory.storage.coordination import audit_hash  # noqa: PLC0415
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
    stdout = base64.b64decode(obs["stdout"])
    printed = _CODE_LINE.findall(stdout)
    codes = [json.loads(text) for text in tables.get("maintainer_bootstrap", [])]
    for row in codes:
        if (len(printed) == 1 and _in(row["expires_at"], window, 600.0)
                and row["code_hash"] == hashlib.sha256(printed[0][0]).hexdigest()):
            row["expires_at"] = "<clock+600>"
            row["code_hash"] = "<code-hash>"
            obs["stdout"] = base64.b64encode(
                _CODE_LINE.sub(rb"One-time enrolment code: <code>\2", stdout)).decode()
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


@contextmanager
def _fixed_nonce():
    from pseudolife_memory.storage import maintainer  # noqa: PLC0415
    fixed = types.SimpleNamespace(token_hex=lambda n: "5e" * n, choice=None)
    with mock.patch.object(maintainer, "secrets", fixed):
        yield


def redeem_during(arm, proc) -> None:
    from pseudolife_memory.storage import maintainer  # noqa: PLC0415
    with _bank.connect(ARM_DB, autocommit=True) as conn:
        stored = _live_code(conn, proc)
        if stored is None:
            return
        store = _store(conn, lambda: HOOK_T)
        with _fixed_nonce():
            payload, mac = store.issue("enrol-bootstrap", {"label": LABELS["desk"]})
        attestation = auth("desk").register(_challenge(payload))
        with mock.patch.object(maintainer, "code_hash", lambda _code: stored):
            store.enrol_bootstrap(payload, mac, attestation, "<from the CLI's stdout>")


def burn_during(arm, proc) -> None:
    from pseudolife_memory.storage.maintainer import (  # noqa: PLC0415
        BOOTSTRAP_MAX_FAILURES, MaintainerError,
    )
    with _bank.connect(ARM_DB, autocommit=True) as conn:
        if _live_code(conn, proc) is None:
            return
        store = _store(conn, lambda: HOOK_T)
        with _fixed_nonce():
            payload, mac = store.issue("enrol-bootstrap", {"label": LABELS["desk"]})
        attestation = auth("desk").register(_challenge(payload))
        for _ in range(BOOTSTRAP_MAX_FAILURES):
            try:
                store.enrol_bootstrap(payload, mac, attestation, WRONG_CODE)
            except MaintainerError as exc:
                if exc.code != "bootstrap_code_invalid":
                    raise


# --- cases -----------------------------------------------------------------------

GUARDS = {
    # Never the daemon container or a lite bank on the host running this.
    "PSEUDOLIFE_DAEMON_EXEC": "1",
    "PSEUDOLIFE_DOCKER": "{HOME}/no-docker",
    "PSEUDOLIFE_MCP_DATA_DIR": "{HOME}/no-bank",
    "PSEUDOLIFE_MCP_DATABASE_URL": None,
}
WRITES = ("maintainer-host-write",)


def case(case_id, kind, argv, *, rules=WRITES, during=None, env=None, dsn=True,
         stdout_closed=False, skip_if=None, note=""):
    """``maintainer <argv>`` on this arm's own copy of a seeded bank."""

    def setup(arm):
        _copy(ensure(kind), ARM_DB)

    def after(arm, obs):
        obs["arm"] = arm.name
        dump = _bank.dump(ARM_DB)
        baseline = _BASELINE[_name(kind)]
        obs["seed_secret"] = _secret(baseline)
        obs["db"] = "unchanged" if dump == baseline else dump

    environment = dict(GUARDS)
    if dsn:
        environment["PSEUDOLIFE_MCP_DATABASE_URL"] = _bank.url(ARM_DB)
    environment.update(env or {})
    return core.Case(case_id, ["maintainer", *argv], env=environment, setup=setup,
                     during=during, after=after, rules=rules, timeout=60,
                     stdout_closed=stdout_closed, skip_if=skip_if, note=note)


def _tz_set() -> bool:
    """The native listing defers its local times while TZ is set."""
    return "TZ" in os.environ


def defer(case_id, kind, argv, **kwargs):
    return case(case_id, kind, argv, rules=("maintainer-deferral",), **kwargs)


def cases() -> list[core.Case]:
    pending, laptop = IDS["pending"], IDS["laptop"]
    return [
        # list
        case("list-empty", "empty", ["list"]),
        case("list-pending", "pending", ["list"], skip_if=_tz_set),
        case("list-rich", "rich", ["list"], skip_if=_tz_set),
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
        # reset
        case("reset-without-yes", "rich", ["reset"]),
        case("reset-rich", "rich", ["reset", "--yes"]),
        case("reset-pending", "pending", ["reset", "--yes"]),
        case("reset-empty", "empty", ["reset", "--yes"]),
        # a refused stdout after a committed change
        case("confirm-stdout-closed", "pending", ["confirm", pending[:9]], stdout_closed=True,
             rules=WRITES + ("maintainer-stdout-closed-trailer",)),
        # deferred before any effect (the oracle arm writes; the candidate must not)
        defer("defer-list-stdout-closed", "rich", ["list"], stdout_closed=True),
        defer("defer-list-tz-set", "rich", ["list"], env={"TZ": "UTC"}),
        defer("defer-help", "rich", ["--help"]),
        defer("defer-list-help", "rich", ["list", "--help"]),
        defer("defer-enrol-poll", "fresh", ["enrol-code", "--poll", "0.01", "--no-wait"]),
        defer("defer-confirm-double-dash", "pending", ["confirm", "--", pending[:9]]),
        defer("defer-reset-repeated-yes", "rich", ["reset", "--yes", "--yes"]),
        defer("defer-no-dsn", "rich", ["list"], dsn=False),
        defer("defer-dsn-option", "rich", ["list"], env={
            "PSEUDOLIFE_MCP_DATABASE_URL": _bank.url(ARM_DB) + "?application_name=x"}),
    ]


MUTANTS = [
    Mutant("maint-confirm-audit-dropped", "maintainer", "shim/src/cli/maintainer/bank.rs",
           '        key_change(&tx, "confirm", Some(&id), Some(&label), None).await?;\n', "",
           ("confirm-dash-prefix",)),
    Mutant("maint-list-order-flipped", "maintainer", "shim/src/cli/maintainer/bank.rs",
           "ORDER BY created_at, credential_id\"", "ORDER BY created_at DESC, credential_id\"",
           ("list-rich",)),
    Mutant("maint-reset-without-yes-exit-2", "maintainer", "shim/src/cli/maintainer/bank.rs",
           "pass --yes\");\n            return Some(1);", "pass --yes\");\n            return Some(2);",
           ("reset-without-yes",)),
    Mutant("maint-refusal-token", "maintainer", "shim/src/cli/maintainer/mod.rs",
           "no single passkey matches that prefix", "no passkey matches that prefix",
           ("revoke-ambiguous",)),
    Mutant("maint-like-escape-dropped", "maintainer", "shim/src/cli/maintainer/bank.rs",
           "Ok(prefix.replace('_', \"\\\\_\") + \"%\")", "Ok(prefix.to_owned() + \"%\")",
           ("revoke-underscore-wildcard",)),
    Mutant("maint-revoke-rerevokes", "maintainer", "shim/src/cli/maintainer/bank.rs",
           "WHERE credential_id=$2 AND state<>'revoked'\"", "WHERE credential_id=$2\"",
           ("revoke-already-revoked",)),
    Mutant("maint-redeemed-text", "maintainer", "shim/src/cli/maintainer/bank.rs",
           "Enrolled (pending): {prefix}  label: {label}",
           "Enrolled (pending): {prefix} label: {label}", ("enrol-code-wait-redeemed",)),
    Mutant("maint-code-ttl", "maintainer", "shim/src/cli/maintainer/bank.rs",
           "let expires_at = clock()? + BOOTSTRAP_TTL as f64;",
           "let expires_at = clock()? + 60.0;", ("enrol-code-no-wait",)),
    Mutant("maint-secret-kept", "maintainer", "shim/src/cli/maintainer/bank.rs",
           '        tx.execute("DELETE FROM meta WHERE key=$1", &[&SECRET_META_KEY])\n'
           "            .await?;\n", "", ("reset-rich",)),
    Mutant("maint-unused-codes-kept", "maintainer", "shim/src/cli/maintainer/bank.rs",
           '"DELETE FROM maintainer_bootstrap WHERE used_at IS NULL"',
           '"DELETE FROM maintainer_bootstrap WHERE false"', ("enrol-code-replaces-unused",)),
]

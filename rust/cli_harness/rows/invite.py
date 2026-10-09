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

Every arm runs with no tailscale or docker CLI reachable (``PATH`` filtered,
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
ARM_DB = PREFIX + "arm"
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


def ensure(kind: str) -> str:
    """The seeded bank of this kind (created once per harness process)."""
    name = PREFIX + kind
    if name in _BASELINE:
        return name
    _CREATED.add(name)
    if kind == "noschema":
        _bank.create(name, schema=False)
    else:
        _bank.create(name)
        with _bank.connect(name, autocommit=True) as conn:
            SEEDERS[kind](conn, name)
    _BASELINE[name] = _bank.dump(name)
    return name


def fresh_copy(kind: str) -> None:
    """A fresh ``ARM_DB`` holding the seed's meta row and principals,
    copied verbatim (each arm writes into its own)."""
    seed = ensure(kind)
    _CREATED.add(ARM_DB)
    _bank.create(ARM_DB, schema=kind != "noschema")
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

def _tool_dirs_removed() -> str:
    """This host's PATH without any directory that holds a tailscale or
    docker CLI candidate: the oracle must reach neither."""
    exts = [e for e in (os.environ.get("PATHEXT") or "").split(";") if e] if core.WINDOWS else []
    names = [f"{tool}{ext}" for tool in ("tailscale", "docker") for ext in ["", *exts]]
    kept = [d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d and not any(os.path.lexists(os.path.join(d.strip('"'), n)) for n in names)]
    return os.pathsep.join(kept)


def env(dsn_kind: str | None = "arm", **extra) -> dict:
    out = {
        "PATH": _tool_dirs_removed(),
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
        out["PSEUDOLIFE_MCP_DATABASE_URL"] = _bank.url(PREFIX + dsn_kind)
    out.update(extra)
    return out


# ── named rules ─────────────────────────────────────────────────────────────

_CODE = re.compile(rb"\b([0-9A-HJKMNP-TV-Z]{4})-([0-9A-HJKMNP-TV-Z]{4})-([0-9A-HJKMNP-TV-Z]{4})\b")


def _when(epoch) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch)) if epoch else "-"


@normalize.rule("invite-written")
def invite_written(obs: dict) -> None:
    """A row this arm wrote. Each clock column that differs from the seed is
    replaced only when it lies inside the arm's own run window (``<clock>``)
    or that window plus the case's code lifetime (``<clock+ttl>``). A
    changed ``code_hash`` is replaced only when it is the SHA-256 of the
    canonical form of the one code this arm printed; then that printed code
    becomes ``<code>``, and the printed expiry (JSON ``expires_at``, or the
    text line's ``(YYYY-MM-DD HH:MM UTC)``) becomes ``<expires>`` only when
    it equals the stored ``code_expires_at`` of that row."""
    db = obs.get("db")
    if not isinstance(db, dict):
        return
    start, end = obs["window"]
    ttl = obs.get("ttl") or 0
    seed = {row["principal"]: row for row in obs.get("seed_rows", [])}
    stdout = base64.b64decode(obs["stdout"])
    codes = {m.group(0) for m in _CODE.finditer(stdout)}
    printed = next(iter(codes)) if len(codes) == 1 else None
    printed_hash = (hashlib.sha256(printed.replace(b"-", b"")).hexdigest()
                    if printed else None)
    rows = _principals(db)
    expiry = None
    for row in rows:
        before = seed.get(row["principal"], {})
        for column in ("created_at", "code_expires_at", "paired_at", "revoked_at"):
            value = row.get(column)
            if value is None or before.get(column) == value:
                continue
            if start - 1 <= value <= end + 1:
                row[column] = "<clock>"
            elif ttl and start + ttl - 1 <= value <= end + ttl + 1:
                if column == "code_expires_at":
                    expiry = value
                row[column] = "<clock+ttl>"
        if row.get("code_hash") and row["code_hash"] != before.get("code_hash") \
                and row["code_hash"] == printed_hash:
            row["code_hash"] = "<printed-code-hash>"
            if expiry is not None and row["code_expires_at"] == "<clock+ttl>":
                text = stdout.replace(printed, b"<code>")
                when = f"({_when(expiry)})".encode()
                text = re.sub(rb'("expires_at": )([-0-9.e+]+)',
                              lambda m: m.group(1) + b'"<expires>"'
                              if float(m.group(2)) == expiry else m.group(0), text)
                text = text.replace(when, b"(<expires>)")
                obs["stdout"] = base64.b64encode(text).decode()
    db["tables"]["principals"] = sorted(json.dumps(row, sort_keys=True) for row in rows)


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


def _record(arm, obs) -> None:
    obs["arm"] = arm.name
    obs["before"] = arm.state.get("before", {})
    obs["before_modes"] = arm.state.get("before_modes", {})


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
        _record(arm, obs)
        name = PREFIX + kind
        dump = _bank.dump(name)
        obs["db"] = "unchanged" if dump == _BASELINE[name] else dump

    return core.Case(case_id, ["invite", *argv], env=env(kind, **(extra_env or {})),
                     setup=setup, after=after, rules=rules, daemon=daemon, timeout=60)


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
        _record(arm, obs)
        dump = _bank.dump(ARM_DB)
        obs["ttl"] = ttl
        obs["seed_rows"] = _principals(arm.state["seed"])
        obs["db"] = "unchanged" if dump == arm.state["seed"] else dump

    return core.Case(case_id, ["invite", *argv], env=env("arm", **(extra_env or {})),
                     setup=setup, after=after, rules=rules, daemon=daemon, timeout=60)


def cases() -> list[core.Case]:
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
    deferral("defer-list-no-dsn", ["--list"], extra_env={"PSEUDOLIFE_MCP_DATABASE_URL": ""})
    tailscale = "tailscale.exe" if core.WINDOWS else "tailscale"
    deferral("defer-tailscale-present", ["laptop"],
             extra_env={"PATH": "{HOME}" + SEP + "tools" + os.pathsep + _tool_dirs_removed()},
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
    Mutant("invite-revoke-dropped", "invite", "shim/src/cli/pairing/invite_db.rs",
           "SET revoked_at = COALESCE(revoked_at, {NOW}), ",
           "SET revoked_at = revoked_at, ", cases=("revoke", "revoke-json")),
    Mutant("invite-list-order", "invite", "shim/src/cli/pairing/invite_db.rs",
           "FROM public.principals ORDER BY principal\"",
           "FROM public.principals ORDER BY principal DESC\"", cases=("list", "list-json")),
    Mutant("invite-refusal-exit", "invite", "shim/src/cli/pairing/invite.rs",
           "Err(refusal) => return Ok(report.fail(EXIT_REFUSED, &refusal)),",
           "Err(refusal) => return Ok(report.fail(EXIT_FAILED, &refusal)),",
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

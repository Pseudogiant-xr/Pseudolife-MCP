"""CLI-TUNNEL-STATUS: the read-only subset of ``pseudolife-mcp tunnel``.

Canonical argv: ``tunnel status [--json] [--profile N] [--profile-dir D]``,
``tunnel update [--profile N] [--profile-dir D]``, ``tunnel verify
[--profile N] [--profile-dir D]`` and ``tunnel setup`` with
``--daemon-url``, ``--token-file``, ``--tunnel-id``, ``--organization-id``,
``--catalog``, ``--key-expires-at`` and ``--accept-access``; each option at
most once, values as the next argument. The native leaf answers status for a
profile with no process record, update when every saved profile is pending,
and the refusals that verify and setup reach before any write, handshake,
process or network use. Everything else defers (``rows/tunnel.md``).

Profiles, keys, refresh records and tokens are written through the oracle's
own writers (``ProfileStore.save`` / ``set_key`` / ``private_write``); an
invalid profile is ``private_write`` of the writer's own payload with one
field edited. Every arm runs with an empty ``PATH`` and its daemon URL on a
loopback listener that records any connection attempt (none may happen),
and every case checks that the caller's real tunnel profile directory did
not change.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import core, normalize
from ..mutants import Mutant

SEP = "\\" if core.WINDOWS else "/"
NEAR = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT00:00:00Z")
REFRESH_ID = "0123456789abcdef0123456789abcdef"
FIXTURE_KEY = "fixture-tunnel-key"
DEFERRAL = "pseudolife-stdio: mode 'tunnel' is deferred in this candidate\n"


def _home(rel: str) -> str:
    return "{HOME}" + SEP + rel.replace("/", SEP)


DIR = _home("tunnels")


# --- guard: the caller's real profile directory --------------------------------

REAL = Path(os.path.expanduser("~")) / ".pseudolife-mcp" / "tunnel"


def _fingerprint(root: Path):
    """Names, sizes, mtime_ns and sha256 of everything under ``root``, kept in
    memory only (the bytes are never written anywhere)."""
    if not os.path.lexists(root):
        return None
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in sorted(dirnames + filenames):
            path = Path(dirpath) / name
            info = os.lstat(path)
            digest = None
            if os.path.isfile(path) and not os.path.islink(path):
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError as error:
                    digest = f"unreadable:{error.__class__.__name__}"
            out[str(path.relative_to(root))] = (info.st_size, info.st_mtime_ns, digest)
    return out


_REAL_BEFORE = _fingerprint(REAL)


def _guard_real() -> None:
    if _fingerprint(REAL) != _REAL_BEFORE:
        raise RuntimeError(f"the real tunnel profile directory changed: {REAL}")


# --- guard: no connection to the daemon URL ------------------------------------

class Listener:
    """A loopback listener named as the arm's daemon: it records every
    accepted connection and answers nothing."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.accepted: list[dict] = []
        self.closing = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.closing:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.accepted.append({"method": "CONNECT", "target": "", "headers": {}, "body": ""})
            conn.close()

    def requests(self):
        return list(self.accepted)

    def close(self):
        self.closing = True
        self.thread.join(2)
        self.sock.close()


# --- seeding through the oracle's writers ---------------------------------------

def _profiles():
    import pseudolife_memory.tunnel_profiles as module  # noqa: PLC0415 (oracle writer)
    return module


def _token(home: Path) -> Path:
    path = home / "token"
    if not path.exists():
        _profiles().private_write(path, b"fixture-daemon-token")
    return path


def _root(home: Path, rel: str = "tunnels") -> Path:
    return home / Path(rel)


def profile(name="dot", *, rel="tunnels", raw_edit=None, file_name=None, **fields):
    """``ProfileStore.save`` of a valid profile, or ``private_write`` of the
    writer's own payload with ``raw_edit`` applied."""
    def seed(home: Path):
        m = _profiles()
        store = m.ProfileStore(_root(home, rel))
        value = m.Profile(name, "http://127.0.0.1:8765", str(_token(home)), **fields)
        if raw_edit is None and file_name is None:
            store.save(value)
            return
        payload = asdict(value)
        if raw_edit:
            raw_edit(payload)
        store._prepare()
        m.private_write(store.root / (file_name or f"{name}.profile.json"),
                        json.dumps(payload, indent=2).encode())
    return seed


def ready(name="dot", **fields):
    return profile(name, consent=True, tunnel_id="tunnel_0123", state="ready", **fields)


def key(name="dot", *, rel="tunnels", raw: bytes | None = None, plain_file=False):
    """``set_key`` (DPAPI on Windows, PLAIN elsewhere), or ``private_write``
    of raw key bytes; ``plain_file`` writes without owner-only protection."""
    def seed(home: Path):
        m = _profiles()
        store = m.ProfileStore(_root(home, rel))
        if raw is None:
            store.set_key(name, FIXTURE_KEY)
        elif plain_file:
            store.key_path(name).write_bytes(raw)
        else:
            m.private_write(store.key_path(name), raw)
    return seed


def private(rel: str, data: bytes):
    def seed(home: Path):
        _profiles().private_write(home / Path(rel), data)
    return seed


def plain(rel: str, data: bytes):
    def seed(home: Path):
        path = home / Path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return seed


def prepared(rel="tunnels"):
    def seed(home: Path):
        _profiles().ProfileStore(_root(home, rel))._prepare()
    return seed


def mkdir(rel: str):
    def seed(home: Path):
        (home / Path(rel)).mkdir(parents=True, exist_ok=True)
    return seed


def edit(**changes):
    def apply(payload):
        payload.update(changes)
    return apply


def refresh(rel: str, **record):
    return private(f"tunnels/{rel}", json.dumps(record).encode())


# --- cases ----------------------------------------------------------------------

def _native(text: str) -> bytes:
    return (text.replace("\n", "\r\n") if core.WINDOWS else text).encode("utf-8")


@normalize.rule("tunnel-deferral")
def tunnel_deferral(obs: dict) -> None:
    """A declared deferral case: the candidate must print the dispatcher's
    deferral line, exit 1 and change nothing. The oracle arm still runs (only
    shapes whose oracle run is local and harmless are listed), but its exit
    and streams are replaced by that expectation and its files and modes by
    the home as setup left it."""
    if obs.get("arm") != "python":
        return
    obs["exit"] = 1
    obs["stdout"] = ""
    obs["stderr"] = base64.b64encode(_native(DEFERRAL)).decode()
    before = {"stdout": "", "stderr": "", "files": dict(obs.get("before", {}))}
    normalize.home_tokens(before, obs["home"])  # as apply() did for the observed files
    obs["files"] = before["files"]
    obs["modes"] = dict(obs.get("before_modes", {}))


@normalize.rule("tunnel-dpapi-key")
def tunnel_dpapi_key(obs: dict) -> None:
    """Windows ``set_key`` output is DPAPI ciphertext, fresh per arm. A key
    file is tokenized only after the oracle's own ``_dpapi`` unprotects it to
    the fixture key, so a changed or foreign key still shows."""
    for rel, value in list(obs["files"].items()):
        if not (rel.endswith(".key") and value.startswith("file:")):
            continue
        data = base64.b64decode(value[5:])
        prefix, _, sealed = data.partition(b"\0")
        if prefix != b"DPAPI":
            continue
        try:
            plain = _profiles()._dpapi(sealed, decrypt=True)
        except Exception:  # noqa: BLE001 - an unreadable key stays as observed
            continue
        if plain == FIXTURE_KEY.encode():
            obs["files"][rel] = "file:" + base64.b64encode(b"DPAPI\0<fixture key>").decode()


def case(case_id, argv, *seeds, env=None, rules=(), platforms=("windows", "linux"), note=""):
    def setup(arm):
        for seed in seeds:
            seed(arm.home)
        arm.state["before"] = core.snapshot(arm.home)
        arm.state["before_modes"] = core.modes(arm.home)

    def after(arm, obs):
        obs["arm"] = arm.name
        obs["before"] = arm.state.get("before", {})
        obs["before_modes"] = arm.state.get("before_modes", {})
        _guard_real()

    environment = {
        "PATH": "",
        "PSEUDOLIFE_MCP_TOKEN": None,
        "PSEUDOLIFE_MCP_TOKEN_FILE": None,
        "CLAUDE_CONFIG_DIR": None,
        "CODEX_HOME": None,
        "OPENAI_API_KEY": None,
    }
    environment.update(env or {})
    return core.Case(case_id, ["tunnel", *argv], env=environment, setup=setup, after=after,
                     rules=rules, platforms=platforms, daemon=Listener, note=note)


def status(case_id, *seeds, extra=(), json_out=True, **kw):
    argv = ["status", "--profile-dir", DIR, *extra] + (["--json"] if json_out else [])
    return case(case_id, argv, *seeds, **kw)


def defer(case_id, argv, *seeds, **kw):
    return case(case_id, argv, *seeds, rules=("tunnel-deferral", "tunnel-dpapi-key"), **kw)


def setup_case(case_id, *extra, seeds=(), env=None):
    return case(case_id, ["setup", "--profile-dir", DIR, "--accept-access", *extra], *seeds,
                env=env)


def explicit(*extra):
    return ["--daemon-url", "http://127.0.0.1:8765", "--token-file", _home("token"), *extra]


WRONG_PREFIX = b"PLAIN\0fixture-tunnel-key" if core.WINDOWS else b"DPAPI\0fixture-tunnel-key"


def cases() -> list[core.Case]:
    return [
        # status: profile states, key presence, expiry
        status("status-pending-text", profile(), json_out=False),
        status("status-pending-json", profile()),
        case("status-default-root", ["status", "--json"], profile(rel=".pseudolife-mcp/tunnel")),
        case("status-default-root-text", ["status"], profile(rel=".pseudolife-mcp/tunnel")),
        status("status-named-profile", profile("work-2"), profile(), extra=["--profile", "work-2"]),
        status("status-ready-no-key", ready()),
        status("status-ready-no-key-text", ready(), json_out=False),
        status("status-key-plain", ready(), key(), platforms=("linux",)),
        status("status-key-plain-text", ready(), key(), json_out=False, platforms=("linux",)),
        status("status-key-wrong-host-prefix", ready(), key(raw=WRONG_PREFIX)),
        status("status-key-not-owner-only", ready(),
               key(raw=b"PLAIN\0fixture-tunnel-key", plain_file=True)),
        status("status-key-invalid-token", ready(), key(raw=b"PLAIN\0two words"),
               platforms=("linux",)),
        status("status-key-no-separator", ready(), key(raw=b"PLAIN")),
        status("status-expired-text", profile(runtime_key_expires_at="2020-01-01T00:00:00Z"),
               json_out=False),
        status("status-expired-json", profile(runtime_key_expires_at="2020-01-01T00:00:00Z")),
        status("status-expiry-date-only", profile(runtime_key_expires_at="2099-12-31")),
        status("status-expiry-near", profile(runtime_key_expires_at=NEAR)),
        status("status-expiry-near-text", profile(runtime_key_expires_at=NEAR), json_out=False),
        status("status-expiry-offset",
               ready(runtime_key_expires_at="2030-06-01T12:00:00.250+05:30")),
        status("status-catalog-full", profile(minimum_catalog="full",
                                              organization_id="org-Fixture_1")),
        # status: refresh records
        status("status-refresh-pending", profile(),
               refresh("dot.reload.json", id=REFRESH_ID, snapshot={}, target={})),
        status("status-refresh-pending-text", profile(),
               refresh("dot.reload.json", id=REFRESH_ID, snapshot={}, target={}),
               json_out=False),
        status("status-refresh-pending-bad-id", profile(), refresh("dot.reload.json", id="x")),
        status("status-refresh-pending-corrupt", profile(),
               private("tunnels/dot.reload.json", b"{not json")),
        status("status-refresh-refreshed", ready(),
               refresh("dot.reload.result.json", state="refreshed", changed=True, running=True,
                       rolled_back=False, id=REFRESH_ID)),
        status("status-refresh-refreshed-text", ready(),
               refresh("dot.reload.result.json", state="refreshed", changed=True, running=True,
                       rolled_back=False, id=REFRESH_ID), json_out=False),
        status("status-refresh-rolled-back-text", ready(),
               refresh("dot.reload.result.json", state="rolled-back", changed=False,
                       running=True, rolled_back=True, id=REFRESH_ID), json_out=False),
        status("status-refresh-rolled-back", ready(),
               refresh("dot.reload.result.json", state="rolled-back", changed=False,
                       running=True, rolled_back=True, id=REFRESH_ID)),
        status("status-refresh-failed", ready(),
               refresh("dot.reload.result.json", state="failed", id=REFRESH_ID)),
        status("status-refresh-bad-state", ready(),
               refresh("dot.reload.result.json", state="done", id=REFRESH_ID)),
        status("status-refresh-null-id", ready(),
               refresh("dot.reload.result.json", state="refreshed", id=None)),
        status("status-refresh-integer-id", ready(),
               private("tunnels/dot.reload.result.json",
                       b'{"state": "failed", "id": 12345678901234567890123456789012}')),
        status("status-refresh-not-owner-only", ready(),
               plain("tunnels/dot.reload.result.json",
                     json.dumps({"state": "refreshed", "id": REFRESH_ID}).encode())),
        status("status-challenge-present", ready(),
               private("tunnels/dot-verification/challenge.json",
                       json.dumps({"nonce": REFRESH_ID, "identity": "a" * 64,
                                   "created": 1.0}).encode())),
        # status: refusals (exit 2, sanitized stderr)
        status("status-missing-profile", prepared()),
        status("status-missing-dir"),
        status("status-corrupt-json", prepared(),
               private("tunnels/dot.profile.json", b"{\"name\": \"dot\",")),
        status("status-not-a-dict", private("tunnels/dot.profile.json", b"[1, 2]")),
        status("status-unknown-field", profile(raw_edit=edit(extra=1))),
        status("status-missing-field",
               profile(raw_edit=lambda p: p.pop("token_file"))),
        status("status-not-owner-only", plain("tunnels/dot.profile.json", json.dumps(
            {"name": "dot", "daemon_url": "http://127.0.0.1:8765", "token_file": "/x"}).encode())),
        status("status-bad-tunnel-id", profile(raw_edit=edit(tunnel_id="tunnel_xyz"))),
        status("status-bad-organization", profile(raw_edit=edit(organization_id="acme"))),
        status("status-bad-state", profile(raw_edit=edit(state="live"))),
        status("status-consent-not-bool", profile(raw_edit=edit(consent=1))),
        status("status-ready-without-consent",
               profile(raw_edit=edit(state="ready", tunnel_id="tunnel_01"))),
        status("status-bad-runtime-version", profile(raw_edit=edit(runtime_version="v1"))),
        status("status-bad-catalog", profile(raw_edit=edit(minimum_catalog="all"))),
        status("status-naive-expiry",
               profile(raw_edit=edit(runtime_key_expires_at="2026-01-01T00:00:00"))),
        status("status-relative-token", profile(raw_edit=edit(token_file="token"))),
        status("status-bad-daemon-url", profile(raw_edit=edit(daemon_url="ftp://127.0.0.1"))),
        status("status-name-mismatch", profile(raw_edit=edit(name="other"))),
        status("status-too-large", private("tunnels/dot.profile.json", b" " * 70000 + b"{}")),
        status("status-invalid-name", prepared(), extra=["--profile", "bad.name"]),
        status("status-reserved-name", prepared(), extra=["--profile", "CON"]),
        # update: nothing saved, or only pending profiles
        case("update-no-dir", ["update", "--profile-dir", DIR]),
        case("update-default-root-absent", ["update"]),
        case("update-empty-dir", ["update", "--profile-dir", DIR], prepared()),
        case("update-pending-only", ["update", "--profile-dir", DIR, "--profile", "ignored"],
             profile("alpha"), ready(), profile("work-2", minimum_catalog="full")),
        case("update-ready-wrong-key", ["update", "--profile-dir", DIR], ready(),
             key(raw=WRONG_PREFIX)),
        case("update-dir-not-owner-only", ["update", "--profile-dir", DIR], mkdir("tunnels")),
        case("update-invalid-file-name", ["update", "--profile-dir", DIR], profile(),
             private("tunnels/bad.name.profile.json", b"{}")),
        case("update-case-folded-suffix", ["update", "--profile-dir", DIR], prepared(),
             private("tunnels/dot.PROFILE.JSON", b"{}")),
        case("update-corrupt-profile", ["update", "--profile-dir", DIR], profile("alpha"),
             private("tunnels/dot.profile.json", b"{")),
        # verify: refused before any challenge is written
        case("verify-pending", ["verify", "--profile-dir", DIR], profile()),
        case("verify-ready-no-key", ["verify", "--profile-dir", DIR], ready()),
        case("verify-ready-wrong-key", ["verify", "--profile-dir", DIR], ready(),
             key(raw=WRONG_PREFIX)),
        # a finished profile without a launch record fails its identity read
        # before begin_challenge writes (POSIX keys; a Windows key is DPAPI)
        case("verify-ready-key-no-record", ["verify", "--profile-dir", DIR], ready(), key(),
             platforms=("linux",)),
        case("verify-missing", ["verify", "--profile-dir", DIR], prepared()),
        case("verify-invalid-name", ["verify", "--profile-dir", DIR, "--profile", "x y"]),
        # setup: refusals before the daemon handshake
        setup_case("setup-literal-token-env", env={"PSEUDOLIFE_MCP_TOKEN": "fixture-literal"}),
        setup_case("setup-no-registrations",
                   env={"CLAUDE_CONFIG_DIR": _home("claude"), "CODEX_HOME": _home("codex")}),
        setup_case("setup-no-registrations-default-home"),
        setup_case("setup-token-file-only", "--token-file", _home("token"),
                   seeds=(lambda home: _token(home),)),
        setup_case("setup-bad-tunnel-id", *explicit("--tunnel-id", "tunnel_xyz"),
                   seeds=(lambda home: _token(home),)),
        setup_case("setup-empty-tunnel-id", *explicit("--tunnel-id", ""),
                   seeds=(lambda home: _token(home),)),
        setup_case("setup-bad-organization", *explicit("--organization-id", "acme"),
                   seeds=(lambda home: _token(home),)),
        setup_case("setup-naive-expiry", *explicit("--key-expires-at", "2026-12-01T00:00:00"),
                   seeds=(lambda home: _token(home),)),
        setup_case("setup-garbage-expiry", *explicit("--key-expires-at", "next week",
                                                     "--catalog", "full"),
                   seeds=(lambda home: _token(home),)),
        setup_case("setup-env-token-file-bad-id", "--tunnel-id", "tunnel_",
                   seeds=(lambda home: _token(home),),
                   env={"PSEUDOLIFE_MCP_TOKEN_FILE": _home("token")}),
        setup_case("setup-relative-token-bad-id", "--daemon-url", "http://127.0.0.1:8765",
                   "--token-file", "token", "--tunnel-id", "nope"),
        setup_case("setup-url-scheme", "--daemon-url", "ftp://127.0.0.1:8765", "--token-file",
                   _home("token"), seeds=(lambda home: _token(home),)),
        setup_case("setup-url-space", "--daemon-url", "http://127.0.0.1 :8765", "--token-file",
                   _home("token")),
        case("setup-invalid-name", ["setup", "--profile-dir", DIR, "--profile", "bad.name",
                                    *explicit()]),
        # declared deferrals: the candidate defers and changes nothing
        defer("defer-status-process-record",
              ["status", "--profile-dir", DIR, "--json"], ready(),
              private("tunnels/dot.process.json", json.dumps(
                  {"pid": 1, "created": 0.0, "exe": "none", "argv": ["none"]}).encode())),
        defer("defer-status-dpapi-key", ["status", "--profile-dir", DIR], ready(), key(),
              platforms=("windows",)),
        defer("defer-update-ready-key", ["update", "--profile-dir", DIR], ready(), key()),
        defer("defer-verify-dpapi-key", ["verify", "--profile-dir", DIR], ready(), key(),
              platforms=("windows",)),
        defer("defer-verify-process-record", ["verify", "--profile-dir", DIR], ready(), key(),
              private("tunnels/dot.process.json", json.dumps(
                  {"pid": 1, "created": 0.0, "exe": "none", "argv": ["none"]}).encode())),
        defer("defer-status-seven-digit-fraction", ["status", "--profile-dir", DIR],
              profile(runtime_key_expires_at="2099-12-31T00:00:00.1234567Z")),
        defer("defer-status-help", ["status", "--help"]),
        defer("defer-no-subcommand", []),
        defer("defer-joined-option", ["status", "--profile=dot", "--profile-dir", DIR],
              profile()),
        defer("defer-repeated-option", ["status", "--json", "--json", "--profile-dir", DIR],
              profile()),
    ]


MUTANTS = [
    Mutant("tunnel-dispatch-dropped", "tunnel", "shim/src/cli.rs",
           'if mode == "tunnel"\n', 'if mode == "tunnel-dropped"\n',
           ("status-pending-json", "update-no-dir")),
    Mutant("tunnel-update-exit-3", "tunnel", "shim/src/cli/tunnel/mod.rs",
           'return Ok((format!("{NO_PROFILES}\\n"), 3));',
           'return Ok((format!("{NO_PROFILES}\\n"), 0));', ("update-no-dir", "update-empty-dir")),
    Mutant("tunnel-refresh-attention-flipped", "tunnel", "shim/src/cli/tunnel/mod.rs",
           '!matches!(self, Refresh::None) && self.state() != "refreshed"',
           '!matches!(self, Refresh::None) && self.state() == "refreshed"',
           ("status-refresh-refreshed", "status-refresh-pending")),
    Mutant("tunnel-near-expiry-window", "tunnel", "shim/src/cli/tunnel/mod.rs",
           "remaining < chrono::Duration::days(7)", "remaining < chrono::Duration::days(2)",
           ("status-expiry-near",)),
    Mutant("tunnel-key-presence-inverted", "tunnel", "shim/src/cli/tunnel/mod.rs",
           "            bool_text(key),\n", "            bool_text(!key),\n",
           ("status-pending-json",)),
    Mutant("tunnel-owner-only-skipped", "tunnel", "shim/src/cli/tunnel/store.rs",
           "    if !owner_only_file(&file) {\n", "    if false && !owner_only_file(&file) {\n",
           ("status-not-owner-only", "status-key-not-owner-only",
            "status-refresh-not-owner-only")),
    Mutant("tunnel-tunnel-id-class", "tunnel", "shim/src/cli/tunnel/store.rs",
           "        u8::is_ascii_hexdigit,\n", "        u8::is_ascii_alphanumeric,\n",
           ("status-bad-tunnel-id", "setup-bad-tunnel-id")),
    Mutant("tunnel-verify-message", "tunnel", "shim/src/cli/tunnel/mod.rs",
           '"finish tunnel setup before requesting cloud verification"',
           '"finish tunnel setup before requesting verification"',
           ("verify-pending",)),
    Mutant("tunnel-ambiguous-literal-accepted", "tunnel", "shim/src/cli/tunnel/mod.rs",
           "        // Only a literal credential: setup requires a file.\n        return Err(Fail::Tunnel(AMBIGUOUS));",
           "        // Only a literal credential: setup requires a file.\n        return Err(Fail::Defer);",
           ("setup-literal-token-env",)),
    Mutant("tunnel-expiry-order", "tunnel", "shim/src/cli/tunnel/mod.rs",
           "let expired = *instant <= now;", "let expired = *instant >= now;",
           ("status-expired-text", "status-expiry-date-only")),
    Mutant("tunnel-dir-owner-only-skipped", "tunnel", "shim/src/cli/tunnel/store.rs",
           "    owner_only_directory(root, &info)?;\n", "",
           ("update-dir-not-owner-only",)),
    Mutant("tunnel-process-record-ignored", "tunnel", "shim/src/cli/tunnel/mod.rs",
           "(psutil): deferred.\n    if occupied(",
           "(psutil): deferred.\n    if false && occupied(",
           ("defer-status-process-record",)),
]

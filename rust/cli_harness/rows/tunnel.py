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
field edited. Guards on every arm:

- before it runs, the environment the child receives is checked: ``PATH``
  and every program-directory variable point only inside the disposable
  home (``PATH`` is empty, ``COMSPEC`` and ``PATHEXT`` are removed);
- every daemon URL, saved in a profile or passed as an argument, is the
  arm's own loopback listener, which records any connection (none may
  happen; the ``tunnel-saved-url-connect`` mutant proves it would show);
- the caller's real ``~/.pseudolife-mcp`` is compared by ``lstat`` metadata
  only (names, size, mtime_ns, mode, inode, link count, Windows attributes)
  before and after the arm. No file there is opened or read; links and
  junctions are not followed. Subtrees other live sessions write while a
  run is going (``LIVE_SHARED``) are not compared; ``tunnel/`` always is.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import stat
import tempfile
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
REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


def _home(rel: str) -> str:
    return "{HOME}" + SEP + rel.replace("/", SEP)


DIR = _home("tunnels")
DAEMON = "{DAEMON}"  # replaced in setup by the arm's listener URL


# --- guard: the caller's real ~/.pseudolife-mcp, by metadata only --------------

PL_HOME = Path(os.path.expanduser("~")) / ".pseudolife-mcp"
# Written by other live sessions during any run (board digests, locks and
# leases, suite results, handshake cache, agent state, overnight ledgers):
# a change there cannot be attributed to an arm, so it is not compared.
LIVE_SHARED = frozenset({"digests", "locks", "suite-results", "handshake-cache",
                         "agent-state", "overnight", "ledgers"})


def _lstat_tree(root: Path):
    """``lstat`` metadata of ``root`` and everything under it. Directories are
    listed; no file is opened; links and junctions are recorded, never
    entered."""
    if not os.path.lexists(root):
        return None
    out: dict[str, object] = {}

    def record(path: str, rel: str):
        info = os.lstat(path)
        out[rel] = (info.st_size, info.st_mtime_ns, info.st_mode, info.st_ino, info.st_nlink,
                    getattr(info, "st_file_attributes", None))
        return info

    def descend(info) -> bool:
        return (stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                and not getattr(info, "st_file_attributes", 0) & REPARSE)

    def walk(directory: str, rel: str):
        try:
            names = sorted(os.listdir(directory))
        except OSError as error:
            out[rel + "/<listing>"] = type(error).__name__
            return
        for name in names:
            if rel == "." and name in LIVE_SHARED:
                continue
            child = name if rel == "." else f"{rel}/{name}"
            path = os.path.join(directory, name)
            try:
                info = record(path, child)
            except OSError as error:
                out[child] = type(error).__name__
                continue
            if descend(info):
                walk(path, child)

    if descend(record(str(root), ".")):
        walk(str(root), ".")
    return out


def _guard_real(before) -> None:
    after = _lstat_tree(PL_HOME)
    if after != before:
        changed = sorted(k for k in set(before or {}) | set(after or {})
                         if (before or {}).get(k) != (after or {}).get(k))
        raise RuntimeError(f"the real {PL_HOME} changed during the arm: {changed[:20]}")


# --- guard: no real program can resolve -----------------------------------------

# Variables that name where programs or per-user program state live. Each one
# the child receives must point inside the disposable home; COMSPEC and
# PATHEXT must be absent; PATH must be empty or inside the home. SYSTEMROOT
# and WINDIR (the OS itself, needed to start an interpreter) and TEMP/TMP stay.
PROGRAM_DIRS = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES",
                "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
                "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432", "PROGRAMDATA",
                "ALLUSERSPROFILE", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME",
                "XDG_DATA_HOME", "XDG_BIN_HOME", "PSEUDOLIFE_SHIM_RUNTIMES",
                "PSEUDOLIFE_SHIM_LAUNCHER", "PSEUDOLIFE_DOCKER", "PSEUDOLIFE_MCP_DATA_DIR",
                "PYTHONHOME", "PYTHONUSERBASE", "VIRTUAL_ENV", "CONDA_PREFIX")
ABSENT = ("COMSPEC", "PATHEXT")


def _prove_environment(case_obj: core.Case, arm: core.Arm) -> None:
    env = core._environment(case_obj, arm, core.Target(arm.name, []))
    home = os.path.normcase(os.path.abspath(arm.home))

    def inside(value: str) -> bool:
        path = os.path.normcase(os.path.abspath(value))
        return path == home or path.startswith(home + os.sep)

    upper = {key.upper(): value for key, value in env.items()}
    bad = [f"PATH entry {entry!r}" for entry in upper.get("PATH", "").split(os.pathsep)
           if entry and not inside(entry)]
    bad += [f"{name} present" for name in ABSENT if name in upper]
    bad += [f"{name}={upper[name]!r}" for name in PROGRAM_DIRS
            if name in upper and not inside(upper[name])]
    if bad:
        raise RuntimeError(f"{case_obj.id}/{arm.name}: child environment reaches outside "
                           f"the disposable home: {bad}")


# --- guard: no connection to the daemon URL ------------------------------------

class Listener:
    """A loopback listener named as the arm's daemon: it records every
    connection and answers nothing. ``requests()`` runs after the arm's
    process exited and also drains connections still in the backlog."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.1)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.accepted: list[dict] = []
        self.closing = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _take(self, conn) -> None:
        self.accepted.append({"method": "CONNECT", "target": "", "headers": {}, "body": ""})
        conn.close()

    def _serve(self):
        while not self.closing:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self._take(conn)

    def requests(self):
        self.closing = True
        self.thread.join(2)
        self.sock.setblocking(False)
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                break
            self._take(conn)
        return list(self.accepted)

    def close(self):
        self.closing = True
        self.thread.join(2)
        self.sock.close()


# --- junctions (Windows) ---------------------------------------------------------

def _junction(target: Path, link: Path) -> None:
    import _winapi  # noqa: PLC0415 (Windows only)
    _winapi.CreateJunction(str(target), str(link))


def _junctions_work() -> bool:
    if not core.WINDOWS:
        return False
    probe = Path(tempfile.mkdtemp(prefix="pl-tunnel-junction-"))
    try:
        (probe / "target").mkdir()
        _junction(probe / "target", probe / "link")
        return (os.lstat(probe / "link").st_file_attributes & REPARSE) != 0
    except OSError:
        return False
    finally:
        link = probe / "link"
        if os.path.lexists(link):
            os.rmdir(link)  # removes the junction itself, never its target
        shutil.rmtree(probe, ignore_errors=True)


JUNCTIONS = _junctions_work()


# --- seeding through the oracle's writers ---------------------------------------
# A seed receives the arm: its home, and its listener (the only daemon URL).

def _profiles():
    import pseudolife_memory.tunnel_profiles as module  # noqa: PLC0415 (oracle writer)
    return module


def _token(home: Path) -> Path:
    path = home / "token"
    if not path.exists():
        _profiles().private_write(path, b"fixture-daemon-token")
    return path


def token(arm) -> None:
    _token(arm.home)


def _root(home: Path, rel: str = "tunnels") -> Path:
    return home / Path(rel)


def profile(name="dot", *, rel="tunnels", raw_edit=None, file_name=None, token_file=None,
            **fields):
    """``ProfileStore.save`` of a valid profile naming the arm's listener as
    its daemon, or ``private_write`` of the writer's own payload with
    ``raw_edit`` applied."""
    def seed(arm):
        home = arm.home
        m = _profiles()
        store = m.ProfileStore(_root(home, rel))
        reference = str(home / token_file) if token_file else str(_token(home))
        value = m.Profile(name, arm.daemon.url, reference, **fields)
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
    def seed(arm):
        m = _profiles()
        store = m.ProfileStore(_root(arm.home, rel))
        if raw is None:
            store.set_key(name, FIXTURE_KEY)
        elif plain_file:
            store.key_path(name).write_bytes(raw)
        else:
            m.private_write(store.key_path(name), raw)
    return seed


def private(rel: str, data: bytes):
    def seed(arm):
        _profiles().private_write(arm.home / Path(rel), data)
    return seed


def plain(rel: str, data: bytes):
    def seed(arm):
        path = arm.home / Path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return seed


def prepared(rel="tunnels"):
    def seed(arm):
        _profiles().ProfileStore(_root(arm.home, rel))._prepare()
    return seed


def mkdir(rel: str):
    def seed(arm):
        (arm.home / Path(rel)).mkdir(parents=True, exist_ok=True)
    return seed


def symlink(rel: str, target_rel: str):
    def seed(arm):
        os.symlink(arm.home / Path(target_rel), arm.home / Path(rel))
    return seed


def junction(rel: str, target_rel: str, *, dangling=False):
    """A junction at ``rel`` to the directory ``target_rel`` (created if
    absent, then removed again when ``dangling``)."""
    def seed(arm):
        target = arm.home / Path(target_rel)
        target.mkdir(parents=True, exist_ok=True)
        _junction(target, arm.home / Path(rel))
        if dangling:
            shutil.rmtree(target)
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
    # The same home and daemon tokens apply() gave the observed files.
    normalize.home_tokens(before, obs["home"])
    if obs.get("daemon_url"):
        normalize.daemon_tokens(before, obs["daemon_url"])
    obs["files"] = before["files"]
    obs["modes"] = dict(obs.get("before_modes", {}))


@normalize.rule("tunnel-dpapi-key")
def tunnel_dpapi_key(obs: dict) -> None:
    """Windows ``set_key`` output is DPAPI ciphertext, fresh per arm. A key
    file is tokenized only when its bytes are exactly the ones this arm's
    setup seeded (recorded before the arm ran) and the oracle's own
    ``_dpapi`` unprotects them to the fixture key. A rewritten, re-encrypted
    or foreign key stays as observed and shows as a difference."""
    seeded = obs.get("before", {})
    for rel, value in list(obs["files"].items()):
        if not (rel.endswith(".key") and value.startswith("file:")) or seeded.get(rel) != value:
            continue
        data = base64.b64decode(value[5:])
        prefix, _, sealed = data.partition(b"\0")
        if prefix != b"DPAPI":
            continue
        try:
            unsealed = _profiles()._dpapi(sealed, decrypt=True)
        except Exception:  # noqa: BLE001 - an unreadable key stays as observed
            continue
        if unsealed == FIXTURE_KEY.encode():
            obs["files"][rel] = "file:" + base64.b64encode(b"DPAPI\0<fixture key>").decode()


def case(case_id, argv, *seeds, env=None, rules=(), platforms=("windows", "linux"),
         skip_if=None, note=""):
    original = ["tunnel", *argv]

    def setup(arm):
        made.argv = [item.replace(DAEMON, arm.daemon.url) for item in original]
        _prove_environment(made, arm)
        arm.state["real"] = _lstat_tree(PL_HOME)
        for seed in seeds:
            seed(arm)
        arm.state["before"] = core.snapshot(arm.home)
        arm.state["before_modes"] = core.modes(arm.home)

    def after(arm, obs):
        obs["arm"] = arm.name
        obs["before"] = arm.state.get("before", {})
        obs["before_modes"] = arm.state.get("before_modes", {})
        _guard_real(arm.state.get("real"))

    environment = {
        "PATH": "",
        "PATHEXT": None,
        "COMSPEC": None,
        "PSEUDOLIFE_MCP_TOKEN": None,
        "PSEUDOLIFE_MCP_TOKEN_FILE": None,
        "CLAUDE_CONFIG_DIR": None,
        "CODEX_HOME": None,
        "OPENAI_API_KEY": None,
    }
    environment.update(env or {})
    made = core.Case(case_id, list(original), env=environment, setup=setup, after=after,
                     rules=rules, platforms=platforms, daemon=Listener, skip_if=skip_if,
                     note=note)
    return made


def status(case_id, *seeds, extra=(), json_out=True, **kw):
    argv = ["status", "--profile-dir", DIR, *extra] + (["--json"] if json_out else [])
    return case(case_id, argv, *seeds, **kw)


def defer(case_id, argv, *seeds, **kw):
    return case(case_id, argv, *seeds, rules=("tunnel-deferral", "tunnel-dpapi-key"), **kw)


def setup_case(case_id, *extra, seeds=(), env=None):
    return case(case_id, ["setup", "--profile-dir", DIR, "--accept-access", *extra], *seeds,
                env=env)


def explicit(*extra):
    return ["--daemon-url", DAEMON, "--token-file", _home("token"), *extra]


def no_junctions() -> bool:
    return not JUNCTIONS


WRONG_PREFIX = b"PLAIN\0fixture-tunnel-key" if core.WINDOWS else b"DPAPI\0fixture-tunnel-key"
DEEP = b"[" * 150  # past the native bound of 100 brackets
RESERVED_ID = b'{"id": {"$serde_json::private::Number": "12345678901234567890123456789012"}'
EXPONENT_ID = b'"id": 123456789012345678901234567890e1'


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
        status("status-refresh-pending-exponent-id", profile(),
               private("tunnels/dot.reload.json", b"{" + EXPONENT_ID + b"}")),
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
        status("status-refresh-result-exponent-id", ready(),
               private("tunnels/dot.reload.result.json",
                       b'{"state": "failed", ' + EXPONENT_ID + b"}")),
        status("status-refresh-not-owner-only", ready(),
               plain("tunnels/dot.reload.result.json",
                     json.dumps({"state": "refreshed", "id": REFRESH_ID}).encode())),
        status("status-refresh-result-junction", ready(), mkdir("elsewhere"),
               junction("tunnels/dot.reload.result.json", "elsewhere"),
               platforms=("windows",), skip_if=no_junctions),
        # status: a challenge record is read only through checked_path/private_read
        status("status-challenge-present", ready(),
               private("tunnels/dot-verification/challenge.json",
                       json.dumps({"nonce": REFRESH_ID, "identity": "a" * 64,
                                   "created": 1.0}).encode())),
        status("status-challenge-symlink", ready(), private("deep.json", DEEP),
               mkdir("tunnels/dot-verification"),
               symlink("tunnels/dot-verification/challenge.json", "deep.json"),
               platforms=("linux",)),
        status("status-challenge-junction", ready(),
               private("deep-dir/challenge.json", DEEP),
               junction("tunnels/dot-verification", "deep-dir"),
               platforms=("windows",), skip_if=no_junctions),
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
            {"name": "dot", "daemon_url": "http://127.0.0.1:9", "token_file": "/x"}).encode())),
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
        status("status-profile-symlink", profile(file_name="real.profile.json.bak"),
               symlink("tunnels/dot.profile.json", "tunnels/real.profile.json.bak"),
               platforms=("linux",)),
        status("status-profile-junction", prepared(), mkdir("elsewhere"),
               junction("tunnels/dot.profile.json", "elsewhere"),
               platforms=("windows",), skip_if=no_junctions),
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
        setup_case("setup-token-file-only", "--token-file", _home("token"), seeds=(token,)),
        setup_case("setup-bad-tunnel-id", *explicit("--tunnel-id", "tunnel_xyz"),
                   seeds=(token,)),
        setup_case("setup-empty-tunnel-id", *explicit("--tunnel-id", ""), seeds=(token,)),
        setup_case("setup-bad-organization", *explicit("--organization-id", "acme"),
                   seeds=(token,)),
        setup_case("setup-naive-expiry", *explicit("--key-expires-at", "2026-12-01T00:00:00"),
                   seeds=(token,)),
        setup_case("setup-garbage-expiry", *explicit("--key-expires-at", "next week",
                                                     "--catalog", "full"),
                   seeds=(token,)),
        setup_case("setup-env-token-file-bad-id", "--tunnel-id", "tunnel_", seeds=(token,),
                   env={"PSEUDOLIFE_MCP_TOKEN_FILE": _home("token")}),
        setup_case("setup-relative-token-bad-id", "--daemon-url", DAEMON,
                   "--token-file", "token", "--tunnel-id", "nope"),
        setup_case("setup-url-scheme", "--daemon-url", "ftp://127.0.0.1:9", "--token-file",
                   _home("token"), seeds=(token,)),
        setup_case("setup-url-space", "--daemon-url", "http://127.0.0.1 :9", "--token-file",
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
        defer("defer-status-one-digit-fraction", ["status", "--profile-dir", DIR],
              profile(runtime_key_expires_at="2099-12-31T00:00:00.5Z")),
        # hour 24: refused by the 3.11 oracle, admitted by CPython 3.14
        defer("defer-status-hour-24-expiry", ["status", "--profile-dir", DIR],
              profile(raw_edit=edit(runtime_key_expires_at="2099-12-31T24:00:00Z"))),
        defer("defer-status-deep-challenge", ["status", "--profile-dir", DIR], ready(),
              private("tunnels/dot-verification/challenge.json", DEEP)),
        defer("defer-status-refresh-reserved-id", ["status", "--profile-dir", DIR], profile(),
              private("tunnels/dot.reload.json", RESERVED_ID + b"}")),
        defer("defer-status-result-reserved-id", ["status", "--profile-dir", DIR], ready(),
              private("tunnels/dot.reload.result.json",
                      RESERVED_ID + b', "state": "failed"}')),
        defer("defer-profile-dangling-junction", ["status", "--profile-dir", DIR], prepared(),
              junction("tunnels/dot.profile.json", "gone", dangling=True),
              platforms=("windows",), skip_if=no_junctions),
        # setup resuming a saved profile (its token reference is missing, so
        # the oracle stops at check_token_file, before any handshake process)
        defer("defer-setup-existing-profile", ["setup", "--profile-dir", DIR],
              profile(token_file="missing-token")),
        # a Codex registration file: its env block is not read natively (the
        # oracle reads it, finds no server block and refuses)
        defer("defer-setup-codex-registration", ["setup", "--profile-dir", DIR],
              plain(".codex/config.toml", b"[other]\nx = 1\n")),
        defer("defer-setup-claude-registration", ["setup", "--profile-dir", DIR],
              plain(".claude.json", b'{"mcpServers": {}}')),
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
    # review round 1
    Mutant("tunnel-saved-url-connect", "tunnel", "shim/src/cli/tunnel/store.rs",
           "        Some(Some(url)) => validate_url(url)?,\n",
           "        Some(Some(url)) => {\n            validate_url(url)?;\n"
           "            let _ = std::net::TcpStream::connect(url.trim_start_matches(\"http://\"));\n"
           "        }\n",
           ("status-pending-json",)),
    Mutant("tunnel-challenge-bound", "tunnel", "shim/src/cli/tunnel/mod.rs",
           "        Ok(data) if data.iter().filter(|b| matches!(b, b'[' | b'{')).count() > 100 => {",
           "        Ok(data) if data.iter().filter(|b| matches!(b, b'[' | b'{')).count() > 1000 => {",
           ("defer-status-deep-challenge",)),
    Mutant("tunnel-existing-profile-resumed", "tunnel", "shim/src/cli/tunnel/mod.rs",
           "    if occupied(&path)? {\n", "    if false && occupied(&path)? {\n",
           ("defer-setup-existing-profile",)),
    Mutant("tunnel-codex-registration-ignored", "tunnel", "shim/src/cli/tunnel/mod.rs",
           "|| occupied(&codex)?", "|| false",
           ("defer-setup-codex-registration",)),
    Mutant("tunnel-redirect-message", "tunnel", "shim/src/cli/tunnel/store.rs",
           "fs::metadata(&target).is_ok() => Err(Fail::Tunnel(REDIRECT)),",
           "fs::metadata(&target).is_ok() => Err(Fail::Tunnel(UNSAFE_PATH)),",
           ("status-profile-junction", "status-profile-symlink")),
    Mutant("tunnel-dangling-junction-answered", "tunnel", "shim/src/cli/tunnel/store.rs",
           "        // A dangling Windows junction: Path.is_symlink() is not decided here.\n"
           "        Ok(_) => Err(Fail::Defer),",
           "        // A dangling Windows junction: Path.is_symlink() is not decided here.\n"
           "        Ok(_) => Err(Fail::Tunnel(REDIRECT)),",
           ("defer-profile-dangling-junction",)),
    Mutant("tunnel-reserved-number-key-admitted", "tunnel", "shim/src/cli/tunnel/syntax.rs",
           "    if reserved_number_key(text) {\n", "    if false && reserved_number_key(text) {\n",
           ("defer-status-refresh-reserved-id", "defer-status-result-reserved-id")),
]

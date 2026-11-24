"""CLI-TUNNEL-STATUS: the read-only subset of ``pseudolife-mcp tunnel``.

Canonical argv: ``tunnel status [--json] [--profile N] [--profile-dir D]``,
``tunnel update [--profile N] [--profile-dir D]``, ``tunnel verify
[--profile N] [--profile-dir D]`` and ``tunnel setup`` with
``--daemon-url``, ``--token-file``, ``--tunnel-id``, ``--organization-id``,
``--catalog``, ``--key-expires-at`` and ``--accept-access``; each option at
most once, values as the next argument. The native leaf answers status for a
profile with no process record, update when every saved profile is pending,
and the refusals that verify and setup reach before any write, handshake,
process or network use. Every other argv is refused with its cause and
the fix, or answers its help (``rows/tunnel.md``); nothing defers.

Profiles, keys, refresh records and tokens are written through the oracle's
own writers (``ProfileStore.save`` / ``set_key`` / ``private_write``); an
invalid profile is ``private_write`` of the writer's own payload with one
field edited. Guards on every arm:

- before it runs, the environment the child receives is checked: ``PATH``
  and every program-directory variable point only inside the disposable
  home (``PATH`` is empty, ``COMSPEC`` and ``PATHEXT`` are removed);
- every daemon URL, saved in a profile or passed as an argument, is the
  arm's own loopback listener, which records any connection (none may
  happen; its automatic source-mutant check was retired on 17 November 2026);
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

SEP = "\\" if core.WINDOWS else "/"
NEAR = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT00:00:00Z")
REFRESH_ID = "0123456789abcdef0123456789abcdef"
FIXTURE_KEY = "fixture-tunnel-key"
REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


def _home(rel: str) -> str:
    return "{HOME}" + SEP + rel.replace("/", SEP)


DIR = _home("tunnels")
DAEMON = "{DAEMON}"  # replaced in setup by the arm's listener URL
# A tunnel process record whose process is gone.
DEAD_RECORD = json.dumps({"pid": 4294967001, "created": 0.0, "exe": "none",
                          "argv": ["none"]}).encode()


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
        if "runtime_key_expires_at" in fields:
            # The exact value written, for rule tunnel-near-expiry.
            arm.state["seeded_expiry"] = fields["runtime_key_expires_at"]
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


def saved_key():
    """The key file as this platform's saved setup holds one, for cases
    whose candidate does not need it to unseal (``update`` reads no key). On
    Windows the bytes are a ``DPAPI``-prefixed stand-in that is no DPAPI
    blob, so ``status`` reports no usable key, and these cases also record
    in a key-authenticated SSH session, which cannot seal with
    CryptProtectData."""
    return key(raw=b"DPAPI\0fixture-sealed-key") if core.WINDOWS else key()


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


def replaceable(rel: str):
    """Give the junction at ``rel`` itself (not where it leads) an access list
    that lets Authenticated Users delete or rewrite it: a link another
    account could replace, which private paths never follow (#1639). Set
    through the Windows API on a handle opened without following it."""
    def seed(arm):
        import ctypes  # noqa: PLC0415 (Windows only)
        from ctypes import wintypes  # noqa: PLC0415
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.HANDLE]
        advapi.GetSecurityDescriptorDacl.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.BOOL)]
        advapi.SetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_void_p]
        descriptor = ctypes.c_void_p()
        # This user, SYSTEM and Administrators, and Modify for Authenticated
        # Users (which includes DELETE and write rights on the link itself).
        assert advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "D:P(A;;FA;;;OW)(A;;FA;;;SY)(A;;FA;;;BA)(A;;0x1301bf;;;AU)", 1,
            ctypes.byref(descriptor), None), ctypes.get_last_error()
        try:
            present, defaulted = wintypes.BOOL(), wintypes.BOOL()
            dacl = ctypes.c_void_p()
            assert advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present),
                                                    ctypes.byref(dacl), ctypes.byref(defaulted))
            # WRITE_DAC | READ_CONTROL; backup semantics, the reparse point itself.
            handle = kernel.CreateFileW(str(arm.home / Path(rel)), 0x00040000 | 0x00020000,
                                        0x7, None, 3, 0x02000000 | 0x00200000, None)
            assert handle not in (None, wintypes.HANDLE(-1).value), ctypes.get_last_error()
            try:
                # SE_FILE_OBJECT; DACL | PROTECTED_DACL.
                result = advapi.SetSecurityInfo(handle, 1, 0x4 | 0x80000000, None, None,
                                                dacl, None)
                assert result == 0, result
            finally:
                kernel.CloseHandle(handle)
        finally:
            kernel.LocalFree(descriptor)
    return seed


def edit(**changes):
    def apply(payload):
        payload.update(changes)
    return apply


def refresh(rel: str, **record):
    return private(f"tunnels/{rel}", json.dumps(record).encode())


# --- cases ----------------------------------------------------------------------

@normalize.rule("tunnel-dpapi-key")
def tunnel_dpapi_key(obs: dict) -> None:
    """Windows ``set_key`` output is DPAPI ciphertext, fresh per arm. A key
    file is tokenized only when its bytes are exactly the ones this arm's
    setup seeded (recorded before the arm ran) and the oracle's own
    ``_dpapi`` unprotected them to the fixture key. The unprotect runs in the
    case's ``after``, as the arm's own user on the arm's own host, and its
    verdict (``dpapi_fixture``) travels with the observation, so a golden
    replays where that user's keys cannot be read. A rewritten,
    re-encrypted or foreign key stays as observed and shows as a
    difference."""
    seeded = obs.get("before", {})
    for rel in obs.get("dpapi_fixture", ()):
        value = obs["files"].get(rel)
        if value is not None and seeded.get(rel) == value:
            obs["files"][rel] = "file:" + base64.b64encode(b"DPAPI\0<fixture key>").decode()


def _dpapi_fixture(home) -> list[str]:
    """Key files under ``home`` the oracle's ``_dpapi`` unprotects to the
    fixture key (Windows ``set_key`` output), as this user on this host."""
    found = []
    for rel, value in core.snapshot(home).items():
        if not (rel.endswith(".key") and value.startswith("file:")):
            continue
        prefix, _, sealed = base64.b64decode(value[5:]).partition(b"\0")
        if prefix != b"DPAPI":
            continue
        try:
            unsealed = _profiles()._dpapi(sealed, decrypt=True)
        except Exception:  # noqa: BLE001 - an unreadable key stays as observed
            continue
        if unsealed == FIXTURE_KEY.encode():
            found.append(rel)
    return sorted(found)


_EXPIRY_FIELD = b'"runtime_key_expires_at": '


@normalize.rule("tunnel-near-expiry")
def tunnel_near_expiry(obs: dict) -> None:
    """``NEAR`` is three days from the day the row was loaded, so a golden
    replayed on another day seeds another date. Only the exact value this
    arm's setup seeded (``seeded_expiry``, recorded with the observation)
    becomes ``<near>``, and only in its named places: the saved profile's
    ``runtime_key_expires_at`` and the status report's ``"expires_at"`` (JSON)
    or ``API key expiry:`` line (text). The seed must be a midnight UTC stamp
    one to three days after this arm's start (the near-expiry class), and the
    profile after the run must hold exactly that value in its one expiry
    field; otherwise nothing is replaced. Any other date, a seed's date moved
    by a day included, stays as written and shows as a difference."""
    seed = obs.get("seeded_expiry")
    if not isinstance(seed, str):
        return
    try:
        moment = datetime.strptime(seed, "%Y-%m-%dT00:00:00Z").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return
    if not 86_400 < moment - obs["window"][0] <= 3 * 86_400:
        return
    value = seed.encode()
    field = _EXPIRY_FIELD + b'"' + value + b'"'
    saved = [rel for rel, data in obs["files"].items()
             if data.startswith("file:") and _EXPIRY_FIELD in base64.b64decode(data[5:])]
    if len(saved) != 1:
        return
    data = base64.b64decode(obs["files"][saved[0]][5:])
    if data.count(_EXPIRY_FIELD) != 1 or data.count(field) != 1:
        return
    obs["files"][saved[0]] = "file:" + base64.b64encode(
        data.replace(field, _EXPIRY_FIELD + b'"<near>"')).decode()
    stdout = base64.b64decode(obs["stdout"])
    for named in (b'"expires_at": "' + value + b'"', b"API key expiry: " + value + b"."):
        stdout = stdout.replace(named, named.replace(value, b"<near>"))
    obs["stdout"] = base64.b64encode(stdout).decode()


def case(case_id, argv, *seeds, env=None, rules=(), platforms=("windows", "linux"),
         skip_if=None, note="", programs=(), stdout_closed=False):
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
        # The setup snapshot with the tokens apply() gives observed files,
        # so neither a deferral's expectation nor a golden carries this
        # arm's home path or daemon port.
        before = {"stdout": "", "stderr": "", "files": dict(arm.state.get("before", {}))}
        normalize.home_tokens(before, str(arm.home))
        normalize.daemon_tokens(before, arm.daemon.url)
        obs["before"] = before["files"]
        obs["before_modes"] = arm.state.get("before_modes", {})
        obs["dpapi_fixture"] = _dpapi_fixture(arm.home) if core.WINDOWS else []
        if "seeded_expiry" in arm.state:
            obs["seeded_expiry"] = arm.state["seeded_expiry"]
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
    if stdout_closed:
        rules = (*rules, "python-stdout-closed-trailer")
    made = core.Case(case_id, list(original), env=environment, setup=setup, after=after,
                     rules=rules, platforms=platforms, daemon=Listener, skip_if=skip_if,
                     note=note, programs=programs, stdout_closed=stdout_closed)
    return made


def status(case_id, *seeds, extra=(), json_out=True, **kw):
    argv = ["status", "--profile-dir", DIR, *extra] + (["--json"] if json_out else [])
    return case(case_id, argv, *seeds, **kw)


def refuse(case_id, argv, *seeds, **kw):
    """A shape the candidate refuses (or, for help, answers) natively; its
    golden is recorded from the candidate."""
    return case(case_id, argv, *seeds, rules=("tunnel-dpapi-key",), **kw)


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
        status("status-expiry-near", profile(runtime_key_expires_at=NEAR),
               rules=("tunnel-near-expiry",)),
        status("status-expiry-near-text", profile(runtime_key_expires_at=NEAR), json_out=False,
               rules=("tunnel-near-expiry",)),
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
        # A junction only this user can change is followed (#1639): the
        # challenge behind it is read as status-challenge-present reads one
        # from a plain folder. One another account could replace is not: the
        # challenge behind it (past the native bound, so a read would show)
        # is never read.
        status("status-challenge-junction", ready(),
               private("real-dir/challenge.json",
                       json.dumps({"nonce": REFRESH_ID, "identity": "a" * 64,
                                   "created": 1.0}).encode()),
               junction("tunnels/dot-verification", "real-dir"),
               platforms=("windows",), skip_if=no_junctions),
        status("status-challenge-junction-replaceable", ready(),
               private("deep-dir/challenge.json", DEEP),
               junction("tunnels/dot-verification", "deep-dir"),
               replaceable("tunnels/dot-verification"),
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
        # process records: read for liveness (pid and image), never acted on.
        # The pid fits a u32 but no host holds it (above Linux's pid_max, not
        # a multiple of four for Windows), so every host reads the record as
        # left from a tunnel that stopped.
        status("status-process-record", ready(), private("tunnels/dot.process.json", DEAD_RECORD)),
        status("status-process-record-text", ready(),
               private("tunnels/dot.process.json", DEAD_RECORD), json_out=False),
        # A Windows key file whose DPAPI blob is damaged: no usable key.
        case("status-dpapi-key", ["status", "--profile-dir", DIR], ready(), saved_key(),
             platforms=("windows",)),
        # JSON pins the key's state: false, the blob does not unseal.
        status("status-dpapi-key-json", ready(), saved_key(), platforms=("windows",)),
        case("update-ready-key", ["update", "--profile-dir", DIR], ready(), saved_key(),
             rules=("tunnel-dpapi-key",)),
        case("update-dead-record", ["update", "--profile-dir", DIR], ready(), saved_key(),
             private("tunnels/dot.process.json", DEAD_RECORD), rules=("tunnel-dpapi-key",)),
        case("update-orphan-record", ["update", "--profile-dir", DIR], prepared(),
             private("tunnels/old.process.json", DEAD_RECORD)),
        # verify: refused before any challenge is written
        case("verify-pending", ["verify", "--profile-dir", DIR], profile()),
        # A refused stdout (review of #678 at 6447829f): the oracle's buffered
        # report fails only CPython's shutdown flush, which turns the exit
        # into 120. A refusal on stderr leaves stdout empty: nothing to flush,
        # so verify-pending-stdout-closed keeps its exit 2.
        status("status-pending-json-stdout-closed", profile(), stdout_closed=True),
        status("status-pending-text-stdout-closed", profile(), json_out=False,
               stdout_closed=True),
        case("update-no-dir-stdout-closed", ["update", "--profile-dir", DIR],
             stdout_closed=True),
        case("verify-pending-stdout-closed", ["verify", "--profile-dir", DIR], profile(),
             stdout_closed=True),
        case("verify-ready-no-key", ["verify", "--profile-dir", DIR], ready()),
        case("verify-ready-wrong-key", ["verify", "--profile-dir", DIR], ready(),
             key(raw=WRONG_PREFIX)),
        # a finished profile without a launch record fails its identity read
        # before begin_challenge writes. On Windows ``set_key`` seals the key
        # with DPAPI and the candidate unseals it, so the case records only
        # in a logon that holds the account's credentials (not key-based SSH).
        case("verify-ready-key-no-record", ["verify", "--profile-dir", DIR], ready(), key(),
             rules=("tunnel-dpapi-key",)),
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
        # shapes that once deferred (the ids keep their recorded names): the
        # candidate refuses with the cause and the fix (or prints help) and
        # changes nothing
        # Linux only: seeding the Windows key needs DPAPI, which a key-based
        # SSH logon cannot seal; a unit test covers the refusal on Windows.
        refuse("defer-verify-process-record", ["verify", "--profile-dir", DIR], ready(), key(),
               private("tunnels/dot.process.json", json.dumps(
                   {"pid": 1, "created": 0.0, "exe": "none", "argv": ["none"]}).encode()),
               platforms=("linux",)),
        refuse("defer-status-seven-digit-fraction", ["status", "--profile-dir", DIR],
              profile(runtime_key_expires_at="2099-12-31T00:00:00.1234567Z")),
        refuse("defer-status-one-digit-fraction", ["status", "--profile-dir", DIR],
              profile(runtime_key_expires_at="2099-12-31T00:00:00.5Z")),
        # hour 24: refused by the 3.11 oracle, admitted by CPython 3.14
        refuse("defer-status-hour-24-expiry", ["status", "--profile-dir", DIR],
              profile(raw_edit=edit(runtime_key_expires_at="2099-12-31T24:00:00Z"))),
        refuse("defer-status-deep-challenge", ["status", "--profile-dir", DIR], ready(),
              private("tunnels/dot-verification/challenge.json", DEEP)),
        refuse("defer-status-refresh-reserved-id", ["status", "--profile-dir", DIR], profile(),
              private("tunnels/dot.reload.json", RESERVED_ID + b"}")),
        refuse("defer-status-result-reserved-id", ["status", "--profile-dir", DIR], ready(),
              private("tunnels/dot.reload.result.json",
                      RESERVED_ID + b', "state": "failed"}')),
        refuse("defer-profile-dangling-junction", ["status", "--profile-dir", DIR], prepared(),
              junction("tunnels/dot.profile.json", "gone", dangling=True),
              platforms=("windows",), skip_if=no_junctions),
        # setup resuming a saved profile (its token reference is missing, so
        # the oracle stops at check_token_file, before any handshake process)
        refuse("defer-setup-existing-profile", ["setup", "--profile-dir", DIR],
              profile(token_file="missing-token")),
        # a Codex registration file: its env block is not read natively (the
        # oracle reads it, finds no server block and refuses)
        refuse("defer-setup-codex-registration", ["setup", "--profile-dir", DIR],
              plain(".codex/config.toml", b"[other]\nx = 1\n")),
        refuse("defer-setup-claude-registration", ["setup", "--profile-dir", DIR],
              plain(".claude.json", b'{"mcpServers": {}}')),
        refuse("defer-status-help", ["status", "--help"]),
        refuse("defer-no-subcommand", []),
        refuse("defer-joined-option", ["status", "--profile=dot", "--profile-dir", DIR],
              profile()),
        refuse("defer-repeated-option", ["status", "--json", "--json", "--profile-dir", DIR],
              profile()),
    ]

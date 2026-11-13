"""CLI-RESTORE-GATES and the start of CLI-RESTORE-DOCKER: what
``pseudolife-mcp restore`` decides before it changes anything, where the
native command and the frozen Python one still answer alike.

Compared: argparse's help and errors at 80 columns, the dump-completeness
refusal, the state-archive refusal, the refusal without ``--yes`` when stdin
and stdout are not terminals, and, confirmed, the refusals of the bank's
selection and of the read-only checks, against a scripted Docker world. The
native command now also restores (the destructive steps, the safety backup,
recovery) and reads some archives more strictly than Python; those cases
are not compared here and are tested in Rust (the spec is ``restore.md``
beside this file).

Safety. Restore replaces a bank, and its defaults name the stack's own
containers. Every case runs with ``PATH`` holding only a recording stand-in
``docker`` (``PSEUDOLIFE_DOCKER`` names it too, and core's program
preflight proves no other one resolves), with the disposable home as the
per-user directory (the update lock and default safety directory land
there), and every confirmed case names ``--safety-dir`` inside its
directory. The stand-in answers only a case's script, call by call, and
refuses anything else, which fails the case; scripted bindings never name
port 8765 and the health check is never reached. Fixture files are removed
after each arm once shown unchanged, so goldens carry no
compressor-dependent bytes.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..core import WINDOWS, Case
from ..mutants import Mutant

MARKER = b"-- PostgreSQL database dump complete"
DIR = "shim/src/cli/restore_cli"


def _native(text: str) -> bytes:
    return (text.replace("\n", "\r\n") if WINDOWS else text).encode("utf-8")


# --- fixtures ----------------------------------------------------------------

def _gz(data: bytes) -> bytes:
    return gzip.compress(data, mtime=0)


GOOD_SQL = b"SELECT 1;\n" + MARKER + b"\n"


def _corrupt_deflate() -> bytes:
    # A stored-block header whose length and its complement disagree:
    # zlib raises "invalid stored block lengths" (zlib.error).
    return bytes([0x1F, 0x8B, 8, 0, 0, 0, 0, 0, 0, 0xFF, 0x01, 0x05, 0x00, 0x00, 0x00])


def _bad_crc(data: bytes) -> bytes:
    raw = bytearray(_gz(data))
    raw[-5] ^= 0xFF
    return bytes(raw)


DUMPS: dict[str, bytes] = {
    "good": _gz(GOOD_SQL),
    "no-marker": _gz(b"SELECT 1;\n"),
    "in-copy": _gz(b"COPY t (a) FROM stdin;\n" + MARKER + b"\n"),
    "copy-closed": _gz(b"COPY t (a) FROM stdin;\n" + MARKER + b"\n\\.\n" + MARKER + b"\n"),
    "copy-no-token": _gz(b"COPY  FROM stdin;\n" + MARKER + b"\n"),
    "copy-separator": _gz(b"COPY t\x1c FROM stdin;\n" + MARKER + b"\n"),
    "crlf": _gz(b"x\r\n" + MARKER + b"\r\n"),
    "lone-cr": _gz(b"x\r" + MARKER + b"\r"),
    "copy-crlf": _gz(b"COPY t FROM stdin;\r\n" + MARKER + b"\r\n"),
    "no-final-newline": _gz(b"x\n" + MARKER),
    "trailing-space": _gz(MARKER + b" \n"),
    "vertical-tab": _gz(b"x\x0b" + MARKER + b"\n"),
    "line-separator": _gz("x ".encode() + MARKER + b"\n"),
    "invalid-utf8": _gz(b"\xff\xfe\n" + MARKER + b"\n"),
    "invalid-utf8-marker": _gz(MARKER + b"\xff\n"),
    "multi-member": _gz(b"x\n") + _gz(MARKER + b"\n"),
    "trailing-zeros": _gz(GOOD_SQL) + b"\0\0\0\0",
    "trailing-junk": _gz(GOOD_SQL) + b"junk",
    "bad-crc": _bad_crc(GOOD_SQL),
    "truncated": _gz(GOOD_SQL)[:-3],
    "plain-sql": GOOD_SQL,
    "empty": b"",
    "corrupt-deflate": _corrupt_deflate(),
    # zlib: "invalid distance too far back"; miniz_oxide decodes it and the
    # CRC then disagrees (the bytes are pinned: a compressor could change them).
    "distance-too-far": bytes.fromhex(
        "1f8b08004efbf56a02ff0b76f571755b5130b4e6d2d55508c82f2e492f4a0d0ef45148492c494c4a2c4e554829cd"
        "2d5048cecf2dc8492d49e50a1e553eaa7c54f9a8f251e5a3ca47958f2a1f59ca010f13ac8a2e090000"),
}


def _field(value: int, width: int) -> bytes:
    return b"%0*o\0" % (width - 1, value)


def _header(name: bytes, kind: bytes = b"0", *, size: int = 0, linkname: bytes = b"",
            prefix: bytes = b"", mode: bytes | None = None, size_field: bytes | None = None) -> bytes:
    """A ustar header with every field given as raw bytes."""
    buf = bytearray(512)
    buf[0:100] = name.ljust(100, b"\0")[:100]
    buf[100:108] = mode if mode is not None else _field(0o644, 8)
    buf[108:116] = _field(0, 8)
    buf[116:124] = _field(0, 8)
    buf[124:136] = size_field if size_field is not None else _field(size, 12)
    buf[136:148] = _field(0, 12)
    buf[156:157] = kind
    buf[157:257] = linkname.ljust(100, b"\0")[:100]
    buf[257:265] = b"ustar\x0000"
    buf[345:500] = prefix.ljust(155, b"\0")[:155]
    buf[148:156] = b" " * 8
    buf[148:156] = b"%06o\0 " % sum(buf)
    return bytes(buf)


def _data(content: bytes) -> bytes:
    return content + b"\0" * (-len(content) % 512)


END = b"\0" * 1024


def _tar(*members: bytes) -> bytes:
    return b"".join(members) + END


def _file(name: bytes, content: bytes = b"x") -> bytes:
    return _header(name, size=len(content)) + _data(content)


def _sym(name: bytes, target: bytes) -> bytes:
    return _header(name, b"2", linkname=target)


def _python_tar(fmt: int, name: str) -> bytes:
    import io  # noqa: PLC0415
    import tarfile  # noqa: PLC0415
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=fmt) as archive:
        info = tarfile.TarInfo(name)
        info.size = 1
        info.mtime = 0
        archive.addfile(info, io.BytesIO(b"x"))
    return out.getvalue()


def _long(kind: bytes, value: bytes) -> bytes:
    """A GNU long name (``L``) or link (``K``) record, as GNU tar writes it."""
    return _header(b"././@LongLink", kind, size=len(value) + 1) + _data(value + b"\0")


def _bad_checksum_header() -> bytes:
    raw = bytearray(_header(b"x"))
    raw[148:156] = b"0000001\0"
    return bytes(raw)


def _signed_checksum_name() -> bytes:
    # A high-bit name byte: CPython also accepts the signed-char sum.
    raw = bytearray(_header(b"\xe9t\xe9"))
    raw[148:156] = b" " * 8
    signed = 256 + sum((b - 256 if b > 127 else b) for i, b in enumerate(raw) if not 148 <= i < 156)
    raw[148:156] = b"%06o\0 " % signed
    return _tar(bytes(raw), _data(b""))


STATES: dict[str, bytes] = {
    # validate_state passes
    "plain": _tar(_header(b".", b"5"), _file(b"./config.yaml"), _header(b"./sub", b"5"),
                  _file(b"./sub/a")),
    "links-inside": _tar(_file(b"config.yaml"), _header(b"sub", b"5"), _sym(b"sub/up", b"../config.yaml"),
                         _sym(b"l", b"sub/up"), _header(b"h", b"1", linkname=b"config.yaml")),
    "link-loop": _tar(_sym(b"a", b"b"), _sym(b"b", b"a")),
    "loop-ends-walk": _tar(_sym(b"a", b"a"), _sym(b"b", b"a/../../x")),
    "self-loop-hides-escape": _tar(_sym(b"a", b"a/../..")),
    "only-zero-blocks": END,
    "bad-later-header": _tar(_file(b"a"), _bad_checksum_header()),
    "short-later-header": _file(b"a") + b"\x01" * 100,
    "later-header-absent": _file(b"a"),
    # A directory skips no data: the escaping member after it is read.
    "directory-size-skips-nothing": _header(b"d", b"5", size=1024) + _file(b"../x") + END,
    "v7-directory-slash": _tar(_header(b"d/", b"\0"), _file(b"d/x")),
    "prefix-join": _tar(_header(b"x", prefix=b"sub")),
    "octal-prefix-fields": _tar(_header(b"x", mode=b"0o_644\0\0", size_field=b"  0o1\0\0\0\0\0\0\0") + _data(b"x")),
    "signed-checksum": _signed_checksum_name(),
    "gnu-long-name": _python_tar(1, "x" * 120),
    "gnu-long-records": _tar(_long(b"L", b"d" * 120 + b"//"), _header(b"short/", b"5"),
                             _long(b"K", b"e" * 110), _long(b"L", b"d" * 120 + b"/l"),
                             _header(b"l", b"2", linkname=b"short-target")),
    "gnu-long-cut-at-nul": _tar(_header(b"././@LongLink", b"L", size=9) + _data(b"ok\0/../x"), _file(b"../x")),
    "multi-member-gzip": None,  # built from "plain" below
    "trailing-zeros-gzip": None,
    # validate_state refuses
    "not-gzip": None,
    "truncated-gzip": None,
    "bad-crc-gzip": None,
    "empty-file": None,
    "empty-gzip": None,
    "garbage-first-header": _bad_checksum_header() + END,
    "invalid-mode-field": _tar(_header(b"x", mode=b"0000648\0")),
    "absolute-path": _tar(_file(b"/etc/x")),
    "dotdot-path": _tar(_file(b"a/../../x")),
    "dotdot-leading": _tar(_file(b"../x")),
    "backslash-path": _tar(_file(b"a\\b")),
    "prefix-escape": _tar(_header(b"x", prefix=b"..")),
    "fifo": _tar(_header(b"p", b"6")),
    "character-device": _tar(_header(b"c", b"3")),
    "block-device": _tar(_header(b"b", b"4")),
    "symlink-absolute": _tar(_sym(b"l", b"/etc")),
    "symlink-backslash": _tar(_sym(b"l", b"a\\b")),
    "symlink-escape": _tar(_sym(b"l", b"../..")),
    "symlink-chain-escape": _tar(_sym(b"a", b"b/x"), _sym(b"b", b"..")),
    "symlink-through-link": _tar(_sym(b"d", b".."), _file(b"d/x")),
    "hardlink-escape": _tar(_header(b"h", b"1", linkname=b"../x")),
    "transient-link": _tar(_file(b"a/x"), _sym(b"a", b"."), _sym(b"a/x", b"a/.."), _sym(b"a", b"b")),
    "member-data-truncated": _header(b"a", size=1000) + b"x" * 700,
    "member-padding-truncated": _header(b"a", size=10) + b"x" * 10,
    "gnu-long-name-escape": _tar(_long(b"L", b"../" + b"x" * 120), _file(b"short")),
    "gnu-long-link-escape": _tar(_long(b"K", b"../../" + b"y" * 120), _sym(b"l", b"short")),
    "gnu-long-then-end": _file(b"a") + _long(b"L", b"x" * 120) + END,
    "gnu-long-data-cut": _file(b"a") + _long(b"L", b"x" * 600)[:700],
    # a POSIX extended header naming a long path (both read it)
    "pax-name": _python_tar(2, "x" * 120),
    "corrupt-deflate-gzip": None,
}
_plain_tar = STATES["plain"]
STATES.update({
    "multi-member-gzip": _gz(_plain_tar[:700]) + _gz(_plain_tar[700:]),
    "trailing-zeros-gzip": _gz(_plain_tar) + b"\0" * 7,
    "not-gzip": _plain_tar,
    "truncated-gzip": _gz(_plain_tar)[:-4],
    "bad-crc-gzip": _bad_crc(_plain_tar),
    "empty-file": b"",
    "empty-gzip": _gz(b""),
    "corrupt-deflate-gzip": _corrupt_deflate(),
})
RAW_STATES = {"multi-member-gzip", "trailing-zeros-gzip", "not-gzip", "truncated-gzip", "bad-crc-gzip",
              "empty-file", "empty-gzip", "corrupt-deflate-gzip"}


def _state_bytes(name: str) -> bytes:
    raw = STATES[name]
    return raw if name in RAW_STATES else _gz(raw)


# --- the recording stand-in docker -------------------------------------------

# Every call is logged. A case may give a script (``docker-script.json``):
# call N must fully match entry N's argv patterns (regular expressions), and
# then gets that entry's exit code and streams. Any other call (and every
# call without a script) is refused with exit 1 and also noted in
# ``docker-unscripted``, which fails the case: the stand-in never improvises.
DOCKER_SCRIPT = """\
import base64, json, os, re, sys
root = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
log = os.path.join(root, "docker-calls.jsonl")
argv = sys.argv[1:]
count = 0
if os.path.exists(log):
    with open(log, encoding="utf-8") as stream:
        count = sum(1 for _ in stream)
with open(log, "a", encoding="utf-8") as stream:
    stream.write(json.dumps(argv) + "\\n")
script = None
path = os.path.join(root, "docker-script.json")
if os.path.exists(path):
    with open(path, encoding="utf-8") as stream:
        script = json.load(stream)
entry = script[count] if script is not None and count < len(script) else None
if entry is None or len(entry["argv"]) != len(argv) or not all(
        re.fullmatch(pattern, arg, re.S) for pattern, arg in zip(entry["argv"], argv)):
    if script is not None:
        with open(os.path.join(root, "docker-unscripted"), "a", encoding="utf-8") as stream:
            stream.write(json.dumps([count, argv]) + "\\n")
    sys.stderr.write("restore row: the stand-in docker refuses every call\\n")
    sys.exit(1)
sys.stdout.buffer.write(base64.b64decode(entry["stdout"]))
sys.stderr.buffer.write(base64.b64decode(entry["stderr"]))
sys.exit(entry["exit"])
"""


def _install_docker(arm) -> None:
    from ..test_update_transactions import _program  # noqa: PLC0415
    path = arm.home / "bin" / "docker"
    path.parent.mkdir()
    _program(path, DOCKER_SCRIPT)


# --- the scripted Docker world ---------------------------------------------------

BIG_KB = 20 * 1024 * 1024  # 20 GiB free
LOW_KB = 1024              # 1 MiB free
URL = "postgresql://pseudolife:fixture@pseudolife-pg:5432/pseudolife_memory"
PG, DAEMON = "pg-x", "daemon-x"
SAFE = ["--yes", "--container", PG, "--daemon-container", DAEMON, "--safety-dir", "safety"]
PG_DATA = "/var/lib/postgresql/data"
PG_DUMP = ('unset PGHOSTADDR PGSERVICE PGSERVICEFILE; pg_dump --no-owner --no-acl -h "$1" -p "$2" '
           '-U "$3" -d "$4" -Z9 > "$5"')
FIND = (r'test -d "$1" && find -H "$1" -mindepth 1 -maxdepth 1 ! -type s ! \( -type f -name '
        r'".s.PGSQL.*.lock" \) -print -quit')
BACKUP_REMOTE = r"/tmp/pl_backup-\d{8}-\d{6}-[0-9a-f]{8}\.sql\.gz"
RESTORE_REMOTE = r"/tmp/pl_restore-[0-9a-f]{16}\.sql\.gz"


def _lit(*args: str) -> list[str]:
    import re  # noqa: PLC0415
    return [re.escape(arg) for arg in args]


def _call(pattern: list[str], exit: int = 0, stdout: str | bytes = "", stderr: str = "") -> dict:
    out = stdout.encode() if isinstance(stdout, str) else stdout
    return {"argv": pattern, "exit": exit, "stdout": base64.b64encode(out).decode(),
            "stderr": base64.b64encode(stderr.encode()).decode()}


def _df(free_kb: int = BIG_KB, mount: str = "/") -> str:
    return ("Filesystem     1024-blocks      Used Available Capacity Mounted on\n"
            f"overlay         99999999         1 {free_kb} 1% {mount}\n")


def _daemon_doc(**changes) -> dict:
    doc = {
        "Name": "/" + DAEMON, "Image": "sha256:fixture-image",
        "State": {"Running": False},
        "Config": {"Env": [f"PSEUDOLIFE_MCP_DATABASE_URL={URL}", "PATH=/usr/bin"], "User": "pseudolife"},
        "Mounts": [{"Type": "volume", "Source": "/var/lib/docker/volumes/state/_data", "Destination": "/data",
                    "RW": True}],
        # Never 8765: a scripted binding must not name a live daemon's port.
        "HostConfig": {"PortBindings": {"8765/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9"}]}},
        "NetworkSettings": {"Networks": {"stack": {"IPAddress": "10.0.0.3"}}, "Ports": {}},
    }
    doc.update(changes)
    return doc


def _pg_doc(**changes) -> dict:
    doc = {
        "Name": "/" + PG, "State": {"Running": True},
        "Mounts": [{"Type": "volume", "Destination": PG_DATA, "RW": True}],
        "NetworkSettings": {"Networks": {"stack": {"Aliases": ["pseudolife-pg"], "IPAddress": "10.0.0.2"}}},
    }
    doc.update(changes)
    return doc


def _inspect(name: str, doc, *, exit: int = 0, stderr: str = "") -> dict:
    text = doc if isinstance(doc, str) else json.dumps([doc], ensure_ascii=False)
    return _call(_lit("inspect", name), exit, text, stderr)


def _exists(answer: str = "t\n", *, exit: int = 0, stderr: str = "", port: str = "5432",
            user: str = "pseudolife", db: str = "pseudolife_memory", pg: str = PG) -> dict:
    literal = "'" + db.replace("'", "''") + "'"
    return _call(_lit("exec", pg, "env", "-u", "PGHOSTADDR", "-u", "PGSERVICE", "-u", "PGSERVICEFILE", "psql",
                      "-X", "-h", "/var/run/postgresql", "-p", port, "-U", user, "-d", "dbname=postgres", "-Atc",
                      f"SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname = {literal})"),
                 exit, answer, stderr)


def _space(name: str, path: str, free_kb: int = BIG_KB, *, exit: int = 0, stdout: str | None = None) -> dict:
    return _call(_lit("exec", name, "df", "-Pk", path), exit, _df(free_kb, path) if stdout is None else stdout)


def _probes(daemon: dict | None = None, pg: dict | None = None, *, exists: str = "t\n",
            mounts: tuple[str, ...] = (PG_DATA,), free_kb: int = BIG_KB, **selection) -> list[dict]:
    """The read-only calls of a run that passes every check, in order."""
    pg = pg or _pg_doc()
    return [_inspect(DAEMON, daemon or _daemon_doc()), _inspect(PG, pg), _exists(exists, **selection),
            _space(PG, "/", free_kb), _space(PG, "/tmp", free_kb), _inspect(PG, pg),
            *[_space(PG, path, free_kb) for path in sorted({"/", *mounts})]]


def _dump_fails(port: str = "5432", user: str = "pseudolife", db: str = "pseudolife_memory") -> list[dict]:
    """The oracle's safety dump, made to fail at once (no data is copied)."""
    return [_space(PG, "/"), _space(PG, "/tmp"),
            _call(_lit("exec", PG, "sh", "-c", PG_DUMP, "_", "/var/run/postgresql", port, user,
                       f"dbname={db}") + [BACKUP_REMOTE], 1, "", "pg_dump: refused by the fixture\n"),
            _call(_lit("exec", PG, "rm", "-f") + [BACKUP_REMOTE]),
            _call(_lit("exec", PG, "rm", "-f") + [RESTORE_REMOTE, RESTORE_REMOTE + r"\.sql"])]


HOLDER = """\
import os, sys
path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
handle = open(path, "a+b")
if os.name == "nt":
    import msvcrt
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
else:
    import fcntl
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
sys.stdout.write("held\\n")
sys.stdout.flush()
sys.stdin.read()
"""


def _hold_lock(arm) -> subprocess.Popen:
    """Another update holding the lock (pid file 4242) for the whole arm."""
    lock = arm.home / ".pseudolife-mcp" / "update.lock"
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE)
    line = proc.stdout.readline()
    if line.strip() != b"held":
        proc.kill()
        raise RuntimeError(f"restore row: the lock holder did not take the lock: {line!r}")
    (lock.parent / "update.lock.pid").write_text("4242\n", encoding="utf-8")
    return proc


# --- cases -----------------------------------------------------------------------

def _case(case_id: str, argv: list[str], *, dump: str | None = "good", state: str | None = None,
          rules: tuple[str, ...] = (), columns: str = "80",
          note: str = "", script=None, prepare=None) -> Case:
    docker = "{HOME}/bin/docker.exe" if WINDOWS else "{HOME}/bin/docker"
    env: dict[str, str | None] = {"PATH": "{HOME}/bin", "COLUMNS": columns, "PSEUDOLIFE_DOCKER": docker,
                                  "PSEUDOLIFE_MCP_DATA_DIR": None}
    case = Case(case_id, ["restore", *argv], env=env, rules=rules, programs=("docker",), note=note)
    fixtures = {}
    if dump is not None:
        fixtures["dump.sql.gz"] = DUMPS[dump]
    if state is not None:
        fixtures["state.tgz"] = _state_bytes(state)

    held: dict[str, subprocess.Popen] = {}

    def setup(arm) -> None:
        _install_docker(arm)
        for name, data in fixtures.items():
            (arm.cwd / name).write_bytes(data)
        if script is not None:
            (arm.home / "docker-script.json").write_text(json.dumps(script(arm)), encoding="utf-8")
        if prepare is not None:
            holder = prepare(arm)
            if holder is not None:
                held[arm.name] = holder

    def after(arm, observation: dict) -> None:
        observation["arm"] = arm.name
        holder = held.pop(arm.name, None)
        if holder is not None:
            holder.stdin.close()
            holder.wait(30)
        log = arm.home / "docker-calls.jsonl"
        observation["docker"] = ([json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
                                 if log.exists() else [])
        log.unlink(missing_ok=True)
        (arm.home / "docker-script.json").unlink(missing_ok=True)
        unscripted = arm.home / "docker-unscripted"
        if unscripted.exists():
            # Compared like any field: a script is written so that the
            # oracle never makes one, so this arm's observation differs.
            observation["unscripted"] = unscripted.read_text(encoding="utf-8").splitlines()
            unscripted.unlink()
        # The launcher embeds this interpreter's path: fixture infrastructure.
        assert (arm.home / "bin" / "docker.py").is_file()
        shutil.rmtree(arm.home / "bin")
        intact = all((arm.cwd / name).is_file()
                     and hashlib.sha256((arm.cwd / name).read_bytes()).digest() == hashlib.sha256(data).digest()
                     for name, data in fixtures.items())
        observation["fixtures"] = "unchanged" if intact else "changed"
        for name in fixtures:
            (arm.cwd / name).unlink(missing_ok=True)

    case.setup = setup
    case.after = after
    case.script = script
    return case


def _null_console(case: Case) -> Case:
    case.null_stdin_stdout = True
    return case


DUMP = ["--backup-file", "dump.sql.gz"]
STATE = ["--state-archive", "state.tgz"]
EVERY_OPTION = ["--leave-stopped", "--container", "pg-x", "--daemon-container", "daemon-x", "--db", "bank",
                "--user", "role", "--db-port", "5433", "--daemon-url", "http://127.0.0.1:9",
                "--safety-dir", "safety"]

DUMP_PASS = ("good", "copy-closed", "copy-no-token", "copy-separator", "crlf", "lone-cr", "no-final-newline",
             "invalid-utf8", "multi-member", "trailing-zeros")
DUMP_REFUSE = ("no-marker", "in-copy", "copy-crlf", "trailing-space", "vertical-tab", "line-separator",
               "invalid-utf8-marker", "trailing-junk", "bad-crc", "truncated", "plain-sql", "empty",
               "corrupt-deflate", "distance-too-far")
STATE_PASS = ("plain", "links-inside", "link-loop", "loop-ends-walk", "self-loop-hides-escape",
              "later-header-absent",
              "v7-directory-slash", "prefix-join",
              "signed-checksum", "gnu-long-name", "gnu-long-records", "gnu-long-cut-at-nul",
              "multi-member-gzip", "trailing-zeros-gzip", "pax-name")
STATE_REFUSE = ("not-gzip", "truncated-gzip", "bad-crc-gzip", "corrupt-deflate-gzip", "empty-file", "empty-gzip",
                "garbage-first-header", "invalid-mode-field", "absolute-path", "dotdot-path", "dotdot-leading",
                "backslash-path", "prefix-escape", "fifo", "character-device", "block-device",
                "symlink-absolute", "symlink-backslash", "symlink-escape", "symlink-chain-escape",
                "symlink-through-link", "hardlink-escape", "transient-link", "member-data-truncated",
                "member-padding-truncated", "directory-size-skips-nothing", "gnu-long-name-escape",
                "gnu-long-link-escape", "gnu-long-then-end", "gnu-long-data-cut")


def _gate_cases() -> list[Case]:
    out = []
    for name in DUMP_PASS + DUMP_REFUSE:
        out.append(_case(f"dump-{name}", DUMP, dump=name))
    for name in STATE_PASS + STATE_REFUSE:
        out.append(_case(f"state-{name}", DUMP + STATE, state=name))
    return out


def cases() -> list[Case]:
    return [
        # argparse at COLUMNS=80
        _case("help", ["--help"], dump=None),
        _case("help-short", ["-h"], dump=None),
        _case("help-after-flags", ["--yes", "--db-port", "1", "-h", "--bogus"], dump=None),
        _case("no-arguments", [], dump=None),
        _case("missing-backup-file", ["--yes", "--bogus"], dump=None),
        _case("missing-value", ["--backup-file"], dump=None),
        _case("missing-value-before-option", ["--backup-file", "--yes"], dump=None),
        _case("port-letters", DUMP + ["--db-port", "abc"]),
        _case("port-empty-joined", DUMP + ["--db-port="]),
        _case("port-spaced", DUMP + ["--db-port", " 1 2"]),
        _case("port-hex", DUMP + ["--db-port", "0x10"]),
        _case("port-control", DUMP + ["--db-port", "\x01"]),
        _case("port-before-help", ["--db-port", "x", "-h"], dump=None),
        _case("port-before-unknown", ["--db-port", "abc", "--bogus"], dump=None),
        _case("unknown-option", DUMP + ["--bogus", "y"]),
        _case("unknown-single-dash", DUMP + ["-x"]),
        _case("positional", DUMP + ["extra"]),
        _case("ambiguous-prefix", ["--db-port", "abc", "--d", "x"], dump=None),
        _case("ambiguous-joined", DUMP + ["--daemon=x"]),
        _case("flag-explicit", DUMP + ["--yes=1"]),
        _case("help-explicit-empty", ["--help=", "--backup-file", "x"], dump=None),
        # values that argparse accepts and the dump check then refuses
        _case("port-negative", ["--backup-file", "missing.sql.gz", "--db-port", "-1"]),
        _case("port-underscore", ["--backup-file", "missing.sql.gz", "--db-port", "1_0"]),
        _case("backup-file-negative-number", ["--backup-file", "-1"]),
        _case("backup-file-empty", ["--backup-file", ""]),
        _case("backup-file-joined", ["--backup-file=missing.sql.gz"]),
        _case("backup-file-directory", ["--backup-file", "."]),
        # the dump check, then the state check (which follows a complete dump only)
        *_gate_cases(),
        _case("state-before-confirmation", DUMP + STATE + ["--leave-stopped"], state="symlink-escape"),
        _case("dump-before-state", DUMP + STATE, dump="no-marker", state="symlink-escape"),
        _case("dump-normalized-path", ["--backup-file", "./dump.sql.gz"]),
        _case("dump-trailing-slash", ["--backup-file", "dump.sql.gz/"]),
        # the refusal without --yes (stdin is a pipe in both arms)
        _case("no-yes", DUMP),
        _case("no-yes-every-option", DUMP + STATE + EVERY_OPTION, state="plain"),
        # stdin and stdout the null device: a character device, never a console
        _null_console(_case("no-yes-null-device", DUMP)),
        # an abbreviation, a `-h` cluster and a non-ASCII option
        _case("abbreviation", ["--back", "missing.sql.gz"]),
        _case("help-cluster", ["-hx"], dump=None),
        _case("unicode-unknown", DUMP + ["--bé"]),
        # argv that passes every gate reaches Docker: the stand-in refuses
        # the first inspect, and both arms stop there under the update lock
        _case("valid-yes", DUMP + ["--yes", "--safety-dir", "safety"]),
        _case("valid-yes-state", DUMP + STATE + ["--yes", "--safety-dir", "safety"], state="links-inside"),
        _case("valid-every-option", DUMP + STATE + EVERY_OPTION + ["--yes"], state="plain"),
        _case("valid-yes-repeated", ["--backup-file", "missing", "--yes", "--backup-file=dump.sql.gz", "--yes",
                                     "--safety-dir", "safety"]),
        _case("valid-port-out-of-range", DUMP + ["--yes", "--db-port", "99999999999999999999999999999999999999999",
                                                 "--safety-dir", "safety"]),
        _case("valid-dump-trailing-slash", ["--backup-file", "dump.sql.gz/", "--yes", "--safety-dir", "safety"]),
        *_docker_cases(),
    ]


def _scripted(case_id: str, calls, *, argv: list[str] | None = None, extra: list[str] = (), **kwargs) -> Case:
    """A confirmed restore against the scripted Docker world. ``calls`` is
    the list of answers, or a function of the arm returning it."""
    script = calls if callable(calls) else (lambda arm, fixed=calls: fixed)
    return _case(case_id, (argv if argv is not None else DUMP + SAFE) + list(extra), script=script, **kwargs)


def _checkout(arm) -> None:
    (arm.cwd / "repo" / ".git").mkdir(parents=True)


def _make_state(arm) -> None:
    (arm.cwd / "state").mkdir()


def _state_mount(arm) -> list[dict]:
    """The daemon's /data is a bind mount of cwd/state; the safety dir is inside it."""
    source = str(arm.cwd / "state")
    if WINDOWS:
        # Docker Desktop reports a Windows bind source in its VM's form.
        drive, rest = source[0].lower(), source[3:].replace("\\", "/")
        source = f"/run/desktop/mnt/host/{drive}/{rest}"
    daemon = _daemon_doc(Mounts=[{"Type": "bind", "Source": source, "Destination": "/data", "RW": True}])
    return [_inspect(DAEMON, daemon), _inspect(PG, _pg_doc()), _exists()]


def _docker_cases() -> list[Case]:
    noisy = "WARNING: the fixture daemon prints this on stderr\n"
    socket = {"Type": "bind", "Destination": "/var/run/postgresql", "RW": True}
    running = _daemon_doc(State={"Running": True})
    return [
        # DockerBank(...): inspect, resolution, the port, the /data mount, PostgreSQL running
        _scripted("docker-inspect-pg-fails", [_inspect(DAEMON, _daemon_doc()), _inspect(PG, "", exit=1)]),
        _scripted("docker-inspect-empty-list", [_inspect(DAEMON, "[]")]),
        _scripted("docker-inspect-not-a-dict", [_inspect(DAEMON, "[5]")]),
        _scripted("docker-inspect-object", [_inspect(DAEMON, '{"Id": "x"}')]),
        _scripted("docker-inspect-empty-output", [_inspect(DAEMON, "")]),
        _scripted("docker-resolve-unmatched-host", [
            _inspect(DAEMON, _daemon_doc(Config={"Env": ["PSEUDOLIFE_MCP_DATABASE_URL=postgresql://u@elsewhere/db"]})),
            _inspect(PG, _pg_doc())]),
        _scripted("docker-resolve-no-shared-network", [
            _inspect(DAEMON, _daemon_doc(NetworkSettings={"Networks": {"other": {}}})), _inspect(PG, _pg_doc())]),
        _scripted("docker-resolve-service", [
            _inspect(DAEMON, _daemon_doc(Config={"Env": [
                "PSEUDOLIFE_MCP_DATABASE_URL=host=pseudolife-pg user=u", "PGSERVICE=bank"]})),
            _inspect(PG, _pg_doc())]),
        # all four selectors bypass the daemon's URL; then PostgreSQL is stopped
        _scripted("docker-explicit-selectors", [
            _inspect(DAEMON, _daemon_doc(Config={"Env": []})), _inspect(PG, _pg_doc(State={"Running": False}))],
            extra=["--db", "bank", "--user", "role", "--db-port", "5433"]),
        _scripted("docker-explicit-port-range", [
            _inspect(DAEMON, _daemon_doc()), _inspect(PG, _pg_doc())],
            extra=["--db", "bank", "--user", "role", "--db-port", "70000"]),
        _scripted("docker-port-option-range", [_inspect(DAEMON, _daemon_doc()), _inspect(PG, _pg_doc())],
                  extra=["--db-port", "0"]),
        _scripted("docker-no-image", [_inspect(DAEMON, _daemon_doc(Image="")), _inspect(PG, _pg_doc())]),
        _scripted("docker-no-data-mount", [
            _inspect(DAEMON, _daemon_doc(Mounts=[{"Destination": "/models"}])), _inspect(PG, _pg_doc())]),
        _scripted("docker-pg-stopped", [_inspect(DAEMON, _daemon_doc()), _inspect(PG, _pg_doc(State={}))]),
        # stdout only: a warning on stderr beside a successful inspect is ignored
        _scripted("docker-stderr-ignored-on-success", [
            _inspect(DAEMON, _daemon_doc(), stderr=noisy),
            _inspect(PG, _pg_doc(State={"Running": False}), stderr=noisy)]),
        # restore(): the health port, the database probe, the safety directory
        _scripted("docker-no-health-port", [
            _inspect(DAEMON, _daemon_doc(HostConfig={})), _inspect(PG, _pg_doc())]),
        _scripted("docker-psql-fails", [
            _inspect(DAEMON, _daemon_doc()), _inspect(PG, _pg_doc()), _exists("", exit=1, stderr="FATAL\n")]),
        _scripted("docker-psql-socket", [
            _inspect(DAEMON, _daemon_doc()), _inspect(PG, _pg_doc()),
            _exists("", exit=2, stderr='psql: error: connection to server on socket '
                                       '"/var/run/postgresql/.s.PGSQL.5432" failed: No such file or directory\n')]),
        _scripted("docker-psql-exit-2-other", [
            _inspect(DAEMON, _daemon_doc()), _inspect(PG, _pg_doc()), _exists("", exit=2, stderr="FATAL: role\n")]),
        _scripted("docker-exists-unknown", [
            _inspect(DAEMON, _daemon_doc()), _inspect(PG, _pg_doc()), _exists("maybe\n")]),
        _scripted("docker-safety-in-checkout", [
            _inspect(DAEMON, _daemon_doc()), _inspect(PG, _pg_doc()), _exists()],
            argv=DUMP + ["--yes", "--container", PG, "--daemon-container", DAEMON, "--safety-dir", "repo/safety"],
            prepare=_checkout),
        _scripted("docker-safety-in-state-mount", _state_mount,
                  argv=DUMP + ["--yes", "--container", PG, "--daemon-container", DAEMON,
                               "--safety-dir", "state/safety"], prepare=_make_state),
        # _restore_space: Docker storage, PostgreSQL's /tmp and its writable mounts
        _scripted("docker-storage-unreadable", [*_probes()[:3], _space(PG, "/", stdout="Filesystem\n")]),
        _scripted("docker-storage-low", [*_probes()[:3], _space(PG, "/", LOW_KB)]),
        _scripted("docker-storage-from-daemon", [*_probes()[:3], _space(PG, "/", exit=1, stdout=""),
                                                 _space(DAEMON, "/", LOW_KB)]),
        _scripted("docker-tmp-fails", [*_probes()[:4], _space(PG, "/tmp", exit=1, stdout="")]),
        # one KiB under the 8 GiB floor
        _scripted("docker-tmp-low", [*_probes()[:4], _space(PG, "/tmp", 8 * 1024 * 1024 - 1)]),
        _scripted("docker-inspect-again-fails", [*_probes()[:5], _inspect(PG, "", exit=1)]),
        _scripted("docker-mount-low", [*_probes()[:7], _space(PG, PG_DATA, LOW_KB)]),
        _scripted("docker-socket-find-fails", [
            *_probes(pg=_pg_doc(Mounts=[socket]))[:5], _inspect(PG, _pg_doc(Mounts=[socket])),
            _call(_lit("exec", PG, "sh", "-c", FIND, "_", "/var/run/postgresql"), 1)]),
        # the lock: another update holds it, so the oracle refuses before Docker
        _scripted("docker-lock-busy", [], prepare=_hold_lock),
    ]


FILE = DIR + "/mod.rs"
ARGS = DIR + "/args.rs"
DUMPRS = DIR + "/dump.rs"
GZIPRS = DIR + "/gzip.rs"
STATERS = DIR + "/state.rs"
BANK = DIR + "/bank.rs"
CONN = DIR + "/conn.rs"

MUTANTS = [
    Mutant("restore-refusal-exit", "restore", FILE, "    ExitCode::from(2)\n}\n\n/// Every run",
           "    ExitCode::from(1)\n}\n\n/// Every run", ("no-yes",)),
    Mutant("restore-yes-ignored", "restore", FILE, "    if !args.yes {", "    if true {", ("valid-yes",)),
    Mutant("restore-null-device-terminal", "restore", "shim/src/cli/connect/mod.rs",
           "        && std::io::stdin().is_terminal()\n        && std::io::stdout().is_terminal()\n", "\n",
           ("no-yes-null-device",), platforms=("windows",)),
    Mutant("restore-state-skipped", "restore", FILE, "if let Some(archive) = &args.state_archive {",
           "if let Some(archive) = None::<&std::path::PathBuf> {", ("state-symlink-escape",)),
    Mutant("restore-dump-after-state", "restore", FILE,
           "    if dump::complete(&args.backup_file) != Verdict::Pass {\n        return refuse(INCOMPLETE_DUMP);",
           "    if dump::complete(&args.backup_file) != Verdict::Pass {\n"
           "        return refuse(if args.state_archive.is_none() { INCOMPLETE_DUMP } else { UNSAFE_STATE });",
           ("dump-before-state",)),
    Mutant("restore-required-before-type", "restore", ARGS, '"--db-port" => match python_int(value) {',
           '"--db-port" if !backup_file => return Parsed::Error("the following arguments are required: --backup-file".into()),\n'
           '            "--db-port" => match python_int(value) {', ("port-before-unknown",)),
    Mutant("restore-negative-as-option", "restore", ARGS, "    } else if negative_number(raw) {\n        return Class::Value;",
           "    } else if false {\n        return Class::Value;", ("port-negative",)),
    Mutant("restore-copy-loose", "restore", DUMPRS, "    token > 0\n", "    token != usize::MAX\n", ("dump-copy-no-token",)),
    Mutant("restore-lone-cr", "restore", DUMPRS, "                b'\\r' => {\n                    self.end_line();",
           "                b'\\r' => {\n                    self.line.push(byte);", ("dump-lone-cr",)),
    Mutant("restore-gzip-padding", "restore", GZIPRS, "                Some(0) => {}\n", "", ("dump-trailing-zeros",)),
    Mutant("restore-gzip-crc", "restore", GZIPRS, "stored_crc != crc.sum()", "stored_crc == crc.sum().wrapping_add(1)", ("dump-bad-crc",)),
    Mutant("restore-directory-skips", "restore", STATERS,
           "let data = if matches!(member.kind, 0 | b'0') {",
           "let data = if true {", ("state-directory-size-skips-nothing",)),
    Mutant("restore-padding-unchecked", "restore", STATERS, "            || self.seen < self.next\n", "",
           ("state-member-padding-truncated",)),
    Mutant("restore-no-transient-pass", "restore", STATERS,
           "    let mut kinds: HashMap<Vec<u8>, u8> = HashMap::new();\n    for member in members {",
           "    let mut kinds: HashMap<Vec<u8>, u8> = HashMap::new();\n    for member in &members[..0] {",
           ("state-transient-link",)),
    Mutant("restore-fifo-allowed", "restore", STATERS, "matches!(self.kind, b'3' | b'4' | b'6')",
           "matches!(self.kind, b'3' | b'4')", ("state-fifo",)),
    Mutant("restore-loop-continues", "restore", STATERS, "                None => return Ok(None),", "                None => {}",
           ("state-loop-ends-walk",)),
    Mutant("restore-prefix-ignored", "restore", STATERS, "    if posix && !prefix.is_empty() {", "    if false {",
           ("state-prefix-escape",)),
    Mutant("restore-signed-checksum", "restore", STATERS, "stored != unsigned && stored != signed",
           "stored != unsigned && stored != signed + 1",
           ("state-signed-checksum",)),
    Mutant("restore-long-name-ignored", "restore", STATERS,
           "                    if kind == b'L' {\n                        member.name = data;",
           "                    if kind == b'L' {\n                        let _ = data;", ("state-gnu-long-name-escape",)),
    Mutant("restore-long-link-ignored", "restore", STATERS,
           "                    } else {\n                        member.linkname = data;",
           "                    } else {\n                        let _ = data;", ("state-gnu-long-link-escape",)),
    Mutant("restore-long-short-read-quiet", "restore", STATERS,
           "        let incomplete = self.long.is_some()\n            || !self.pending.is_empty()",
           "        let incomplete = false\n            && !self.pending.is_empty()", ("state-gnu-long-data-cut",)),
    Mutant("restore-long-keeps-after-nul", "restore", STATERS, "                                long.ended = true;",
           "                                long.data.extend_from_slice(&part[at..]);", ("state-gnu-long-cut-at-nul",)),
    Mutant("restore-confirmed-skipped", "restore", FILE, "    let code = locked(&args, &directory, state_size);",
           "    let _ = locked;\n    let code = ExitCode::SUCCESS;",
           ("valid-yes", "docker-pg-stopped", "docker-mount-low")),
    Mutant("restore-busy-pid-unknown", "restore", FILE,
           "    let pid = if !pid.is_empty() && pid.bytes().all(|b| b.is_ascii_digit()) {", "    let pid = if false {",
           ("docker-lock-busy",)),
    Mutant("restore-lock-busy-ignored", "restore", FILE, "        Ok(false) => return busy(&directory),",
           "        Ok(false) => {}", ("docker-lock-busy",)),
    Mutant("restore-refusal-unlocked", "restore", FILE,
           '    let mut lock = os_lock::Lock::new(directory.join("update.lock"));',
           '    let mut lock = os_lock::Lock::new(directory.join("update.lock.unused"));', ("docker-pg-stopped",)),
    Mutant("restore-inspect-exit", "restore", BANK,
           '                        self.pg, self.daemon\n                    ),\n                    1,',
           '                        self.pg, self.daemon\n                    ),\n                    2,',
           ("docker-inspect-pg-fails",)),
    Mutant("restore-inspect-stderr", "restore", BANK,
           "let value: Value = serde_json::from_slice(&ran.stdout)",
           "let value: Value = serde_json::from_slice(&[ran.stdout.clone(), ran.stderr.clone()].concat())",
           ("docker-stderr-ignored-on-success",)),
    Mutant("restore-inspect-object-accepted", "restore", BANK,
           "        match value {\n            Value::Array(items)",
           "        match value {\n            item @ Value::Object(_) => Ok(item),\n            Value::Array(items)",
           ("docker-inspect-object",)),
    Mutant("restore-explicit-ignored", "restore", BANK, "    let explicit = nonempty(&args.container).is_some()",
           "    let explicit = false && nonempty(&args.container).is_some()", ("docker-explicit-selectors",)),
    Mutant("restore-port-range", "restore", BANK, "    if !(1..=65535).contains(&port) {\n        return fail(PORT, 2);",
           "    if port < 0 {\n        return fail(PORT, 2);", ("docker-port-option-range", "docker-explicit-port-range")),
    Mutant("restore-image-unchecked", "restore", BANK, "        _ => return fail(NO_MOUNT, 2),\n    };",
           "        _ => String::new(),\n    };", ("docker-no-image",)),
    Mutant("restore-data-mount-unchecked", "restore", BANK, '    if !state_mounts.iter().any(|m| m == "/data") {',
           "    if false {", ("docker-no-data-mount",)),
    Mutant("restore-pg-running-unchecked", "restore", BANK, "    if !pg_running {", "    if false {",
           ("docker-pg-stopped",)),
    Mutant("restore-health-port-unchecked", "restore", BANK,
           "    if !args.leave_stopped && daemon_url.is_empty() {", "    if false {", ("docker-no-health-port",)),
    Mutant("restore-socket-reason", "restore", BANK, 'reason.contains("socket")', 'reason.contains("sockets")',
           ("docker-psql-socket",)),
    Mutant("restore-exists-unchecked", "restore", BANK, "        _ => return fail(EXISTS, 2),", "        _ => false,",
           ("docker-exists-unknown",)),
    Mutant("restore-checkout-unchecked", "restore", BANK, 'pathlib::join(&parent, ".git")',
           'pathlib::join(&parent, ".gitx")', ("docker-safety-in-checkout",)),
    Mutant("restore-state-mount-unchecked", "restore", BANK,
           "            if pathlib::is_relative_to(&safety, &resolved) {", "            if false {",
           ("docker-safety-in-state-mount",)),
    Mutant("restore-desktop-unmapped", "restore", BANK,
           '    for prefix in ["/run/desktop/mnt/host/", "/host_mnt/"] {', "    for prefix in [\"/unmapped/\"] {",
           ("docker-safety-in-state-mount",), platforms=("windows",)),
    Mutant("restore-storage-order", "restore", BANK, "        for name in [self.pg.clone(), self.daemon.clone()] {",
           "        for name in [self.daemon.clone(), self.pg.clone()] {", ("docker-storage-from-daemon",)),
    Mutant("restore-storage-location", "restore", BANK, '&format!("Docker storage ({name})")',
           '&format!("Docker storage {name}")', ("docker-storage-unreadable",)),
    Mutant("restore-space-order", "restore", BANK,
           '    bank.storage_space(&image, run_tag)?;\n    bank.container_space("/tmp")?;',
           '    bank.container_space("/tmp")?;\n    bank.storage_space(&image, run_tag)?;', ("docker-tmp-fails",)),
    Mutant("restore-free-floor", "restore", BANK, "    if free < MIN_FREE {", "    if free < MIN_FREE - 1024 {",
           ("docker-tmp-low",)),
    Mutant("restore-socket-find-exit", "restore", BANK, "                return fail(SOCKET_MOUNT, 2);",
           "                continue;", ("docker-socket-find-fails",)),
    Mutant("restore-psql-argv", "restore", BANK,
           '            "psql",\n            "-X",\n            "-h",\n            "/var/run/postgresql",\n            "-p",\n        ]',
           '            "psql",\n            "-h",\n            "/var/run/postgresql",\n            "-p",\n        ]',
           ("docker-psql-fails",)),
    Mutant("restore-dbname-quoted", "restore", CONN, "    if escaped.chars().any(python_space) {", "    if true {",
           ("docker-psql-fails",)),
    Mutant("restore-host-unmatched-allowed", "restore", CONN,
           "    } else if host.is_empty() || !aliases.contains(&host) {", "    } else if host.is_empty() {",
           ("docker-resolve-unmatched-host",)),
    Mutant("restore-service-ignored", "restore", CONN,
           '    if setting("service", "PGSERVICE").is_some_and(|v| !v.is_empty()) {', "    if false {",
           ("docker-resolve-service",)),
    Mutant("restore-unshared-network-read", "restore", CONN,
           "        if !daemon_networks.contains_key(name) {\n            continue;\n        }", "",
           ("docker-resolve-no-shared-network",)),
]


def _values(argv: list[str], flag: str) -> list[str]:
    out = []
    for at, arg in enumerate(argv):
        if arg == flag and at + 1 < len(argv):
            out.append(argv[at + 1])
        elif arg.startswith(flag + "="):
            out.append(arg[len(flag) + 1:])
    return out


def _assert_safe() -> None:
    """Every case reaches only the stand-in docker; every confirmed case
    (one past the gates) names a safety directory inside its own directory;
    no case names a daemon URL but the closed loopback port, and no scripted
    inspect publishes a binding on 8765 (a live daemon's default port)."""
    from ..core import DEAD_DAEMON_URL, Arm  # noqa: PLC0415
    probe = Arm("check", Path("/nonexistent-restore-home"), Path("/nonexistent-restore-home/cwd"))
    for case in cases():
        argv = case.argv[1:]
        assert case.env.get("PATH") == "{HOME}/bin" and case.env.get("PSEUDOLIFE_DOCKER"), case.id
        assert "docker" in case.programs, case.id
        gate = any(case.id.startswith(prefix) for prefix in ("help", "missing", "port-", "flag-", "defer-"))
        if "--yes" in argv and not gate:
            safety = _values(argv, "--safety-dir")
            assert safety and not Path(safety[-1]).is_absolute() and ".." not in Path(safety[-1]).parts, case.id
        assert all(url == DEAD_DAEMON_URL for url in _values(argv, "--daemon-url")), case.id
        for entry in (case.script(probe) if case.script else []):
            text = base64.b64decode(entry["stdout"]).decode("utf-8")
            assert '"HostPort": "8765"' not in text, case.id


_assert_safe()

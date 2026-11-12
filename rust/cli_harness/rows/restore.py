"""CLI-RESTORE-GATES: what ``pseudolife-mcp restore`` decides before Docker.

Answered: argparse's help and errors at 80 columns, the dump-completeness
refusal, the state-archive refusal (every ``validate_state`` reason) and the
refusal without ``--yes`` when stdin and stdout are not terminals (the spec
is ``restore.md`` beside this file).

Safety. Restore replaces a bank, and its defaults name the stack's own
containers. The oracle runs only on argv that stop at those gates; argv
that passes them (``ORACLE_NOT_RUN``) runs a verified stub
``pseudolife_memory.cli`` in the Python arm, and the expectation is the
Rust deferral. Every case also runs with ``PATH`` holding only a recording
stand-in ``docker`` (``PSEUDOLIFE_DOCKER`` names it too) that answers every
call with exit 1, and with the disposable home as the per-user directory,
so a gate that failed open could reach neither a real container nor the
real update lock. Its call log is compared and must be empty in both arms.
Fixture files are removed after each arm once shown unchanged, so goldens
carry no compressor-dependent bytes.
"""

from __future__ import annotations

import atexit
import base64
import gzip
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import normalize
from ..core import WINDOWS, Case
from ..mutants import Mutant

DEFERRAL = "pseudolife-stdio: mode 'restore' is deferred in this candidate\n"
STUB_MARKER = "restore row: the oracle was not run for this argv\n"
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


def _bad_checksum_header() -> bytes:
    raw = bytearray(_header(b"x"))
    raw[148:156] = b"0000001\0"
    return bytes(raw)


def _deep_chain(depth: int) -> bytes:
    members = [_sym(b"l%d" % i, b"l%d" % (i + 1)) for i in range(depth)]
    return _tar(*members, _file(b"l%d" % depth))


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
    "directory-with-size": _header(b"d", b"5", size=1024) + _file(b"d/x") + END,
    # A directory skips no data: the escaping member after it is read.
    "directory-size-skips-nothing": _header(b"d", b"5", size=1024) + _file(b"../x") + END,
    "v7-directory-slash": _tar(_header(b"d/", b"\0"), _file(b"d/x")),
    "unknown-type-skips": _tar(_header(b"v", b"V", size=600) + _data(b"\x01" * 600), _file(b"w")),
    "prefix-join": _tar(_header(b"x", prefix=b"sub")),
    "octal-prefix-fields": _tar(_header(b"x", mode=b"0o_644\0\0", size_field=b"  0o1\0\0\0\0\0\0\0") + _data(b"x")),
    "signed-checksum": _signed_checksum_name(),
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
    # the oracle answers; this leaf defers
    "pax-name": _python_tar(2, "x" * 120),
    "gnu-long-name": _python_tar(1, "x" * 120),
    "negative-size": _tar(_header(b"x", size_field=b"\xff" * 12)),
    "deep-link-chain": _deep_chain(450),
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

DOCKER_SCRIPT = """\
import json, os, sys
log = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "docker-calls.jsonl")
with open(log, "a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
sys.stderr.write("restore row: the stand-in docker refuses every call\\n")
sys.exit(1)
"""


def _install_docker(arm) -> None:
    from ..test_update_transactions import _program  # noqa: PLC0415
    path = arm.home / "bin" / "docker"
    path.parent.mkdir()
    _program(path, DOCKER_SCRIPT)


# --- the stub oracle for argv the real one must never run ----------------------

_CHECKED: dict[str, object] = {}
_STUB_ROOT = Path(tempfile.gettempdir()) / f"pl-restore-stub-{os.getpid()}-{secrets.token_hex(4)}"


def _stub_dir() -> str:
    """A ``pseudolife_memory`` package whose ``cli`` prints a marker and
    exits 0, checked before first use: if PYTHONPATH did not put it ahead of
    every installed copy, the row refuses to run."""
    if "stub" in _CHECKED:
        return _CHECKED["stub"]  # type: ignore[return-value]
    root = _STUB_ROOT
    root.mkdir()
    atexit.register(shutil.rmtree, root, True)
    package = root / "pseudolife_memory"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(
        "import sys\nsys.stderr.write(%r)\nraise SystemExit(0)\n" % STUB_MARKER, encoding="utf-8")
    env = {k: os.environ[k] for k in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR") if k in os.environ}
    env.update({"PYTHONPATH": str(root), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1"})
    probe = subprocess.run([sys.executable, "-P", "-m", "pseudolife_memory.cli", "version"],
                           env=env, capture_output=True, timeout=60)
    if probe.returncode != 0 or probe.stdout or probe.stderr != _native(STUB_MARKER):
        raise RuntimeError(f"restore row: the stub oracle did not shadow the real one: {probe!r}")
    _CHECKED["stub"] = str(root)
    return str(root)


# --- named rules -----------------------------------------------------------------

def _untouched(obs: dict) -> bool:
    return obs.get("docker") == [] and obs.get("fixtures") == "unchanged"


@normalize.rule("restore-oracle-not-run")
def oracle_not_run(obs: dict) -> None:
    """Argv that passes every gate: the oracle is not run (it would take the
    update lock and drive Docker). The Python arm must show it ran the stub
    and nothing else (exit 0, the marker, no docker call, fixtures intact);
    it becomes the expectation that the candidate defers, exit 1."""
    if obs.get("arm") != "python" or not _untouched(obs):
        return
    if (obs["exit"], base64.b64decode(obs["stdout"]), base64.b64decode(obs["stderr"])) != (
            0, b"", _native(STUB_MARKER)):
        return  # left as observed: the comparison then fails
    obs["exit"] = 1
    obs["stderr"] = base64.b64encode(_native(DEFERRAL)).decode()


@normalize.rule("restore-declared-deferral")
def declared_deferral(obs: dict) -> None:
    """The oracle answers before any Docker command (exit 0 or 2) but this
    leaf defers by design (a shape or archive it does not read): the
    expectation becomes the deferral, exit 1, with nothing else changed."""
    if obs.get("arm") != "python" or obs["exit"] not in (0, 2) or not _untouched(obs):
        return
    obs["exit"] = 1
    obs["stdout"] = ""
    obs["stderr"] = base64.b64encode(_native(DEFERRAL)).decode()


@normalize.rule("restore-zlib-traceback")
def zlib_traceback(obs: dict) -> None:
    """A deflate error escapes the oracle's checks as an uncaught
    ``zlib.error`` traceback (exit 1); this leaf defers there. Only a
    traceback whose last line is that exception, with no output and nothing
    touched, becomes the deferral."""
    if obs.get("arm") != "python" or obs["exit"] != 1 or not _untouched(obs):
        return
    stderr = base64.b64decode(obs["stderr"]).decode("utf-8", "replace").splitlines()
    if base64.b64decode(obs["stdout"]) or not stderr or stderr[0] != "Traceback (most recent call last):":
        return
    if not stderr[-1].startswith("zlib.error: Error -3 while decompressing data: "):
        return
    obs["stderr"] = base64.b64encode(_native(DEFERRAL)).decode()


# --- cases -----------------------------------------------------------------------

def _case(case_id: str, argv: list[str], *, dump: str | None = "good", state: str | None = None,
          rules: tuple[str, ...] = (), columns: str = "80", oracle: bool = True,
          note: str = "") -> Case:
    docker = "{HOME}/bin/docker.exe" if WINDOWS else "{HOME}/bin/docker"
    env: dict[str, str | None] = {"PATH": "{HOME}/bin", "COLUMNS": columns, "PSEUDOLIFE_DOCKER": docker,
                                  "PSEUDOLIFE_MCP_DATA_DIR": None}
    if not oracle:
        env["PYTHONPATH"] = str(_STUB_ROOT)
    case = Case(case_id, ["restore", *argv], env=env, rules=rules, programs=("docker",), note=note)
    fixtures = {}
    if dump is not None:
        fixtures["dump.sql.gz"] = DUMPS[dump]
    if state is not None:
        fixtures["state.tgz"] = _state_bytes(state)

    def setup(arm) -> None:
        if not oracle:
            _stub_dir()
        _install_docker(arm)
        for name, data in fixtures.items():
            (arm.cwd / name).write_bytes(data)

    def after(arm, observation: dict) -> None:
        observation["arm"] = arm.name
        log = arm.home / "docker-calls.jsonl"
        observation["docker"] = ([json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
                                 if log.exists() else [])
        log.unlink(missing_ok=True)
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
    return case


def _deferred(case_id: str, argv: list[str], **kwargs) -> Case:
    return _case(case_id, argv, rules=("restore-declared-deferral",), **kwargs)


def _valid(case_id: str, argv: list[str], **kwargs) -> Case:
    return _case(case_id, argv, rules=("restore-oracle-not-run",), oracle=False, **kwargs)


DUMP = ["--backup-file", "dump.sql.gz"]
STATE = ["--state-archive", "state.tgz"]
EVERY_OPTION = ["--leave-stopped", "--container", "pg-x", "--daemon-container", "daemon-x", "--db", "bank",
                "--user", "role", "--db-port", "5433", "--daemon-url", "http://127.0.0.1:9",
                "--safety-dir", "safety"]

DUMP_PASS = ("good", "copy-closed", "copy-no-token", "copy-separator", "crlf", "lone-cr", "no-final-newline",
             "invalid-utf8", "multi-member", "trailing-zeros")
DUMP_REFUSE = ("no-marker", "in-copy", "copy-crlf", "trailing-space", "vertical-tab", "line-separator",
               "invalid-utf8-marker", "trailing-junk", "bad-crc", "truncated", "plain-sql", "empty")
STATE_PASS = ("plain", "links-inside", "link-loop", "loop-ends-walk","self-loop-hides-escape", "only-zero-blocks",
              "bad-later-header", "short-later-header", "later-header-absent", "directory-with-size",
              "v7-directory-slash", "unknown-type-skips", "prefix-join", "octal-prefix-fields",
              "signed-checksum", "multi-member-gzip", "trailing-zeros-gzip")
STATE_REFUSE = ("not-gzip", "truncated-gzip", "bad-crc-gzip", "empty-file", "empty-gzip",
                "garbage-first-header", "invalid-mode-field", "absolute-path", "dotdot-path", "dotdot-leading",
                "backslash-path", "prefix-escape", "fifo", "character-device", "block-device",
                "symlink-absolute", "symlink-backslash", "symlink-escape", "symlink-chain-escape",
                "symlink-through-link", "hardlink-escape", "transient-link", "member-data-truncated",
                "member-padding-truncated", "directory-size-skips-nothing")
STATE_DEFER = ("pax-name", "gnu-long-name", "negative-size", "deep-link-chain")


# The oracle refuses a CRC or length mismatch; this leaf defers there, as a
# corruption zlib rejects but miniz_oxide decodes ends the same way.
CRC_DEFER = ("bad-crc", "bad-crc-gzip")


def _gate_cases() -> list[Case]:
    out = []
    crc = ("restore-declared-deferral",)
    for name in DUMP_PASS + DUMP_REFUSE:
        out.append(_case(f"dump-{name}", DUMP, dump=name, rules=crc if name in CRC_DEFER else ()))
    for name in STATE_PASS + STATE_REFUSE:
        out.append(_case(f"state-{name}", DUMP + STATE, state=name, rules=crc if name in CRC_DEFER else ()))
    for name in STATE_DEFER:
        out.append(_deferred(f"defer-state-{name}", DUMP + STATE, state=name))
    return out


# Cases whose argv passes every gate: the oracle is never run for them.
ORACLE_NOT_RUN = ("valid-yes", "valid-yes-state", "valid-every-option", "valid-yes-repeated",
                  "valid-port-out-of-range", "valid-dump-trailing-slash")


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
        # the oracle answers before any effect; the candidate defers by design
        _deferred("defer-help-columns", ["--help"], dump=None, columns="100"),
        _deferred("defer-usage-columns", [], dump=None, columns="120"),
        _deferred("defer-abbreviation", ["--back", "missing.sql.gz"]),
        _deferred("defer-double-dash", DUMP + ["--"]),
        _deferred("defer-help-cluster", ["-hx"], dump=None),
        _deferred("defer-unicode-port", DUMP + ["--db-port", "١"]),
        _deferred("defer-unicode-unknown", DUMP + ["--bé"]),
        _case("defer-dump-corrupt-deflate", DUMP, dump="corrupt-deflate", rules=("restore-zlib-traceback",)),
        _case("defer-dump-distance-too-far", DUMP, dump="distance-too-far", rules=("restore-zlib-traceback",)),
        _case("defer-state-corrupt-deflate", DUMP + STATE, state="corrupt-deflate-gzip",
              rules=("restore-zlib-traceback",)),
        # argv that passes every gate: the candidate defers; the oracle is never run
        _valid("valid-yes", DUMP + ["--yes"]),
        _valid("valid-yes-state", DUMP + STATE + ["--yes"], state="links-inside"),
        _valid("valid-every-option", DUMP + STATE + EVERY_OPTION + ["--yes"], state="plain"),
        _valid("valid-yes-repeated", ["--backup-file", "missing", "--yes", "--backup-file=dump.sql.gz", "--yes"]),
        _valid("valid-port-out-of-range", DUMP + ["--yes", "--db-port", "99999999999999999999999999999999999999999"]),
        _valid("valid-dump-trailing-slash", ["--backup-file", "dump.sql.gz/", "--yes"]),
    ]


FILE = DIR + "/mod.rs"
ARGS = DIR + "/args.rs"
DUMPRS = DIR + "/dump.rs"
GZIPRS = DIR + "/gzip.rs"
STATERS = DIR + "/state.rs"

MUTANTS = [
    Mutant("restore-refusal-exit", "restore", FILE, "Some(ExitCode::from(2))\n}", "Some(ExitCode::from(1))\n}",
           ("no-yes",)),
    Mutant("restore-yes-ignored", "restore", FILE, "    if !args.yes {", "    if true {", ("valid-yes",)),
    Mutant("restore-state-skipped", "restore", FILE, "if let Some(archive) = &args.state_archive {",
           "if let Some(archive) = None::<&std::path::PathBuf> {", ("state-symlink-escape",)),
    Mutant("restore-dump-after-state", "restore", FILE, "        Verdict::Refuse => return refuse(INCOMPLETE_DUMP),",
           "        Verdict::Refuse if args.state_archive.is_none() => return refuse(INCOMPLETE_DUMP),\n"
           "        Verdict::Refuse => return refuse(UNSAFE_STATE),", ("dump-before-state",)),
    Mutant("restore-required-before-type", "restore", ARGS, '"--db-port" => match python_int(value)? {',
           '"--db-port" if !backup_file => return error("the following arguments are required: --backup-file".into()),\n'
           '            "--db-port" => match python_int(value)? {', ("port-before-unknown",)),
    Mutant("restore-negative-as-option", "restore", ARGS, "        if negative {\n            return Some(Class::Value);",
           "        if false {\n            return Some(Class::Value);", ("port-negative",)),
    Mutant("restore-help-any-width", "restore", ARGS, "backup_args::width() == 78", "backup_args::width() > 0",
           ("defer-help-columns",)),
    Mutant("restore-copy-loose", "restore", DUMPRS, "    token > 0\n", "    token != usize::MAX\n", ("dump-copy-no-token",)),
    Mutant("restore-lone-cr", "restore", DUMPRS, "                b'\\r' => {\n                    self.end_line();",
           "                b'\\r' => {\n                    self.line.push(byte);", ("dump-lone-cr",)),
    Mutant("restore-gzip-padding", "restore", GZIPRS, "                Some(0) => {}\n", "", ("dump-trailing-zeros",)),
    Mutant("restore-gzip-crc", "restore", GZIPRS, "stored_crc != crc.sum()", "stored_crc == crc.sum().wrapping_add(1)", ("dump-bad-crc",)),
    Mutant("restore-crc-answered", "restore", GZIPRS,
           "            return Err(Stop::Unsure);\n        }\n        // Zero padding",
           "            return Err(Stop::Refuse);\n        }\n        // Zero padding", ("defer-dump-distance-too-far",)),
    Mutant("restore-zlib-answered", "restore", GZIPRS, ".map_err(|_| Stop::Unsure)?;", ".map_err(|_| Stop::Refuse)?;",
           ("defer-dump-corrupt-deflate",)),
    Mutant("restore-later-header-error", "restore", STATERS,
           "            Header::Invalid => {\n                self.end = if self.next == 0 {",
           "            Header::Invalid => {\n                self.end = if true {", ("state-bad-later-header",)),
    Mutant("restore-directory-skips", "restore", STATERS, "let skips = !matches!(kind, b'1' | b'2' | b'3' | b'4' | b'5' | b'6');",
           "let skips = !matches!(kind, b'1' | b'2' | b'3' | b'4' | b'6');", ("state-directory-size-skips-nothing",)),
    Mutant("restore-padding-unchecked", "restore", STATERS, "            debug_assert!(self.skipped);\n            self.end = End::Error;",
           "            self.end = End::Done;", ("state-member-padding-truncated",)),
    Mutant("restore-no-transient-pass", "restore", STATERS, "        links.map.clear();\n        for member in members {",
           "        links.map.clear();\n        for member in &members[..0] {", ("state-transient-link",)),
    Mutant("restore-fifo-allowed", "restore", STATERS, "matches!(self.kind, b'3' | b'4' | b'6')",
           "matches!(self.kind, b'3' | b'4')", ("state-fifo",)),
    Mutant("restore-loop-continues", "restore", STATERS, "                None => return Ok(None),", "                None => {}",
           ("state-loop-ends-walk",)),
    Mutant("restore-prefix-ignored", "restore", STATERS, "    if !prefix.is_empty() {", "    if false {",
           ("state-prefix-escape",)),
    Mutant("restore-signed-checksum", "restore", STATERS, "stored != unsigned && stored != signed",
           "stored != unsigned && stored != signed + 1",
           ("state-signed-checksum",)),
    Mutant("restore-octal-prefix", "restore", STATERS, "    if prefixed {", "    if prefixed && false {",
           ("state-octal-prefix-fields",)),
]


def _assert_safe() -> None:
    """Every case that runs the real oracle must stop at a gate: only the
    ORACLE_NOT_RUN cases pass them, and only they use the stub."""
    for case in cases():
        if case.id in ORACLE_NOT_RUN:
            assert "restore-oracle-not-run" in case.rules, case.id
        else:
            assert "restore-oracle-not-run" not in case.rules, case.id
            assert "--yes" not in case.argv or any(
                case.id.startswith(prefix) for prefix in ("help", "missing", "port-", "flag-", "defer-")), case.id


_assert_safe()

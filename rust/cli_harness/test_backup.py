"""Backup observation rules keep durable-state and error differences visible."""
import base64
import copy
import errno

import pytest

from . import compare
from .rows.backup import discovery_traceback


def observation(stderr, **changes):
    result = {"exit": 1, "stdout": "", "stderr": base64.b64encode(stderr).decode(),
              "files": {"bank/backups": "dir"}}
    result.update(changes)
    return result


@pytest.mark.parametrize("class_name,code,message", [
    ("PermissionError", 13, "Permission denied"),
    ("TimeoutError", 110, "Connection timed out"),
    ("InterruptedError", 4, "Interrupted system call"),
    ("BlockingIOError", errno.EAGAIN, "unavailable"),
    ("ChildProcessError", errno.ECHILD, "no child"),
    ("BrokenPipeError", errno.EPIPE, "broken pipe"),
    ("ConnectionAbortedError", errno.ECONNABORTED, "aborted"),
    ("ConnectionRefusedError", errno.ECONNREFUSED, "refused"),
    ("ConnectionResetError", errno.ECONNRESET, "reset"),
    ("FileExistsError", errno.EEXIST, "exists"),
    ("FileNotFoundError", errno.ENOENT, "absent"),
    ("IsADirectoryError", errno.EISDIR, "directory"),
    ("NotADirectoryError", errno.ENOTDIR, "not directory"),
    ("ProcessLookupError", errno.ESRCH, "no process"),
])
def test_discovery_traceback_only_removes_interpreter_frames(class_name, code, message):
    final = f"{class_name}: [Errno {code}] {message}: '/fixture/.pg0/installation'\n".encode()
    traceback = (b'Traceback (most recent call last):\n'
                 b'  File "backup_cli.py", line 106, in _find_pg_dump\n'
                 b'    for vdir in sorted(install_root.iterdir(), reverse=True):\n'
                 b'                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n')
    want = observation(traceback + final)
    got = observation(final)
    rules = ("backup-discovery-traceback",)
    assert not compare.diff(want, got, rules)
    for old, new in [(class_name.encode(), b"OSError"), (str(code).encode(), b"12"),
                     (message.encode(), b"different message"),
                     (b"installation", b"other")]:
        assert compare.diff(want, observation(final.replace(old, new)), rules)
    assert compare.diff(want, observation(final, exit=2), rules)
    extra = copy.deepcopy(got)
    extra["files"]["bank/backups/partial"] = "file:"
    assert compare.diff(want, extra, rules)
    for stderr in [b"warning\n" + traceback + final, traceback + final + b"extra\n",
                   traceback + b"RuntimeError: bad\n"]:
        obs = observation(stderr)
        before = copy.deepcopy(obs)
        discovery_traceback(obs)
        assert obs == before


def _native_backup():
    import os
    from pathlib import Path
    from .runner import _default_candidate
    import pytest
    requested = os.environ.get("PSEUDOLIFE_RUST_BINARY")
    candidate = Path(requested) if requested else _default_candidate()
    if not candidate.is_file():
        if os.environ.get("PSEUDOLIFE_BACKUP_NATIVE_REQUIRED") == "1":
            pytest.fail("required backup native candidate is missing")
        pytest.skip("build the CLI harness candidate before native backup proofs")
    return str(candidate)


def _nss_compiler():
    import os
    import shutil
    import sys
    if sys.platform != "linux":
        pytest.skip("Linux NSS interposition proof")
    compiler = shutil.which("cc")
    if not compiler:
        if os.environ.get("PSEUDOLIFE_BACKUP_NATIVE_REQUIRED") == "1":
            pytest.fail("required Linux NSS compiler is missing")
        pytest.skip("Linux NSS interposition proof requires a C compiler")
    return compiler


def test_native_proofs_use_the_explicit_candidate(tmp_path, monkeypatch):
    candidate = tmp_path / "built candidate.exe"
    candidate.write_bytes(b"fixture")
    monkeypatch.setenv("PSEUDOLIFE_RUST_BINARY", str(candidate))
    monkeypatch.setenv("PSEUDOLIFE_BACKUP_NATIVE_REQUIRED", "1")
    assert _native_backup() == str(candidate)


def test_required_native_proofs_cannot_skip_a_missing_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_RUST_BINARY", str(tmp_path / "missing"))
    monkeypatch.setenv("PSEUDOLIFE_BACKUP_NATIVE_REQUIRED", "1")
    with pytest.raises(pytest.fail.Exception, match="required backup native candidate"):
        _native_backup()


def test_required_linux_nss_proofs_cannot_skip_a_missing_compiler(monkeypatch):
    import shutil
    import sys
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setenv("PSEUDOLIFE_BACKUP_NATIVE_REQUIRED", "1")
    with pytest.raises(pytest.fail.Exception, match="required Linux NSS compiler"):
        _nss_compiler()


def test_help_uses_the_terminal_when_columns_is_unusable():
    import errno
    import os
    import struct
    import subprocess
    import sys
    import pytest
    if sys.platform != "linux":
        pytest.skip("POSIX PTY proof")
    import fcntl
    import pty
    import termios
    native = _native_backup()

    def help_output(command, columns):
        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
            env = dict(os.environ)
            env.pop("COLUMNS", None)
            if columns is not None:
                env["COLUMNS"] = columns
            result = subprocess.run([*command, "backup", "-h"], env=env,
                                    stdout=slave, stderr=subprocess.PIPE, timeout=10)
            os.close(slave)
            slave = None
            output = bytearray()
            while True:
                try:
                    chunk = os.read(master, 4096)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                output.extend(chunk)
            return result.returncode, bytes(output), result.stderr
        finally:
            os.close(master)
            if slave is not None:
                os.close(slave)

    # Explicit widths also cover the cached 80-column help and wider wrapping.
    # The expected help is the frozen Python CLI's, recorded through the same
    # 100-column pty (fixtures/backup-help.linux.json), since the freeze.
    import json
    from pathlib import Path
    recorded = json.loads((Path(__file__).resolve().parent / "fixtures" / "backup-help.linux.json")
                          .read_text(encoding="utf-8"))["cases"]
    for columns in [None, "invalid", "0", "-1", "80", "120"]:
        want = recorded["unset" if columns is None else columns]
        code, stdout, stderr = help_output([native], columns)
        assert (code, stdout.decode("utf-8"), stderr.decode("utf-8")) == (
            want["exit"], want["stdout"], want["stderr"]), columns


@pytest.mark.parametrize("option", [
    "--container", "--daemon-container", "--db", "--user", "--db-port",
    "--c", "--daemon", "--u", "--db-p",
    # These became ambiguous when the Docker selectors were added.
    "--d", "--da",
])
@pytest.mark.parametrize("inline", [False, True])
def test_docker_selectors_defer_before_bank_effects(tmp_path, option, inline):
    import os
    import subprocess
    bank = tmp_path / "bank"
    bank.mkdir()
    config = bank / "config.yaml"
    config.write_bytes(b"storage: files\n")
    out = tmp_path / "backups"
    selector = [f"{option}=5432"] if inline else [option, "5432"]
    result = subprocess.run([_native_backup(), "backup", "--data-dir", str(bank),
                             "--out", str(out), *selector],
                            capture_output=True, timeout=10)
    assert (result.returncode, result.stdout, result.stderr) == (
        1, b"", ("pseudolife-stdio: mode 'backup' is deferred in this candidate"
                  + os.linesep).encode(),
    )
    assert not out.exists()
    assert list(bank.iterdir()) == [config]
    assert config.read_bytes() == b"storage: files\n"


# What the frozen Python CLI prints for an unrecognised backup option at 80
# columns, recorded on Windows and Linux on 14 November 2026 (only the line
# separator differs). A deliberate change to the native backup help edits
# this and fixtures/backup-help.linux.json by hand; from then on they
# describe the native CLI.
BACKUP_USAGE = ("usage: pseudolife-mcp backup [-h] [--data-dir DATA_DIR] [--out OUT]\n"
                "                             [--keep-days KEEP_DAYS] [--container CONTAINER]\n"
                "                             [--daemon-container DAEMON_CONTAINER] [--db DB]\n"
                "                             [--user USER] [--db-port DB_PORT]\n")


@pytest.mark.parametrize("arguments, unrecognized", [
    (["--containerized", "pg"], "--containerized pg"), (["--db-port-extra=5432"], "--db-port-extra=5432"),
    (["--unknown"], "--unknown"), (["--", "--container", "pg"], "-- --container pg"),
    (["--out=--container", "--unknown"], "--unknown"),
])
def test_unknown_options_keep_python_errors(arguments, unrecognized):
    import os
    import subprocess
    env = dict(os.environ, COLUMNS="80")
    actual = subprocess.run([_native_backup(), "backup", *arguments], env=env,
                            capture_output=True, timeout=10)
    expected = BACKUP_USAGE + f"pseudolife-mcp backup: error: unrecognized arguments: {unrecognized}\n"
    assert (actual.returncode, actual.stdout, actual.stderr) == (
        2, b"", expected.replace("\n", os.linesep).encode())


def test_nss_errors_keep_numeric_ownership_and_empty_names(tmp_path):
    import os
    import subprocess
    import tarfile
    compiler = _nss_compiler()
    native = _native_backup()
    source = tmp_path / "nss.c"
    source.write_text('''#include <pwd.h>
#include <grp.h>
#include <errno.h>
int getpwuid_r(uid_t id, struct passwd *p, char *b, size_t n, struct passwd **r) { *r=0; return EIO; }
int getgrgid_r(gid_t id, struct group *p, char *b, size_t n, struct group **r) { *r=0; return EIO; }
struct passwd *getpwuid(uid_t id) { errno=EIO; return 0; }
struct group *getgrgid(gid_t id) { errno=EIO; return 0; }
''', encoding="utf-8")
    library = tmp_path / "nss.so"
    subprocess.run([compiler, "-shared", "-fPIC", str(source), "-o", str(library)], check=True)
    bank = tmp_path / "bank"
    bank.mkdir()
    (bank / "state").write_bytes(b"state")
    env = {key: value for key, value in os.environ.items() if "DATABASE_URL" not in key}
    env.update(LD_PRELOAD=str(library), PSEUDOLIFE_MCP_DATA_DIR=str(bank))
    # The native CLI only, since the Python freeze: the oracle ran the frozen
    # Python backup under the same assertions.
    for label, command in [("native", [native])]:
        out = tmp_path / label
        result = subprocess.run([*command, "backup", "--data-dir", str(bank), "--out", str(out)],
                                env=env, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        with tarfile.open(next(out.glob("*.tar.gz"))) as archive:
            member = archive.getmember("state")
            assert (member.uid, member.gid) == (os.getuid(), os.getgid())
            assert (member.uname, member.gname) == ("", "")
            assert archive.extractfile(member).read() == b"state"


def test_non_utf8_nss_names_roundtrip_through_binary_pax(tmp_path):
    import os
    import subprocess
    import tarfile
    compiler = _nss_compiler()
    native = _native_backup()
    source = tmp_path / "nss-raw.c"
    source.write_text('''#include <pwd.h>
#include <grp.h>
#include <errno.h>
#include <string.h>
static char user[] = {'u','s','e','r','-',(char)0xff,0};
static char group[] = {'g','r','o','u','p','-',(char)0xfe,0};
static char *members[] = {0};
int getpwuid_r(uid_t id, struct passwd *p, char *b, size_t n, struct passwd **r) {
    if (n < sizeof user) { *r=0; return ERANGE; }
    memset(p,0,sizeof *p); memcpy(b,user,sizeof user); p->pw_name=b; p->pw_uid=id; *r=p; return 0;
}
int getgrgid_r(gid_t id, struct group *p, char *b, size_t n, struct group **r) {
    if (n < sizeof group) { *r=0; return ERANGE; }
    memset(p,0,sizeof *p); memcpy(b,group,sizeof group); p->gr_name=b; p->gr_gid=id; p->gr_mem=members; *r=p; return 0;
}
struct passwd *getpwuid(uid_t id) { static struct passwd p; memset(&p,0,sizeof p); p.pw_name=user; p.pw_uid=id; return &p; }
struct group *getgrgid(gid_t id) { static struct group p; memset(&p,0,sizeof p); p.gr_name=group; p.gr_gid=id; p.gr_mem=members; return &p; }
''', encoding="utf-8")
    library = tmp_path / "nss-raw.so"
    subprocess.run([compiler, "-shared", "-fPIC", str(source), "-o", str(library)], check=True)
    bank = tmp_path / "bank"
    bank.mkdir()
    (bank / "state").write_bytes(b"state")
    raw_name = os.fsdecode(b"state-\xff")
    (bank / raw_name).write_bytes(b"raw state")
    os.symlink(b"state-\xff", os.fsencode(bank) + b"/link-\xfe")
    env = {key: value for key, value in os.environ.items() if "DATABASE_URL" not in key}
    env.update(LD_PRELOAD=str(library), PSEUDOLIFE_MCP_DATA_DIR=str(bank))
    # The native CLI only, since the Python freeze: the oracle ran the frozen
    # Python backup under the same assertions.
    for label, command in [("native", [native])]:
        out = tmp_path / label
        result = subprocess.run([*command, "backup", "--data-dir", str(bank), "--out", str(out)],
                                env=env, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        with tarfile.open(next(out.glob("*.tar.gz"))) as archive:
            for member in archive.getmembers():
                assert (member.uid, member.gid) == (os.getuid(), os.getgid())
                assert member.uname.encode("utf-8", "surrogateescape") == b"user-\xff"
                assert member.gname.encode("utf-8", "surrogateescape") == b"group-\xfe"
                assert member.pax_headers["hdrcharset"] == "BINARY"
            assert archive.extractfile("state").read() == b"state"
            assert archive.extractfile(raw_name).read() == b"raw state"
            link = archive.getmember(os.fsdecode(b"link-\xfe"))
            assert link.issym()
            assert link.linkname.encode("utf-8", "surrogateescape") == b"state-\xff"

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
    from .runner import _default_candidate
    import pytest
    candidate = _default_candidate()
    if not candidate.is_file():
        pytest.skip("build the CLI harness candidate before native backup proofs")
    return str(candidate)


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

    for columns in [None, "invalid", "0", "-1"]:
        want = help_output([sys.executable, "-m", "pseudolife_memory.cli"], columns)
        assert want[0] == 0
        assert help_output([native], columns) == want


def test_nss_errors_keep_numeric_ownership_and_empty_names(tmp_path):
    import os
    import shutil
    import subprocess
    import sys
    import tarfile
    import pytest
    if sys.platform != "linux" or not shutil.which("cc"):
        pytest.skip("Linux NSS interposition proof requires a C compiler")
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
    subprocess.run(["cc", "-shared", "-fPIC", str(source), "-o", str(library)], check=True)
    bank = tmp_path / "bank"
    bank.mkdir()
    (bank / "state").write_bytes(b"state")
    env = {key: value for key, value in os.environ.items() if "DATABASE_URL" not in key}
    env.update(LD_PRELOAD=str(library), PSEUDOLIFE_MCP_DATA_DIR=str(bank))
    for label, command in [("oracle", [sys.executable, "-m", "pseudolife_memory.cli"]),
                           ("native", [native])]:
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
    import shutil
    import subprocess
    import sys
    import tarfile
    import pytest
    if sys.platform != "linux" or not shutil.which("cc"):
        pytest.skip("Linux NSS interposition proof requires a C compiler")
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
    subprocess.run(["cc", "-shared", "-fPIC", str(source), "-o", str(library)], check=True)
    bank = tmp_path / "bank"
    bank.mkdir()
    (bank / "state").write_bytes(b"state")
    raw_name = os.fsdecode(b"state-\xff")
    (bank / raw_name).write_bytes(b"raw state")
    os.symlink(b"state-\xff", os.fsencode(bank) + b"/link-\xfe")
    env = {key: value for key, value in os.environ.items() if "DATABASE_URL" not in key}
    env.update(LD_PRELOAD=str(library), PSEUDOLIFE_MCP_DATA_DIR=str(bank))
    for label, command in [("oracle", [sys.executable, "-m", "pseudolife_memory.cli"]),
                           ("native", [native])]:
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

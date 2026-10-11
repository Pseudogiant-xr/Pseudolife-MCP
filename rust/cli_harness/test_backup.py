"""Backup observation rules keep durable-state and error differences visible."""
import base64
import copy

from . import compare
from .rows.backup import discovery_traceback


def observation(stderr, **changes):
    result = {"exit": 1, "stdout": "", "stderr": base64.b64encode(stderr).decode(),
              "files": {"bank/backups": "dir"}}
    result.update(changes)
    return result


def test_discovery_traceback_only_removes_interpreter_frames():
    final = b"PermissionError: [Errno 13] Permission denied: '/fixture/.pg0/installation'\n"
    traceback = (b'Traceback (most recent call last):\n'
                 b'  File "backup_cli.py", line 106, in _find_pg_dump\n'
                 b'    for vdir in sorted(install_root.iterdir(), reverse=True):\n'
                 b'                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n')
    want = observation(traceback + final)
    got = observation(final)
    rules = ("backup-discovery-traceback",)
    assert not compare.diff(want, got, rules)
    for old, new in [(b"PermissionError", b"OSError"), (b"13", b"12"),
                     (b"Permission denied", b"different message"),
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

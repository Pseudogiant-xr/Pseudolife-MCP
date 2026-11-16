"""The plugin hooks and the native client apply one rule to private files
behind a link (#1639).

The hooks' ``private_regular`` keeps its own checks of the file itself, and
when a folder above it is a symlink it asks the native client
(``pseudolife-mcp private-path-check``, ``rust/shim/src/private_path.rs``)
instead of carrying a second copy of the link rule. One fixture table runs
through both: every row must give the same verdict from the client and from
each hook's function, and the verdict the table expects.

POSIX only (Linux; macOS: not yet run), with the real client named by
``PSEUDOLIFE_NATIVE_TEST_BINARY``. ``PSEUDOLIFE_TEST_BASH`` names another
bash to run the hooks with (macOS ships bash 3.2). Windows reparse points
are covered by the client's own tests.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import native_client_stub

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixtures")

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ["stop-wake.sh", "coordination-start.sh", "session-start.sh",
         "session-end.sh", "subagent-board.sh"]
MARKER = "pseudolife-private-path: trusted"
REAL = native_client_stub.real_binary()
BASH = os.environ.get("PSEUDOLIFE_TEST_BASH") or shutil.which("bash") or "bash"


def _function(text: str, name: str) -> str:
    match = re.search(rf"(?ms)^{name}\(\) \{{.*?^\}}", text)
    assert match, name
    return match[0]


def _hook_functions(hook: str) -> str:
    text = (ROOT / "plugin" / "hooks" / hook).read_text(encoding="utf-8")
    return _function(text, "private_path_trusted") + "\n" + _function(text, "private_regular")


def _dir(folder: Path) -> Path:
    """A folder only this user may write, whatever the umask (002 under
    Debian's user private groups would make it group-writable)."""
    folder.mkdir(parents=True, exist_ok=True)
    for made in [folder, *folder.parents]:
        if made == made.parent or not str(made).startswith(str(_dir.base)):
            break
        made.chmod(0o700)
    return folder


def _token(folder: Path) -> Path:
    _dir(folder)
    token = folder / "token"
    token.write_text("fixture-token")
    token.chmod(0o600)
    return token


# Each row builds its layout under a disposable folder and returns the
# token file's path as a client or hook would be given it.
def _plain(base: Path) -> Path:
    return _token(base / "home" / ".pseudolife-mcp")


def _own_link_home(base: Path) -> Path:
    _token(base / "real-home" / ".pseudolife-mcp")
    (base / "home").symlink_to(base / "real-home")
    return base / "home" / ".pseudolife-mcp" / "token"


def _relative_dotdot(base: Path) -> Path:
    _token(base / "real")
    _dir(base / "a")
    (base / "a" / "up").symlink_to("../real")
    return base / "a" / "up" / "token"


def _own_link_in_sticky(base: Path) -> Path:
    _token(base / "real")
    _dir(base / "tmp")
    (base / "tmp" / "link").symlink_to(base / "real")
    (base / "tmp").chmod(0o1777)
    return base / "tmp" / "link" / "token"


def _link_in_writable_folder(base: Path) -> Path:
    _token(base / "real")
    _dir(base / "shared")
    (base / "shared" / "link").symlink_to(base / "real")
    (base / "shared").chmod(0o777)
    return base / "shared" / "link" / "token"


def _link_in_group_writable_folder(base: Path) -> Path:
    _token(base / "real")
    _dir(base / "shared")
    (base / "shared" / "link").symlink_to(base / "real")
    (base / "shared").chmod(0o775)
    return base / "shared" / "link" / "token"


def _trusted_link_into_untrusted(base: Path) -> Path:
    _token(base / "real")
    _dir(base / "shared")
    (base / "shared" / "inner").symlink_to(base / "real")
    (base / "shared").chmod(0o777)
    (base / "outer").symlink_to(base / "shared" / "inner")
    return base / "outer" / "token"


TABLE = [
    ("plain folders", _plain, True),
    ("a home behind the user's own link", _own_link_home, True),
    ("a relative link through ..", _relative_dotdot, True),
    ("the user's link in a sticky folder", _own_link_in_sticky, True),
    ("a link in a folder others can write", _link_in_writable_folder, False),
    ("a link in a group-writable folder", _link_in_group_writable_folder, False),
    ("a trusted link into an untrusted one", _trusted_link_into_untrusted, False),
]


@pytest.fixture
def base(tmp_path):
    base = tmp_path / "layout"
    base.mkdir()
    base.chmod(0o700)
    _dir.base = base
    yield base
    # Restore modes so the temporary tree can be removed.
    for folder in [base, *base.rglob("*")]:
        if folder.is_dir() and not folder.is_symlink():
            folder.chmod(0o700)


REFUSED = "pseudolife-private-path: refused: "


def _client(file: Path) -> bool:
    result = subprocess.run([str(REAL), "private-path-check", str(file)],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=60)
    trusted = result.returncode == 0 and result.stdout.strip() == MARKER
    assert trusted or (result.returncode == 1 and result.stdout.startswith(REFUSED)
                       and "fix:" in result.stdout), result
    return trusted


def _hook(hook: str, file: Path, home: Path, launcher: str) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
           "PSEUDOLIFE_SHIM_LAUNCHER": launcher, "PSEUDOLIFE_TEST_FILE": str(file)}
    return subprocess.run(
        [BASH, "-c", _hook_functions(hook) + '\nprivate_regular "$PSEUDOLIFE_TEST_FILE" 4096'],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(REAL is None, reason="set PSEUDOLIFE_NATIVE_TEST_BINARY to a built native client")
@pytest.mark.parametrize("name, build, expected", TABLE, ids=[row[0] for row in TABLE])
@pytest.mark.parametrize("hook", HOOKS)
def test_the_hooks_and_the_client_give_one_verdict(base, tmp_path, name, build, expected, hook):
    file = build(base)
    assert _client(file) is expected, name
    result = _hook(hook, file, tmp_path, str(REAL))
    assert (result.returncode == 0) is expected, (name, result.stderr)
    if not expected:
        # The hook relays the client's own line: the link, the cause, the fix.
        assert "fix:" in result.stderr and "no native" not in result.stderr, result.stderr


@pytest.mark.parametrize("hook", HOOKS)
def test_plain_folders_never_ask_the_client(base, tmp_path, hook):
    """The common case costs no spawn: with no link above the file, the hook
    decides alone, even when no client is installed."""
    file = _plain(base)
    result = _hook(hook, file, tmp_path, str(tmp_path / "no-such-client"))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("hook", HOOKS)
def test_without_a_client_a_linked_folder_stays_refused_and_says_why(base, tmp_path, hook):
    file = _own_link_home(base)
    result = _hook(hook, file, tmp_path, str(tmp_path / "no-such-client"))
    assert result.returncode == 1
    assert f"not reading {file}" in result.stderr and "fix:" in result.stderr, result.stderr


@pytest.mark.parametrize("hook", HOOKS)
def test_only_the_clients_exact_marker_counts(base, tmp_path, hook):
    """An older or non-native client answers an unknown command some other
    way; even exit 0 without the marker leaves the file refused."""
    file = _own_link_home(base)
    impostor = tmp_path / "impostor"
    impostor.write_text("#!/bin/sh\necho ok\nexit 0\n")
    impostor.chmod(0o700)
    assert _hook(hook, file, tmp_path, str(impostor)).returncode == 1


def test_every_hook_carries_the_same_functions():
    expected = _hook_functions("stop-wake.sh")
    # stop-wake.sh alone notes the Windows timing measurement.
    expected = re.sub(r"(?m)^        # Three Windows runs.*\n^        # 0\.344.*\n", "", expected)
    for hook in HOOKS[1:]:
        got = re.sub(r"(?m)^        # Three Windows runs.*\n^        # 0\.344.*\n", "",
                     _hook_functions(hook))
        assert got == expected, hook

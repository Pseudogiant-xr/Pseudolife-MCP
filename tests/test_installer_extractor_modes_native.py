"""The native installer (ops/install-native.*): the tests of test_installer_extractor_modes.py
that the native client path changed, run against the native installer.

test_installer_extractor_modes.py keeps the default (Python client) installer's tests.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest

from tests.user_path_guard import user_path_unchanged

from rust.ci import release_version
from tests import native_client_stub as native_stub
from tests.test_installer_client_only_native import BASH, ROOT, _Shell

def _block(name: str) -> str:
    text = (ROOT / "ops" / "install-native.sh").read_text(encoding="utf-8")
    assert f"# >>> {name} >>>\n" in text, f"install-native.sh has no '{name}' marker block"
    return text.split(f"# >>> {name} >>>\n", 1)[1].split(f"# <<< {name} <<<", 1)[0]


def _release_env(shell: _Shell) -> dict[str, str]:
    """A local fake release of this checkout's version: the whole installer
    stages the native client before it reads the client-only flags."""
    version = release_version.read(ROOT)
    env = native_stub.write_release(shell.tmp / "release", version, "exit 0\n")
    env["PSEUDOLIFE_NATIVE_ARCHIVE_URL"] = shell.path(shell.tmp / "release")
    return env


@BASH
def test_client_only_refuses_an_extractor_url(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    with user_path_unchanged(shell.home):
        proc = shell.run(f"exec '{shell.path(ROOT / 'ops' / 'install-native.sh')}' "
                         "--daemon-url http://100.64.0.2:8765 "
                         "--extractor-url http://pl.example.invalid:1234/v1 --no-art\n",
                         extra_env=_release_env(shell))
    assert proc.returncode == 2
    # The client-only refusal itself, not an unknown-flag usage exit.
    [line] = [line for line in proc.stderr.splitlines() if "client-only install" in line]
    assert "--extractor-url" in line
    assert shell.logged() == []


def _markers() -> str:
    text = (ROOT / "ops" / "install-native.sh").read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines()
                     if re.match(r"^(LEGACY_(SHIM_)?)?OVERRIDE_MARKER=", line)) + "\n"


def _marker_texts() -> list[str]:
    return [line.split("=", 1)[1].strip('"') for line in _markers().splitlines()]


def _installer_state() -> str:
    """The recorded-choices block's installer_state, which the sidecar
    override writes its file through."""
    text = (ROOT / "ops" / "install-native.sh").read_text(encoding="utf-8")
    [line] = [line for line in text.splitlines() if line.startswith("installer_state() {")]
    return line + "\n"


def _override(shell: _Shell, mode: str, first_line: str):
    override = shell.tmp / "docker-compose.override.yml"
    override.write_text(first_line + "\nservices:\n  mine: {}\n", encoding="utf-8")
    proc = shell.run(
        f"EXTRACTOR='{mode}'\nMODEL=''\nEXTRACTOR_URL=''\nCLIENT_ONLY=''\n"
        f"override_file='{shell.path(override)}'\n" + _markers() + _installer_state()
        + _block("extractor modes") + _block("sidecar override"))
    return proc, override


@pytest.mark.parametrize("marker", range(3))
@BASH
def test_every_installer_marker_is_owned(bash, tmp_path, marker):
    """A file headed by the current marker or either earlier one is the
    installer's: a sidecar mode removes it, a single-extractor mode rewrites
    it under the current marker."""
    texts = _marker_texts()
    assert len(texts) == 3
    proc, override = _override(_Shell(bash, tmp_path), "sidecar", texts[marker])
    assert proc.returncode == 0, proc.stderr
    assert not override.exists()
    again = tmp_path / "again"
    again.mkdir()
    proc, override = _override(_Shell(bash, again), "claude-only", texts[marker])
    assert proc.returncode == 0, proc.stderr
    lines = override.read_text(encoding="utf-8").splitlines()
    assert lines[0] == texts[0] and 'profiles: ["disabled"]' in lines[-1]


@pytest.mark.parametrize("mode", ["sidecar", "claude-only"])
@BASH
def test_an_operators_own_override_is_never_touched(bash, tmp_path, mode):
    proc, override = _override(_Shell(bash, tmp_path), mode, "# my own compose override")
    assert proc.returncode == 0, proc.stderr
    assert override.read_text(encoding="utf-8").startswith("# my own compose override\n")
    if mode == "claude-only":
        assert "not installer-managed" in proc.stdout

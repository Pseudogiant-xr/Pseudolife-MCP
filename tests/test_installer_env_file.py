"""Executable contracts for how ``ops/install.sh`` and ``ops/update.sh`` read
``ops/.env``.

An ``ops/.env`` copied from a Windows host arrives with CRLF line endings.
Docker Compose tolerates that; the installer's own reads did not: a volume
name read with ``sed`` carried a trailing CR and ``docker volume create``
refused ``"pseudolife-mcp-bank-pg18\\r"`` as an invalid name (Debian 13,
2026-09-29). Both scripts now rewrite such a file with LF endings before
reading it, after keeping a copy of the original beside it, and say so;
a file already on LF is not touched. Blocks run extracted from the scripts
with stub CLIs in a disposable home.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

from tests.test_installer_client_only import BASH, ROOT, _Shell

CRLF = b"PSEUDOLIFE_BANK_VOLUME=custom-bank\r\nPSEUDOLIFE_STATE_VOLUME=custom-state\r\n"
LF = CRLF.replace(b"\r\n", b"\n")
# Git Bash (MSYS) reads a CRLF file as LF: sed, grep and tr never see the CR,
# so on Windows there is nothing to detect and nothing to reproduce. The
# CRLF cases run on Linux and macOS (CI's test and lite jobs, the dogfood
# box); the LF, missing-file and text cases run everywhere.
CRLF_VISIBLE = pytest.mark.skipif(
    os.name == "nt", reason="Git Bash reads CRLF files as LF (MSYS text mode)")


def _block(script: str, name: str) -> str:
    text = (ROOT / "ops" / script).read_text(encoding="utf-8")
    assert f"# >>> {name} >>>\n" in text, f"{script} has no '{name}' marker block"
    return text.split(f"# >>> {name} >>>\n", 1)[1].split(f"# <<< {name} <<<", 1)[0]


def _env_file(tmp_path: Path, content: bytes) -> Path:
    env = tmp_path / "ops-env" / ".env"
    env.parent.mkdir()
    env.write_bytes(content)
    env.chmod(0o600)
    return env


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _install(shell: _Shell, env: Path, *blocks: str, client_only: str = ""):
    return shell.run(f"env_file='{shell.path(env)}'\nCLIENT_ONLY='{client_only}'\n"
                     + "".join(_block("install.sh", b) for b in blocks))


def _raw_calls(shell: _Shell) -> bytes:
    """The stub call log as bytes: ``str.splitlines`` splits on a CR too, so
    a text read would hide exactly the byte these tests are about."""
    return shell.calls.read_bytes() if shell.calls.exists() else b""


@CRLF_VISIBLE
@BASH
def test_install_rewrites_a_crlf_env_file_before_reading_volume_names(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    env = _env_file(tmp_path, CRLF)
    proc = _install(shell, env, "env line endings", "volumes")
    assert proc.returncode == 0, proc.stderr
    assert env.read_bytes() == LF
    [backup] = list(env.parent.glob(".env.crlf-*"))
    assert backup.read_bytes() == CRLF
    if os.name != "nt":
        assert _mode(env) == 0o600 and _mode(backup) == 0o600
    [note] = [line for line in proc.stdout.splitlines() if "LF line endings" in line]
    assert note.startswith("STEP: ") and backup.name in note
    assert b"docker|volume create custom-bank\n" in _raw_calls(shell)
    assert b"docker|volume create custom-state\n" in _raw_calls(shell)
    assert b"\r" not in _raw_calls(shell)


@BASH
def test_install_leaves_an_lf_env_file_alone(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    env = _env_file(tmp_path, LF)
    before = env.stat().st_mtime_ns
    proc = _install(shell, env, "env line endings", "volumes")
    assert proc.returncode == 0, proc.stderr
    assert env.read_bytes() == LF
    assert env.stat().st_mtime_ns == before
    assert list(env.parent.glob(".env.crlf-*")) == []
    assert "LF line endings" not in proc.stdout
    assert "docker|volume create custom-bank" in shell.logged()


@CRLF_VISIBLE
@BASH
def test_install_rewrites_once_and_keeps_one_copy(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    env = _env_file(tmp_path, CRLF)
    first = _install(shell, env, "env line endings")
    second = _install(shell, env, "env line endings")
    assert first.returncode == 0 and second.returncode == 0
    assert "LF line endings" in first.stdout
    assert "LF line endings" not in second.stdout
    assert len(list(env.parent.glob(".env.crlf-*"))) == 1
    assert env.read_bytes() == LF


@CRLF_VISIBLE
@BASH
def test_a_client_only_install_never_touches_the_env_file(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    env = _env_file(tmp_path, CRLF)
    proc = _install(shell, env, "env line endings", client_only="1")
    assert proc.returncode == 0, proc.stderr
    assert env.read_bytes() == CRLF
    assert list(env.parent.glob(".env.crlf-*")) == []


@BASH
def test_a_missing_env_file_is_not_an_error(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _install(shell, tmp_path / "ops-env" / ".env", "env line endings")
    assert proc.returncode == 0, proc.stderr
    assert "LF line endings" not in proc.stdout


@CRLF_VISIBLE
@BASH
def test_a_value_read_from_a_crlf_env_file_carries_no_cr_even_unrewritten(bash, tmp_path):
    """The volume reads strip a trailing CR themselves: a reader placed before
    the rewrite, or run on a file rewritten by hand, still gets clean names."""
    shell = _Shell(bash, tmp_path)
    env = _env_file(tmp_path, CRLF)
    proc = _install(shell, env, "volumes")
    assert proc.returncode == 0, proc.stderr
    assert b"docker|volume create custom-bank\n" in _raw_calls(shell)
    assert b"\r" not in _raw_calls(shell)


@CRLF_VISIBLE
@BASH
def test_update_rewrites_a_crlf_env_file_the_same_way(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    env = _env_file(tmp_path, CRLF)
    proc = shell.run(f"env_file='{shell.path(env)}'\n" + _block("update.sh", "env line endings"))
    assert proc.returncode == 0, proc.stderr
    assert env.read_bytes() == LF
    [backup] = list(env.parent.glob(".env.crlf-*"))
    assert backup.read_bytes() == CRLF
    [note] = [line for line in proc.stdout.splitlines() if "LF line endings" in line]
    assert backup.name in note
    again = shell.run(f"env_file='{shell.path(env)}'\n" + _block("update.sh", "env line endings"))
    assert again.returncode == 0 and "LF line endings" not in again.stdout
    assert len(list(env.parent.glob(".env.crlf-*"))) == 1


def test_both_scripts_carry_the_same_rewrite() -> None:
    """One implementation, twice: a fix in one script must not drift from the other."""
    assert _block("install.sh", "env line endings").replace(
        '[ -n "$CLIENT_ONLY" ] || ', "") == _block("update.sh", "env line endings")

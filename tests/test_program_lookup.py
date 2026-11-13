"""``program_lookup.find_program``: a program is found only in an absolute
PATH directory, never in the current directory.

Pure filesystem: each test plants stand-in files inside its own temporary
directory and checks the path the lookup returns. Nothing is run.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from pseudolife_memory import program_lookup
from pseudolife_memory.program_lookup import find_program

WINDOWS = os.name == "nt"


def _plant(directory: Path, name: str = "tool", ext: str = ".exe") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    planted = directory / (name + ext if WINDOWS else name)
    planted.write_bytes(b"not run\n")
    planted.chmod(0o755)
    return planted


def _same(found, planted) -> bool:
    """The path found, ignoring the extension's case (PATHEXT spells it)."""
    return found is not None and os.path.normcase(found) == os.path.normcase(str(planted))


def _env(*entries, **extra) -> dict:
    return {"PATH": os.pathsep.join(str(e) for e in entries), **extra}


@pytest.fixture
def work(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def test_the_current_directory_is_never_searched(tmp_path, work):
    _plant(work)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert find_program("tool", _env(empty)) is None


@pytest.mark.parametrize("entry", ["", ".", "bin", os.path.join(".", "bin"), '""', "  "])
def test_relative_and_empty_entries_are_skipped(tmp_path, work, entry):
    _plant(work)
    _plant(work / "bin")
    assert find_program("tool", _env(entry)) is None
    real = _plant(tmp_path / "real")
    assert _same(find_program("tool", _env(entry, tmp_path / "real")), real)


@pytest.mark.skipif(not WINDOWS, reason="Windows' drive-relative and rooted spellings")
@pytest.mark.parametrize("entry", ["C:bin", "\\bin", "/bin"])
def test_drive_relative_and_rooted_entries_are_skipped_on_windows(work, entry):
    assert program_lookup.path_directories(_env(entry)) == []


def test_an_unset_path_searches_nothing(work):
    _plant(work)
    assert find_program("tool", {}) is None


def test_the_first_absolute_directory_wins(tmp_path, work):
    first = _plant(tmp_path / "a")
    _plant(tmp_path / "b")
    assert _same(find_program("tool", _env(tmp_path / "a", tmp_path / "b")), first)


def test_surrounding_space_and_quotes_are_removed(tmp_path, work):
    real = _plant(tmp_path / "real dir")
    assert _same(find_program("tool", _env(f' "{tmp_path / "real dir"}" ')), real)


def test_a_directory_is_not_a_program(tmp_path, work):
    (tmp_path / "d" / ("tool.exe" if WINDOWS else "tool")).mkdir(parents=True)
    assert find_program("tool", _env(tmp_path / "d")) is None


@pytest.mark.skipif(WINDOWS, reason="POSIX execute permission")
def test_a_file_this_user_cannot_run_is_skipped(tmp_path, work):
    planted = _plant(tmp_path / "a")
    planted.chmod(0o644)
    real = _plant(tmp_path / "b")
    assert _same(find_program("tool", _env(tmp_path / "a", tmp_path / "b")), real)


@pytest.mark.skipif(not WINDOWS, reason="PATHEXT")
def test_pathext_order_and_spelling_hold_and_only_launchable_types_count(tmp_path, work):
    d = tmp_path / "d"
    _plant(d, ext=".cmd")
    _plant(d, ext=".exe")
    (d / "tool.js").write_bytes(b"")
    assert find_program("tool", _env(d, PATHEXT=".CMD;.EXE")) == os.path.join(str(d), "tool.CMD")
    assert find_program("tool", _env(d, PATHEXT=".exe;.cmd")) == os.path.join(str(d), "tool.exe")
    assert find_program("tool", _env(d, PATHEXT=".JS")) is None
    assert find_program("tool", _env(d)) == os.path.join(str(d), "tool.EXE")
    assert find_program("tool", _env(d, PATHEXT="")) == os.path.join(str(d), "tool.EXE")


def test_a_name_with_a_separator_is_a_path_not_a_search(tmp_path, work):
    real = _plant(tmp_path / "real")
    assert find_program(str(real), _env()) == str(real)
    assert find_program(str(tmp_path / "missing" / "tool"), _env(tmp_path / "real")) is None
    assert find_program("", _env(tmp_path / "real")) is None


def test_the_default_environment_is_the_process_one(tmp_path, work, monkeypatch):
    real = _plant(tmp_path / "real")
    monkeypatch.setenv("PATH", str(tmp_path / "real"))
    assert _same(find_program("tool"), real)


# -- every call site that runs what it finds uses the lookup --------------------

def _decoys(work: Path, name: str) -> None:
    """The program in the current directory and in a relative entry's."""
    _plant(work, name)
    _plant(work / "bin", name)


def _site_env(monkeypatch, tmp_path, name: str) -> Path:
    """PATH: relative and empty entries, then one absolute directory holding
    the real program; returns the real one."""
    real = _plant(tmp_path / "real", name)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "bin", str(tmp_path / "real")]))
    return real


def _sites():
    from pseudolife_memory import (backup_cli, client_updates, codex_connection, codex_doorbell,
                                   maintainer_setup, runtimes, update_cli)

    def tunnel(name):
        from pseudolife_memory import tunnel_runtime
        return tunnel_runtime.stable_command()[0]

    return {
        "update_cli.which": ("docker", update_cli.which),
        "maintainer_setup.which": ("docker", maintainer_setup.which),
        "client_updates.which": ("claude", client_updates.which),
        "client_updates._git_executable": ("git", lambda name: client_updates._git_executable()),
        "codex_doorbell.resolve_codex_command": (
            "codex", lambda name: codex_doorbell.resolve_codex_command(dict(os.environ))[0]),
        "codex_connection.resolve_codex": ("codex", lambda name: codex_connection.resolve_codex()),
        "backup_cli._find_pg_dump": ("pg_dump", None),
        "runtimes.base_interpreter": ("python3", runtimes.base_interpreter),
        "tunnel_runtime.stable_command": ("pseudolife-mcp", tunnel),
    }


@pytest.mark.parametrize("site", sorted(_sites()))
def test_each_call_site_finds_only_the_absolute_path_program(tmp_path, work, monkeypatch, site):
    from pseudolife_memory import backup_cli, tunnel_runtime

    name, find = _sites()[site]
    monkeypatch.delenv("PSEUDOLIFE_CODEX_BIN", raising=False)
    monkeypatch.delenv("CODEX_CLI_PATH", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-local"))
    monkeypatch.setattr(tunnel_runtime, "installed_launcher", lambda: None)
    monkeypatch.setattr(tunnel_runtime, "checked_path", lambda path: Path(path))
    if find is None:
        def find(name):
            found = backup_cli._find_pg_dump(home=tmp_path / "no-home")
            return str(found) if found else None
    _decoys(work, name)
    real = _site_env(monkeypatch, tmp_path, name)
    assert _same(find(name), real), site


@pytest.mark.skipif(not WINDOWS, reason="PATHEXT")
def test_a_name_that_carries_its_extension_is_that_file(tmp_path, work):
    """``python.exe`` names one file, as ``shutil.which`` reads it: no
    extension is added, and it is still found only in an absolute PATH
    directory."""
    d = tmp_path / "d"
    planted = _plant(d, ext=".exe")
    _plant(work, ext=".exe")
    assert find_program("tool.exe", _env(d)) == os.path.join(str(d), "tool.exe")
    assert find_program("TOOL.EXE", _env(d)) == os.path.join(str(d), "TOOL.EXE")
    assert _same(find_program("tool.exe", _env(d, PATHEXT=".CMD")), planted)
    assert find_program("tool.exe", _env(".", "")) is None
    assert find_program("tool.cmd", _env(d)) is None


@pytest.mark.skipif(not WINDOWS, reason="the desktop Codex install location is Windows-only")
@pytest.mark.parametrize("local", [None, "", "relative"])
def test_codex_setup_never_finds_a_desktop_build_under_the_current_directory(tmp_path, work, monkeypatch,
                                                                            local):
    from pseudolife_memory import codex_connection

    decoy = work / (local or "") / "OpenAI" / "Codex" / "bin" / "1.0" / "codex.exe"
    decoy.parent.mkdir(parents=True)
    decoy.write_bytes(b"not run\n")
    monkeypatch.delenv("CODEX_CLI_PATH", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    if local is None:
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
    else:
        monkeypatch.setenv("LOCALAPPDATA", local)
    with pytest.raises(codex_connection.SetupError):
        codex_connection.resolve_codex()

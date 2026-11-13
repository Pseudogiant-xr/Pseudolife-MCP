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

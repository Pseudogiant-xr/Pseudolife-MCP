"""Executable spelling may vary; relocation still belongs to the captured home."""
import ctypes
import os
from pathlib import Path

import pytest

from evals.rust_port import cli_process


def short_path(path):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_short = kernel.GetShortPathNameW
    get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    get_short.restype = ctypes.c_uint32
    size = get_short(str(path), None, 0)
    if not size:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(size)
    if not get_short(str(path), buffer, size):
        raise ctypes.WinError(ctypes.get_last_error())
    result = Path(buffer.value)
    if result == path:
        pytest.skip("filesystem has no distinct Windows short-name spelling")
    assert result.resolve(strict=True) == path.resolve(strict=True)
    return result


def admission(root, home, target):
    original = root / "original"
    original.write_bytes(b"original executable fixture")
    command = [str(original), "-m", "pseudolife_memory.cli"]
    return cli_process.prepared_command(
        {"mode": "help"}, command, {"oracle": command}, root=root,
        home=home, env={"PATH": os.defpath}, prepare=lambda *args: [str(target), *command[1:]])


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"original executable fixture")
    return path


def directory_link(target, link):
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_regular_relocation_preserves_public_command_spelling(tmp_path):
    home = tmp_path / "captured-home"
    target = executable(home / "runtime" / "native")
    selected, identity, original = admission(tmp_path, home, target)
    assert selected == [str(target), "-m", "pseudolife_memory.cli"]
    assert identity["executable_sha256"] == original["executable_sha256"]
    assert identity["executable_bytes"] == original["executable_bytes"]


@pytest.mark.skipif(os.name != "nt", reason="Windows short-name filesystem spelling")
@pytest.mark.parametrize("home_short", [False, True], ids=["long-home", "short-home"])
@pytest.mark.parametrize("target_short", [False, True], ids=["long-target", "short-target"])
def test_windows_short_and_long_spelling_admit_the_same_owned_file(tmp_path, home_short, target_short):
    home = tmp_path / "captured home with a long directory name"
    target = executable(home / "runtime directory" / "native executable fixture")
    selected_home = short_path(home) if home_short else home
    selected_target = short_path(target) if target_short else target
    selected, identity, original = admission(tmp_path, selected_home, selected_target)
    assert selected == [str(selected_target), "-m", "pseudolife_memory.cli"]
    assert Path(selected[0]).resolve(strict=True) == target.resolve(strict=True)
    assert identity["executable_sha256"] == original["executable_sha256"]


@pytest.mark.parametrize("placement", ["outside-copy", "inside-link-outside", "outside-link-inside",
                                     "root-link"], ids=str)
def test_relocation_refuses_external_files_and_link_boundaries(tmp_path, placement):
    home = tmp_path / "captured-home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    if placement == "outside-copy":
        target = executable(outside / "native")
    elif placement == "inside-link-outside":
        executable(outside / "runtime" / "native")
        directory_link(outside, home / "link")
        target = home / "link" / "runtime" / "native"
    elif placement == "outside-link-inside":
        executable(home / "runtime" / "native")
        directory_link(home, outside / "link")
        target = outside / "link" / "runtime" / "native"
    else:
        executable(outside / "runtime" / "native")
        home.rmdir()
        directory_link(outside, home)
        target = home / "runtime" / "native"
    with pytest.raises(ValueError, match="inside the home|link or junction"):
        admission(tmp_path, home, target)
    assert target.read_bytes() == b"original executable fixture"


def test_relocation_requires_the_selected_file_to_exist(tmp_path):
    home = tmp_path / "captured-home"
    home.mkdir()
    with pytest.raises(FileNotFoundError):
        admission(tmp_path, home, home / "missing")


@pytest.mark.skipif(os.name != "nt", reason="Windows short-name filesystem spelling")
def test_windows_short_spelling_does_not_admit_an_outside_file(tmp_path):
    home = tmp_path / "captured-home"
    home.mkdir()
    target = executable(tmp_path / "outside runtime with a long name" / "native executable fixture")
    with pytest.raises(ValueError, match="inside the home"):
        admission(tmp_path, home, short_path(target))
    assert target.read_bytes() == b"original executable fixture"


def test_relocation_refuses_different_executable_bytes(tmp_path):
    home = tmp_path / "captured-home"
    target = executable(home / "native")
    target.write_bytes(b"substituted bytes")
    with pytest.raises(ValueError, match="original arm prefix and executable bytes"):
        admission(tmp_path, home, target)

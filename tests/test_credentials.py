"""Credential snapshots stay private, bounded, and rotation-safe."""
from __future__ import annotations

import os
from pathlib import Path
import threading

import pytest

from pseudolife_memory.credentials import (
    CredentialError,
    CredentialProvider,
    CredentialSnapshot,
    _write_token_file,
)


def private_token(path: Path, token: str) -> None:
    _write_token_file(path, token)


@pytest.mark.parametrize("writer", ["credential", "coordination"])
def test_windows_writer_assigns_token_user_before_protecting_file(monkeypatch, writer):
    """Model an elevated token whose default file owner is a group."""
    import ctypes
    from types import SimpleNamespace

    from pseudolife_memory.credentials import _secure_windows_file

    user_sid = "S-1-5-21-100-200-300-1001"
    state = {"owner": "S-1-5-32-544", "protected": False}
    buffers = []

    def convert_descriptor(sddl, revision, output, size):
        state["descriptor"] = sddl
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = 123
        return True

    def apply_security(path, flags, descriptor):
        sddl = state["descriptor"]
        if flags & 1:
            state["owner"] = sddl.split("O:", 1)[1].split("D:", 1)[0]
        state["protected"] = bool(flags & 0x80000004 == 0x80000004)
        return True

    def open_token(process, access, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = 456
        return True

    def get_token(handle, info_class, buffer, length, needed):
        # TokenUser, not TokenOwner: the latter would keep the group owner.
        assert info_class == 1
        ctypes.cast(needed, ctypes.POINTER(ctypes.c_ulong))[0] = 32
        if buffer is None:
            return False
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0] = 789
        return True

    def sid_to_string(sid, output):
        assert sid == 789
        buffer = ctypes.create_unicode_buffer(user_sid)
        buffers.append(buffer)
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
        return True

    def success(*args):
        return True

    advapi = SimpleNamespace(
        ConvertStringSecurityDescriptorToSecurityDescriptorW=convert_descriptor,
        SetFileSecurityW=apply_security, OpenProcessToken=open_token,
        GetTokenInformation=get_token, ConvertSidToStringSidW=sid_to_string)
    kernel = SimpleNamespace(LocalFree=success, CloseHandle=success,
                             GetCurrentProcess=success)
    monkeypatch.setattr(ctypes, "WinDLL",
                        lambda name, **kwargs: advapi if name == "advapi32" else kernel,
                        raising=False)
    if writer == "coordination":
        from pseudolife_memory import coordination_adapter
        monkeypatch.setattr(coordination_adapter, "os", SimpleNamespace(name="nt"))
        coordination_adapter._private_fd(-1, Path("new-identity-file"))
    else:
        _secure_windows_file(Path("new-credential-file"))
    assert state["owner"] == user_sid
    assert state["protected"]
    assert state["descriptor"].endswith("D:P(A;;FA;;;OW)")


def test_static_snapshot_is_frozen_stable_and_secret_free() -> None:
    provider = CredentialProvider(token="fixture-secret")
    first = provider.snapshot()
    second = provider.snapshot()
    assert isinstance(first, CredentialSnapshot)
    assert first.token == "fixture-secret"
    assert first.generation == second.generation
    assert "fixture-secret" not in repr(first)
    assert "fixture-secret" not in repr(provider)
    with pytest.raises((AttributeError, TypeError)):
        first.token = "changed"  # type: ignore[misc]


def test_file_snapshot_observes_atomic_rotation(tmp_path: Path) -> None:
    path = tmp_path / "token"
    private_token(path, "first-secret")
    provider = CredentialProvider(path=path)
    first = provider.snapshot()
    private_token(path, "second-secret")
    second = provider.snapshot()
    assert first.token == "first-secret"
    assert second.token == "second-secret"
    assert first.generation != second.generation
    assert provider.snapshot().generation == second.generation


def test_generation_detects_unobserved_a_b_a_replacement(tmp_path: Path) -> None:
    path = tmp_path / "token"
    private_token(path, "same-secret")
    provider = CredentialProvider(path=path)
    before = provider.snapshot()
    private_token(path, "other-secret")
    private_token(path, "same-secret")
    after = provider.snapshot()
    assert before.token == after.token
    assert before.generation != after.generation


def test_from_environment_prefers_explicit_file_and_never_falls_back(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "token"
    private_token(path, "file-secret")
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN_FILE", str(path))
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "stale-secret")
    assert CredentialProvider.from_environment().snapshot().token == "file-secret"
    path.unlink()
    with pytest.raises(CredentialError, match="missing") as raised:
        CredentialProvider.from_environment().snapshot()
    assert "stale-secret" not in str(raised.value)


def test_empty_explicit_file_path_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN_FILE", "")
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "stale-secret")
    with pytest.raises(CredentialError, match="path is empty") as raised:
        CredentialProvider.from_environment()
    assert "stale-secret" not in str(raised.value)


def test_tokenless_environment_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN_FILE", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN", raising=False)
    snapshot = CredentialProvider.from_environment().snapshot()
    assert snapshot.token is None


@pytest.mark.parametrize("payload", [b"", b"two\nlines", b"surrounded ", b"x" * 4097])
def test_malformed_or_oversized_file_is_rejected_without_payload(
        tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "token"
    _write_token_file(path, "temporary")
    path.write_bytes(payload)
    if os.name != "nt":
        path.chmod(0o600)
    with pytest.raises(CredentialError) as raised:
        CredentialProvider(path=path).snapshot()
    message = str(raised.value)
    assert "two" not in message and "surrounded" not in message


def test_one_or_more_terminal_newlines_are_accepted(tmp_path: Path) -> None:
    path = tmp_path / "token"
    private_token(path, "fixture")
    path.write_bytes(b"fixture\r\n")
    if os.name != "nt":
        path.chmod(0o600)
    assert CredentialProvider(path=path).snapshot().token == "fixture"


def test_constructor_rejects_ambiguous_sources_without_echoing_values(tmp_path: Path) -> None:
    with pytest.raises(CredentialError) as raised:
        CredentialProvider(token="fixture-secret", path=tmp_path / "token")
    assert "fixture-secret" not in str(raised.value)


def test_symlink_and_hardlink_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    private_token(source, "fixture-secret")
    hardlink = tmp_path / "hardlink"
    os.link(source, hardlink)
    with pytest.raises(CredentialError, match="private regular file"):
        CredentialProvider(path=hardlink).snapshot()
    source.unlink()
    hardlink.unlink()
    private_token(source, "fixture-secret")
    symlink = tmp_path / "symlink"
    try:
        symlink.symlink_to(source)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(CredentialError, match="private regular file"):
        CredentialProvider(path=symlink).snapshot()


def test_symlink_ancestor_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    path = real / "token"
    private_token(path, "fixture-secret")
    redirect = tmp_path / "redirect"
    try:
        redirect.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(CredentialError, match="redirect"):
        CredentialProvider(path=redirect / "token").snapshot()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_group_or_world_readable_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "token"
    private_token(path, "fixture-secret")
    path.chmod(0o640)
    with pytest.raises(CredentialError, match="owner-only"):
        CredentialProvider(path=path).snapshot()


@pytest.mark.skipif(os.name == "nt", reason="POSIX open-handle unlink semantics")
def test_atomic_replacement_after_open_retains_private_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "token"
    replacement = tmp_path / "replacement"
    private_token(path, "old-fixture-token")
    private_token(replacement, "new-fixture-token")
    original_open = os.open

    def replace_after_open(candidate, flags, *args, **kwargs):
        fd = original_open(candidate, flags, *args, **kwargs)
        if Path(candidate) == path and replacement.exists():
            os.replace(replacement, path)
            assert os.fstat(fd).st_nlink == 0
        return fd

    monkeypatch.setattr(os, "open", replace_after_open)
    provider = CredentialProvider(path=path)
    old = provider.snapshot()
    new = provider.snapshot()
    assert old.token == "old-fixture-token"
    assert new.token == "new-fixture-token"
    assert old.generation != new.generation


def test_concurrent_atomic_rotation_never_returns_torn_credentials(tmp_path: Path) -> None:
    path = tmp_path / "token"
    tokens = ("a" * 2000, "b" * 2000)
    private_token(path, tokens[0])
    provider = CredentialProvider(path=path)
    failures: list[BaseException] = []

    def rotate() -> None:
        try:
            for index in range(40):
                private_token(path, tokens[index % 2])
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=rotate)
    worker.start()
    observed = {provider.snapshot().token for _ in range(100)}
    worker.join()
    assert not failures
    assert observed <= set(tokens)

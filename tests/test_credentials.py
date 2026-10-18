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
        # The adapter's _private_fd is a wrapper over private_state's, which
        # is what reads os.name: patch there, call through the wrapper.
        from pseudolife_memory import coordination_adapter, private_state
        monkeypatch.setattr(private_state, "os", SimpleNamespace(name="nt"))
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


# --- refusals name their cause and their fix ----------------------------------
# 2026-10-18: a token rotation rewrote ~/.pseudolife-mcp/claude-code.token with
# a plain temp file and os.replace, so it lost the owner-only ACL. Every MCP
# call then failed with "restore the configured credential", and the file the
# message said to restore already held the right token. The reason and the one
# fix travel with the error now.

def shared_token(path: Path, token: str) -> None:
    """A token file this user owns whose permissions are broader than owner-only,
    as a plain write leaves it: inherited ACL on Windows, mode 0644 elsewhere."""
    path.unlink(missing_ok=True)
    path.write_bytes(token.encode("utf-8"))
    if os.name != "nt":
        path.chmod(0o644)


def _refusal(path: Path) -> CredentialError:
    with pytest.raises(CredentialError) as raised:
        CredentialProvider(path=path).snapshot()
    return raised.value


def test_a_broad_permission_refusal_names_the_file_and_the_owner_only_fix(tmp_path: Path) -> None:
    path = tmp_path / "claude-code.token"
    shared_token(path, "fixture-secret")
    error = _refusal(path)
    assert str(error) == "credential file must be owner-only"
    assert error.path == path
    fix = error.recovery()
    if os.name == "nt":
        assert f'icacls "{path}" /inheritance:r /grant:r *S-1-3-4:F' in fix
    else:
        assert f"chmod 600 {path}" in fix
    assert "fixture-secret" not in fix


@pytest.mark.skipif(os.name != "nt", reason="the Windows ACL fix")
def test_the_named_windows_fix_makes_the_file_usable(tmp_path: Path) -> None:
    """Run exactly the command the refusal names: it must make the same file
    pass the shim's own check, with its token unchanged."""
    import subprocess

    path = tmp_path / "claude-code.token"
    shared_token(path, "fixture-secret")
    assert "icacls" in _refusal(path).recovery()
    completed = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", "*S-1-3-4:F"],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert CredentialProvider(path=path).snapshot().token == "fixture-secret"


def test_a_missing_file_refusal_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "missing.token"
    error = _refusal(path)
    assert str(error) == "configured credential file is missing"
    assert error.path == path
    assert str(path) in error.recovery()
    assert "PSEUDOLIFE_MCP_TOKEN_FILE" in error.recovery()


@pytest.mark.parametrize("payload", [b"", b"\n", b"two words"])
def test_an_invalid_token_refusal_says_what_the_file_must_hold(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "token"
    private_token(path, "temporary")
    path.write_bytes(payload)
    if os.name != "nt":
        path.chmod(0o600)
    error = _refusal(path)
    assert str(error) == "credential file contains an invalid bearer token"
    assert "only the token" in error.recovery()
    assert "words" not in error.recovery()


def test_a_hardlink_refusal_says_to_use_a_regular_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    private_token(source, "fixture-secret")
    hardlink = tmp_path / "hardlink"
    os.link(source, hardlink)
    error = _refusal(hardlink)
    assert str(error) == "credential file must be a private regular file"
    assert "regular file" in error.recovery()


def test_a_redirected_ancestor_refusal_says_to_name_the_real_path(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    private_token(real / "token", "fixture-secret")
    redirect = tmp_path / "redirect"
    try:
        redirect.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    error = _refusal(redirect / "token")
    assert str(error) == "credential path must not contain redirects"
    assert "real path" in error.recovery()


def test_every_reading_reason_has_a_fix_and_unknown_ones_have_none() -> None:
    from pathlib import PurePosixPath, PureWindowsPath

    from pseudolife_memory.credentials import credential_recovery

    path = PurePosixPath("/fixture/dir/token")
    for reason in ("configured credential file is missing",
                   "configured credential file is unavailable",
                   "credential path ancestors cannot be inspected",
                   "credential path must not contain redirects",
                   "credential file must be a private regular file",
                   "credential file contains an invalid bearer token"):
        assert str(path) in credential_recovery(reason, path, windows=False), reason
    assert "chmod 600 /fixture/dir/token" in credential_recovery(
        "credential file must be owner-only", path, windows=False)
    assert r'icacls "C:\fixture\token" /inheritance:r /grant:r *S-1-3-4:F' in credential_recovery(
        "credential file must be owner-only", PureWindowsPath("C:/fixture/token"), windows=True)
    # A path with a space stays one shell word.
    assert "chmod 600 '/fixture/my dir/token'" in credential_recovery(
        "credential file must be owner-only", PurePosixPath("/fixture/my dir/token"), windows=False)
    assert credential_recovery("credential file validation failed", path) is None
    # Without a known path the fix still reads as a sentence, not "None".
    assert "None" not in credential_recovery("configured credential file is missing", None)


def test_a_usable_owner_only_file_is_not_refused(tmp_path: Path) -> None:
    """The near miss: the file every writer produces passes untouched."""
    path = tmp_path / "token"
    private_token(path, "fixture-secret")
    assert CredentialProvider(path=path).snapshot().token == "fixture-secret"


# --- the writer repairs what it owns -------------------------------------------

def test_writer_replaces_an_own_file_whose_permissions_are_too_broad(tmp_path: Path) -> None:
    """Replacing it can only narrow access, so refusing helped nobody: the
    2026-10-18 repair had to delete the file before the writer would act."""
    path = tmp_path / "token"
    shared_token(path, "old-fixture")
    _write_token_file(path, "new-fixture")
    assert CredentialProvider(path=path).snapshot().token == "new-fixture"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_writer_still_refuses_a_broad_file_another_account_owns(tmp_path: Path, monkeypatch) -> None:
    from pseudolife_memory import credentials

    path = tmp_path / "token"
    shared_token(path, "old-fixture")
    monkeypatch.setattr(credentials, "_owned_by_current_user", lambda fd: False)
    with pytest.raises(CredentialError, match="owner-only"):
        _write_token_file(path, "new-fixture")
    assert path.read_bytes() == b"old-fixture"


def test_writer_still_refuses_a_hardlinked_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    private_token(source, "fixture-secret")
    hardlink = tmp_path / "hardlink"
    os.link(source, hardlink)
    with pytest.raises(CredentialError, match="private regular file"):
        _write_token_file(hardlink, "new-fixture")
    assert hardlink.read_bytes() == source.read_bytes() == b"fixture-secret"

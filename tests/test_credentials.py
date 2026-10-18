"""Credential snapshots stay private, bounded, and rotation-safe."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
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
    """A token file this user owns whose permissions are broader than owner-only:
    an explicit Everyone read entry on Windows, mode 0644 elsewhere. Written by
    the writer first, which pins the owner to this user, so the fixture does not
    depend on elevation: an elevated process's plain write is owned by
    Administrators (hosted Windows runners, 2026-10-18)."""
    path.unlink(missing_ok=True)
    _write_token_file(path, "placeholder")
    path.write_bytes(token.encode("utf-8"))
    if os.name == "nt":
        subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:R"], check=True,
                       capture_output=True)
    else:
        path.chmod(0o644)


def plain_token(path: Path, token: str) -> None:
    """A token file as a plain write leaves it (the 2026-10-18 rotation): mode
    0644 on POSIX; on Windows inherited permissions, and owned by
    Administrators instead of this user when the process is elevated."""
    path.unlink(missing_ok=True)
    path.write_bytes(token.encode("utf-8"))
    if os.name != "nt":
        path.chmod(0o644)


def _refusal(path: Path) -> CredentialError:
    with pytest.raises(CredentialError) as raised:
        CredentialProvider(path=path).snapshot()
    return raised.value


REPAIR = "-m pseudolife_memory.credentials repair"


def test_a_broad_permission_refusal_names_the_file_and_the_owner_only_fix(tmp_path: Path) -> None:
    path = tmp_path / "claude-code.token"
    shared_token(path, "fixture-secret")
    error = _refusal(path)
    assert str(error) == "credential file must be owner-only"
    assert error.path == path
    fix = error.recovery()
    if os.name == "nt":
        assert f'"{sys.executable}" {REPAIR} "{path}"' in fix
    else:
        assert fix.endswith(f"chmod 600 {path}")
    assert "fixture-secret" not in fix


def _run_repair(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pseudolife_memory.credentials", "repair",
                           str(path)], capture_output=True, text=True, timeout=60)


# (WD), not W: FILE_GENERIC_WRITE carries SYNCHRONIZE, and denying it makes
# the file unopenable, which is a different refusal.
@pytest.mark.skipif(os.name != "nt", reason="Windows explicit ACL entries")
@pytest.mark.parametrize("explicit", [[], [["/grant", "*S-1-1-0:R"], ["/deny", "*S-1-5-32-545:(WD)"]]],
                         ids=["inherited", "explicit-entries"])
def test_the_named_repair_makes_a_broad_windows_file_usable(tmp_path: Path, explicit) -> None:
    """Run the command the refusal names: the same file passes the shim's own
    check afterwards with its token unchanged, also when it carried explicit
    entries (an icacls recipe kept those: review finding, 2026-10-18)."""
    path = tmp_path / "claude-code.token"
    shared_token(path, "fixture-secret")
    for entry in explicit:
        subprocess.run(["icacls", str(path), *entry], check=True, capture_output=True)
    assert REPAIR in _refusal(path).recovery()
    completed = _run_repair(path)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "fixture-secret" not in completed.stdout + completed.stderr
    assert CredentialProvider(path=path).snapshot().token == "fixture-secret"


def test_repair_rewrites_a_broad_file_owner_only_with_its_token(tmp_path: Path) -> None:
    from pseudolife_memory.credentials import repair_token_file

    path = tmp_path / "token"
    shared_token(path, "fixture-secret")
    repair_token_file(path)
    assert CredentialProvider(path=path).snapshot().token == "fixture-secret"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_repair_replaces_a_file_another_account_owns(tmp_path: Path, monkeypatch) -> None:
    """The fix the foreign-owner refusal names: a new owner-only file of the
    caller's own, written before it replaces the old one, with the same token."""
    from pseudolife_memory import credentials

    path = tmp_path / "token"
    private_token(path, "fixture-secret")
    original = path.stat().st_ino
    if os.name == "nt":
        real = credentials._windows_file_access
        monkeypatch.setattr(credentials, "_windows_file_access",
                            lambda fd, **kw: "foreign" if os.fstat(fd).st_ino == original else real(fd, **kw))
    else:
        real_fstat = os.fstat
        uid = os.geteuid()

        class ForeignOwned:
            # Every attribute of the real result (st_mtime_ns included: the
            # repair's identity check reads it), with another account's uid.
            def __init__(self, info):
                self._info = info

            def __getattr__(self, name):
                return uid + 1 if name == "st_uid" else getattr(self._info, name)

        def fstat(fd):
            info = real_fstat(fd)
            return info if info.st_ino != original else ForeignOwned(info)
        monkeypatch.setattr(credentials.os, "fstat", fstat)
    assert str(_refusal(path)) == "credential file is owned by another account"
    credentials.repair_token_file(path)
    assert path.stat().st_ino != original
    assert CredentialProvider(path=path).snapshot().token == "fixture-secret"


def test_repair_refuses_what_it_cannot_safely_rewrite(tmp_path: Path) -> None:
    from pseudolife_memory.credentials import repair_token_file

    with pytest.raises(CredentialError, match="missing"):
        repair_token_file(tmp_path / "missing")
    source = tmp_path / "source"
    private_token(source, "fixture-secret")
    hardlink = tmp_path / "hardlink"
    os.link(source, hardlink)
    with pytest.raises(CredentialError, match="private regular file"):
        repair_token_file(hardlink)
    invalid = tmp_path / "invalid"
    shared_token(invalid, "two words")
    with pytest.raises(CredentialError, match="invalid bearer token"):
        repair_token_file(invalid)
    assert invalid.read_bytes() == b"two words"


def test_repair_command_reports_without_the_token(tmp_path: Path) -> None:
    path = tmp_path / "token"
    shared_token(path, "fixture-secret")
    completed = _run_repair(path)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert str(path) in completed.stdout and "owner-only" in completed.stdout
    assert "fixture-secret" not in completed.stdout + completed.stderr
    missing = _run_repair(tmp_path / "missing")
    assert missing.returncode == 1
    assert "configured credential file is missing" in missing.stderr
    assert "Traceback" not in missing.stderr


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
    assert "If the token in it is yours" in credential_recovery(
        "credential file is owned by another account", path, windows=False)
    for reason in ("credential file changed while it was being repaired",
                   "configured credential file is missing",
                   "configured credential file is unavailable",
                   "credential path ancestors cannot be inspected",
                   "credential path must not contain redirects",
                   "credential file must be a private regular file",
                   "credential file contains an invalid bearer token",
                   "credential file is owned by another account"):
        assert str(path) in credential_recovery(reason, path, windows=False), reason
    assert credential_recovery("credential file must be owner-only", path,
                               windows=False).endswith("chmod 600 /fixture/dir/token")
    python = sys.executable
    assert f"{shlex.quote(python)} {REPAIR} /fixture/dir/token" in credential_recovery(
        "credential file is owned by another account", path, windows=False)
    windows_path = PureWindowsPath("C:/fixture/token")
    for reason in ("credential file must be owner-only",
                   "credential file is owned by another account"):
        assert f'"{python}" {REPAIR} "C:\\fixture\\token"' in credential_recovery(
            reason, windows_path, windows=True), reason
        for windows in (True, False):
            assert "None" not in credential_recovery(reason, None, windows=windows)
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


def _another_account_owns_every_file(monkeypatch) -> None:
    from pseudolife_memory import credentials

    if os.name == "nt":
        monkeypatch.setattr(credentials, "_windows_file_access", lambda fd, **_: "foreign")
    else:
        real = os.geteuid()
        monkeypatch.setattr(credentials.os, "geteuid", lambda: real + 1)


def test_another_accounts_file_gets_its_own_reason_not_the_permissions_fix(
        tmp_path: Path, monkeypatch) -> None:
    """Narrowing permissions on a file another account owns (say, Administrators,
    after an elevated write) would lock this user out of it: that case has its
    own reason and fix (review finding, 2026-10-18)."""
    path = tmp_path / "token"
    private_token(path, "fixture-secret")
    _another_account_owns_every_file(monkeypatch)
    error = _refusal(path)
    assert str(error) == "credential file is owned by another account"
    assert REPAIR in error.recovery()
    assert "Other accounts can read it" not in error.recovery()


def test_writer_still_refuses_a_file_another_account_owns(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "token"
    shared_token(path, "old-fixture")
    _another_account_owns_every_file(monkeypatch)
    with pytest.raises(CredentialError, match="owned by another account"):
        _write_token_file(path, "new-fixture")
    assert path.read_bytes() == b"old-fixture"


def test_writer_refuses_a_broad_file_whose_owner_cannot_be_proven(tmp_path: Path, monkeypatch) -> None:
    from pseudolife_memory import credentials

    path = tmp_path / "token"
    shared_token(path, "old-fixture")
    monkeypatch.setattr(credentials, "_owned_by_current_user", lambda fd: False)
    with pytest.raises(CredentialError, match="owner-only"):
        _write_token_file(path, "new-fixture")
    assert path.read_bytes() == b"old-fixture"


def test_a_plainly_written_file_is_refused_and_the_writer_replaces_it(tmp_path: Path) -> None:
    """Elevated or not: unelevated it is this user's broad file; elevated it is
    owned by Administrators, this token's default owner, which the shim
    refuses as another account's and the writer may still replace (2026-10-18,
    the Windows lite lane's elevated runners)."""
    path = tmp_path / "token"
    plain_token(path, "old-fixture")
    error = _refusal(path)
    if os.name == "nt":
        assert str(error) in ("credential file must be owner-only",
                              "credential file is owned by another account")
        assert REPAIR in error.recovery()
    else:
        assert str(error) == "credential file must be owner-only"
    _write_token_file(path, "new-fixture")
    assert CredentialProvider(path=path).snapshot().token == "new-fixture"


@pytest.mark.skipif(os.name != "nt", reason="Windows file owners")
def test_writer_refuses_a_file_owned_by_neither_the_user_nor_its_default_owner(tmp_path: Path) -> None:
    """The near miss for trusting the token's default owner: a third owner
    (SYSTEM here) is still another account's file to the writer. Setting it
    needs an elevated process, as on the hosted Windows runners (Opus review,
    2026-10-18)."""
    path = tmp_path / "token"
    plain_token(path, "old-fixture")
    completed = subprocess.run(["icacls", str(path), "/setowner", "*S-1-5-18"],
                               capture_output=True, text=True)
    if completed.returncode != 0:
        pytest.skip("giving the file another owner needs an elevated process")
    with pytest.raises(CredentialError, match="owned by another account"):
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


def test_repair_never_undoes_a_rotation_that_lands_while_it_runs(tmp_path: Path, monkeypatch) -> None:
    """A rotation between repair's read and its rename must win: repair refuses
    rather than write the old token back (Codex review, 2026-10-18)."""
    from pseudolife_memory import credentials

    path = tmp_path / "token"
    shared_token(path, "old-fixture")
    real_secure = credentials._secure_windows_file
    real_fchmod = getattr(os, "fchmod", None)
    rotated = []

    def rotate_once():
        if not rotated:
            rotated.append(True)
            private_token(path, "rotated-fixture")

    if os.name == "nt":
        def secure(temporary):
            real_secure(temporary)
            if Path(temporary) != path:
                rotate_once()
        monkeypatch.setattr(credentials, "_secure_windows_file", secure)
    else:
        def fchmod(fd, mode):
            real_fchmod(fd, mode)
            rotate_once()
        monkeypatch.setattr(credentials.os, "fchmod", fchmod)
    with pytest.raises(CredentialError, match="changed"):
        credentials.repair_token_file(path)
    monkeypatch.undo()
    assert rotated
    assert CredentialProvider(path=path).snapshot().token == "rotated-fixture"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".pseudolife-token-")]


def test_repair_command_survives_a_console_that_cannot_print_the_path(tmp_path: Path) -> None:
    """A path the console encoding cannot show is escaped, not a traceback after
    the file was already rewritten (Codex review, 2026-10-18)."""
    directory = tmp_path / "t\u00f8ken-\u2713"
    directory.mkdir()
    path = directory / "token"
    shared_token(path, "fixture-secret")
    environment = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    for target, code in ((path, 0), (directory / "missing", 1)):
        completed = subprocess.run(
            [sys.executable, "-m", "pseudolife_memory.credentials", "repair", str(target)],
            capture_output=True, timeout=60, env=environment)
        output = (completed.stdout + completed.stderr).decode("cp1252", "replace")
        assert completed.returncode == code, output
        assert "Traceback" not in output
    assert CredentialProvider(path=path).snapshot().token == "fixture-secret"

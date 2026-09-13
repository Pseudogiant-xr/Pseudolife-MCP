"""Bounded bearer credential snapshots for long-lived clients."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import threading
import time


MAX_TOKEN_BYTES = 4096


class CredentialError(RuntimeError):
    """A sanitized credential failure safe to surface to a caller."""


@dataclass(frozen=True)
class _Generation:
    _marker: tuple[object, ...] = field(repr=False)

    def __repr__(self) -> str:
        return "<credential generation>"


@dataclass(frozen=True)
class CredentialSnapshot:
    """One coherent credential value and its opaque equality marker."""

    token: str | None = field(repr=False)
    generation: object


def _is_redirect(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse)


def _reject_ancestor_redirects(path: Path) -> None:
    for ancestor in reversed(path.parent.parents):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise CredentialError("credential path ancestors cannot be inspected") from error
        if _is_redirect(info):
            raise CredentialError("credential path must not contain redirects")
    try:
        parent_info = path.parent.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise CredentialError("credential path ancestors cannot be inspected") from error
    if _is_redirect(parent_info):
        raise CredentialError("credential path must not contain redirects")


def _secure_windows_file(path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    apply = advapi.SetFileSecurityW
    apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    apply.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    if not convert("D:P(A;;FA;;;OW)", 1, ctypes.byref(descriptor), None):
        raise CredentialError("credential file permissions could not be protected")
    try:
        if not apply(str(path), 0x80000004, descriptor):
            raise CredentialError("credential file permissions could not be protected")
    finally:
        kernel.LocalFree(descriptor)


def _windows_owner_only(fd: int) -> bool:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class ACL(ctypes.Structure):
        _fields_ = [("AclRevision", wintypes.BYTE), ("Sbz1", wintypes.BYTE),
                    ("AclSize", wintypes.WORD), ("AceCount", wintypes.WORD),
                    ("Sbz2", wintypes.WORD)]

    class ACE_HEADER(ctypes.Structure):
        _fields_ = [("AceType", wintypes.BYTE), ("AceFlags", wintypes.BYTE),
                    ("AceSize", wintypes.WORD)]

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TOKEN_USER(ctypes.Structure):
        _fields_ = [("User", SID_AND_ATTRIBUTES)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_security = advapi.GetSecurityInfo
    get_security.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                             ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                             ctypes.POINTER(ctypes.POINTER(ACL)), ctypes.POINTER(ctypes.POINTER(ACL)),
                             ctypes.POINTER(ctypes.c_void_p)]
    get_security.restype = wintypes.DWORD
    get_control = advapi.GetSecurityDescriptorControl
    get_control.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD),
                            ctypes.POINTER(wintypes.DWORD)]
    get_control.restype = wintypes.BOOL
    get_ace = advapi.GetAce
    get_ace.argtypes = [ctypes.POINTER(ACL), wintypes.DWORD,
                        ctypes.POINTER(ctypes.c_void_p)]
    get_ace.restype = wintypes.BOOL
    equal_sid = advapi.EqualSid
    equal_sid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    equal_sid.restype = wintypes.BOOL
    well_known_sid = advapi.IsWellKnownSid
    well_known_sid.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    well_known_sid.restype = wintypes.BOOL
    open_token = advapi.OpenProcessToken
    open_token.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                           ctypes.POINTER(wintypes.HANDLE)]
    open_token.restype = wintypes.BOOL
    get_token = advapi.GetTokenInformation
    get_token.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    get_token.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]

    owner = ctypes.c_void_p()
    dacl = ctypes.POINTER(ACL)()
    descriptor = ctypes.c_void_p()
    result = get_security(msvcrt.get_osfhandle(fd), 1, 0x00000005,
                          ctypes.byref(owner), None, ctypes.byref(dacl), None,
                          ctypes.byref(descriptor))
    if result != 0 or not descriptor or not owner or not dacl:
        if descriptor:
            kernel.LocalFree(descriptor)
        return False
    try:
        token_handle = wintypes.HANDLE()
        if not open_token(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token_handle)):
            return False
        try:
            needed = wintypes.DWORD()
            get_token(token_handle, 1, None, 0, ctypes.byref(needed))
            if not needed.value:
                return False
            token_buffer = ctypes.create_string_buffer(needed.value)
            if not get_token(token_handle, 1, token_buffer, needed, ctypes.byref(needed)):
                return False
            current_owner = ctypes.cast(token_buffer, ctypes.POINTER(TOKEN_USER)).contents.User.Sid
            if not equal_sid(owner, current_owner):
                return False
        finally:
            kernel.CloseHandle(token_handle)
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            return False
        if not (control.value & 0x1000):  # SE_DACL_PROTECTED
            return False
        owner_allowed = False
        for index in range(dacl.contents.AceCount):
            ace = ctypes.c_void_p()
            if not get_ace(dacl, index, ctypes.byref(ace)):
                return False
            header = ctypes.cast(ace, ctypes.POINTER(ACE_HEADER)).contents
            if header.AceType == 1:  # ACCESS_DENIED_ACE_TYPE
                continue
            if header.AceType != 0:  # Reject unfamiliar allow/object ACEs.
                return False
            sid = ctypes.c_void_p(ace.value + 8)
            # The protected DACL writer uses the OWNER RIGHTS well-known SID,
            # which resolves to the current file owner without embedding an
            # account identifier in the descriptor.
            if not equal_sid(owner, sid) and not well_known_sid(sid, 71):
                return False
            owner_allowed = True
        return owner_allowed
    finally:
        kernel.LocalFree(descriptor)


def _validate_file(fd: int, *, allow_unlinked: bool = False) -> os.stat_result:
    info = os.fstat(fd)
    valid_links = info.st_nlink == 1 or (
        allow_unlinked and os.name != "nt" and info.st_nlink == 0)
    if not stat.S_ISREG(info.st_mode) or not valid_links:
        raise CredentialError("credential file must be a private regular file")
    if os.name == "nt":
        if not _windows_owner_only(fd):
            raise CredentialError("credential file must be owner-only")
    elif info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise CredentialError("credential file must be owner-only")
    return info


def _decode_token(data: bytes) -> str:
    if not data or len(data) > MAX_TOKEN_BYTES:
        raise CredentialError("credential file contains an invalid bearer token")
    try:
        token = data.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError as error:
        raise CredentialError("credential file contains an invalid bearer token") from error
    if not token or any(character.isspace() or ord(character) < 0x20 for character in token):
        raise CredentialError("credential file contains an invalid bearer token")
    return token


def _open_token(path: Path) -> tuple[str, _Generation]:
    _reject_ancestor_redirects(path)
    try:
        path_info = path.lstat()
    except FileNotFoundError as error:
        raise CredentialError("configured credential file is missing") from error
    except OSError as error:
        raise CredentialError("configured credential file is unavailable") from error
    if _is_redirect(path_info):
        raise CredentialError("credential file must be a private regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    for attempt in range(20):
        try:
            fd = os.open(path, flags)
            break
        except PermissionError as error:
            if os.name != "nt" or attempt == 19:
                raise CredentialError("configured credential file is unavailable") from error
            time.sleep(0.005)
        except FileNotFoundError as error:
            raise CredentialError("configured credential file is missing") from error
        except OSError as error:
            raise CredentialError("configured credential file is unavailable") from error
    try:
        # POSIX atomic replacement can unlink an already-open snapshot. Zero
        # links expose no alias; ownership and private mode still must hold.
        info = _validate_file(fd, allow_unlinked=True)
        chunks = []
        remaining = MAX_TOKEN_BYTES + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        token = _decode_token(data)
    finally:
        os.close(fd)
    # The opened handle is the snapshot boundary. An atomic replacement after
    # open leaves this handle on the complete old file; the next call opens the
    # complete new file and receives its distinct identity marker.
    marker = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
              info.st_ctime_ns, hashlib.sha256(data).digest())
    return token, _Generation(marker)


def _write_token_file(path: str | Path, token: str) -> None:
    """Atomically create or rotate one owner-only credential file."""
    data = token.encode("utf-8")
    _decode_token(data)
    target = Path(os.path.abspath(Path(path).expanduser()))
    _reject_ancestor_redirects(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_ancestor_redirects(target)
    if target.exists() or target.is_symlink():
        existing_fd = os.open(target, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                              | getattr(os, "O_NOFOLLOW", 0))
        try:
            _validate_file(existing_fd)
        finally:
            os.close(existing_fd)
    fd, temporary = tempfile.mkstemp(prefix=".pseudolife-token-", dir=target.parent)
    temporary_path = Path(temporary)
    try:
        if os.name == "nt":
            _secure_windows_file(temporary_path)
        else:
            os.fchmod(fd, 0o600)
        _validate_file(fd)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # The 2026-09-13 Windows 40-rotation/100-snapshot stress test observed
        # brief delete-share conflicts; cap recovery at 20 x 5 ms.
        for attempt in range(20):
            try:
                os.replace(temporary_path, target)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 19:
                    raise
                time.sleep(0.005)
        token_read, _ = _open_token(target)
        if token_read != token:
            raise CredentialError("credential file validation failed")
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


class CredentialProvider:
    """Return coherent static or file-backed bearer snapshots."""

    def __init__(self, token: str | None = None, path: str | Path | None = None):
        if token is not None and path is not None:
            raise CredentialError("configure exactly one credential source")
        if path is not None and not os.fspath(path):
            raise CredentialError("configured credential file path is empty")
        if token is not None:
            _decode_token(token.encode("utf-8"))
        self._token = token
        self._path = (Path(os.path.abspath(Path(path).expanduser()))
                      if path is not None else None)
        self._static_generation = _Generation(("static", id(self)))
        self._lock = threading.RLock()

    @classmethod
    def from_environment(cls) -> "CredentialProvider":
        if "PSEUDOLIFE_MCP_TOKEN_FILE" in os.environ:
            return cls(path=os.environ["PSEUDOLIFE_MCP_TOKEN_FILE"])
        token = os.environ.get("PSEUDOLIFE_MCP_TOKEN") or None
        return cls(token=token)

    def snapshot(self) -> CredentialSnapshot:
        with self._lock:
            if self._path is None:
                return CredentialSnapshot(self._token, self._static_generation)
            token, generation = _open_token(self._path)
            return CredentialSnapshot(token, generation)

    def __repr__(self) -> str:
        source = "file" if self._path is not None else "static"
        return f"CredentialProvider(source={source!r})"

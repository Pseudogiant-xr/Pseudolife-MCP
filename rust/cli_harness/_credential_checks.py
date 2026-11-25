"""The frozen client's own credential-file checks: the Windows owner-only
test (owner is this user, a protected DACL that allows only the owner or
OWNER RIGHTS) and the bearer token's decoding. The transport rows record
their verdicts on the files each arm leaves (``credential_files``).

Copied verbatim, by syntax tree, from pseudolife_memory/credentials.py at 6ca817d70:
MAX_TOKEN_BYTES :16-16, _is_redirect :109-112, _reject_ancestor_redirects
:115-132, _windows_owner_only :250-251, _windows_file_access :254-387,
_validate_regular :390-396, _validate_file :399-414, _decode_token :443-452,
_open_token :455-500.

The source is frozen, so the copy cannot drift from it; never edit it by hand.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import time


class CredentialError(RuntimeError):
    """What the frozen client raises for a file it refuses: the message is
    the reason (a stand-in for credentials.CredentialError, which also
    carries the path for its recovery text)."""


class _Generation:
    """A stand-in for credentials._Generation: the snapshot identity marker,
    which no harness check reads."""

    def __init__(self, marker):
        self._marker = marker


MAX_TOKEN_BYTES = 4096


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


def _windows_owner_only(fd: int) -> bool:
    return _windows_file_access(fd) == "owner-only"


def _windows_file_access(fd: int, *, default_owner_is_own: bool = False) -> str:
    """``owner-only`` when the current user owns the file and only its owner
    may access it; ``owned`` when the current user owns it but the DACL is
    broader; ``foreign`` when another account owns it; ``unknown`` when its
    security or this process's user could not be read.

    ``default_owner_is_own`` (writers only) also counts as the current
    user's a file owned by this token's default owner: an elevated token's
    is BUILTIN\\Administrators, so a file this process wrote plainly is owned
    by it (measured 2026-10-18 on the Windows dev VM over SSH, High
    integrity, and on GitHub's Windows runners). Reading never does: an
    unelevated shim holds Administrators deny-only."""
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

    class TOKEN_OWNER(ctypes.Structure):
        _fields_ = [("Owner", ctypes.c_void_p)]

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
        return "unknown"
    try:
        token_handle = wintypes.HANDLE()
        if not open_token(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token_handle)):
            return "unknown"
        try:
            needed = wintypes.DWORD()
            get_token(token_handle, 1, None, 0, ctypes.byref(needed))
            if not needed.value:
                return "unknown"
            token_buffer = ctypes.create_string_buffer(needed.value)
            if not get_token(token_handle, 1, token_buffer, needed, ctypes.byref(needed)):
                return "unknown"
            current_owner = ctypes.cast(token_buffer, ctypes.POINTER(TOKEN_USER)).contents.User.Sid
            if not equal_sid(owner, current_owner):
                if not default_owner_is_own:
                    return "foreign"
                owner_needed = wintypes.DWORD()
                get_token(token_handle, 4, None, 0, ctypes.byref(owner_needed))  # TokenOwner
                if not owner_needed.value:
                    return "foreign"
                owner_buffer = ctypes.create_string_buffer(owner_needed.value)
                if not get_token(token_handle, 4, owner_buffer, owner_needed,
                                 ctypes.byref(owner_needed)):
                    return "foreign"
                default_owner = ctypes.cast(owner_buffer, ctypes.POINTER(TOKEN_OWNER)).contents.Owner
                if not equal_sid(owner, default_owner):
                    return "foreign"
        finally:
            kernel.CloseHandle(token_handle)
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            return "owned"
        if not (control.value & 0x1000):  # SE_DACL_PROTECTED
            return "owned"
        owner_allowed = False
        for index in range(dacl.contents.AceCount):
            ace = ctypes.c_void_p()
            if not get_ace(dacl, index, ctypes.byref(ace)):
                return "owned"
            header = ctypes.cast(ace, ctypes.POINTER(ACE_HEADER)).contents
            if header.AceType == 1:  # ACCESS_DENIED_ACE_TYPE
                continue
            if header.AceType != 0:  # Reject unfamiliar allow/object ACEs.
                return "owned"
            sid = ctypes.c_void_p(ace.value + 8)
            # The protected DACL writer uses the OWNER RIGHTS well-known SID,
            # which resolves to the current file owner without embedding an
            # account identifier in the descriptor.
            if not equal_sid(owner, sid) and not well_known_sid(sid, 71):
                return "owned"
            owner_allowed = True
        return "owner-only" if owner_allowed else "owned"
    finally:
        kernel.LocalFree(descriptor)


def _validate_regular(fd: int, *, allow_unlinked: bool = False) -> os.stat_result:
    info = os.fstat(fd)
    valid_links = info.st_nlink == 1 or (
        allow_unlinked and os.name != "nt" and info.st_nlink == 0)
    if not stat.S_ISREG(info.st_mode) or not valid_links:
        raise CredentialError("credential file must be a private regular file")
    return info


def _validate_file(fd: int, *, allow_unlinked: bool = False) -> os.stat_result:
    info = _validate_regular(fd, allow_unlinked=allow_unlinked)
    # Another account's file is told apart from a broad one: the fix for a
    # broad file (narrow its permissions) would lock this user out of a file
    # another account owns (review finding, 2026-10-18).
    if os.name == "nt":
        access = _windows_file_access(fd)
        if access == "foreign":
            raise CredentialError("credential file is owned by another account")
        if access != "owner-only":
            raise CredentialError("credential file must be owner-only")
    elif info.st_uid != os.geteuid():
        raise CredentialError("credential file is owned by another account")
    elif stat.S_IMODE(info.st_mode) & 0o077:
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

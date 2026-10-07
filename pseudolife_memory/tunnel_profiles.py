"""Private, resumable metadata for optional Secure MCP Tunnel access.

Secrets never belong to a profile. Windows key storage uses user-scoped DPAPI;
POSIX key storage uses owner-only files, so disk encryption remains a host choice.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from urllib.parse import urlsplit

from pseudolife_memory import credentials


class TunnelError(RuntimeError):
    """A sanitized failure suitable for command output."""


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', name):
        raise TunnelError('invalid tunnel profile name')
    if name.upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))}:
        raise TunnelError('invalid tunnel profile name')
    return name


def validate_url(url: str, *, local: bool = False) -> str:
    if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url) or '\\' in url:
        raise TunnelError('invalid tunnel endpoint')
    try:
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError
        parsed.port
        if parsed.scheme != 'https':
            if not local or parsed.scheme != 'http':
                raise ValueError
            if parsed.hostname != 'localhost' and not ipaddress.ip_address(parsed.hostname).is_loopback:
                raise ValueError
    except (ValueError, TypeError):
        raise TunnelError('endpoint requires HTTPS, or loopback HTTP for the local daemon') from None
    return url


def checked_path(path: str | Path) -> Path:
    target = Path(os.path.abspath(Path(path).expanduser()))
    try:
        credentials._reject_ancestor_redirects(target)
        if target.exists() or target.is_symlink():
            if credentials._is_redirect(target.lstat()):
                raise TunnelError('tunnel paths must not contain redirects')
    except (OSError, credentials.CredentialError):
        raise TunnelError('tunnel path cannot be inspected safely') from None
    return target


def _sharing_retry(call, *args):
    """Run ``call``, riding out a transient Windows sharing violation.

    A reader opened without delete sharing blocks ``os.replace`` of the
    record it reads, and a pending replace blocks a new open; either clears
    within milliseconds. Windows reports both as winerror 32 or 5, and 5 is
    also a genuine ACL denial, which is therefore retried for about 200 ms
    before it propagates. Any other error, and the last attempt, propagate.
    """
    for attempt in range(20):
        try:
            return call(*args)
        except PermissionError as exc:
            if getattr(exc, 'winerror', None) not in (5, 32) or attempt == 19:
                raise
            time.sleep(0.01)


def private_write(path: Path, data: bytes) -> None:
    target = checked_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    checked_path(target)
    if target.exists():
        private_validate(target)
    descriptor, temporary = tempfile.mkstemp(prefix='.tunnel-', dir=target.parent)
    staged = Path(temporary)
    try:
        if os.name == 'nt':
            credentials._secure_windows_file(staged)
        else:
            os.fchmod(descriptor, 0o600)
        credentials._validate_file(descriptor)
        with os.fdopen(descriptor, 'wb') as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        checked_path(target)
        _sharing_retry(os.replace, staged, target)
        if os.name != 'nt':
            parent_fd = os.open(target.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    except (OSError, credentials.CredentialError):
        raise TunnelError('private tunnel file could not be saved') from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        staged.unlink(missing_ok=True)


def private_validate(path: Path) -> None:
    target = checked_path(path)
    descriptor = -1
    try:
        descriptor = _sharing_retry(os.open, target, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        credentials._validate_file(descriptor)
    except (OSError, credentials.CredentialError):
        raise TunnelError('private tunnel file is unavailable or not owner-only') from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _windows_private_directory(path: Path) -> bool:
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x00020000, 7, None, 3, 0x02000000 | 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        return False
    descriptor = -1
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        return credentials._windows_owner_only(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        else:
            kernel.CloseHandle(handle)


def private_read(path: Path, limit: int = 65536) -> bytes:
    target = checked_path(path)
    descriptor = -1
    try:
        descriptor = _sharing_retry(os.open, target, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        credentials._validate_file(descriptor)
        data = os.read(descriptor, limit + 1)
        if len(data) > limit:
            raise TunnelError('private tunnel file is too large')
        return data
    except (OSError, credentials.CredentialError):
        raise TunnelError('private tunnel file is unavailable or not owner-only') from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise TunnelError('private tunnel key could not be protected or unlocked')
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


@dataclass(frozen=True)
class Profile:
    name: str
    daemon_url: str
    token_file: str
    tunnel_id: str | None = None
    organization_id: str | None = None
    state: str = 'pending'
    consent: bool = False
    autostart_consent: bool = False
    runtime_version: str | None = None
    minimum_catalog: str = 'current'
    runtime_key_expires_at: str | None = None
    cloud_verification_file: str | None = None

    def validate(self) -> None:
        validate_name(self.name)
        validate_url(self.daemon_url, local=True)
        if not isinstance(self.token_file, str) or not self.token_file or not Path(self.token_file).is_absolute():
            raise TunnelError('daemon credential reference requires an absolute path')
        checked_path(self.token_file)
        if self.tunnel_id is not None and (not isinstance(self.tunnel_id, str) or not re.fullmatch(r'tunnel_[a-fA-F0-9]+', self.tunnel_id)):
            raise TunnelError('invalid tunnel identifier')
        if self.organization_id is not None and (not isinstance(self.organization_id, str) or not re.fullmatch(r'org-[a-zA-Z0-9_-]+', self.organization_id)):
            raise TunnelError('invalid organization identifier')
        if not isinstance(self.state, str) or self.state not in {'pending', 'ready'} or type(self.consent) is not bool or type(self.autostart_consent) is not bool:
            raise TunnelError('invalid tunnel profile state')
        if self.state == 'ready' and (not self.consent or not self.tunnel_id):
            raise TunnelError('ready tunnel requires explicit consent and an identifier')
        if self.runtime_version is not None and (not isinstance(self.runtime_version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', self.runtime_version)):
            raise TunnelError('invalid runtime version')
        if not isinstance(self.minimum_catalog, str) or self.minimum_catalog not in {'current', 'full'}:
            raise TunnelError('invalid tunnel catalog selection')
        if self.runtime_key_expires_at is not None:
            parse_expiry(self.runtime_key_expires_at)
        if self.cloud_verification_file is not None:
            if not isinstance(self.cloud_verification_file, str) or not Path(self.cloud_verification_file).is_absolute():
                raise TunnelError('cloud verification reference requires an absolute path')
            checked_path(self.cloud_verification_file)


def default_root() -> Path:
    return Path.home() / '.pseudolife-mcp' / 'tunnel'


class ProfileStore:
    def __init__(self, root: str | Path | None = None):
        self.root = checked_path(root or default_root())

    def _prepare(self) -> None:
        existed = self.root.exists()
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        checked_path(self.root)
        if not self.root.is_dir():
            raise TunnelError('tunnel profile directory is unavailable')
        if os.name == 'nt':
            if not existed:
                credentials._secure_windows_file(self.root)
            if not _windows_private_directory(self.root):
                raise TunnelError('tunnel profile directory must be owner-only')
        elif self.root.stat().st_uid != os.geteuid() or stat.S_IMODE(self.root.stat().st_mode) & 0o077:
            raise TunnelError('tunnel profile directory must be owner-only')

    def profile_path(self, name: str) -> Path:
        return checked_path(self.root / (validate_name(name) + '.profile.json'))

    def key_path(self, name: str) -> Path:
        return checked_path(self.root / (validate_name(name) + '.key'))

    def save(self, profile: Profile) -> None:
        profile.validate()
        self._prepare()
        private_write(self.profile_path(profile.name), json.dumps(asdict(profile), indent=2).encode('utf-8'))

    def load(self, name: str) -> Profile:
        try:
            payload = json.loads(private_read(self.profile_path(name)))
            if not isinstance(payload, dict) or set(payload) - {f.name for f in fields(Profile)}:
                raise ValueError
            profile = Profile(**payload)
            profile.validate()
            if profile.name != name:
                raise ValueError
            return profile
        except (ValueError, TypeError, UnicodeError):
            raise TunnelError('tunnel profile is invalid') from None

    def list(self) -> list[str]:
        if not self.root.exists():
            return []
        self._prepare()
        return sorted(validate_name(path.name.removesuffix('.profile.json')) for path in self.root.glob('*.profile.json'))

    def set_key(self, name: str, key: str) -> None:
        try:
            encoded = key.encode('utf-8')
            credentials._decode_token(encoded)
        except (AttributeError, UnicodeError, credentials.CredentialError):
            raise TunnelError('invalid tunnel private key') from None
        self._prepare()
        data = b'DPAPI\0' + _dpapi(encoded) if os.name == 'nt' else b'PLAIN\0' + encoded
        private_write(self.key_path(name), data)

    def read_key(self, name: str) -> str:
        data = private_read(self.key_path(name), 16384)
        prefix, _, encoded = data.partition(b'\0')
        if os.name == 'nt' and prefix == b'DPAPI':
            encoded = _dpapi(encoded, decrypt=True)
        elif os.name != 'nt' and prefix == b'PLAIN':
            pass
        else:
            raise TunnelError('tunnel key protection does not match this host')
        try:
            return credentials._decode_token(encoded)
        except credentials.CredentialError:
            raise TunnelError('invalid tunnel private key') from None

    def import_key(self, name: str, source: str | Path) -> None:
        try:
            key = credentials.CredentialProvider(path=checked_path(source)).snapshot().token
        except credentials.CredentialError:
            raise TunnelError('private key input must be an owner-only token file') from None
        self.set_key(name, key)


def parse_expiry(value: str) -> datetime:
    try:
        expiry = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if expiry.tzinfo is None:
            if len(value) != 10:
                raise ValueError
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise TunnelError('key expiry requires an ISO date or timestamp with timezone') from None


def key_expiry(profile: Profile, *, now: datetime | None = None) -> str:
    if profile.runtime_key_expires_at is None:
        return 'unknown'
    remaining = (parse_expiry(profile.runtime_key_expires_at) - (now or datetime.now(timezone.utc))).total_seconds()
    if remaining <= 0:
        return 'expired'
    return 'near-expiry' if remaining < 7 * 86400 else 'valid'

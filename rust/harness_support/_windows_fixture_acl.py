"""Owner-only ACLs for files created in a disposable harness home."""
import ctypes
from ctypes import wintypes

_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x0001
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000004
_ERROR_ACCESS_DENIED = 5

_advapi = ctypes.WinDLL("advapi32", use_last_error=True)
_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_void = ctypes.c_void_p
_void_out = ctypes.POINTER(_void)
_dword_out = ctypes.POINTER(wintypes.DWORD)
_bool_out = ctypes.POINTER(wintypes.BOOL)


def _api(library, name, result, *arguments):
    function = getattr(library, name)
    function.argtypes = arguments
    function.restype = result
    return function


_current_process = _api(_kernel, "GetCurrentProcess", wintypes.HANDLE)
_close_handle = _api(_kernel, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
_local_free = _api(_kernel, "LocalFree", _void, _void)
_open_token = _api(_advapi, "OpenProcessToken", wintypes.BOOL,
                   wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
_get_token = _api(_advapi, "GetTokenInformation", wintypes.BOOL,
                  wintypes.HANDLE, ctypes.c_int, _void, wintypes.DWORD, _dword_out)
_convert_descriptor = _api(_advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                            wintypes.BOOL, wintypes.LPCWSTR, wintypes.DWORD, _void_out, _dword_out)
_get_dacl = _api(_advapi, "GetSecurityDescriptorDacl", wintypes.BOOL, _void, _bool_out, _void_out, _bool_out)
_get_security = _api(_advapi, "GetNamedSecurityInfoW", wintypes.DWORD,
                     wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                     _void_out, _void_out, _void_out, _void_out, _void_out)
_set_security = _api(_advapi, "SetNamedSecurityInfoW", wintypes.DWORD,
                     wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, _void, _void, _void, _void)
_equal_sid = _api(_advapi, "EqualSid", wintypes.BOOL, _void, _void)


class _TokenUser(ctypes.Structure):
    # TOKEN_USER contains one SID_AND_ATTRIBUTES.
    _fields_ = [("sid", _void), ("attributes", wintypes.DWORD)]


def _token_user_buffer():
    token = wintypes.HANDLE()
    if not _open_token(_current_process(), _TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError()
    try:
        needed = wintypes.DWORD()
        _get_token(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise ctypes.WinError()
        buffer = ctypes.create_string_buffer(needed.value)
        if not _get_token(token, _TOKEN_USER, buffer, needed, ctypes.byref(needed)):
            raise ctypes.WinError()
        return buffer
    finally:
        _close_handle(token)


def _owner_matches(path, user):
    owner, descriptor = _void(), _void()
    result = _get_security(str(path), _SE_FILE_OBJECT, _OWNER_SECURITY_INFORMATION,
                           ctypes.byref(owner), None, None, None, ctypes.byref(descriptor))
    try:
        return result == 0 and bool(owner) and bool(_equal_sid(owner, user))
    finally:
        if descriptor:
            _local_free(descriptor)


def secure_windows_file(path):
    """Assign TokenUser and keep only the owner ACE on a new fixture file."""
    token = _token_user_buffer()
    user = ctypes.cast(token, ctypes.POINTER(_TokenUser)).contents.sid
    descriptor = _void()
    if not _convert_descriptor("D:P(A;;FA;;;OW)", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError()
    try:
        present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), _void()
        if not _get_dacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
            raise ctypes.WinError()
        if not present.value or not dacl:
            raise OSError("fixture security descriptor does not contain a DACL")
        flags = _PROTECTED_DACL_SECURITY_INFORMATION | _OWNER_SECURITY_INFORMATION
        result = _set_security(str(path), _SE_FILE_OBJECT, flags, user, None, dacl, None)
        # Modify-only directories may deny setting ownership. Apply the
        # protected DACL alone only after proving the owner already is TokenUser.
        if result == _ERROR_ACCESS_DENIED and _owner_matches(path, user):
            result = _set_security(str(path), _SE_FILE_OBJECT, _PROTECTED_DACL_SECURITY_INFORMATION,
                                   None, None, dacl, None)
        if result:
            raise ctypes.WinError(result)
    finally:
        _local_free(descriptor)

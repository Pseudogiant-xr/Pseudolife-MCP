"""Windows held-handle locking, exact creation times and owned-run jobs."""
import ctypes
from ctypes import wintypes
import os

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_DATETIME_FILETIME_OFFSET = 504911232000000000

kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
kernel.CreateFileW.restype = wintypes.HANDLE
kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel.CloseHandle.restype = wintypes.BOOL
kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel.OpenProcess.restype = wintypes.HANDLE
kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4))
kernel.GetProcessTimes.restype = wintypes.BOOL


def try_lock(path):
    import msvcrt

    # Match File.Open(ReadWrite, FileShare.Read): readers may inspect the
    # record, while another writer cannot acquire the turn or delete it.
    handle = kernel.CreateFileW(str(path), 0x80000000 | 0x40000000, 1, None, 4, 0x80, None)
    if handle == _INVALID_HANDLE:
        error = ctypes.get_last_error()
        if error in (32, 33):  # Sharing/lock violation: an OS-held turn.
            return None
        raise ctypes.WinError(error)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    try:
        os.set_inheritable(fd, False)
        return os.fdopen(fd, "r+b", buffering=0)
    except BaseException:
        os.close(fd)
        raise


def _ticks(value):
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


def process_started(pid):
    handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION.
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # This PID no longer exists.
            return None
        raise ctypes.WinError(error)
    try:
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(system), ctypes.byref(user)):
            raise ctypes.WinError(ctypes.get_last_error())
        if _ticks(exited):
            return None
        return str(_ticks(created) + _DATETIME_FILETIME_OFFSET)
    finally:
        kernel.CloseHandle(handle)


class _BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD), ("minimum_working_set", ctypes.c_size_t),
                ("maximum_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("reads", "writes", "other", "read_bytes", "write_bytes", "other_bytes")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", _IoCounters),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


kernel.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
kernel.CreateJobObjectW.restype = wintypes.HANDLE
kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
kernel.SetInformationJobObject.restype = wintypes.BOOL
kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
kernel.AssignProcessToJobObject.restype = wintypes.BOOL
kernel.GetCurrentProcess.argtypes = ()
kernel.GetCurrentProcess.restype = wintypes.HANDLE
_lifetime_job = None


def contain_process_tree():
    global _lifetime_job
    if _lifetime_job is not None:
        return
    job = kernel.CreateJobObjectW(None, None)  # Handle is not inherited.
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = _ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.get_last_error()
        kernel.CloseHandle(job)
        raise ctypes.WinError(error)
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel.CloseHandle(job)
        raise ctypes.WinError(error)
    # Do not close or finalize this job while the owner runs: it contains
    # the owner too. OS closure on process death ends remaining descendants.
    _lifetime_job = job

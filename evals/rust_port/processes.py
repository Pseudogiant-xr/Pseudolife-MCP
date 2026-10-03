"""Own a subprocess tree before it runs, and reclaim it before returning."""
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import time


def execution_sources():
    paths = [Path(__file__)]
    if os.name == "nt":
        from pseudolife_memory import codex_doorbell
        paths.append(Path(codex_doorbell.__file__))
    return paths


def _windows_job():
    # Reuse the project's native signatures, but never its uncontained fallback.
    from pseudolife_memory import codex_doorbell as bindings
    job = bindings._KillJob.create()
    if job is None:
        raise OSError("Windows subprocess job ownership unavailable")
    return bindings, job


def _windows_adopt(bindings, job, process):
    handle = int(process._handle)
    if not bindings._kernel32.AssignProcessToJobObject(job._handle, handle):
        # Still suspended: it has not had an opportunity to create descendants.
        raise OSError("Windows subprocess job assignment unavailable")
    if bindings._ntdll.NtResumeProcess(handle) < 0:
        raise OSError("Windows owned subprocess resume failed")


def _windows_active(job):
    import ctypes
    from ctypes import wintypes

    # SDK layout and information class 1:
    # https://learn.microsoft.com/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information
    class Accounting(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in (
            "TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")]
        _fields_ += [(name, wintypes.DWORD) for name in (
            "TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    query = kernel.QueryInformationJobObject
    query.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                      ctypes.POINTER(wintypes.DWORD))
    query.restype = wintypes.BOOL
    value = Accounting()
    if not query(job._handle, 1, ctypes.byref(value), ctypes.sizeof(value), None):
        raise OSError("Windows owned subprocess accounting unavailable")
    return value.ActiveProcesses


def _posix_active(group):
    import psutil
    for process in psutil.process_iter(["pid", "status"]):
        try:
            if os.getpgid(process.pid) == group and process.status() != psutil.STATUS_ZOMBIE:
                return True
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
    return False


def _windows_membership_api():
    import ctypes
    from ctypes import wintypes
    from pseudolife_memory.codex_doorbell import _kernel32
    signatures = {
        "OpenProcess": ((wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), wintypes.HANDLE),
        "GetProcessId": ((wintypes.HANDLE,), wintypes.DWORD),
        "IsProcessInJob": ((wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)), wintypes.BOOL),
        "GetExitCodeProcess": ((wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)), wintypes.BOOL),
        "WaitForSingleObject": ((wintypes.HANDLE, wintypes.DWORD), wintypes.DWORD),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(_kernel32, name)
        function.argtypes, function.restype = arguments, result
    return _kernel32


def _windows_owned_alive(job, pid):
    import ctypes
    from ctypes import wintypes
    kernel = _windows_membership_api()
    # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE: query this exact Job
    # and check process signaling without inheriting the temporary handle.
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        return False
    valid = False
    try:
        member, exit_code = wintypes.BOOL(), wintypes.DWORD()
        valid = bool(kernel.GetProcessId(handle) == pid
                     and kernel.IsProcessInJob(handle, job._handle, ctypes.byref(member)) and member.value
                     and kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and exit_code.value == 259
                     and kernel.WaitForSingleObject(handle, 0) == 258)
        # A terminated process can itself return 259; only an unsignaled process
        # handle proves liveness. All native query failures refuse membership.
    finally:
        if not kernel.CloseHandle(handle):
            valid = False
    return valid


def _posix_owned_alive(group, pid):
    import psutil
    try:
        if os.getpgid(pid) != group:
            return False
        os.kill(pid, 0)
        return (psutil.Process(pid).status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
                and os.getpgid(pid) == group)
    except (OSError, psutil.NoSuchProcess, psutil.AccessDenied):
        return False


@contextmanager
def owned_process(command, *, cwd, env, stdin=subprocess.DEVNULL,
                  stdout=subprocess.PIPE, stderr=subprocess.PIPE):
    """Yield Popen; successful exit proves its owned tree has stopped.

    Windows jobs do not permit breakaway. POSIX ownership covers the new process
    group; candidates deliberately creating another session are unsupported.
    Ownership failures raise instead of falling back to a racy descendant walk.
    """
    process = None
    job = None
    ownership_open = False
    options = {}
    if os.name == "nt":
        bindings, job = _windows_job()
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | bindings._CREATE_SUSPENDED
    elif os.name == "posix":
        options["start_new_session"] = True
    else:
        raise OSError("subprocess tree ownership unsupported on this platform")
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=stdin,
                                   stdout=stdout, stderr=stderr, **options)
        process.owned_cleanup = {"process_stopped": False, "subtree_stopped": False}
        if job is not None:
            _windows_adopt(bindings, job, process)
        ownership_open = True

        def owns_runtime_pid(pid):
            # No coercion: booleans, strings and overflowing Windows DWORDs
            # cannot identify a different process through native truncation.
            maximum_pid = 0xFFFFFFFF if job is not None else 0x7FFFFFFF
            if not ownership_open or type(pid) is not int or not 0 < pid <= maximum_pid:
                return False
            try:
                return (_windows_owned_alive(job, pid) if job is not None
                        else _posix_owned_alive(process.pid, pid))
            except (OSError, AttributeError, OverflowError, ValueError):
                return False

        process.owns_runtime_pid = owns_runtime_pid
        yield process
    finally:
        ownership_open = False
        try:
            if process is not None:
                if job is not None:
                    if not job.terminate():
                        raise OSError("Windows owned subprocess termination failed")
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                # Also covers a suspended child whose job assignment failed.
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                deadline = time.monotonic() + 5
                while (_windows_active(job) if job is not None else _posix_active(process.pid)):
                    if time.monotonic() >= deadline:
                        raise OSError("owned subprocess subtree did not stop")
                    time.sleep(0.01)
                process.owned_cleanup.update(process_stopped=True, subtree_stopped=True)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
        finally:
            if process is not None and os.name == "nt" and process.returncode is not None:
                # A retained Popen must not retain the terminated kernel object.
                process._handle.Close()
            if job is not None:
                job.close()

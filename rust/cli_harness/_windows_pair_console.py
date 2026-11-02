"""Exercise a pairing prompt inside this process's disposable Windows console."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def exercise(command, token, action):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class Coord(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class ScreenInfo(ctypes.Structure):
        _fields_ = [("size", Coord), ("cursor", Coord), ("attributes", wintypes.WORD),
                    ("window", wintypes.SMALL_RECT), ("maximum", Coord)]

    class KeyEvent(ctypes.Structure):
        _fields_ = [("down", wintypes.BOOL), ("repeat", wintypes.WORD),
                    ("key", wintypes.WORD), ("scan", wintypes.WORD),
                    ("char", wintypes.WCHAR), ("control", wintypes.DWORD)]

    class Event(ctypes.Union):
        _fields_ = [("key", KeyEvent), ("padding", ctypes.c_byte * 16)]

    class InputRecord(ctypes.Structure):
        _fields_ = [("kind", wintypes.WORD), ("event", Event)]

    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ScreenInfo)]
    kernel.ReadConsoleOutputCharacterW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
                                                  wintypes.DWORD, Coord,
                                                  ctypes.POINTER(wintypes.DWORD)]
    kernel.WriteConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(InputRecord),
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    kernel.SetConsoleCtrlHandler.argtypes = [handler_type, wintypes.BOOL]
    kernel.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE

    def checked(value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())
        return value

    # A handler table is local to this supervisor; children retain their own
    # Ctrl-C behavior. Never set the inheritable "ignore Ctrl-C" attribute.
    checked(kernel.SetConsoleCtrlHandler(handler_type(), False))
    handler = handler_type(lambda event: event == 0)
    checked(kernel.SetConsoleCtrlHandler(handler, True))
    pids = (wintypes.DWORD * 8)()
    count = kernel.GetConsoleProcessList(pids, len(pids))
    assert count == 1 and pids[0] == os.getpid(), "console is not isolated"

    import msvcrt
    # Python's WindowsConsoleIO rejects reading CONOUT$, but the screen APIs
    # require a handle opened for both reading and writing.
    output_handle = kernel.CreateFileW("CONOUT$", 0xC0000000, 3, None, 3, 0, None)
    assert output_handle != wintypes.HANDLE(-1).value
    output_fd = msvcrt.open_osfhandle(output_handle, os.O_WRONLY | os.O_BINARY)
    with open("CONIN$", "r+b", buffering=0) as console_in, \
            os.fdopen(output_fd, "wb", buffering=0) as console_out:
        input_handle = msvcrt.get_osfhandle(console_in.fileno())
        output_handle = msvcrt.get_osfhandle(console_out.fileno())

        def mode():
            value = wintypes.DWORD()
            checked(kernel.GetConsoleMode(input_handle, ctypes.byref(value)))
            return value.value

        def screen():
            info = ScreenInfo()
            checked(kernel.GetConsoleScreenBufferInfo(output_handle, ctypes.byref(info)))
            text = ctypes.create_unicode_buffer(info.size.X * info.size.Y)
            read = wintypes.DWORD()
            checked(kernel.ReadConsoleOutputCharacterW(
                output_handle, text, len(text), Coord(0, 0), ctypes.byref(read)))
            return text[:read.value]

        def write(text):
            records = (InputRecord * len(text))()
            for record, char in zip(records, text):
                record.kind = 1  # KEY_EVENT
                record.event.key = KeyEvent(True, 1, 0, 0, char, 0)
            written = wintypes.DWORD()
            checked(kernel.WriteConsoleInputW(input_handle, records, len(records),
                                              ctypes.byref(written)))
            assert written.value == len(records)

        before = mode()
        if action == "setup-refusal":
            with token.open("wb") as errors:
                child = subprocess.Popen(command, stdin=console_in, stdout=console_out,
                                         stderr=errors)
                try:
                    code = child.wait(timeout=20)
                    return {"exit_code": code, "stdout": screen(),
                            "stderr": token.read_text(encoding="utf-8"),
                            "isolated_console": True}
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=10)
        child = subprocess.Popen(command, stdin=console_in, stdout=console_out,
                                 stderr=console_out)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if "pairing code: " in screen() and not mode() & 4:
                    break
                assert child.poll() is None, "candidate exited before prompting"
                time.sleep(0.01)
            else:
                raise AssertionError("candidate did not hide echo and prompt")
            during = mode()
            if action == "interrupt":
                # A real Ctrl-C is a control event in processed mode and a
                # character in raw mode. Target only our new console.
                if during & 1:
                    checked(kernel.GenerateConsoleCtrlEvent(0, 0))
                else:
                    write("\x03")
            else:
                write("x\b\r")
            try:
                exit_code = child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                return {"before": before, "during": during, "after": mode(),
                        "exit_code": None, "token_exists": token.exists(),
                        "empty_code": False, "isolated_console": True}
            output = screen()
            return {"before": before, "during": during, "after": mode(),
                    "exit_code": exit_code, "token_exists": token.exists(),
                    "empty_code": "--read-code read no code on stdin" in output,
                    "isolated_console": True}
        finally:
            # Popen's retained process handle proves ownership even if its PID
            # is later reused; never signal another console or process group.
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            checked(kernel.SetConsoleMode(input_handle, before))


if __name__ == "__main__":
    result_path, token_path, action, *command = sys.argv[1:]
    try:
        result = exercise(command, Path(token_path), action)
    except BaseException as error:
        result = {"error": f"{type(error).__name__}: {error}"}
    Path(result_path).write_text(json.dumps(result), encoding="utf-8")

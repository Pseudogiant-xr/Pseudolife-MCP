"""Real Python-shim timeout cleanup, using disposable providers only."""
from __future__ import annotations

import concurrent.futures
import ctypes
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
# Readiness, socket operations, kill completion and reap each have a ten-second
# harness hang guard. None is an assertion about provider or taskkill latency.
GUARD = 10

CHILD = r'''
import os, socket, sys
with socket.create_connection(('127.0.0.1', int(sys.argv[1]))) as witness:
    witness.sendall((str(os.getpid()) + '\n').encode())
    with witness.makefile('rb') as commands:
        for command in commands:
            if command == b'RELEASE\n':
                break
            if command == b'PING\n':
                witness.sendall(b'PONG\n')
'''
PROVIDER = r'''
import subprocess, sys
child = subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])
child.wait()
'''


class ProcessWitness:
    """Retain a Windows process handle; never send Windows signal zero."""

    def __init__(self, pid):
        self.pid = pid
        self.handle = None
        if os.name == 'nt':
            self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            self.kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            self.kernel.OpenProcess.restype = ctypes.c_void_p
            self.kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            self.kernel.WaitForSingleObject.restype = ctypes.c_uint32
            self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            self.handle = self.kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())

    def alive(self):
        if self.handle is not None:
            result = self.kernel.WaitForSingleObject(self.handle, 0)
            if result == 0xffffffff:
                raise ctypes.WinError(ctypes.get_last_error())
            return result == 0x102  # WAIT_TIMEOUT
        try:
            os.kill(self.pid, 0)  # POSIX only.
        except ProcessLookupError:
            return False
        # An orphan may await its init process's reap after it has exited.
        stat = Path(f'/proc/{self.pid}/stat')
        try:
            return stat.read_text().rsplit(')', 1)[1].split()[0] != 'Z'
        except (FileNotFoundError, ProcessLookupError):
            return os.path.exists('/proc') is False

    def wait_ended(self):
        deadline = time.monotonic() + GUARD
        while self.alive():
            if time.monotonic() >= deadline:
                raise AssertionError('owned descendant did not exit after cleanup')
            time.sleep(.01)

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def root_identity(proc):
    if os.name != 'nt':
        # Popen owns this unreaped child, so its PID cannot have been reused.
        return proc.pid
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] * 5
    kernel.GetProcessTimes.restype = ctypes.c_int
    created, exited, system, user = (ctypes.c_uint64() for _ in range(4))
    if not kernel.GetProcessTimes(int(proc._handle), ctypes.byref(created),
                                 ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
        raise ctypes.WinError(ctypes.get_last_error())
    return created.value


def require_closed(connection, reader):
    try:
        connection.sendall(b'PING\n')
        reply = reader.readline()
    except ConnectionError:
        return  # Windows can reset the socket when taskkill terminates its owner.
    if reply:
        raise AssertionError(f'descendant survived timeout: {reply!r}')


class TimeoutLifecycle(unittest.TestCase):
    def exercise(self, kind, mode):
        spec = importlib.util.spec_from_file_location(
            f'lifecycle_{kind}', ROOT / 'evals' / f'{kind}_shim.py')
        shim = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shim)
        cli_type = shim.ClaudeCli if kind == 'claude' else shim.CodexCli
        cli = cli_type(Path('unused-fixture-cli'), 'fixture', .05)
        real_popen, real_kill = subprocess.Popen, shim._kill_tree
        state = {}
        killed = threading.Event()
        pool = concurrent.futures.ThreadPoolExecutor(1)
        future = None
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            listener.settimeout(GUARD)
            argv = [sys.executable, '-c', PROVIDER, CHILD, str(listener.getsockname()[1])]

            def ready_popen(args, *positional, **kwargs):
                proc = real_popen(args, *positional, **kwargs)
                if args == argv:
                    state['root'] = proc
                    state['identity'] = root_identity(proc)
                    connection, _ = listener.accept()
                    state['connection'] = connection
                    connection.settimeout(GUARD)
                    reader = connection.makefile('rb')
                    state['reader'] = reader
                    state['child'] = ProcessWitness(int(reader.readline()))
                    self.assertTrue(state['child'].alive(), 'descendant was not ready')
                    # _run starts communicate(timeout=...) only after this returns.
                return proc

            def kill(proc):
                self.assertIs(proc, state['root'])
                self.assertIsNone(proc.poll())
                self.assertEqual(root_identity(proc), state['identity'])
                if mode == 'delayed':
                    # Deliberately exceed the former HTTP .75s inference while
                    # retaining exactly the same termination/reap requirements.
                    time.sleep(.9)
                if mode == 'root-only':
                    proc.kill()
                else:
                    real_kill(proc)
                killed.set()

            try:
                with mock.patch.object(shim.subprocess, 'Popen', ready_popen), \
                        mock.patch.object(shim, '_kill_tree', kill):
                    future = pool.submit(cli._run, argv, b'')
                    self.assertTrue(killed.wait(GUARD), 'timeout kill did not complete (harness guard)')
                    if mode == 'root-only':
                        with self.assertRaisesRegex(AssertionError, 'descendant survived timeout'):
                            require_closed(state['connection'], state['reader'])
                        self.assertFalse(future.done(), 'root-only mutation did not hold the pipes')
                        state['connection'].sendall(b'RELEASE\n')
                    else:
                        require_closed(state['connection'], state['reader'])
                    with self.assertRaises(subprocess.TimeoutExpired):
                        future.result(timeout=GUARD)
                    self.assertIsNotNone(state['root'].poll(), 'owned provider was not reaped')
                    state['child'].wait_ended()
            finally:
                # Release the descendant even when a mutant or assertion fails.
                connection = state.get('connection')
                if connection is not None:
                    try:
                        connection.sendall(b'RELEASE\n')
                    except OSError:
                        pass
                proc = state.get('root')
                if proc is not None and proc.poll() is None:
                    self.assertEqual(root_identity(proc), state['identity'])
                    real_kill(proc)
                    proc.wait(timeout=GUARD)
                try:
                    if future is not None:
                        try:
                            future.result(timeout=GUARD)
                        except subprocess.TimeoutExpired:
                            pass
                    if 'child' in state:
                        state['child'].wait_ended()
                finally:
                    if 'reader' in state:
                        state['reader'].close()
                    if connection is not None:
                        connection.close()
                    if 'child' in state:
                        state['child'].close()
                    pool.shutdown(wait=False)

    def test_tree_kill_terminates_descendant_and_reaps_pipes(self):
        for kind in ('claude', 'codex'):
            with self.subTest(kind=kind):
                self.exercise(kind, 'tree')

    def test_slow_tree_kill_still_terminates_descendant_and_reaps_pipes(self):
        for kind in ('claude', 'codex'):
            with self.subTest(kind=kind):
                self.exercise(kind, 'delayed')

    def test_root_only_mutation_is_detected_and_released(self):
        for kind in ('claude', 'codex'):
            with self.subTest(kind=kind):
                self.exercise(kind, 'root-only')


if __name__ == '__main__':
    unittest.main()

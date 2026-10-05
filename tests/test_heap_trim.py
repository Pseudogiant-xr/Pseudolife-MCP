"""The daemon's idle heap trim (pseudolife_memory/utils/heap_trim.py).

2026-09-23: after concurrent embedder encodes on persistent worker threads,
glibc kept over a gigabyte of freed memory resident, still there after the
burst was over (evals/results/allocator-trim-pool-20260923.json). The
daemon now calls malloc_trim(0) from a background thread once a minute.
"""
from __future__ import annotations

import ast
import logging
import sys
from pathlib import Path

import pytest

from pseudolife_memory.utils import heap_trim

MB = 1024 * 1024
REPO = Path(__file__).resolve().parents[1]


class _Heap:
    """Resident anon bytes; a trim drops them to ``after`` when set."""

    def __init__(self, anon: int) -> None:
        self.anon = anon
        self.after: int | None = None
        self.trims: list[int] = []

    def read(self) -> int | None:
        return self.anon

    def trim(self, pad: int) -> int:
        self.trims.append(pad)
        if self.after is not None:
            self.anon, self.after = self.after, None
        return 1


def test_a_trim_reports_what_it_returned() -> None:
    heap = _Heap(3000 * MB)
    heap.after = 2600 * MB
    assert heap_trim.trim_once(heap.trim, heap.read) == 400 * MB
    assert heap.trims == [0]


def test_every_check_trims() -> None:
    heap = _Heap(2600 * MB)
    for _ in range(3):
        assert heap_trim.trim_once(heap.trim, heap.read) == 0
    assert heap.trims == [0, 0, 0]


def test_a_check_during_a_burst_does_not_stop_the_next_trim() -> None:
    """Review finding (2026-09-23): a growth gate whose floor was set by a
    check landing mid-burst — live tensors, nothing to return — never
    trimmed the retention the burst left behind. Trimming on every check
    has no floor to poison."""
    heap = _Heap(3900 * MB)            # mid-burst: all of it live
    assert heap_trim.trim_once(heap.trim, heap.read) == 0
    heap.anon = 3931 * MB              # burst over: 1,231 MiB freed, kept
    heap.after = 2700 * MB
    assert heap_trim.trim_once(heap.trim, heap.read) == 1231 * MB


def test_unreadable_anon_still_trims() -> None:
    trims: list[int] = []
    assert heap_trim.trim_once(lambda pad: trims.append(pad) or 1,
                               lambda: None) is None
    assert trims == [0]


class _Stop(Exception):
    pass


def _run_loop(monkeypatch, returns: list) -> list[float]:
    """Drive _trim_loop through len(returns) checks, then stop it."""
    sleeps: list[float] = []
    pending = list(returns)

    def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if not pending:
            raise _Stop

    def _trim_once(malloc_trim, read_anon=None):
        outcome = pending.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(heap_trim.time, "sleep", _sleep)
    monkeypatch.setattr(heap_trim, "trim_once", _trim_once)
    with pytest.raises(_Stop):
        heap_trim._trim_loop(60.0, lambda pad: 1)
    return sleeps


def test_a_failing_check_does_not_end_the_loop(monkeypatch) -> None:
    sleeps = _run_loop(monkeypatch, [RuntimeError("proc unreadable"), 0])
    assert sleeps == [60.0, 60.0, 60.0]


def test_only_a_worthwhile_return_is_logged(monkeypatch, caplog) -> None:
    """Small, zero, negative (memory allocated during the trim) and unknown
    returns stay out of the log; a real one says how much."""
    with caplog.at_level(logging.INFO, logger=heap_trim.logger.name):
        _run_loop(monkeypatch, [heap_trim.LOG_MIN_BYTES - 1, 0, -5 * MB, None,
                                726 * MB])
    lines = [r.getMessage() for r in caplog.records]
    assert lines == ["heap trim: returned 726 MiB of freed heap to the OS"]


class _FakeLibc:
    def __init__(self, name: str) -> None:
        self.malloc_trim = lambda pad: 1


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_no_trim_function_off_linux(monkeypatch, platform: str) -> None:
    """Even where some library would load, only Linux gets a trimmer."""
    monkeypatch.setattr(heap_trim.sys, "platform", platform)
    monkeypatch.setattr(heap_trim.ctypes, "CDLL", _FakeLibc)
    assert heap_trim.load_malloc_trim() is None


def test_no_trim_function_without_glibc(monkeypatch) -> None:
    """musl has no libc.so.6 (and no malloc_trim)."""
    monkeypatch.setattr(heap_trim.sys, "platform", "linux")

    def _missing(name: str):
        raise OSError(f"{name}: cannot open shared object file")

    monkeypatch.setattr(heap_trim.ctypes, "CDLL", _missing)
    assert heap_trim.load_malloc_trim() is None


def test_no_trim_function_when_libc_lacks_it(monkeypatch) -> None:
    class _NoTrim:
        def __init__(self, name: str) -> None:
            pass

    monkeypatch.setattr(heap_trim.sys, "platform", "linux")
    monkeypatch.setattr(heap_trim.ctypes, "CDLL", _NoTrim)
    assert heap_trim.load_malloc_trim() is None


class _FakeThread:
    made: list[dict] = []

    def __init__(self, *args, **kwargs) -> None:
        _FakeThread.made.append(kwargs)

    def start(self) -> None:
        pass


def _trim(pad: int) -> int:
    return 1


@pytest.fixture
def fake_start(monkeypatch):
    _FakeThread.made = []
    monkeypatch.setattr(heap_trim.threading, "Thread", _FakeThread)
    monkeypatch.setattr(heap_trim, "_started", False)
    monkeypatch.setattr(heap_trim, "load_malloc_trim", lambda: _trim)
    monkeypatch.delenv(heap_trim.INTERVAL_ENV, raising=False)
    return _FakeThread.made


def test_start_runs_one_daemon_thread_once(fake_start) -> None:
    assert heap_trim.start() is True
    assert heap_trim.start() is False
    assert len(fake_start) == 1
    kwargs = fake_start[0]
    assert kwargs["target"] is heap_trim._trim_loop
    assert kwargs["args"] == (heap_trim.DEFAULT_INTERVAL_SECONDS, _trim)
    assert kwargs["name"] == "pl-heap-trim"
    assert kwargs["daemon"] is True


def test_start_reads_the_interval_from_the_env(fake_start, monkeypatch) -> None:
    monkeypatch.setenv(heap_trim.INTERVAL_ENV, "15")
    assert heap_trim.start() is True
    assert fake_start[0]["args"][0] == 15.0


def test_an_empty_interval_means_the_default(fake_start, monkeypatch) -> None:
    """Compose passes an unset ops/.env value as an empty string."""
    monkeypatch.setenv(heap_trim.INTERVAL_ENV, "")
    assert heap_trim.start() is True
    assert fake_start[0]["args"][0] == heap_trim.DEFAULT_INTERVAL_SECONDS


@pytest.mark.parametrize("raw", ["one minute", "nan", "inf"])
def test_a_malformed_interval_names_the_variable(fake_start, monkeypatch,
                                                 raw: str) -> None:
    """nan/inf would pass a "<= 0" check and kill the thread at its first
    sleep after start() had already announced it."""
    monkeypatch.setenv(heap_trim.INTERVAL_ENV, raw)
    with pytest.raises(ValueError, match=heap_trim.INTERVAL_ENV):
        heap_trim.start()
    assert fake_start == []


def test_a_zero_interval_disables_it(fake_start, monkeypatch) -> None:
    monkeypatch.setenv(heap_trim.INTERVAL_ENV, "0")
    assert heap_trim.start() is False
    assert fake_start == []


def test_no_thread_without_glibc(fake_start, monkeypatch) -> None:
    monkeypatch.setattr(heap_trim, "load_malloc_trim", lambda: None)
    assert heap_trim.start() is False
    assert fake_start == []


def test_the_daemon_starts_the_trimmer_before_serving() -> None:
    """run_daemon() is not exercised end to end (uvicorn blocks), so pin the
    call — and that it precedes the blocking uvicorn.run."""
    tree = ast.parse((REPO / "pseudolife_memory" / "daemon.py").read_text(
        encoding="utf-8"))
    run = next(node for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef) and node.name == "run_daemon")
    lines = {}
    for node in ast.walk(run):
        if isinstance(node, ast.Call):
            lines.setdefault(ast.unparse(node.func), node.lineno)
    assert "heap_trim.start" in lines
    assert lines["heap_trim.start"] < lines["uvicorn.run"]


def test_the_docker_daemon_can_be_given_the_interval() -> None:
    """Compose passes only the variables it lists; without this entry an
    operator could not disable or retune the trim on the Docker tier."""
    import yaml

    stack = yaml.safe_load((REPO / "ops" / "docker-compose.yml").read_text(
        encoding="utf-8"))
    env = stack["services"]["pseudolife-daemon"]["environment"]
    assert env[heap_trim.INTERVAL_ENV] == "${PSEUDOLIFE_MALLOC_TRIM_SECONDS:-}"
    template = (REPO / "ops" / ".env.example").read_text(encoding="utf-8")
    assert f"#{heap_trim.INTERVAL_ENV}=\n" in template


def test_the_config_doc_states_the_code_default() -> None:
    row = next(line for line in (REPO / "docs" / "guide" / "configuration.md")
               .read_text(encoding="utf-8").splitlines()
               if line.startswith(f"| `{heap_trim.INTERVAL_ENV}`"))
    assert f"| `{heap_trim.DEFAULT_INTERVAL_SECONDS:.0f}` |" in row


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="glibc malloc_trim exists only on Linux")
def test_real_glibc_trim_runs() -> None:
    trim = heap_trim.load_malloc_trim()
    if trim is None:
        pytest.skip("not glibc")
    assert heap_trim.trim_once(trim) is not None
    assert heap_trim.read_anon_bytes() > 0

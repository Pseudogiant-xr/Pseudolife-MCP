"""/health reports how close the daemon sits to its memory limit.

2026-09-23: the production daemon was cgroup OOM-killed while /health said
"ok". It lived at ~95% of its 4 GiB cap, and after the restart the kernel's
own ``memory.events`` ``max`` counter reached 8,021 within ~27 minutes as
reclaim squeezed hot library pages (15.8 s searches). Nothing the daemon
published showed any of it. Silence read as health.

Contract pinned here:

* inside a cgroup v2 limit, /health carries ``memory``: current, limit,
  used fraction, anon/file split, the ``memory.events`` max / oom /
  oom_kill counters, and the process RSS and its peak;
* the cgroup is found through ``/proc/self/cgroup`` (a nested host cgroup)
  or at the root (a container's private cgroup namespace);
* with no cgroup limit readable it falls back to process RSS, and says
  ``unavailable`` rather than omitting the block when nothing is readable;
* ``near_limit`` turns true at 90% of the limit, and the daemon logs a
  rate-limited WARNING — but ``status`` is never touched: a non-ok payload
  is an HTTP 503 that the Docker healthcheck and ops/update.* treat as
  fatal;
* /health also names the embedder's backend, device and resolved dtype, so
  "verify live" can confirm bf16 after a deploy.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from pseudolife_memory.utils import memory_headroom as mh

GIB = 1024 ** 3


def _cgroup(root: Path, *, current: int, limit: str,
            events: str = "low 0\nhigh 0\nmax 8123\noom 3\noom_kill 1\n"
                          "oom_group_kill 0\n",
            stat: str = "anon 3300000000\nfile 600000000\nkernel 1000\n") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "memory.current").write_text(f"{current}\n", encoding="utf-8")
    (root / "memory.max").write_text(f"{limit}\n", encoding="utf-8")
    (root / "memory.events").write_text(events, encoding="utf-8")
    (root / "memory.stat").write_text(stat, encoding="utf-8")
    return root


def _proc(root: Path, *, cgroup_line: str = "0::/\n",
          status: str = "Name:\tpython\nVmHWM:\t 3800000 kB\n"
                        "VmRSS:\t 3000000 kB\n") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if cgroup_line is not None:
        (root / "cgroup").write_text(cgroup_line, encoding="utf-8")
    if status is not None:
        (root / "status").write_text(status, encoding="utf-8")
    return root


def test_reads_a_container_cgroup_v2_limit(tmp_path: Path) -> None:
    cg = _cgroup(tmp_path / "cg", current=int(3.9 * GIB), limit=str(4 * GIB))
    proc = _proc(tmp_path / "proc")
    got = mh.read_memory_headroom(cgroup_root=cg, proc_self=proc)

    assert got["source"] == "cgroup"
    assert got["current_bytes"] == int(3.9 * GIB)
    assert got["limit_bytes"] == 4 * GIB
    assert got["used_fraction"] == pytest.approx(0.975, abs=1e-3)
    assert got["near_limit"] is True
    assert got["events"] == {"max": 8123, "oom": 3, "oom_kill": 1}
    assert got["anon_bytes"] == 3_300_000_000
    assert got["file_bytes"] == 600_000_000
    assert got["rss_bytes"] == 3_000_000 * 1024
    assert got["rss_peak_bytes"] == 3_800_000 * 1024


def test_below_ninety_percent_is_not_near_the_limit(tmp_path: Path) -> None:
    cg = _cgroup(tmp_path / "cg", current=int(2.0 * GIB), limit=str(6 * GIB))
    got = mh.read_memory_headroom(cgroup_root=cg, proc_self=_proc(tmp_path / "p"))
    assert got["near_limit"] is False
    assert got["used_fraction"] == pytest.approx(1 / 3, abs=1e-3)


def test_the_threshold_is_ninety_percent() -> None:
    assert mh.NEAR_LIMIT_FRACTION == 0.90


def test_an_unlimited_cgroup_reports_no_fraction(tmp_path: Path) -> None:
    cg = _cgroup(tmp_path / "cg", current=int(2.0 * GIB), limit="max")
    got = mh.read_memory_headroom(cgroup_root=cg, proc_self=_proc(tmp_path / "p"))
    assert got["source"] == "cgroup"
    assert got["limit_bytes"] is None
    assert got["used_fraction"] is None
    assert got["near_limit"] is None


def test_finds_a_nested_host_cgroup_through_proc_self(tmp_path: Path) -> None:
    root = tmp_path / "sys-fs-cgroup"
    root.mkdir()
    _cgroup(root / "system.slice" / "pseudolife.service",
            current=int(1.0 * GIB), limit=str(2 * GIB))
    proc = _proc(tmp_path / "proc",
                 cgroup_line="0::/system.slice/pseudolife.service\n")
    got = mh.read_memory_headroom(cgroup_root=root, proc_self=proc)
    assert got["source"] == "cgroup"
    assert got["limit_bytes"] == 2 * GIB


def test_falls_back_to_process_rss_without_a_cgroup(tmp_path: Path) -> None:
    got = mh.read_memory_headroom(cgroup_root=tmp_path / "absent",
                                  proc_self=_proc(tmp_path / "proc"))
    assert got["source"] == "process"
    assert got["rss_bytes"] == 3_000_000 * 1024
    assert got["near_limit"] is None


def test_says_unavailable_rather_than_going_quiet(tmp_path: Path) -> None:
    got = mh.read_memory_headroom(cgroup_root=tmp_path / "absent",
                                  proc_self=tmp_path / "absent-proc")
    assert got == {"source": "unavailable"}


# ── /health ────────────────────────────────────────────────────────────────


class _Svc:
    _db_url = "postgresql://fake"
    _persist_errors = 0
    _init_refusal = None
    _storage = None
    _embedder = None


class _Embedder:
    def describe(self) -> dict:
        return {"backend": "torch", "device": "cpu", "dtype": "bf16"}


def _near(monkeypatch: pytest.MonkeyPatch, near: bool) -> dict:
    reading = {"source": "cgroup", "current_bytes": int(3.9 * GIB),
               "limit_bytes": 4 * GIB, "used_fraction": 0.975,
               "near_limit": near, "events": {"max": 8123, "oom": 3,
                                              "oom_kill": 1}}
    monkeypatch.setattr(mh, "read_memory_headroom", lambda: dict(reading))
    return reading


def test_health_carries_the_memory_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    from pseudolife_memory.daemon import _build_health_payload

    reading = _near(monkeypatch, near=False)
    payload = _build_health_payload(_Svc(), token_present=False)
    assert payload["memory"] == reading


def test_near_the_limit_warns_but_never_degrades_status(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from pseudolife_memory import daemon

    _near(monkeypatch, near=True)
    monkeypatch.setattr(daemon, "_last_memory_warning", None)
    with caplog.at_level(logging.WARNING, logger=daemon.logger.name):
        payload = daemon._build_health_payload(_Svc(), token_present=False)
    assert payload["status"] == "ok"
    assert payload["memory"]["near_limit"] is True
    assert any("memory" in r.getMessage() and "limit" in r.getMessage()
               for r in caplog.records)


def test_the_near_limit_warning_is_rate_limited(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The Docker healthcheck polls every 15 s; one warning per interval,
    not one per poll."""
    from pseudolife_memory import daemon

    _near(monkeypatch, near=True)
    clock = [10_000.0]
    monkeypatch.setattr(daemon, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(daemon, "_last_memory_warning", None)
    with caplog.at_level(logging.WARNING, logger=daemon.logger.name):
        for _ in range(3):
            daemon._build_health_payload(_Svc(), token_present=False)
            clock[0] += 15.0
        first = len(caplog.records)
        clock[0] += daemon._MEMORY_WARNING_INTERVAL_S
        daemon._build_health_payload(_Svc(), token_present=False)
    assert first == 1
    assert len(caplog.records) == 2


def test_health_names_the_embedder_precision_once_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pseudolife_memory.daemon import _build_health_payload

    _near(monkeypatch, near=False)
    assert "embedder" not in _build_health_payload(_Svc(), token_present=False)
    svc = _Svc()
    svc._embedder = _Embedder()
    payload = _build_health_payload(svc, token_present=False)
    assert payload["embedder"] == {
        "backend": "torch", "device": "cpu", "dtype": "bf16"}

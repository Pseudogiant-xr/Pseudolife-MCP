"""How close this process sits to its memory limit, for /health.

The 2026-09-23 OOM kill came from a daemon living at ~95% of a 4 GiB cgroup
cap while /health said "ok"; after the restart the kernel's
``memory.events`` ``max`` counter reached 8,021 within ~27 minutes, and
nothing the daemon published showed it. This reads the numbers that would
have shown it: the cgroup v2 usage, limit and events when a limit is
readable, otherwise the process RSS, and an explicit ``unavailable`` rather
than silence when neither is (native Windows, macOS).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Usage at or above this fraction of the cgroup limit is "near_limit". The
# 2026-09-23 incident daemon sat at ~95% for weeks before the kill.
NEAR_LIMIT_FRACTION = 0.90

_EVENT_KEYS = ("max", "oom", "oom_kill")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):  # ValueError covers UnicodeDecodeError
        return None


def _int(text: str | None) -> int | None:
    # int() rather than str.isdigit(), which accepts digits like "²" that
    # int() rejects.
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def _keyed_ints(text: str | None) -> dict[str, int]:
    """Parse ``key value`` lines (memory.events, memory.stat)."""
    out: dict[str, int] = {}
    for line in (text or "").splitlines():
        key, _, value = line.partition(" ")
        parsed = _int(value)
        if parsed is not None:
            out[key] = parsed
    return out


def _cgroup_dir(cgroup_root: Path, proc_self: Path) -> Path | None:
    """This process's cgroup v2 directory: the ``0::`` path from
    /proc/self/cgroup (a nested host cgroup), else the root itself (a
    container's private cgroup namespace shows ``0::/``)."""
    for line in (_read(proc_self / "cgroup") or "").splitlines():
        if line.startswith("0::"):
            nested = cgroup_root / line[3:].strip().lstrip("/")
            if (nested / "memory.current").exists():
                return nested
    if (cgroup_root / "memory.current").exists():
        return cgroup_root
    return None


def _process_rss(proc_self: Path) -> dict[str, int]:
    status = _read(proc_self / "status")
    out: dict[str, int] = {}
    for line in (status or "").splitlines():
        key, _, rest = line.partition(":")
        name = {"VmRSS": "rss_bytes", "VmHWM": "rss_peak_bytes"}.get(key)
        kb = _int(rest.split()[0]) if name and rest.split() else None
        if kb is not None:
            out[name] = kb * 1024  # reported in kB
    return out


def read_memory_headroom(
    *,
    cgroup_root: str | Path = "/sys/fs/cgroup",
    proc_self: str | Path = "/proc/self",
) -> dict[str, Any]:
    """Current memory use against the limit that would OOM-kill us.

    ``source`` says where the numbers came from: ``cgroup`` (usage, limit,
    ``used_fraction``, ``near_limit``, the anon/file split and the
    ``memory.events`` max/oom/oom_kill counters, plus RSS), ``process``
    (RSS only: no cgroup v2 ``memory.current`` readable), or
    ``unavailable``. ``used_fraction`` is the working set (current minus
    ``inactive_file``, the page cache the kernel reclaims first; the
    kubelet's measure) over the limit. ``near_limit`` is None whenever no
    limit is known (``memory.max`` = ``max``). Never raises.
    """
    cgroup_root, proc_self = Path(cgroup_root), Path(proc_self)
    rss = _process_rss(proc_self)
    cg = _cgroup_dir(cgroup_root, proc_self)
    current = _int(_read(cg / "memory.current")) if cg is not None else None
    if current is not None:
        limit = _int(_read(cg / "memory.max"))
        events = _keyed_ints(_read(cg / "memory.events"))
        stat = _keyed_ints(_read(cg / "memory.stat"))
        working_set = max(0, current - stat.get("inactive_file", 0))
        fraction = working_set / limit if limit else None
        reading: dict[str, Any] = {
            "source": "cgroup",
            "current_bytes": current,
            "working_set_bytes": working_set,
            "limit_bytes": limit,
            "used_fraction": round(fraction, 4) if fraction is not None else None,
            "near_limit": (fraction >= NEAR_LIMIT_FRACTION
                           if fraction is not None else None),
            "events": {k: events[k] for k in _EVENT_KEYS if k in events},
        }
        for key in ("anon", "file"):
            if key in stat:
                reading[f"{key}_bytes"] = stat[key]
        reading.update(rss)
        return reading
    if rss:
        return {"source": "process", "near_limit": None, **rss}
    return {"source": "unavailable"}

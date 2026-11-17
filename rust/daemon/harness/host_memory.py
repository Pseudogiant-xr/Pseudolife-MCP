"""Expected /health memory shape from host files, without computing telemetry.

Only presence, scalar kinds and limit nullability are host-dependent in the
golden. run.normalize_memory independently validates every raw daemon value.
"""
from pathlib import Path


def _text(path):
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def _integer(text):
    try:
        return int(text)
    except ValueError:
        return None


def _fields(path):
    return {parts[0].removesuffix(":"): number for line in _text(path).splitlines()
            if len(parts := line.split()) >= 2 and (number := _integer(parts[1])) is not None}


def memory_shape(cgroup_root=Path("/sys/fs/cgroup"), proc_self=Path("/proc/self")):
    status = _fields(proc_self / "status")
    rss = {field: "<free int>" for key, field in (("VmRSS", "rss_bytes"), ("VmHWM", "rss_peak_bytes"))
           if key in status}
    roots = [cgroup_root / line[3:].strip().lstrip("/")
             for line in _text(proc_self / "cgroup").splitlines() if line.startswith("0::")]
    roots.append(cgroup_root)
    cg = next((root for root in roots if (root / "memory.current").exists()), None)
    if cg is None or _integer(_text(cg / "memory.current")) is None:
        return {"source": "process", "near_limit": None, **rss} if rss else {"source": "unavailable"}
    limit = _integer(_text(cg / "memory.max"))
    stat, events = _fields(cg / "memory.stat"), _fields(cg / "memory.events")
    return {"source": "cgroup", "current_bytes": "<free int>", "working_set_bytes": "<free int>",
            "limit_bytes": None if limit is None else "<free int>",
            "used_fraction": "<free float>" if limit else None,
            "near_limit": "<free bool>" if limit else None,
            "events": {key: "<free int>" for key in ("max", "oom", "oom_kill") if key in events},
            **{key + "_bytes": "<free int>" for key in ("anon", "file") if key in stat}, **rss}

"""Portable golden values shared by the daemon harnesses."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import daemons

REPO = Path(__file__).resolve().parents[3]


def _raw_tree(text: str):
    """The body as written: objects as ordered pairs, numbers as their text."""
    return json.loads(text, object_pairs_hook=lambda pairs: ("obj", pairs),
                      parse_float=lambda t: ("num", t), parse_int=lambda t: ("num", t),
                      parse_constant=lambda t: ("num", t))


def _free(v) -> bool:
    return isinstance(v, str) and v.startswith("<") and (">" in v)


def number_tokens(resp: dict, normalized: dict) -> list[dict]:
    """Portable wire tokens at exact JSON paths, excluding declared free cells.

    Keep paths as key/index arrays (keys may themselves contain dots). A
    recording carries only numeric spellings, never the raw response text.
    Filter again at comparison time for replay's seeded-clock normalizer.
    """
    def exact(path):
        value = normalized.get("json")
        for key in path:
            if _free(value):
                return False
            if isinstance(value, dict) and key in value:
                value = value[key]
            elif isinstance(value, list) and isinstance(key, int) and 0 <= key < len(value):
                value = value[key]
            else:
                return False
        return not _free(value)

    if "number_tokens" in resp:
        return [item for item in resp["number_tokens"] if exact(item["path"])]
    out = []

    def walk(value, path):
        if not exact(path):
            return
        if isinstance(value, tuple) and value[0] == "obj":
            for key, child in value[1]:
                walk(child, path + [key])
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + [index])
        elif isinstance(value, tuple) and value[0] == "num":
            out.append({"path": path, "token": value[1]})

    if "raw" in resp:
        walk(_raw_tree(resp["raw"]), [])
    return out


def _machine_paths() -> list[str]:
    """Paths of this machine that a response may echo (data dirs, homes),
    in every escaping a golden can hold them in."""
    out = []
    for path in {str(Path.home()), str(daemons.scratch_root()), str(REPO), tempfile.gettempdir()}:
        for form in {path, path.replace("\\", "/"), json.dumps(path)[1:-1], json.dumps(json.dumps(path)[1:-1])[1:-1]}:
            if len(form) > 3:
                out.append(form)
    return sorted(out, key=len, reverse=True)


def golden_scrub(value):
    """What a committed golden may hold: no raw bodies, no machine paths
    (refusal texts and /api/config echo the data dir), and vectors as a
    digest, so the file stays small and portable. Exact JSON number tokens
    survive separately from decoded values."""
    import hashlib
    paths = _machine_paths()

    def digest(v) -> str:
        return hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest()[:24]

    def walk(v):
        if isinstance(v, dict):
            out = {}
            if isinstance(v.get("raw"), str) and "json" in v:
                out["number_tokens"] = number_tokens(v, v)
            for k, x in v.items():
                if k == "raw":
                    continue
                if k == "bytes" and isinstance(x, str) and len(x) > 4096:
                    out[k] = f"<{len(x)} bytes sha256:{digest(x)}>"
                elif k == "catalog" and isinstance(x, dict) and all(isinstance(r, list) for r in x.values()):
                    out[k] = {name: {"rows": len(rows), "sha256": digest(walk(rows))} for name, rows in x.items()}
                else:
                    out[k] = walk(x)
            return out
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str):
            if v.startswith("[") and v.count(",") >= 255 and len(v) > 2000:
                return "<vector sha256:" + hashlib.sha256(v.encode()).hexdigest()[:16] + ">"
            for path in paths:
                v = v.replace(path, "<machine path>")
            return v
        return v
    return walk(value)

"""Field-by-field comparison of two normalized observations."""

from __future__ import annotations

import base64
import difflib
import json

from . import normalize

# Fields that describe the run rather than the CLI's behaviour.
_RUN_FIELDS = ("home", "window", "daemon_url", "utc_offset")


def _text(value: str) -> str:
    return base64.b64decode(value).decode("utf-8", "backslashreplace")


def _stream_diff(field: str, want: str, got: str) -> list[str]:
    a, b = _text(want), _text(got)
    lines = [f"{field}: python {a!r}", f"{field}: rust   {b!r}"]
    lines += ["  " + line for line in difflib.unified_diff(
        a.splitlines(True), b.splitlines(True), "python", "rust", lineterm="", n=1)][:40]
    return lines


def diff(python: dict, rust: dict, rules: tuple[str, ...]) -> list[str]:
    """Differences after each side's own normalization; empty means equal."""
    a = normalize.apply(python, rules, python.get("home"))
    b = normalize.apply(rust, rules, rust.get("home"))
    out: list[str] = []
    if a["exit"] != b["exit"]:
        out.append(f"exit: python {a['exit']} rust {b['exit']}")
    for field in ("stdout", "stderr"):
        if a[field] != b[field]:
            out += _stream_diff(field, a[field], b[field])
    files_a, files_b = a.get("files", {}), b.get("files", {})
    for rel in sorted(set(files_a) | set(files_b)):
        va, vb = files_a.get(rel), files_b.get(rel)
        if va == vb:
            continue
        if va is None or vb is None:
            out.append(f"file {rel}: python {'absent' if va is None else 'present'}, "
                       f"rust {'absent' if vb is None else 'present'}")
        elif va.startswith("file:") and vb.startswith("file:"):
            out += _stream_diff(f"file {rel}", va[5:], vb[5:])
        else:
            out.append(f"file {rel}: python {va[:80]!r} rust {vb[:80]!r}")
    for field in ("requests", "db", "listener", "modes"):
        if a.get(field) != b.get(field):
            ja = json.dumps(a.get(field), indent=1, sort_keys=False).splitlines()
            jb = json.dumps(b.get(field), indent=1, sort_keys=False).splitlines()
            out.append(f"{field} differ:")
            out += ["  " + line for line in difflib.unified_diff(
                ja, jb, "python", "rust", lineterm="", n=2)][:80]
    return out


def strip_run_fields(observation: dict) -> dict:
    return {k: v for k, v in observation.items() if k not in _RUN_FIELDS}

"""The memory-change hook's answer (`GET /api/hook/memory-changes`, CUTOVER
B3): line 1 is the daemon's clock as a cursor. `w2e_hooks.py` checks its
value against the call's window; here only the leading cursor line is
masked, so the rest of the note compares byte for byte."""

import re

_CURSOR = re.compile(r"\A[0-9]{1,12}\.[0-9]{6}\n")


def _scrub(resp: dict) -> dict:
    if isinstance(resp.get("bytes"), str):
        resp["bytes"] = _CURSOR.sub("<cursor>\n", resp["bytes"])
    return resp


def register(harness):
    harness.register_response_scrub("/api/hook/memory-changes", _scrub)
    return []

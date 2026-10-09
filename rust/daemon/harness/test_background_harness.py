"""Rejecting controls for the background clock observation layer."""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from background_sessions import normalize_closes, validate_restart_clocks


def state():
    return {"rows": {
        "public.episodes": {"columns": ["id", "parent_id", "ended_at"],
                            "rows": [["a" * 32, None, 100.0], ["b" * 32, "a" * 32, 100.0]]},
        "public.client_sessions": {"columns": ["session_key", "episode_ids", "ended_at"],
                                   "rows": [["fixture", ["a" * 32], 100.0]]},
        "public.meta": {"columns": ["key", "value"],
                        "rows": [["deferred_empty_roots", {"a" * 32: 100.0}]]},
    }}


def before():
    prior = state()
    for row in prior["rows"]["public.episodes"]["rows"]: row[2] = None
    prior["rows"]["public.client_sessions"]["rows"][0][2] = None
    prior["rows"]["public.meta"]["rows"] = []
    return prior


def test_clock_projection_preserves_raw_and_root_association():
    raw = state()
    projected = normalize_closes(raw, before(), 90.0, 110.0)
    assert raw == state()
    assert projected["rows"]["public.client_sessions"]["rows"][0][2] == "<close:" + "a" * 32 + ">"


@pytest.mark.parametrize("target,value", [("child", 100.1), ("client", 100.1), ("root", 120.0)])
def test_inconsistent_or_out_of_window_clock_is_rejected(target, value):
    broken = state()
    if target == "client": broken["rows"]["public.client_sessions"]["rows"][0][2] = value
    else: broken["rows"]["public.episodes"]["rows"][target == "child"][2] = value
    with pytest.raises(RuntimeError): normalize_closes(broken, before(), 90.0, 110.0)


def test_restart_cannot_rewrite_all_clocks_consistently():
    closed = state()
    changed = copy.deepcopy(closed)
    for row in changed["rows"]["public.episodes"]["rows"]: row[2] = 101.0
    changed["rows"]["public.client_sessions"]["rows"][0][2] = 101.0
    changed["rows"]["public.meta"]["rows"][0][1]["a" * 32] = 101.0
    with pytest.raises(RuntimeError): validate_restart_clocks(changed, closed)

"""Unit tests for the pure abstention helper (no torch/PG)."""
import pytest

from pseudolife_memory.memory.abstain import low_confidence
from tests.helpers import reload_mcp_filemode as _reload_mcp_filemode


@pytest.mark.parametrize("scores,floor,expected", [
    ([], 0.0, True),                  # nothing found -> abstain
    ([0.05, 0.01], 0.0, False),       # floor 0 = off, only empty triggers
    ([0.30, 0.10], 0.35, True),       # best hit too weak
    ([0.42, 0.10], 0.35, False),      # best hit clears the floor
])
def test_low_confidence_gates_on_the_top_score(scores, floor, expected):
    assert low_confidence(scores, floor=floor) is expected


# ---------------------------------------------------------------------------
# Tool-layer cortex guard: a confident canonical fact must never be flagged
# low-confidence, even when associative recall is weak/empty (the cortex block
# IS the answer). Monkeypatch the service so no embedder/PG is needed.
# ---------------------------------------------------------------------------


def test_cortex_hit_overrides_low_confidence(tmp_path, monkeypatch):
    mod = _reload_mcp_filemode(tmp_path, monkeypatch)
    monkeypatch.setattr(mod.service, "search", lambda **kw: {
        "query": kw.get("query", ""), "count": 0, "entries": [],
        "low_confidence": True,
    })
    monkeypatch.setattr(mod.service, "cortex_search", lambda *a, **k: {
        "entries": [{
            "entity": "checkout-service", "attribute": "default port",
            "value": "9090", "origin": "agent", "confidence": 0.8, "score": 0.7,
        }],
    })
    res = mod.memory_search("checkout-service default port")
    assert res.get("cortex")                  # canonical fact surfaced
    assert res["low_confidence"] is False      # cortex answer => confident


def test_no_cortex_keeps_low_confidence(tmp_path, monkeypatch):
    mod = _reload_mcp_filemode(tmp_path, monkeypatch)
    monkeypatch.setattr(mod.service, "search", lambda **kw: {
        "query": kw.get("query", ""), "count": 0, "entries": [],
        "low_confidence": True,
    })
    monkeypatch.setattr(mod.service, "cortex_search", lambda *a, **k: {"entries": []})
    res = mod.memory_search("nonexistent thing")
    assert not res.get("cortex")
    assert res["low_confidence"] is True


def test_guard_min_score_is_passed_through(tmp_path, monkeypatch):
    mod = _reload_mcp_filemode(tmp_path, monkeypatch)
    captured = {}

    def fake_cortex_search(query, top_k=5, min_score=0.0):
        captured["min_score"] = min_score
        return {"entries": []}

    monkeypatch.setattr(mod.service, "search", lambda **kw: {
        "query": kw.get("query", ""), "count": 0, "entries": [],
        "low_confidence": False,
    })
    monkeypatch.setattr(mod.service, "cortex_search", fake_cortex_search)
    mod.service.config.memory.cortex.guard_min_score = 0.65
    mod.memory_search("anything")
    assert captured["min_score"] == 0.65

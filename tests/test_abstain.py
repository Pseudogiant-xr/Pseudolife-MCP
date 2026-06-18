"""Unit tests for the pure abstention helper (no torch/PG)."""
from pseudolife_memory.memory.abstain import low_confidence


def test_empty_scores_is_low_confidence():
    assert low_confidence([], floor=0.0) is True          # nothing found -> abstain


def test_floor_off_only_empty_triggers():
    assert low_confidence([0.05, 0.01], floor=0.0) is False  # floor 0 = off


def test_top_below_floor_is_low_confidence():
    assert low_confidence([0.30, 0.10], floor=0.35) is True   # best hit too weak


def test_top_at_or_above_floor_is_confident():
    assert low_confidence([0.42, 0.10], floor=0.35) is False


# ---------------------------------------------------------------------------
# Tool-layer cortex guard: a confident canonical fact must never be flagged
# low-confidence, even when associative recall is weak/empty (the cortex block
# IS the answer). Monkeypatch the service so no embedder/PG is needed.
# ---------------------------------------------------------------------------


def _reload_mcp_filemode(tmp_path, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)  # force file mode
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)
    return mod


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

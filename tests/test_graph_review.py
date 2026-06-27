# tests/test_graph_review.py
from pseudolife_memory.memory import graph_review as gr


def _ents(*names):
    return [{"id": i + 1, "display": n, "canonical": n.lower(), "etype": None}
            for i, n in enumerate(names)]


def test_duplicate_candidates_flags_near_identical_names():
    ents = _ents("Cortex Console web frontend", "web frontend (Cortex Console)", "postgres")
    dups = gr.duplicate_candidates(ents)
    assert dups and dups[0]["type"] == "duplicate" and dups[0]["action"] == "merge"
    assert "postgres" not in dups[0]["label"]


def test_test_artifacts_matches_known_patterns():
    ents = _ents("payments/payments-db", "pl-healthcheck-target", "pseudolife-mcp")
    arts = gr.test_artifacts(ents)
    assert arts and arts[0]["action"] == "delete"
    assert set(arts[0]["entities"]) == {"payments/payments-db", "pl-healthcheck-target"}


def test_orphans_flags_degree_le_1():
    ents = _ents("a", "b", "lonely")
    edges = [{"src_id": 1, "relation": "uses", "dst_id": 2, "origin": "action", "confidence": 0.9}]
    orph = gr.orphans(edges, ents)
    assert orph and "lonely" in orph[0]["entities"]


def test_dubious_edges_flags_low_conf_agent():
    ents = _ents("memory_recall", "docker-desktop")
    edges = [{"src_id": 1, "relation": "runs-on", "dst_id": 2, "origin": "agent", "confidence": 0.6}]
    dub = gr.dubious_edges(ents and edges, ents)
    assert dub and dub[0]["action"] == "prune" and dub[0]["edges"][0]["src"] == "memory_recall"


def test_unattributed_flags_entities_without_sources():
    ents = _ents("attributed", "orphan-of-project")
    un = gr.unattributed(ents, {1: ["pseudolife"]})
    assert un and un[0]["entities"] == ["orphan-of-project"] and un[0]["action"] == "assign"


def test_review_aggregates_all_groups():
    ents = _ents("payments-db", "lonely")
    out = gr.review([], ents, {})
    types = {f["type"] for f in out["findings"]}
    assert {"test_artifact", "orphan", "unattributed"} <= types
    assert out["counts"]["total"] == len(out["findings"])

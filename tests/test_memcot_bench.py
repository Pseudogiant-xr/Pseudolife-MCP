import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import memcot_bench as mb  # noqa: E402


def test_known_entities_cover_all_edge_endpoints():
    endpoints = set()
    for rec in mb.CORPUS:
        for src, _rel, dst in rec["edges"]:
            endpoints.add(src)
            endpoints.add(dst)
    assert endpoints <= mb.KNOWN_ENTITIES


def test_every_edge_uses_closed_vocab():
    allowed = {"depends-on", "runs-on", "part-of", "uses", "stores-data-in"}
    for rec in mb.CORPUS:
        for _src, rel, _dst in rec["edges"]:
            assert rel in allowed


def test_every_question_gold_is_reachable_within_its_hops():
    # BFS over the seeded edges (undirected) from any entity named in the
    # question; gold must be reachable within `hops` steps.
    adj: dict[str, set[str]] = {}
    for rec in mb.CORPUS:
        for src, _rel, dst in rec["edges"]:
            adj.setdefault(src, set()).add(dst)
            adj.setdefault(dst, set()).add(src)
    for q in mb.QUESTIONS:
        seeds = mb.spot_entities(q["question"], mb.KNOWN_ENTITIES)
        seen = set(seeds)
        frontier = set(seeds)
        for _ in range(q["hops"]):
            nxt = set()
            for e in frontier:
                nxt |= adj.get(e, set())
            seen |= nxt
            frontier = nxt
        assert q["gold"] in seen, q


def test_spot_entities_word_boundary():
    known = {"checkout-svc", "jvm-21"}
    assert mb.spot_entities("checkout-svc depends-on billing-lib", known) == ["checkout-svc"]
    assert mb.spot_entities("nothing relevant here", known) == []

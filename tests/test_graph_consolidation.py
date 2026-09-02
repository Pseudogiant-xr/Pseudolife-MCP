import numpy as np

from pseudolife_memory.memory import graph_consolidation as gc
from pseudolife_memory.memory.graph_consolidation import (
    junk_name_reason, variant_tokens, variant_conflict)

ENTS = [
    {"id": 1, "canonical": "daemon", "display": "daemon", "etype": None},
    {"id": 2, "canonical": "docker", "display": "docker", "etype": None},
    {"id": 3, "canonical": "user", "display": "user", "etype": None},
    {"id": 4, "canonical": "windows 11", "display": "Windows 11", "etype": None},
]


def _edge(eid, s, rel, d, conf, origin="agent"):
    return {"id": eid, "src_id": s, "relation": rel, "dst_id": d,
            "confidence": conf, "origin": origin}


def test_rescore_only_changes_agent_edges_that_differ():
    edges = [
        _edge(10, 1, "runs-on", 2, 0.6),          # clean -> should become 0.70
        _edge(11, 3, "runs-on", 4, 0.6),          # violation -> should become 0.175
        _edge(12, 1, "related-to", 2, 0.45),      # already correct -> omitted
        _edge(13, 1, "runs-on", 2, 0.6, "user"),  # non-agent -> omitted
    ]
    out = dict(gc.rescore_edges(edges, ENTS))
    assert out == {10: 0.70, 11: 0.175}


def test_hard_violation_edges_flags_only_typed_violations():
    edges = [
        _edge(10, 1, "runs-on", 2, 0.7),   # daemon(service)->docker(runtime): OK
        _edge(11, 3, "runs-on", 4, 0.175), # user(person)->windows(runtime): violation
        _edge(12, 1, "related-to", 4, 0.45),  # unconstrained relation: never a violation
    ]
    ids = [e["id"] for e in gc.hard_violation_edges(edges, ENTS)]
    assert ids == [11]


def test_exact_duplicate_pairs_folds_lower_degree_into_higher():
    ents = [
        {"id": 1, "canonical": "gemma sidecar", "display": "Gemma sidecar", "etype": None},
        {"id": 2, "canonical": "gemma sidecar", "display": "gemma  sidecar", "etype": None},
        {"id": 3, "canonical": "unrelated", "display": "unrelated thing", "etype": None},
    ]
    # entity 1 has an edge (degree 1), entity 2 has none -> fold 2 into 1
    edges = [_edge(10, 1, "related-to", 3, 0.45)]
    assert gc.exact_duplicate_pairs(ents, edges) == [(2, 1)]


def test_exact_duplicate_pairs_ignores_non_identical_token_sets():
    ents = [
        {"id": 1, "canonical": "schema v8", "display": "schema v8", "etype": None},
        {"id": 2, "canonical": "schema 11", "display": "schema 11", "etype": None},
    ]
    assert gc.exact_duplicate_pairs(ents, []) == []


def test_exact_duplicate_pairs_equal_degree_folds_higher_id_into_lower():
    # token-set-identical displays, both degree 0 (no edges) -> equal degree:
    # fold the HIGHER id into the LOWER id, i.e. (from_id=9, into_id=5).
    ents = [
        {"id": 5, "canonical": "dup", "display": "dup thing", "etype": None},
        {"id": 9, "canonical": "dup", "display": "dup  thing", "etype": None},
    ]
    assert gc.exact_duplicate_pairs(ents, []) == [(9, 5)]


def _vec(*xs):
    return np.asarray(xs, dtype=np.float32)


def test_exact_duplicate_pairs_excludes_concat_artifacts():
    # Two independently-extracted "A<->B" junk concat-artifacts with the same
    # token multiset (e.g. from different extraction passes) must NOT be
    # proposed for auto-merge on this path -- there is no human review here,
    # unlike partition_candidates' merge_cands queue, and _name_contains
    # already refuses to treat a concat artifact as a merge endpoint there.
    ents = [
        {"id": 1, "canonical": "memory_recall<->recall.py",
         "display": "memory_recall<->recall.py", "etype": None},
        {"id": 2, "canonical": "recall.py<->memory_recall",
         "display": "recall.py<->memory_recall", "etype": None},
    ]
    assert gc.exact_duplicate_pairs(ents, []) == []


def test_entity_context_vectors_trace_primary_then_mention_fallback():
    ents = [
        {"id": 1, "canonical": "alpha", "display": "alpha", "etype": None},
        {"id": 2, "canonical": "beta", "display": "beta", "etype": None},
        {"id": 3, "canonical": "ghost", "display": "ghost", "etype": None},
    ]
    entries = [
        {"id": 100, "text": "alpha runs nightly", "embedding": _vec(1, 0)},
        {"id": 101, "text": "beta and alpha discussed", "embedding": _vec(0, 1)},
    ]
    # min_mentions=1: this test checks SOURCE selection (trace vs scan), not the threshold.
    vecs, mentions = gc.entity_context_vectors(ents, entries, {"alpha": [100]}, min_mentions=1)
    assert set(vecs) == {1, 2}                 # ghost omitted (no trace, no mention)
    assert np.allclose(vecs[1], _vec(1, 0))    # alpha from its trace entry
    assert np.allclose(vecs[2], _vec(0, 1))    # beta from the mention scan
    assert mentions[1] == frozenset({100}) and mentions[2] == frozenset({101})


def test_candidate_pairs_filters_edges_scope_and_threshold():
    ents = [
        {"id": 1, "canonical": "a", "display": "a", "etype": None},
        {"id": 2, "canonical": "b", "display": "b", "etype": None},
        {"id": 3, "canonical": "c", "display": "c", "etype": None},
        {"id": 4, "canonical": "d", "display": "d", "etype": None},
    ]
    vectors = {1: _vec(1, 0), 2: _vec(1, 0), 3: _vec(1, 0), 4: _vec(0, 1)}
    mentions = {1: frozenset({10}), 2: frozenset({20}),
                3: frozenset({30}), 4: frozenset({40})}   # all distinct
    edges = [{"id": 9, "src_id": 1, "relation": "related-to", "dst_id": 3,
              "confidence": 0.45, "origin": "agent"}]
    scope = {1: ["pseudolife"], 2: ["pseudolife"], 3: ["gw2-reshade"], 4: ["pseudolife"]}
    out = gc.candidate_pairs(vectors, edges, ents, scope, mentions,
                             min_similarity=0.55, top_k=50)
    pairs = {(c["src_id"], c["dst_id"]) for c in out}
    # 1-2 kept (sim 1.0, same scope, no edge). 1-3 dropped (edge exists).
    # 2-3 dropped (disjoint scope). 1-4 / 2-4 dropped (sim 0 < 0.55).
    assert pairs == {(1, 2)}
    assert out[0]["similarity"] == 1.0


def test_exact_duplicate_pairs_keeps_short_discriminators():
    # Distinct entities whose ONLY difference is a token graph_review._token_set
    # would drop (<=2 chars, no digit) must NOT be auto-merged.
    cases = [
        ("Extractor", "pg+extractor"),               # 'pg' dropped by the old filter
        ("heuristic bug (a)", "heuristic bug (b)"),   # 'a'/'b'
        ("Phase-2 Option B", "Phase-2 Option C"),     # 'B'/'C'
    ]
    for da, db in cases:
        ents = [
            {"id": 1, "canonical": da.lower(), "display": da, "etype": None},
            {"id": 2, "canonical": db.lower(), "display": db, "etype": None},
        ]
        assert gc.exact_duplicate_pairs(ents, []) == [], f"should not merge {da!r}/{db!r}"


def test_exact_duplicate_pairs_still_merges_quote_artifacts():
    # Pairs differing only by non-alphanumeric noise (quotes, extra spaces) ARE
    # the same entity and must still auto-merge.
    ents = [
        {"id": 1, "canonical": "fixture devserver", "display": "fixture devserver", "etype": None},
        {"id": 2, "canonical": "'fixture devserver'", "display": "'fixture devserver'", "etype": None},
    ]
    assert gc.exact_duplicate_pairs(ents, []) == [(2, 1)]  # equal degree -> higher id folds into lower


def test_entity_context_vectors_min_mentions_gate():
    ents = [
        {"id": 1, "canonical": "one", "display": "one", "etype": None},   # 1 entry
        {"id": 2, "canonical": "two", "display": "two", "etype": None},   # 2 entries
    ]
    entries = [
        {"id": 10, "text": "one only", "embedding": _vec(1, 0)},
        {"id": 20, "text": "two here", "embedding": _vec(1, 0)},
        {"id": 21, "text": "two again", "embedding": _vec(0, 1)},
    ]
    traces = {"one": [10], "two": [20, 21]}
    vecs, mentions = gc.entity_context_vectors(ents, entries, traces)  # default min_mentions=2
    assert set(vecs) == {2}                          # 'one' omitted (only 1 mention)
    assert mentions[2] == frozenset({20, 21})


def test_candidate_pairs_skips_dismissed_pairs():
    # A human 'these are NOT duplicates' verdict (dismissed_pairs, stored as
    # sorted canonical names) must stop the pair resurfacing as a candidate.
    ents = [
        {"id": 1, "canonical": "a", "display": "a", "etype": None},
        {"id": 2, "canonical": "b", "display": "b", "etype": None},
        {"id": 3, "canonical": "c", "display": "c", "etype": None},
    ]
    vectors = {1: _vec(1, 0), 2: _vec(1, 0), 3: _vec(1, 0)}
    mentions = {1: frozenset({10}), 2: frozenset({20}), 3: frozenset({30})}
    out = gc.candidate_pairs(vectors, [], ents, {}, mentions,
                             min_similarity=0.55, top_k=50,
                             dismissed={("a", "b")})
    pairs = {(c["src_id"], c["dst_id"]) for c in out}
    assert pairs == {(1, 3), (2, 3)}


def test_candidate_pairs_drops_identical_mention_sets():
    ents = [
        {"id": 1, "canonical": "a", "display": "a", "etype": None},
        {"id": 2, "canonical": "b", "display": "b", "etype": None},
        {"id": 3, "canonical": "c", "display": "c", "etype": None},
    ]
    vectors = {1: _vec(1, 0), 2: _vec(1, 0), 3: _vec(1, 0)}
    # 1 and 2 share the SAME supporting entries (pure co-occurrence) -> dropped.
    # 3 has a distinct set -> 1-3 and 2-3 survive.
    mentions = {1: frozenset({10, 11}), 2: frozenset({10, 11}), 3: frozenset({12, 13})}
    out = gc.candidate_pairs(vectors, [], ents, {}, mentions,
                             min_similarity=0.55, top_k=50)
    pairs = {(c["src_id"], c["dst_id"]) for c in out}
    assert pairs == {(1, 3), (2, 3)}


def test_candidate_pairs_drops_high_support_overlap():
    # Near-identical supporting-entry sets are still co-occurrence, not
    # independent similarity: Jaccard overlap >= max_support_overlap drops the
    # pair (strict equality is the overlap-1.0 special case).
    ents = [
        {"id": 1, "canonical": "a", "display": "a", "etype": None},
        {"id": 2, "canonical": "b", "display": "b", "etype": None},
        {"id": 3, "canonical": "c", "display": "c", "etype": None},
    ]
    vectors = {1: _vec(1, 0), 2: _vec(1, 0), 3: _vec(1, 0)}
    # 1-2 overlap 4/5 = 0.8 -> dropped at threshold 0.8; 1-3 / 2-3 overlap 0 -> kept.
    mentions = {1: frozenset({10, 11, 12, 13, 14}), 2: frozenset({10, 11, 12, 13}),
                3: frozenset({20, 21})}
    out = gc.candidate_pairs(vectors, [], ents, {}, mentions,
                             min_similarity=0.55, top_k=50,
                             max_support_overlap=0.8)
    pairs = {(c["src_id"], c["dst_id"]) for c in out}
    assert pairs == {(1, 3), (2, 3)}


# Both exclusions below must run INSIDE candidate_pairs, before top-k —
# filtering afterwards would still let the excluded pairs consume top-k slots
# (the 2026-08-12 round-2 pass lost ~20 of 49 slots to pairs with pending
# link proposals and 6 more to one junk-flagged compound entity).

def _cand_fixture():
    ents = [{"id": 1, "canonical": "a", "display": "a", "etype": None},
            {"id": 2, "canonical": "b", "display": "b", "etype": None},
            {"id": 3, "canonical": "c", "display": "c", "etype": None}]
    vectors = {1: _vec(1, 0), 2: _vec(1, 0), 3: _vec(1, 0)}
    mentions = {1: frozenset({10}), 2: frozenset({20}), 3: frozenset({30})}
    return ents, vectors, mentions


def test_candidate_pairs_skips_pending_proposal_pairs():
    ents, vectors, mentions = _cand_fixture()
    out = gc.candidate_pairs(vectors, [], ents, {}, mentions,
                             min_similarity=0.55, top_k=50,
                             pending_pairs={frozenset((1, 2))})
    assert {(c["src_id"], c["dst_id"]) for c in out} == {(1, 3), (2, 3)}


def test_candidate_pairs_skips_excluded_ids():
    ents, vectors, mentions = _cand_fixture()
    out = gc.candidate_pairs(vectors, [], ents, {}, mentions,
                             min_similarity=0.55, top_k=50,
                             excluded_ids={3})
    assert {(c["src_id"], c["dst_id"]) for c in out} == {(1, 2)}


def test_candidate_pairs_matches_naive_reference_on_random_graph():
    """Equivalence pin for the vectorized similarity prefilter (2026-09-01).

    candidate_pairs was a pure-Python O(n^2) pair loop — 4.2s per deep
    tick at the live bank's 2,070 vector-eligible entities and quadratic
    in entity count — replaced by a matmul prefilter plus per-survivor
    exact scoring. The refactor's contract is bit-identical OUTPUT (the
    per-pair filters, the reported similarity values from the same
    np.dot, ordering and top-k), so this test runs the shipped function
    against a verbatim naive reference over a seeded random graph that
    exercises every filter class at once."""
    rng = np.random.default_rng(20260901)
    n = 90
    ents = [{"id": i, "canonical": f"ent {i}", "display": f"ent {i}",
             "etype": None} for i in range(n)]
    # Clustered vectors so many pairs land near the threshold from both
    # sides; a couple of exact-duplicate names to exercise the dup filter;
    # one name-contained pair for the containment exemption.
    base = rng.normal(size=(6, 24))
    vecs = {}
    for i in range(n):
        v = base[i % 6] + rng.normal(scale=0.4, size=24)
        # float32, like the live embeddings — so the fixture exercises the
        # matmul-vs-dot accumulation error the prefilter margins absorb.
        vecs[i] = (v / np.linalg.norm(v)).astype(np.float32)
    ents[7]["canonical"] = ents[3]["canonical"]      # exact-dup pair
    ents[11]["display"] = "ent 4 service"
    ents[11]["canonical"] = "ent 4 service"          # name-contains ent 4
    edges = [{"id": 1000 + k, "src_id": int(a), "dst_id": int(b),
              "relation": "related-to", "confidence": 0.5, "origin": "agent"}
             for k, (a, b) in enumerate(
                 rng.integers(0, n, size=(40, 2)).tolist()) if a != b]
    mentions = {i: frozenset(rng.integers(0, 60, size=rng.integers(1, 6)).tolist())
                for i in range(n) if i % 5}          # some ids have no mentions
    scope = {i: (["p1"] if i % 3 == 0 else ["p2"] if i % 3 == 1 else [])
             for i in range(0, n, 2)}                # some unattributed
    dismissed = {tuple(sorted((f"ent {a}", f"ent {b}")))
                 for a, b in rng.integers(0, n, size=(15, 2)).tolist()}
    pending = {frozenset((int(a), int(b)))
               for a, b in rng.integers(0, n, size=(10, 2)).tolist()
               if a != b}
    excluded = {int(x) for x in rng.integers(0, n, size=5)}
    kwargs = dict(min_similarity=0.55, top_k=25, dismissed=dismissed,
                  max_support_overlap=0.8, pending_pairs=pending,
                  excluded_ids=excluded)

    def naive(vectors, edges, entities, scope_map, mention_map, *,
              min_similarity, top_k, dismissed, max_support_overlap,
              pending_pairs, excluded_ids):
        # Verbatim pre-2026-09-01 loop body.
        disp = {e["id"]: e["display"] for e in entities}
        canon = {e["id"]: e["canonical"] for e in entities}
        linked = {frozenset((e["src_id"], e["dst_id"])) for e in edges}
        linked |= pending_pairs or set()
        excl = excluded_ids or set()
        dup = {frozenset(p) for p in gc.exact_duplicate_pairs(entities, edges)}
        ids = sorted(i for i in vectors if i not in excl)
        scored = []
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                u, v = ids[i], ids[j]
                key = frozenset((u, v))
                if key in linked or key in dup:
                    continue
                if dismissed and tuple(sorted((canon.get(u, ""), canon.get(v, "")))) in dismissed:
                    continue
                mu, mv = mention_map.get(u), mention_map.get(v)
                if (mu and mv
                        and (len(mu & mv) / min(len(mu), len(mv))
                             >= max_support_overlap)
                        and not gc._name_contains(disp.get(u, ""), disp.get(v, ""))):
                    continue
                su, sv = set(scope_map.get(u, [])), set(scope_map.get(v, []))
                if su and sv and not (su & sv):
                    continue
                sim = float(np.dot(vectors[u], vectors[v]))
                if sim < min_similarity:
                    continue
                scored.append({"src_id": u, "dst_id": v,
                               "src": disp.get(u, str(u)),
                               "dst": disp.get(v, str(v)),
                               "similarity": round(sim, 4)})
        scored.sort(key=lambda c: (-c["similarity"], c["src_id"], c["dst_id"]))
        return scored[:top_k]

    expect = naive(vecs, edges, ents, scope, mentions, **kwargs)
    got = gc.candidate_pairs(vecs, edges, ents, scope, mentions, **kwargs)
    assert len(expect) > 5, "fixture too sparse to prove anything"
    assert got == expect


def test_partition_candidates_merge_vs_link():
    ents = [
        {"id": 1, "canonical": "atlas review", "display": "Atlas Review", "etype": None},
        {"id": 2, "canonical": "atlas review queue", "display": "Atlas Review queue", "etype": None},
        {"id": 3, "canonical": "track a (recall)", "display": "Track A (recall)", "etype": None},
        {"id": 4, "canonical": "track b (insight)", "display": "Track B (insight)", "etype": None},
    ]
    # entity 1 has an edge (degree 1) so 'Atlas Review queue' folds into 'Atlas Review'.
    edges = [_edge(99, 1, "related-to", 3, 0.45)]
    pairs = [
        {"src_id": 1, "dst_id": 2, "src": "Atlas Review", "dst": "Atlas Review queue", "similarity": 0.99},
        {"src_id": 3, "dst_id": 4, "src": "Track A (recall)", "dst": "Track B (insight)", "similarity": 0.98},
    ]
    merges, links = gc.partition_candidates(pairs, ents, edges, merge_min_similarity=0.90)
    assert [(m["from_id"], m["into_id"]) for m in merges] == [(2, 1)]   # >=2-token subset -> merge
    assert merges[0]["reason"] == "token-subset"
    assert [(p["src_id"], p["dst_id"]) for p in links] == [(3, 4)]      # distinct names -> link


def test_partition_candidates_below_threshold_is_link():
    ents = [
        {"id": 1, "canonical": "test", "display": "test", "etype": None},
        {"id": 2, "canonical": "test harness", "display": "test harness", "etype": None},
    ]
    # name-contained, but low similarity -> NOT a merge (guard against coincidental containment).
    pairs = [{"src_id": 1, "dst_id": 2, "src": "test", "dst": "test harness", "similarity": 0.70}]
    merges, links = gc.partition_candidates(pairs, ents, [], merge_min_similarity=0.90)
    assert merges == []
    assert len(links) == 1


def test_junk_entities_flags_artifacts_not_real():
    ents = [
        {"id": 1, "canonical": "2", "display": "2", "etype": None},          # bare number
        {"id": 2, "canonical": "live", "display": "LIVE", "etype": None},     # status word
        {"id": 3, "canonical": "ok", "display": "ok", "etype": None},        # too short AND status
        {"id": 4, "canonical": "daemon", "display": "daemon", "etype": None}, # real entity
        {"id": 5, "canonical": "merged", "display": "merged", "etype": None}, # status word, BUT high degree
    ]
    edges = [_edge(10, 5, "related-to", 4, 0.45), _edge(11, 5, "related-to", 1, 0.45)]  # entity 5 degree 2
    out = {j["entity_id"]: j["reason"] for j in gc.junk_entities(ents, edges, max_degree=1)}
    assert out == {1: "bare-number", 2: "status-word", 3: "too-short"}  # 4 real, 5 well-connected


def test_partition_candidates_never_targets_a_contentless_entity():
    # 2026-07-26: zero-fact/zero-degree nodes ("Atlas graph cleanup") acted as
    # merge MAGNETS — degree alone decided the fold, so on a 0-0 degree tie the
    # empty node won by id and swallowed a richly-specified work item. Rank by
    # (degree, facts) so the evidence-bearing side is always the target.
    ents = [{"id": 1, "canonical": "atlas-graph-cleanup", "display": "Atlas graph cleanup", "etype": None},
            {"id": 2, "canonical": "atlas-graph-cleanup-pr18", "display": "Atlas graph cleanup PR18", "etype": None}]
    pairs = [{"src_id": 1, "dst_id": 2, "src": "Atlas graph cleanup",
              "dst": "Atlas graph cleanup PR18", "similarity": 0.97}]
    merges, _ = gc.partition_candidates(pairs, ents, [], fact_counts={2: 6})
    assert len(merges) == 1
    assert merges[0]["into"] == "Atlas graph cleanup PR18"   # facts win the tie
    assert merges[0]["from"] == "Atlas graph cleanup"


def test_partition_candidates_facts_outrank_a_single_stray_edge():
    # Evidence is degree + facts, so a fact-rich node is not out-ranked by a
    # contentless one that happens to carry one stray edge.
    ents = [{"id": 1, "canonical": "atlas-graph-cleanup", "display": "Atlas graph cleanup", "etype": None},
            {"id": 2, "canonical": "atlas-graph-cleanup-pr18", "display": "Atlas graph cleanup PR18", "etype": None}]
    pairs = [{"src_id": 1, "dst_id": 2, "src": "Atlas graph cleanup",
              "dst": "Atlas graph cleanup PR18", "similarity": 0.97}]
    edges = [_edge(10, 1, "related-to", 99, 0.45)]      # id 1: degree 1, 0 facts
    merges, _ = gc.partition_candidates(pairs, ents, edges, fact_counts={2: 6})
    assert merges[0]["into"] == "Atlas graph cleanup PR18"


def test_partition_candidates_keeps_bare_vs_path_when_both_sides_are_thin():
    # Regression guard for the ranking change: two evidence-free entities must
    # still partition as a merge (the ordinary bare-name -> path-form case).
    ents = [{"id": 1, "canonical": "update-ps1", "display": "update.ps1", "etype": None},
            {"id": 2, "canonical": "ops-update-ps1", "display": "ops/update.ps1", "etype": None}]
    pairs = [{"src_id": 1, "dst_id": 2, "src": "update.ps1",
              "dst": "ops/update.ps1", "similarity": 0.99}]
    merges, links = gc.partition_candidates(pairs, ents, [], fact_counts={})
    assert len(merges) == 1 and links == []


def test_shared_mention_entries_returns_only_entries_naming_both():
    # Evidence for a retype judgement: the notes where the two entities
    # actually co-occur, not everything mentioning either one.
    entries = [
        {"id": 1, "text": "mcp-publisher pushes the server to the MCP registry"},
        {"id": 2, "text": "mcp-publisher was reinstalled at v1.8.0"},
        {"id": 3, "text": "the MCP registry served 0.10.0 as latest"},
    ]
    got = gc.shared_mention_entries(entries, "mcp-publisher", "MCP registry", limit=5)
    assert got == ["mcp-publisher pushes the server to the MCP registry"]
    assert gc.shared_mention_entries(entries, "mcp-publisher", "nothing here", 5) == []


def test_shared_mention_entries_respects_limit():
    entries = [{"id": i, "text": f"alpha and beta note {i}"} for i in range(6)]
    assert len(gc.shared_mention_entries(entries, "alpha", "beta", limit=2)) == 2


def test_junk_entities_flags_slot_key_artifacts_against_known_entities():
    # 2026-07-26: `X.attribute` entities minted when an extractor flattens a
    # vocab slot key. Detected only when the PREFIX is itself a known entity,
    # so genuinely-dotted names survive: `llama.cpp` stays unless an entity
    # called `llama` exists, and `host.docker.internal` is never touched.
    ents = [
        {"id": 1, "canonical": "0-9-0-release", "display": "0.9.0-release", "etype": None},
        {"id": 2, "canonical": "0-9-0-release-bm25", "display": "0-9-0-release.bm25", "etype": None},
        {"id": 3, "canonical": "llama-cpp", "display": "llama.cpp", "etype": None},
        {"id": 4, "canonical": "host-docker-internal", "display": "host.docker.internal", "etype": None},
        {"id": 5, "canonical": "daemon", "display": "daemon", "etype": None},
    ]
    known = frozenset({"0-9-0-release", "llama-cpp", "host-docker-internal", "daemon"})
    out = {j["entity_id"]: j["reason"]
           for j in gc.junk_entities(ents, [], max_degree=1, known_norms=known)}
    assert out.get(2) == "slot-key-artifact"
    assert 3 not in out and 4 not in out and 1 not in out and 5 not in out


def test_is_concat_artifact_detects_relation_separators():
    for name in ["memory_recall<->recall.py", "schema v8 <-> schema 11",
                 "a ↔ b", "x -> y", "Phase 1 plan<->Phase 2 plan"]:
        assert gc._is_concat_artifact(name) is True, name


def test_is_concat_artifact_ignores_plain_names():
    for name in ["memory_graph", "Atlas Review queue", "claude-code", "4090/Qwen3.6-27B"]:
        assert gc._is_concat_artifact(name) is False, name


def test_is_concat_artifact_requires_nonempty_both_sides():
    assert gc._is_concat_artifact("<-> y") is False   # empty left
    assert gc._is_concat_artifact("x <->") is False   # empty right


def test_name_contains_requires_two_contained_tokens():
    assert gc._name_contains("Atlas Review", "Atlas Review queue") == "token-subset"
    assert gc._name_contains("memory_graph", "Graph") is None       # {graph} = 1 token
    assert gc._name_contains("bank", "live bank") is None           # {bank} = 1 token


def test_name_contains_excludes_concat_artifacts():
    assert gc._name_contains("Phase 2 plan", "Phase 1 plan<->Phase 2 plan") is None


def test_partition_candidates_single_token_subset_is_link_not_merge():
    ents = [
        {"id": 1, "canonical": "bank", "display": "bank", "etype": None},
        {"id": 2, "canonical": "live bank", "display": "live bank", "etype": None},
    ]
    pairs = [{"src_id": 1, "dst_id": 2, "src": "bank", "dst": "live bank", "similarity": 0.99}]
    merges, links = gc.partition_candidates(pairs, ents, [], merge_min_similarity=0.90)
    assert merges == []
    assert [(p["src_id"], p["dst_id"]) for p in links] == [(1, 2)]


def test_partition_candidates_concat_artifact_target_is_not_merged():
    ents = [
        {"id": 1, "canonical": "phase 2 plan", "display": "Phase 2 plan", "etype": None},
        {"id": 2, "canonical": "phase 1 plan<->phase 2 plan",
         "display": "Phase 1 plan<->Phase 2 plan", "etype": None},
    ]
    pairs = [{"src_id": 1, "dst_id": 2, "src": "Phase 2 plan",
              "dst": "Phase 1 plan<->Phase 2 plan", "similarity": 0.99}]
    merges, links = gc.partition_candidates(pairs, ents, [], merge_min_similarity=0.90)
    assert merges == []                         # artifact endpoint excluded from merge
    assert len(links) == 1


def test_junk_entities_flags_concat_artifacts_regardless_of_degree():
    ents = [
        {"id": 1, "canonical": "memory_recall<->recall.py",
         "display": "memory_recall<->recall.py", "etype": None},
        {"id": 2, "canonical": "recall.py", "display": "recall.py", "etype": None},
        {"id": 3, "canonical": "memory_recall", "display": "memory_recall", "etype": None},
    ]
    # entity 1 is well-connected (degree 2) yet must still be flagged as an artifact
    edges = [_edge(10, 1, "related-to", 2, 0.45), _edge(11, 1, "related-to", 3, 0.45)]
    out = {j["entity_id"]: j["reason"] for j in gc.junk_entities(ents, edges, max_degree=1)}
    assert out == {1: "concat-artifact"}   # 2 and 3 are real; flagged despite degree 2


# --- store curation: cross-key near-duplicate slot pairs (lessons / world) ----

def _slot_rec(key, entity, attribute, value, emb, **extra):
    return {"key": key, "entity": entity, "attribute": attribute,
            "value": value, "embedding": emb, **extra}


def test_slot_duplicate_candidates_pairs_cross_key_near_dups():
    recs = [
        _slot_rec("deploy-daemon|approach", "deploy daemon", "approach",
                  "backup first", _vec(1, 0)),
        _slot_rec("deploy-host|pitfall", "deploy host", "pitfall",
                  "always backup first", _vec(1, 0.05)),
        _slot_rec("gpu-run|approach", "gpu run", "approach",
                  "keep compile on", _vec(0, 1)),
    ]
    out = gc.slot_duplicate_candidates(recs, min_similarity=0.85, top_k=20)
    assert len(out) == 1
    c = out[0]
    # pair keys are emitted sorted (a_key < b_key) for stable identity
    assert (c["a_key"], c["b_key"]) == ("deploy-daemon|approach", "deploy-host|pitfall")
    assert c["similarity"] >= 0.85
    # the record's label fields ride along as evidence; embedding/key do not
    assert c["a"] == {"entity": "deploy daemon", "attribute": "approach",
                      "value": "backup first"}
    assert c["b"]["value"] == "always backup first"
    assert "embedding" not in c["a"] and "key" not in c["b"]


def test_slot_duplicate_candidates_respects_dismissed():
    recs = [
        _slot_rec("a|x", "a", "x", "v1", _vec(1, 0)),
        _slot_rec("b|y", "b", "y", "v2", _vec(1, 0)),
        _slot_rec("c|z", "c", "z", "v3", _vec(1, 0)),
    ]
    out = gc.slot_duplicate_candidates(
        recs, min_similarity=0.85, top_k=20, dismissed={("a|x", "b|y")})
    pairs = {(c["a_key"], c["b_key"]) for c in out}
    assert pairs == {("a|x", "c|z"), ("b|y", "c|z")}


def test_slot_duplicate_candidates_skips_records_without_embeddings():
    recs = [
        _slot_rec("a|x", "a", "x", "v1", _vec(1, 0)),
        _slot_rec("b|y", "b", "y", "v2", None),        # legacy row, no embedding
        _slot_rec("c|z", "c", "z", "v3", _vec(1, 0)),
    ]
    out = gc.slot_duplicate_candidates(recs, min_similarity=0.85, top_k=20)
    assert {(c["a_key"], c["b_key"]) for c in out} == {("a|x", "c|z")}


def test_slot_duplicate_candidates_orders_and_caps():
    recs = [
        _slot_rec("a|x", "a", "x", "v1", _vec(1, 0)),
        _slot_rec("b|y", "b", "y", "v2", _vec(1, 0)),       # sim 1.0 with a|x
        _slot_rec("c|z", "c", "z", "v3", _vec(1, 0.2)),     # lower sim with both
    ]
    out = gc.slot_duplicate_candidates(recs, min_similarity=0.85, top_k=20)
    assert [(c["a_key"], c["b_key"]) for c in out][0] == ("a|x", "b|y")
    assert [c["similarity"] for c in out] == sorted(
        (c["similarity"] for c in out), reverse=True)
    capped = gc.slot_duplicate_candidates(recs, min_similarity=0.85, top_k=1)
    assert len(capped) == 1 and capped[0]["similarity"] == 1.0


def test_slot_duplicate_candidates_holds_same_entity_pairs_to_higher_floor():
    # Aspect siblings (one task, approach vs pitfall) embed close but are
    # deliberate structure, not duplicates — below the same-entity floor they
    # must not be listed even though they clear min_similarity.
    siblings = [
        _slot_rec("deploy-daemon|approach", "deploy daemon", "approach",
                  "backup first, then rebuild", _vec(1, 0.35)),
        _slot_rec("deploy-daemon|pitfall", "deploy daemon", "pitfall",
                  "never compose down -v", _vec(1, 0)),   # sim ~0.94
    ]
    assert gc.slot_duplicate_candidates(siblings, min_similarity=0.80) == []
    # A near-verbatim value under a second attribute (key-mint drift) still
    # clears the stricter floor and is listed.
    drift = [
        _slot_rec("deploy-daemon|approach", "deploy daemon", "approach",
                  "backup first, then rebuild", _vec(1, 0)),
        _slot_rec("deploy-daemon|correction", "deploy daemon", "correction",
                  "backup first, then rebuild.", _vec(1, 0.05)),  # sim ~0.999
    ]
    out = gc.slot_duplicate_candidates(drift, min_similarity=0.80)
    assert [(c["a_key"], c["b_key"]) for c in out] == [
        ("deploy-daemon|approach", "deploy-daemon|correction")]


def test_slot_duplicate_candidates_skips_id_keyed_sibling_entities():
    # Two records keyed by different identifiers under one prefix are
    # different referents by construction (arxiv:X vs arxiv:Y) — never
    # listed, even at similarity 1.0. A non-id cross-entity pair at the
    # same similarity is the control and still lists.
    recs = [
        _slot_rec("arxiv:2602-05665|relevance", "arxiv:2602-05665",
                  "relevance", "graph memory survey", _vec(1, 0)),
        _slot_rec("arxiv:2604-12285|relevance", "arxiv:2604-12285",
                  "relevance", "graph memory hierarchy", _vec(1, 0)),
        _slot_rec("gam-survey|relevance", "gam survey", "relevance",
                  "graph memory survey notes", _vec(1, 0)),
    ]
    out = gc.slot_duplicate_candidates(recs, min_similarity=0.80)
    pairs = {(c["a_key"], c["b_key"]) for c in out}
    assert ("arxiv:2602-05665|relevance", "arxiv:2604-12285|relevance") not in pairs
    assert ("arxiv:2602-05665|relevance", "gam-survey|relevance") in pairs
    assert ("arxiv:2604-12285|relevance", "gam-survey|relevance") in pairs


# ── junk-name gate (also the write-time gate's rule source) ──────────────────

def test_junk_name_reason_blocks_known_junk_classes():
    assert junk_name_reason("a<->b") == "concat-artifact"
    assert junk_name_reason("memory_recall->recall.py") == "concat-artifact"
    assert junk_name_reason("42") == "bare-number"
    assert junk_name_reason("done") == "status-word"
    assert junk_name_reason("  ") == "empty"


def test_junk_name_reason_allows_legitimate_names():
    # Short names are legitimate at write time (Go, uv) — they remain
    # review-queue material, judged by degree, not write-blocked.
    assert junk_name_reason("Go") is None
    assert junk_name_reason("PostgreSQL") is None
    assert junk_name_reason("RTX 4090") is None


def test_junk_name_reason_blocks_2026_07_02_cleanup_classes():
    # Every class below dominated the 612 hand-deleted entities of the
    # 2026-07-02 live-cortex cleanup; the gate must stop the re-supply.
    assert junk_name_reason("236 memories") == "count-prefix"
    assert junk_name_reason("5 type-violation junk edges") == "count-prefix"
    assert junk_name_reason("2026-07-02") == "bare-date"
    assert junk_name_reason("pseudolife_memory-20260702-194002.sql.gz") == "dump-file"
    assert junk_name_reason("data/backups/pseudolife_memory-20260624-200948.sql") == "dump-file"
    assert junk_name_reason("pseudolife-daemon:0.2.0-pre-gi") == "image-tag"
    assert junk_name_reason("docker compose -f ops/docker-compose.yml build x") == "command-string"
    assert junk_name_reason("python -m pseudolife_memory.web.devserver") == "command-string"
    assert junk_name_reason("LOCAL master = 8e2b992") == "hash-status"
    assert junk_name_reason("Action: accept-link") == "action-prefix"
    assert junk_name_reason(
        "deploy a schema change to the live pseudolife-mcp daemon") == "sentence"
    assert junk_name_reason("P3 SURFACE POLISH") == "status-shard"
    assert junk_name_reason("P1_roadmap_item") == "status-shard"


def test_junk_name_reason_new_rules_spare_legitimate_names():
    # Near-misses for each new rule that must stay storable.
    assert junk_name_reason("2026-07-02 review roadmap") is None    # dated title, not bare date
    assert junk_name_reason("arXiv:2606.22844") is None             # 2-part id, not an image tag
    assert junk_name_reason("3d-force-graph@1.73.6") is None        # versioned lib (@, not :)
    assert junk_name_reason("8-band continuum") is None             # hyphenated, not count-prefix
    assert junk_name_reason("docker compose") is None               # tool name, not a command line
    assert junk_name_reason("backup.ps1 off-disk mirror") is None   # short noun phrase
    assert junk_name_reason("Language Models Need Sleep") is None   # short paper name
    assert junk_name_reason(
        "Track A (graphify-derived recall hub-gating)") is None     # 5 tokens < sentence floor
    assert junk_name_reason("AllowTelemetry=0 at both HKLM Policies") is None  # '=' but no hash
    assert junk_name_reason("P2P protocol") is None                 # P<digit><letter>: no shard boundary


def test_junk_name_reason_blocks_metric_readings_and_lists():
    # 2026-07-11 curation classes: metric readings and captured enumerations
    assert junk_name_reason("stale 0.8") == "metric-reading"
    assert junk_name_reason("stale 0.0") == "metric-reading"
    assert junk_name_reason("stale_leak 0.7-0.8") == "metric-reading"
    assert junk_name_reason("data/, ops/.env, *.pt") == "list-artifact"


def test_junk_name_reason_spares_metric_and_list_near_misses():
    assert junk_name_reason("CUDA Toolkit 13.1") is None        # uppercase token
    assert junk_name_reason("Gemma 4 E4B") is None              # non-decimal tail
    assert junk_name_reason("User (jdoe, jdoe@example.com)") is None
    assert junk_name_reason("8-band continuum") is None


def test_junk_entities_flags_metric_readings_and_lists():
    from pseudolife_memory.memory.graph_consolidation import junk_entities
    ents = [{"id": 1, "display": "stale 0.8"},
            {"id": 2, "display": "data/, ops/.env, *.pt"}]
    out = junk_entities(ents, [], max_degree=1)
    assert {(j["display"], j["reason"]) for j in out} == {
        ("stale 0.8", "metric-reading"),
        ("data/, ops/.env, *.pt", "list-artifact")}
    # list-artifact is degree-agnostic (like concat-artifact); metric-reading
    # respects the degree cap
    out2 = junk_entities(ents, [], max_degree=-1)
    assert [j["reason"] for j in out2] == ["list-artifact"]


def test_junk_entities_flags_resolvable_compounds_only():
    from pseudolife_memory.memory.graph_consolidation import junk_entities
    # Slash compounds must be SPACED since 2026-09-02 (an unspaced slash is
    # a ref/path/route separator — see _COMPOUND_SEP).
    ents = [{"id": 1, "display": "memory_lesson_search / world_search"},
            {"id": 2, "display": "pg+extractor"},
            {"id": 3, "display": "ops/backup.ps1"},       # extension-exempt
            {"id": 4, "display": "C++"}]                  # empty right side
    known = frozenset({"memory-lesson-search", "world-search", "pg",
                       "extractor", "ops", "backup-ps1"})
    out = junk_entities(ents, [], max_degree=1, known_norms=known)
    reasons = {j["display"]: j["reason"] for j in out}
    assert reasons.get("memory_lesson_search / world_search") == "compound-artifact"
    assert reasons.get("pg+extractor") == "compound-artifact"
    assert "ops/backup.ps1" not in reasons
    assert "C++" not in reasons
    # without known_norms (default) nothing is flagged as compound
    out2 = junk_entities(ents, [], max_degree=1)
    assert all(j["reason"] != "compound-artifact" for j in out2)


# ── variant tokens & conflicts ────────────────────────────────────────────

def test_variant_tokens_extract_size_quant_version():
    assert variant_tokens("Gemma 4 E4B") == frozenset({"e4b"})
    toks = variant_tokens("gemma-4-26B_q4_0-it.gguf")
    assert "26b" in toks and "q4-0" in toks
    assert variant_tokens("pseudolife-daemon:0.2.0") == frozenset({"0.2.0"})
    assert variant_tokens("plain name") == frozenset()


def test_variant_conflict_blocks_cross_model_pairs():
    # the 9 merge proposals hand-rejected on 2026-07-11
    assert variant_conflict("Gemma-4-E4B-QAT (UD-Q4_K_XL)",
                            "gemma-4-E2B-it-qat-UD-Q4_K_XL")
    assert variant_conflict("gemma-E4B Q4_K_M", "Gemma-4-E4B-QAT (UD-Q4_K_XL)")
    assert variant_conflict("gemma-4-26B", "Gemma 4 E4B")
    assert variant_conflict("Qwen3.5-4B", "Qwen3.6-27B")
    # uppercase is the canonical GGUF quant spelling
    assert variant_conflict("gemma Q4_0", "gemma Q8_0")


def test_variant_conflict_allows_same_or_absent_variants():
    assert not variant_conflict("Gemma 4 E4B", "gemma-4-E4B-it base")
    assert not variant_conflict("update.ps1", "ops/update.ps1")
    assert not variant_conflict("Claude shim", "evals/claude_shim.py")
    # underscore vs hyphen quant forms are the SAME token (norm_name folds _ to -)
    assert not variant_conflict("UD-Q4_K_XL quant", "ud-q4-k-xl quant")
    # Q4 alone (quarter label) is NOT a variant token — it needs _K suffix
    assert not variant_conflict("Q4 2026 roadmap", "Q1 2027 roadmap")


def test_variant_tokens_quarter_labels_not_variants():
    # Quarter labels (Q1, Q4 standalone) are NOT variant tokens; only Q<digit>_K*
    assert variant_tokens("Q4 2026 roadmap") == frozenset()
    assert variant_tokens("Q1 2027 roadmap") == frozenset()
    # Q4_K forms ARE variants
    assert "q4-k" in variant_tokens("Q4_K 2026 quant")


# ── 2026-09-02 junk-judge feedback: shape classes with a measured FP tail ──

def test_slot_key_artifact_spares_code_symbols_and_versions():
    # The 2026-09-02 junk panel scored slot-key-artifact at 3/10 precision:
    # seven flags were dotted CODE/CONFIG paths whose prefix happened to be
    # a known entity (cortex._norm_key, cms.store, nomem_arm.nomem_system,
    # lme.RAG_TOP_K, memory.dream.extractor_reasoning_effort) or a version
    # dot (gpt-5.6-luna). A flattened slot key came through the cortex
    # normalizer, so its tail is lowercase-hyphenated prose; a code symbol
    # keeps underscores / capitals / a leading underscore, and a version
    # dot sits between digits.
    ents = [
        {"id": 1, "canonical": "cortex", "display": "cortex", "etype": None},
        {"id": 2, "canonical": "cortex-norm-key", "display": "cortex._norm_key", "etype": None},
        {"id": 3, "canonical": "nomem-arm", "display": "nomem_arm", "etype": None},
        {"id": 4, "canonical": "nomem-arm-nomem-system", "display": "nomem_arm.nomem_system", "etype": None},
        {"id": 5, "canonical": "lme", "display": "lme", "etype": None},
        {"id": 6, "canonical": "lme-rag-top-k", "display": "lme.RAG_TOP_K", "etype": None},
        {"id": 7, "canonical": "gpt-5", "display": "gpt-5", "etype": None},
        {"id": 8, "canonical": "gpt-5-6-luna", "display": "gpt-5.6-luna", "etype": None},
        {"id": 9, "canonical": "memory-dream", "display": "memory.dream", "etype": None},
        {"id": 10, "canonical": "memory-dream-extractor-reasoning-effort",
         "display": "memory.dream.extractor_reasoning_effort", "etype": None},
        # still a slot-key artifact: normalized prose tail under a known head
        {"id": 11, "canonical": "qwen38-migration", "display": "qwen38-migration", "etype": None},
        {"id": 12, "canonical": "qwen38-migration-deferred-work",
         "display": "qwen38-migration.deferred-work", "etype": None},
        {"id": 13, "canonical": "gpu-window-queue-pending-slot",
         "display": "gpu-window-queue.pending slot", "etype": None},
        {"id": 14, "canonical": "gpu-window-queue", "display": "gpu-window-queue", "etype": None},
    ]
    known = frozenset(e["canonical"] for e in ents)
    out = {j["entity_id"]: j["reason"]
           for j in gc.junk_entities(ents, [], max_degree=1, known_norms=known)}
    for spared in (2, 4, 6, 8, 10):
        assert out.get(spared) != "slot-key-artifact", ents[spared - 1]["display"]
    assert out.get(12) == "slot-key-artifact"
    assert out.get(13) == "slot-key-artifact"


def test_compound_artifact_requires_spaced_slash():
    # origin/master and fix/autostart-elevation-guidance were flagged as
    # compounds because both halves are known entities — but an unspaced
    # slash is a ref/path separator. A genuine slash compound in the corpus
    # is spaced ("codex-cli / multi-provider-installer"); "+" compounds
    # (pg+extractor, 2026-07-11) stay flagged either way.
    ents = [
        {"id": 1, "canonical": "origin", "display": "origin", "etype": None},
        {"id": 2, "canonical": "master", "display": "master", "etype": None},
        {"id": 3, "canonical": "origin-master", "display": "origin/master", "etype": None},
        {"id": 4, "canonical": "codex-cli", "display": "codex-cli", "etype": None},
        {"id": 5, "canonical": "multi-provider-installer", "display": "multi-provider-installer", "etype": None},
        {"id": 6, "canonical": "codex-cli-multi-provider-installer",
         "display": "codex-cli / multi-provider-installer", "etype": None},
        {"id": 7, "canonical": "pg", "display": "pg", "etype": None},
        {"id": 8, "canonical": "extractor", "display": "extractor", "etype": None},
        {"id": 9, "canonical": "pg-extractor", "display": "pg+extractor", "etype": None},
    ]
    known = frozenset(e["canonical"] for e in ents)
    out = {j["entity_id"]: j["reason"]
           for j in gc.junk_entities(ents, [], max_degree=1, known_norms=known)}
    assert out.get(3) != "compound-artifact"
    assert out.get(6) == "compound-artifact"
    assert out.get(9) == "compound-artifact"

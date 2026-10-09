"""Canonical graph-read inputs, separate from the graph-store row inventory."""
import random

MUTANTS = ["graph-read-alias", "graph-read-degree", "graph-read-transitive",
           "graph-read-inverse-closure", "graph-read-provenance", "graph-read-depth",
           "graph-read-path-order", "graph-read-suggestions"]


def operations():
    def edge(src, relation, dst, **metadata):
        return dict(src=src, relation=relation, dst=dst, **metadata)
    registry = {"a-trans": dict(transitive=True, inverse_of="z-inverse"),
                "uses": {}, "z-inverse": {}}
    chain = [edge(i, "uses", i + 1) for i in range(1, 7)]
    cases = [dict(op="alias_canonical_map", entities=[dict(id=1, canonical="node-a"),
                dict(id=2, canonical="node-b")], aliases={"1": ["first", "node-b"], "9": ["orphan"]}),
        dict(op="alias_canonical_map", entities=[], aliases={}),
        dict(op="degree_counts", edges=[dict(src_id=3, dst_id=1), dict(src_id=1, dst_id=1)]),
        dict(op="degree_counts", edges=[]),
        dict(op="degrees_by_name", edges=[dict(src_id=3, dst_id=1), dict(src_id=1, dst_id=1),
            dict(src_id=2, dst_id=1)], entities=[dict(id=3, display="same"), dict(id=1, display="same"),
                                              dict(id=2, display="two")])]
    for raw in [" Depends_On ", "dependson", "unknown", "az", "", "École!"]:
        cases.append(dict(op="resolve_relation", known=["depends-on", "uses", "part-of", "ab", "ac", "ad", "ae", "école"], raw=raw))
    for length in [199, 200, 201]:
        cases.append(dict(op="resolve_relation", known=["a" * length + "b", "b" + "a" * length,
            "a" * (length + 1)], raw="a" * length + "c"))
    for src, dst, hops in [(1, 1, -1), (99, 99, 0), (1, 7, 5), (1, 7, 6), (7, 1, 8), (1, 99, 8)]:
        cases.append(dict(op="shortest_path", edges=[dict(src_id=e["src"], dst_id=e["dst"]) for e in chain],
                          src_id=src, dst_id=dst, max_hops=hops))
    transitive = [edge(1, "a-trans", 2), edge(2, "a-trans", 3)]
    cycles = transitive + [edge(3, "a-trans", 1), edge(3, "uses", 4), edge(4, "uses", 4)]
    for edges in [[], transitive, cycles, chain, chain + [chain[0]]]:
        cases.append(dict(op="derive_edges", edges=edges, relations=registry))
        for depth in [-1, 1, 2, 99]:
            cases.append(dict(op="build_subgraph", edges=edges, relations=registry, root=1, depth=depth, to=7))
    collision = {"r-a": dict(inverse_of="r-b"), "r-b": {}, "r-c": dict(inverse_of="r-b")}
    cases.append(dict(op="derive_edges", edges=[edge(1, "r-a", 2), edge(1, "r-c", 2)], relations=collision))
    cases.append(dict(op="build_subgraph", edges=[edge(a, "uses", b) for a, b in
        [(1, 4), (4, 6), (6, 8), (1, 3), (3, 5), (5, 8)]], relations={"uses": {}}, root=1, depth=1, to=8))
    rng = random.Random(37)
    for index in range(100):
        ids = rng.sample(range(1000, 1050), 10) if index % 2 else rng.sample(range(1, 50), 10)
        edges = [edge(ids[i], rng.choice(["a-trans", "uses"]), ids[i + 1]) for i in range(9)]
        edges += [edge(rng.choice(ids), rng.choice(["a-trans", "uses"]), rng.choice(ids)) for _ in range(8)]
        rng.shuffle(edges)
        cases.append(dict(op="build_subgraph", edges=edges, relations=registry, root=ids[0], depth=1, to=ids[-1]))
    # Actual GraphStore reads: registry and edges come from the disposable bank.
    cases += [dict(op="upsert_relation", name="z-inverse", description="fixture inverse"),
              dict(op="upsert_relation", name="a-trans", description="fixture transitive", transitive=True, inverse_of="z-inverse")]
    for src, dst in [(1, 2), (2, 3)]:
        cases.append(dict(op="upsert_edge", src_id=src, relation="a-trans", dst_id=dst, confidence=0.8, origin="user"))
    for root in [1, 2, 9]:
        for depth in [1, 3]:
            cases.append(dict(op="subgraph", root=root, depth=depth, to=3))
    return cases

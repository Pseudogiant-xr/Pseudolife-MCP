"""Write the golden cases for the Rust graph-insight refresh
(``src/graph_insight.rs``, task P2b under Option B).

Every expected value comes from calling ``memory/graph_insight.py`` in this
checkout, in two modes per case:

* ``exact``: NetworkX's Louvain with ``seed.shuffle`` disabled, the
  oversized-community split run on the subgraph in its parent's node and
  adjacency order, and betweenness exact at every size. That is the
  deterministic algorithm the Rust port implements, so its unit test
  asserts byte equality against these outputs (the digest, the summaries
  and the assignment).
* ``real``: the functions as the Python daemon runs them (``seed=42``, the
  hash-ordered subgraph view, betweenness sampled above
  ``betweenness_sample`` nodes). The Rust test compares its own output with
  these structurally: partition agreement (adjusted Rand index), community
  count, size distribution, and the god-node and surprise sets. Each
  measure must fall inside the envelope Python itself spans across seeds
  1..20 against seed 42 (``envelope``), so the port disagrees with Python's
  seed no more than another seed of Python would.

The corpus: hand-written graphs for every branch of the analytics
(self-loops, parallel and opposite edges, agent and low-confidence edges,
a prior assignment to remap, contested facts, low cohesion, non-ASCII
displays, zero and negative ``top_n``), planted-partition graphs at sizes
where Python's betweenness is exact, one past ``betweenness_sample`` where
it samples, and a small ``max_community_fraction`` that forces the split.

Output: ``goldens/graph-insight.json``.

usage: python gen_graph_insight_goldens.py
"""
from __future__ import annotations

import json
import random
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TARGET = Path(__file__).resolve().parent / "goldens" / "graph-insight.json"
sys.path.insert(0, str(REPO))

import networkx as nx  # noqa: E402

from pseudolife_memory.memory import graph_insight as gi  # noqa: E402

COMPUTED_AT = 1792000000.25
DEFAULT_CFG = {"resolution": 1.0, "max_community_fraction": 0.25, "god_nodes_top_n": 10,
               "surprises_top_n": 10, "questions_top_n": 7, "betweenness_sample": 200}
SEEDS = range(1, 21)


class _NoShuffle(random.Random):
    """A ``random.Random`` whose ``shuffle`` keeps the order."""

    def shuffle(self, x, *args, **kwargs):  # noqa: D401, ARG002
        return None


def _parent_order(g):
    """A subgraph view rebuilt in its parent's node and adjacency order."""
    base = getattr(g, "_graph", None)
    if base is None:
        return g
    h = nx.Graph()
    nodes = [n for n in base if n in g]
    h.add_nodes_from(nodes)
    for n in nodes:
        for nbr, dd in base._adj[n].items():
            if nbr in g:
                h._adj[n][nbr] = dd
    return h


def _partition_seeded(seed):
    def part(g, *, resolution, algorithm):  # noqa: ARG001
        return list(nx.community.louvain_communities(g, seed=seed, resolution=resolution))
    return part


def _partition_exact(g, *, resolution, algorithm):  # noqa: ARG001
    return list(nx.community.louvain_communities(
        _parent_order(g), seed=_NoShuffle(42), resolution=resolution))


def _betweenness_exact(g, sample):  # noqa: ARG001
    if not g.number_of_edges():
        return {}
    return nx.betweenness_centrality(g, k=None, seed=42)


def _betweenness_seeded(seed):
    def bc(g, sample):
        if not g.number_of_edges():
            return {}
        k = sample if (sample and g.number_of_nodes() > sample) else None
        return nx.betweenness_centrality(g, k=k, seed=seed)
    return bc


def run(case, partition, betweenness):
    """``_refresh_graph_insight``'s computation with the two seams swapped."""
    saved = gi._partition, gi._betweenness
    gi._partition, gi._betweenness = partition, betweenness
    try:
        cfg = {**DEFAULT_CFG, **case.get("cfg", {})}
        edges = case["edges"]
        entities = case["entities"]
        prior = {int(k): v for k, v in case.get("prior", {}).items()}
        communities = gi.detect_communities(
            edges, resolution=cfg["resolution"],
            max_community_fraction=cfg["max_community_fraction"], algorithm="louvain")
        communities = gi.remap_to_previous(communities, prior)
        summaries = gi.summarize_communities(communities, edges, entities)
        assignment = {eid: cid for cid, ids in communities.items() for eid in ids}
        digest = gi.build_digest(
            communities, summaries, edges, entities, case.get("contested", []), COMPUTED_AT,
            god_top_n=cfg["god_nodes_top_n"], surprises_top_n=cfg["surprises_top_n"],
            questions_top_n=cfg["questions_top_n"],
            betweenness_sample=cfg["betweenness_sample"])
    finally:
        gi._partition, gi._betweenness = saved
    return {"summaries": summaries,
            "assignment": sorted([e, c] for e, c in assignment.items()),
            "digest": digest}


# -- structural measures (the Rust test computes the same ones) -------------

def _comb2(x):
    return x * (x - 1) / 2


def ari(a: dict, b: dict) -> float:
    """Adjusted Rand index over the entities both assignments hold."""
    keys = sorted(set(a) & set(b))
    n = len(keys)
    if n < 2:
        return 1.0
    table: dict = {}
    for k in keys:
        table[(a[k], b[k])] = table.get((a[k], b[k]), 0) + 1
    rows: dict = {}
    cols: dict = {}
    for (x, y), v in table.items():
        rows[x] = rows.get(x, 0) + v
        cols[y] = cols.get(y, 0) + v
    index = sum(_comb2(v) for v in table.values())
    sr = sum(_comb2(v) for v in rows.values())
    sc = sum(_comb2(v) for v in cols.values())
    expected = sr * sc / _comb2(n)
    maximum = (sr + sc) / 2
    if maximum == expected:
        return 1.0
    return (index - expected) / (maximum - expected)


def jaccard(a: set, b: set) -> float:
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def sizes_distance(a: list, b: list) -> float:
    """L1 distance between sorted size vectors, over the entity count."""
    a, b = sorted(a, reverse=True), sorted(b, reverse=True)
    width = max(len(a), len(b))
    a += [0] * (width - len(a))
    b += [0] * (width - len(b))
    total = max(1, sum(a))
    return sum(abs(x - y) for x, y in zip(a, b)) / total


def structure(out) -> dict:
    """What the structural comparison reads from one output."""
    return {"assignment": out["assignment"],
            "sizes": [s["size"] for s in out["summaries"]],
            "gods": sorted(g["entity_id"] for g in out["digest"]["god_nodes"]),
            "surprises": sorted([s["src"], s["dst"], s["relation"]]
                                for s in out["digest"]["surprises"])}


def measures(out, ref) -> dict:
    """The structural measures between two ``structure`` values."""
    a = {e: c for e, c in out["assignment"]}
    b = {e: c for e, c in ref["assignment"]}
    sa, sb = out["sizes"], ref["sizes"]
    return {"ari": ari(a, b), "count_delta": abs(len(sa) - len(sb)),
            "sizes_distance": sizes_distance(sa, sb),
            "god_jaccard": jaccard(set(out["gods"]), set(ref["gods"])),
            "surprise_jaccard": jaccard({tuple(x) for x in out["surprises"]},
                                        {tuple(x) for x in ref["surprises"]})}


# -- the corpus --------------------------------------------------------------

def edge(src, dst, relation="related_to", confidence=0.8, origin=None):
    return {"src_id": src, "dst_id": dst, "relation": relation,
            "confidence": confidence, "origin": origin}


def ents(ids, names=None):
    names = names or {}
    return [{"id": i, "display": names.get(i, f"node-{i:03d}")} for i in sorted(ids)]


def hand_cases():
    cases = []
    # Two triangles and a bridge; opposite and parallel edges; a self-loop;
    # agent and low-confidence edges; a dangling entity with no edge.
    names = {1: "Ärger", 2: "alpha", 3: "Zeta", 4: "zeta", 5: "éclair", 6: "beta",
             7: "dangling", 8: "gamma"}
    e = [edge(1, 2), edge(2, 3), edge(3, 1, "depends_on"), edge(2, 1, "uses"),
         edge(3, 4, "uses", 0.5, "agent"), edge(4, 5), edge(5, 6), edge(6, 4, origin="agent"),
         edge(5, 5, "self_ref"), edge(6, 8, confidence=0.59), edge(4, 6, "depends_on")]
    cases.append({"name": "two-triangles", "entities": ents(range(1, 9), names), "edges": e,
                  "contested": [{"entity": "svc", "attribute": "port", "value": "8080",
                                 "contender_value": "8081", "contender_origin": "agent"},
                                {"entity": "db", "attribute": "host", "value": "a",
                                 "contender_value": "b", "contender_origin": ""}]})
    # The same graph against a prior assignment (remap onto old ids, with an
    # old id that no longer matches and a gap the fresh ids fill).
    cases.append({**cases[0], "name": "two-triangles-prior",
                  "prior": {"1": 7, "2": 7, "3": 7, "4": 2, "5": 2, "6": 0, "8": 9}})
    # A star tree: low cohesion, a god node with agent edges, isolated leaves.
    star = [edge(100, 100 + i, origin="agent" if i % 3 == 0 else None,
                 confidence=0.4 if i % 4 == 0 else 0.9) for i in range(1, 16)]
    star += [edge(101, 120 + i) for i in range(1, 4)]
    ids = {x for s in star for x in (s["src_id"], s["dst_id"])}
    cases.append({"name": "star-tree", "entities": ents(ids), "edges": star})
    # Zero and negative top_n (Python slices, and surprises keeps one).
    cases.append({**cases[0], "name": "top-n-zero",
                  "cfg": {"god_nodes_top_n": 0, "surprises_top_n": 0, "questions_top_n": 0}})
    cases.append({**cases[2], "name": "top-n-negative",
                  "cfg": {"god_nodes_top_n": -2, "surprises_top_n": -1,
                          "questions_top_n": -3}})
    # Degenerate graphs.
    cases.append({"name": "single-edge", "entities": ents([1, 2]), "edges": [edge(1, 2)]})
    cases.append({"name": "self-loop-only", "entities": ents([1, 2]), "edges": [edge(1, 1)]})
    cases.append({"name": "path", "entities": ents(range(1, 13)),
                  "edges": [edge(i, i + 1) for i in range(1, 12)]})
    # Resolution away from 1.
    cases.append({**cases[2], "name": "star-resolution-2", "cfg": {"resolution": 2.0}})
    return cases


def planted(name, groups, size, p_in, p_out, seed, cfg=None, agent_every=7, base=1000):
    g = nx.planted_partition_graph(groups, size, p_in, p_out, seed=seed)
    rng = random.Random(seed)
    edges = []
    for i, (u, v) in enumerate(sorted(g.edges())):
        # Shuffle direction so the first-appearance order is not sorted.
        if rng.random() < 0.5:
            u, v = v, u
        edges.append(edge(base + u, base + v,
                          rng.choice(["related_to", "uses", "depends_on", "part_of"]),
                          rng.choice([0.4, 0.55, 0.8, 0.9, 1.0]),
                          "agent" if i % agent_every == 0 else rng.choice([None, "user"])))
    rng.shuffle(edges)
    ids = {x for s in edges for x in (s["src_id"], s["dst_id"])}
    ids |= {base + groups * size + k for k in range(3)}  # isolated entities
    return {"name": name, "entities": ents(ids), "edges": edges, "cfg": cfg or {}}


def hub_graph(name, n, m, seed, base=5000):
    """Barabási–Albert: hubs and a long tail, like an entity graph."""
    g = nx.barabasi_albert_graph(n, m, seed=seed)
    rng = random.Random(seed)
    edges = [edge(base + u, base + v, origin="agent" if rng.random() < 0.2 else None)
             for u, v in sorted(g.edges())]
    rng.shuffle(edges)
    ids = {x for s in edges for x in (s["src_id"], s["dst_id"])}
    return {"name": name, "entities": ents(ids), "edges": edges}


def corpus():
    cases = hand_cases()
    cases.append(planted("planted-3x10", 3, 10, 0.8, 0.02, seed=1))
    cases.append(planted("planted-4x20", 4, 20, 0.5, 0.01, seed=2))
    cases.append(planted("planted-6x25", 6, 25, 0.3, 0.005, seed=3))
    cases.append(planted("planted-5x40-split", 5, 40, 0.25, 0.004, seed=4,
                         cfg={"max_community_fraction": 0.05}))
    cases.append(planted("planted-8x40-sampled", 8, 40, 0.2, 0.003, seed=5))
    cases.append(hub_graph("hubs-150", 150, 2, seed=6))
    cases.append(hub_graph("hubs-260-sampled", 260, 2, seed=7))
    return cases


def main() -> int:
    out = []
    for case in corpus():
        exact = run(case, _partition_exact, _betweenness_exact)
        real = structure(run(case, _partition_seeded(42), _betweenness_seeded(42)))
        envelope = [measures(structure(run(case, _partition_seeded(s),
                                           _betweenness_seeded(s))), real)
                    for s in SEEDS]
        env = {"ari_min": min(m["ari"] for m in envelope),
               "ari_median": statistics.median(m["ari"] for m in envelope),
               "count_delta_max": max(m["count_delta"] for m in envelope),
               "sizes_distance_max": max(m["sizes_distance"] for m in envelope),
               "god_jaccard_min": min(m["god_jaccard"] for m in envelope),
               "surprise_jaccard_min": min(m["surprise_jaccard"] for m in envelope)}
        port = measures(structure(exact), real)
        out.append({**case, "exact": exact, "real": real, "envelope": env,
                    "exact_vs_real": port})
        print(f"{case['name']:24s} nodes={len(case['entities']):4d} "
              f"ari={port['ari']:.3f} (min {env['ari_min']:.3f}) "
              f"count_delta={port['count_delta']} (max {env['count_delta_max']}) "
              f"sizes={port['sizes_distance']:.3f} (max {env['sizes_distance_max']:.3f}) "
              f"god={port['god_jaccard']:.2f} (min {env['god_jaccard_min']:.2f}) "
              f"surprise={port['surprise_jaccard']:.2f} "
              f"(min {env['surprise_jaccard_min']:.2f})")
    head = json.dumps({"networkx": nx.__version__, "python": sys.version.split()[0]},
                      ensure_ascii=False)
    lines = ",\n".join(json.dumps(c, ensure_ascii=False, separators=(",", ":")) for c in out)
    TARGET.write_text(head[:-1] + ', "cases": [\n' + lines + "\n]}\n", encoding="utf-8")
    print(f"wrote {TARGET.relative_to(REPO)} ({len(out)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

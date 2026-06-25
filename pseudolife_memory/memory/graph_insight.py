"""Pure topology analytics over the asserted entity graph (Track B).

DB-free and unit-testable like recall.py/graph.py: the service supplies
edges/entities/prior-assignment/contested-facts and persists the results.
Louvain via networkx by default; graspologic Leiden used only if importable.
"""
from __future__ import annotations

import networkx as nx

from pseudolife_memory.graph import degree_counts

_MAX_COMMUNITY_FRACTION = 0.25
_MIN_SPLIT_SIZE = 10


def _undirected(edges: list[dict]) -> nx.Graph:
    g = nx.Graph()
    for e in edges:
        g.add_edge(e["src_id"], e["dst_id"])
    return g


def _partition(g: nx.Graph, *, resolution: float, algorithm: str) -> list[set]:
    """Return a list of node-sets. Leiden if asked-for AND importable, else Louvain."""
    if algorithm == "leiden":
        try:
            from graspologic.partition import leiden
            result = leiden(g, resolution=resolution, random_seed=42, trials=1)
            groups: dict[int, set] = {}
            for node, cid in result.items():
                groups.setdefault(cid, set()).add(node)
            return list(groups.values())
        except Exception:
            pass  # fall back to Louvain
    return list(nx.community.louvain_communities(g, seed=42, resolution=resolution))


def _reindex(groups: list[list[int]]) -> dict[int, list[int]]:
    """Size-desc, total-ordered (tuple(sorted(ids)) tiebreak) -> {cid: [ids]}."""
    groups = [sorted(map(int, g)) for g in groups]
    groups.sort(key=lambda ids: (-len(ids), tuple(ids)))
    return {i: ids for i, ids in enumerate(groups)}


def detect_communities(edges: list[dict], *, resolution: float = 1.0,
                       max_community_fraction: float = 0.25,
                       algorithm: str = "louvain") -> dict[int, list[int]]:
    if not edges:
        return {}
    g = _undirected(edges)
    n = g.number_of_nodes()
    groups = [list(s) for s in _partition(g, resolution=resolution, algorithm=algorithm)]
    # Split oversized communities with a second partition pass on the subgraph.
    max_size = max(_MIN_SPLIT_SIZE, int(n * max_community_fraction))
    final: list[list[int]] = []
    for members in groups:
        if len(members) > max_size and g.subgraph(members).number_of_edges() > 0:
            sub = _partition(g.subgraph(members), resolution=resolution, algorithm=algorithm)
            final.extend([list(s) for s in sub] if len(sub) > 1 else [members])
        else:
            final.append(members)
    return _reindex(final)


def cohesion_score(edges: list[dict], member_ids: list[int]) -> float:
    n = len(member_ids)
    if n <= 1:
        return 1.0
    members = set(member_ids)
    # Count distinct unordered pairs so parallel relations between the same two
    # entities can't push cohesion above 1.0.
    actual = len({frozenset((e["src_id"], e["dst_id"])) for e in edges
                  if e["src_id"] in members and e["dst_id"] in members
                  and e["src_id"] != e["dst_id"]})
    possible = n * (n - 1) / 2
    return actual / possible if possible else 0.0


def remap_to_previous(communities: dict[int, list[int]],
                      prior: dict[int, int]) -> dict[int, list[int]]:
    """Greedy overlap match: each new community inherits the prior community id it
    most overlaps; unmatched get fresh ids in deterministic (size-desc) order."""
    if not prior:
        return {c: list(ids) for c, ids in communities.items()}  # copy — never alias input
    old_sets: dict[int, set] = {}
    for node, oid in prior.items():
        old_sets.setdefault(oid, set()).add(node)
    overlaps = []
    for new_cid, ids in communities.items():
        s = set(ids)
        for old_cid, old in old_sets.items():
            inter = len(s & old)
            if inter:
                overlaps.append((inter, old_cid, new_cid))
    overlaps.sort(key=lambda x: (-x[0], x[1], x[2]))
    new_to_final: dict[int, int] = {}
    used_old: set[int] = set()
    matched_new: set[int] = set()
    for _inter, old_cid, new_cid in overlaps:
        if old_cid in used_old or new_cid in matched_new:
            continue
        new_to_final[new_cid] = old_cid
        used_old.add(old_cid)
        matched_new.add(new_cid)
    unmatched = [c for c in communities if c not in matched_new]
    unmatched.sort(key=lambda c: (-len(communities[c]), tuple(sorted(communities[c]))))
    nxt = 0
    for new_cid in unmatched:
        while nxt in used_old:
            nxt += 1
        new_to_final[new_cid] = nxt
        used_old.add(nxt)
        nxt += 1
    return {new_to_final[c]: sorted(ids) for c, ids in communities.items()}


def _node_community(communities: dict[int, list[int]]) -> dict[int, int]:
    return {n: cid for cid, ids in communities.items() for n in ids}


def summarize_communities(communities: dict[int, list[int]], edges: list[dict],
                          entities: list[dict]) -> list[dict]:
    """Per-community {id, label, size, cohesion}. Label = highest-degree member's display."""
    deg = degree_counts(edges)
    disp = {e["id"]: e["display"] for e in entities}
    out = []
    for cid, ids in sorted(communities.items()):
        top = max(ids, key=lambda i: (deg.get(i, 0), i)) if ids else None
        out.append({
            "id": cid,
            "label": disp.get(top, str(top)) if top is not None else f"community-{cid}",
            "size": len(ids),
            "cohesion": round(cohesion_score(edges, ids), 4),
        })
    return out


_LOW_CONFIDENCE = 0.6
_PERIPHERAL_DEGREE = 2


def god_nodes(edges: list[dict], entities: list[dict], *, top_n: int = 10,
              exclude_etypes: tuple[str, ...] = ()) -> list[dict]:
    deg = degree_counts(edges)
    excluded = {e["id"] for e in entities if e.get("etype") in exclude_etypes}
    disp = {e["id"]: e["display"] for e in entities}
    ranked = sorted(
        ((eid, d) for eid, d in deg.items() if eid not in excluded),
        key=lambda kv: (-kv[1], kv[0]),
    )
    return [{"entity_id": eid, "display": disp.get(eid, str(eid)), "degree": d}
            for eid, d in ranked[:top_n]]


def surprising_connections(edges: list[dict], entities: list[dict],
                           node_community: dict[int, int], *,
                           top_n: int = 10) -> list[dict]:
    deg = degree_counts(edges)
    disp = {e["id"]: e["display"] for e in entities}
    god_cutoff = _god_degree_cutoff(deg)
    scored = []
    for e in edges:
        u, v = e["src_id"], e["dst_id"]
        if u == v:
            continue
        score, reasons = 0, []
        if (e.get("confidence", 1.0) < _LOW_CONFIDENCE) or (e.get("origin") == "agent"):
            score += 2
            reasons.append("agent-inferred or low-confidence")
        cu, cv = node_community.get(u), node_community.get(v)
        cross = cu is not None and cv is not None and cu != cv
        if cross:
            score += 3
            reasons.append(f"bridge between community {cu} and {cv}")
        du, dv = deg.get(u, 0), deg.get(v, 0)
        if min(du, dv) <= _PERIPHERAL_DEGREE and max(du, dv) >= god_cutoff:
            score += 1
            reasons.append("peripheral node reaches a hub")
        if score <= 0:
            continue
        pair = tuple(sorted((cu, cv))) if cross else None
        scored.append({
            "src": disp.get(u, str(u)), "dst": disp.get(v, str(v)),
            "relation": e.get("relation", ""), "confidence": e.get("confidence"),
            "origin": e.get("origin"), "score": score,
            "why": "; ".join(reasons), "_pair": pair,
        })
    scored.sort(key=lambda s: (-s["score"], s["src"], s["dst"]))
    seen_pairs: set = set()
    out = []
    for s in scored:
        pair = s.pop("_pair")
        if pair is not None:
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
        out.append(s)
        if len(out) >= top_n:
            break
    return out


def _god_degree_cutoff(deg: dict[int, int]) -> int:
    """Degree at/above which a node counts as a 'hub' for peripheral->hub. The
    10th-highest degree, floored at 5 (mirrors the god_nodes top-N notion)."""
    if not deg:
        return 5
    top = sorted(deg.values(), reverse=True)
    return max(5, top[min(len(top) - 1, 9)])


_LOW_COHESION = 0.15
_MIN_COHESION_SIZE = 5
_AGENT_EDGE_MIN = 2


def suggest_questions(edges: list[dict], entities: list[dict],
                      communities: dict[int, list[int]], node_community: dict[int, int],
                      contested_facts: list[dict], summaries: list[dict], *,
                      top_n: int = 7, betweenness_sample: int = 200) -> list[dict]:
    disp = {e["id"]: e["display"] for e in entities}
    deg = degree_counts(edges)
    label = {s["id"]: s["label"] for s in summaries}
    questions: list[dict] = []

    # 1. contested facts (our richest signal)
    for c in contested_facts:
        questions.append({
            "type": "contested_fact",
            "question": (f"Which value of `{c['attribute']}` for `{c['entity']}` is "
                         f"correct — `{c['value']}` or `{c['contender_value']}`?"),
            "why": f"Contested fact; rival from origin={c.get('contender_origin')}.",
        })

    g = _undirected(edges)
    # 2. bridge entities (high betweenness spanning >=2 communities)
    if g.number_of_edges():
        k = betweenness_sample if (betweenness_sample and g.number_of_nodes() > betweenness_sample) else None
        bc = nx.betweenness_centrality(g, k=k, seed=42)
        bridges = sorted(((n, s) for n, s in bc.items() if s > 0),
                         key=lambda kv: (-kv[1], kv[0]))
        for n, _s in bridges[:3]:
            cid = node_community.get(n)
            other = {node_community.get(nb) for nb in g.neighbors(n)} - {cid, None}
            nd = disp.get(n, n)
            # The communities this node bridges *to*, labelled. Never the node's
            # own community, and never a label that collides with the node itself
            # (community labels are the top-degree member, frequently the bridge
            # node — which produced "Why does X connect X to ..."). Dedup labels.
            names: list[str] = []
            for c in sorted(other):
                lab = label.get(c, f"community {c}")
                if lab != nd and lab not in names:
                    names.append(lab)
            if names:
                targets = ", ".join(f"`{x}`" for x in names)
                questions.append({
                    "type": "bridge_entity",
                    "question": f"Why is `{nd}` a bridge to {targets}?",
                    "why": "High betweenness — a cross-community bridge.",
                })

    # 3. verify a god-node with many agent-origin edges
    for gnode in god_nodes(edges, entities, top_n=5):
        eid = gnode["entity_id"]
        agent_n = sum(1 for e in edges
                      if (e["src_id"] == eid or e["dst_id"] == eid) and e.get("origin") == "agent")
        if agent_n >= _AGENT_EDGE_MIN:
            questions.append({
                "type": "verify_inferred",
                "question": f"Are the {agent_n} inferred relationships involving `{gnode['display']}` correct?",
                "why": f"{agent_n} agent-origin (dream-inferred) edges need verification.",
            })
            break

    # 4. isolated entities (degree <= 1)
    isolated = [e["display"] for e in entities if deg.get(e["id"], 0) <= 1]
    if isolated:
        shown = ", ".join(f"`{n}`" for n in isolated[:3])
        questions.append({
            "type": "isolated_entity",
            "question": f"What connects {shown} to the rest of the graph?",
            "why": f"{len(isolated)} weakly-connected entities — possible gaps.",
        })

    # 5. low-cohesion communities
    for s in summaries:
        if s["cohesion"] < _LOW_COHESION and s["size"] >= _MIN_COHESION_SIZE:
            questions.append({
                "type": "low_cohesion",
                "question": f"Should community `{s['label']}` be split into tighter groups?",
                "why": f"Cohesion {s['cohesion']} over {s['size']} entities.",
            })

    return questions[:top_n]


def build_digest(communities: dict[int, list[int]], summaries: list[dict],
                 edges: list[dict], entities: list[dict], contested_facts: list[dict],
                 computed_at: float, *, god_top_n: int = 10, surprises_top_n: int = 10,
                 questions_top_n: int = 7, betweenness_sample: int = 200) -> dict:
    node_comm = _node_community(communities)
    return {
        "computed_at": computed_at,
        "communities": summaries,
        "god_nodes": god_nodes(edges, entities, top_n=god_top_n),
        "surprises": surprising_connections(edges, entities, node_comm, top_n=surprises_top_n),
        "questions": suggest_questions(edges, entities, communities, node_comm,
                                       contested_facts, summaries, top_n=questions_top_n,
                                       betweenness_sample=betweenness_sample),
        "totals": {"entities": len(entities), "edges": len(edges),
                   "communities": len(communities)},
    }

"""The graph-insight refresh's differential scenarios (task P2b), merged into
``w2e.py``'s ``SCENARIOS`` by ``scenarios(w2e_module)``.

Every ``memory_dream run`` ends with the graph-insight refresh: communities
over the asserted edges (``communities``, ``entity_communities``) and the
``graph_digest`` meta snapshot. Under Option B the Rust daemon's Louvain
visits nodes in graph order where NetworkX shuffles them with seed 42, so a
bank compares exactly only when its partition does not depend on the
visiting order. These scenarios seed graphs with planted structure (dense
clusters joined by single bridges), and
``test_w2e_lifecycle.py::test_graph_insight_scenarios_partition_independently_of_order``
checks each one against ``memory/graph_insight.py`` with and without the
shuffle before it is trusted here. The structural comparison for graphs
whose partition does depend on the order is ``src/graph_insight_tests.rs``
over ``goldens/graph-insight.json``.

Normalizers: ``communities.computed_at`` and
``entity_communities.computed_at`` and ``meta.graph_digest.computed_at`` are
the refresh's clock (null-or-not), as every other write clock.
"""
from __future__ import annotations

# (table, column) wall clocks the refresh writes (w2e.CLOCK).
CLOCK = {("communities", "computed_at"), ("entity_communities", "computed_at")}
# meta key -> wall-clock keys inside its value (w2e.META_CLOCK).
META_CLOCK = {"graph_digest": {"computed_at"}}
# Deliberate breaks (``src/graph_insight.rs``), each caught by
# dream-insight-planted: the second refresh renumbers the old communities
# without the remap, and the digest row never appears without its write.
MUTANTS = ["mcp-dream-insight-no-remap", "mcp-dream-insight-no-digest"]

# The insight on; everything else as the dream-run scenarios have it.
INSIGHT_CONFIG = (
    "memory:\n"
    "  lessons:\n    enabled: false\n"
    "  dream:\n    enabled: false\n    stall_notice: false\n"
)
INSIGHT_OFF_CONFIG = INSIGHT_CONFIG + "  graph_insight:\n    enabled: false\n"

# Three planted clusters (complete graphs) joined by single bridges. Two of
# the bridges and one cluster edge are agent-origin or low-confidence, so the
# digest's surprises, its verify-inferred question and its bridge questions
# all have material.
CLUSTERS = {
    "Ledger": ["Ledger API", "Ledger DB", "Ledger Worker", "Ledger Cache", "Ledger Queue"],
    "Search": ["Search API", "Search Index", "Search Crawler", "Search Ranker", "Search Store"],
    "Auth": ["Auth API", "Auth DB", "Auth Tokens", "Auth Audit"],
}
RELATION = {"Ledger": "related-to", "Search": "uses", "Auth": "depends-on"}


def planted_edges() -> list[tuple]:
    """``(src, relation, dst, kwargs)`` in seed order (edge id order)."""
    out = []
    for name, members in CLUSTERS.items():
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                out.append((a, RELATION[name], b, {}))
    out[1] = (*out[1][:3], {"origin": "agent"})
    out += [
        ("Ledger API", "uses", "Search API", {"origin": "agent", "confidence": 0.5}),
        ("Search Store", "depends-on", "Auth DB", {"origin": "agent"}),
        ("Ledger Worker", "related-to", "Ledger Sidecar", {}),
    ]
    return out


# The second phase: a new cluster, larger than any old one, hung off Auth.
# Fresh ids would number it first; the remap keeps every old community's id
# and gives it the next free one.
BILLING = ["Billing API", "Billing DB", "Billing Jobs", "Billing Mail", "Billing Tax",
           "Billing Ledger", "Billing Export"]
GROWTH = [(a, "uses", b, {}) for i, a in enumerate(BILLING) for b in BILLING[i + 1:]]
GROWTH.append(("Billing API", "depends-on", "Auth API", {"confidence": 0.55}))


def scenarios(w) -> dict:
    """The scenarios, built with ``w2e``'s own op helpers (``w`` is the
    ``w2e`` module, passed in to avoid a circular import)."""
    seed_graph = [w.relate(a, r, b, **kw) for a, r, b, kw in planted_edges()]
    contested = [
        # a user-stated slot and a lower-trust rival: a contested question
        w.fact("Ledger API", "port", "8443", origin="user"),
        w.fact("Ledger API", "port", "9443"),
        # an entity with no edge: an isolated-entity question
        w.fact("Orphan Host", "OS", "Debian 12"),
    ]
    return {
        "dream-insight-planted": {
            "config": INSIGHT_CONFIG,
            "extractor": {"model": "stub-model", "replies": []},
            "seed": [*contested, *seed_graph],
            "ops": [
                w.dream("run"),
                w.graph_read("Ledger API"),
                # the second refresh remaps onto the communities just written
                *[w.relate(a, r, b, **kw) for a, r, b, kw in GROWTH],
                w.dream("run"),
                w.graph_read("Billing API"),
                # a bridge gone: the remap keeps both sides' ids
                w.unrelate("Search Store", "depends-on", "Auth DB"),
                w.dream("run"),
                w.dream("run"),
            ],
        },
        "dream-insight-empty": {
            # entities, no edges: the refresh answers empty_graph and writes
            # nothing; then the insight switched off answers disabled
            "config": INSIGHT_CONFIG,
            "extractor": {"model": "stub-model", "replies": []},
            "seed": [w.fact("Lone Host", "OS", "Debian 12")],
            "ops": [
                w.dream("run"),
                {"op": "restart", "config": INSIGHT_OFF_CONFIG},
                w.relate("Lone Host", "related-to", "Lone Rack"),
                w.dream("run"),
            ],
        },
    }


def graphs() -> dict[str, list[tuple]]:
    """Each scenario's asserted edges at every refresh, for the order check."""
    first = planted_edges()
    grown = first + GROWTH
    cut = [e for e in grown if (e[0], e[1], e[2]) != ("Search Store", "depends-on", "Auth DB")]
    return {"dream-insight-planted": [first, grown, cut]}

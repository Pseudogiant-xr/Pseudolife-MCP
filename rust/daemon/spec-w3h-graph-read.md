# Graph read-model contract (W3-H, next increment)

Python `graph.py` and `memory/graph_store.py` are the oracle. This increment
adds pure read-model helpers and the store subgraph entry point that graph
services consume. It registers no Console GET or MCP handler.

| Behavior | Oracle |
|---|---|
| Alias map skips missing entities and aliases that collide with any canonical | `graph.py:alias_canonical_map` |
| Relation normalization and top-three fuzzy suggestions | `graph.py:resolve_relation`; Python difflib matching blocks and score/name tie order |
| Transitive closure stays within one relation, excludes existing triples and self-loop conclusions; inverse rules apply in both directions and mirror transitive conclusions | `graph.py:derive_edges` |
| Every asserted edge adds one to each endpoint's degree, including two for a self-loop; display-name overwrite follows endpoint encounter order | `graph.py:degree_counts`, `degrees_by_name` |
| Undirected shortest paths use asserted edges; equal endpoints return one node, including isolated IDs; hop limits reject longer paths | `graph.py:shortest_path` |
| Neighborhood depth clamps to 1..3, counts derived edges, always includes root; target path nodes are included beyond depth; kept edges retain direction | `graph.py:build_subgraph` |
| Store subgraph returns the full entity and alias maps alongside its neighborhood, reading live edges and registry from the bank | `memory/graph_store.py:PostgresNetworkxGraphStore.subgraph` |

Canonical nodes are integer bank IDs; relation and alias keys are normalized
strings. Entity and asserted-edge input order comes from load_graph's ID order;
registry iteration preserves load_relations's row order, including database
collation; it is not re-sorted by byte order. Asserted edge metadata
and multiplicity remain exact. Asserted edges precede derived edges. Derived
triples and provenance compare as multisets under the delegate's standing
ruling. Nodes compare as sets. Standalone paths compare by minimum hop count
and oracle-edge validity;
the native search retains NetworkX's smaller-fringe bidirectional search and
adjacency insertion order. Subgraph path ties affect the returned node/edge
selection and therefore compare exactly, including the selected path. The
six-edge root1/depth1/target8 tie example is a required rejecting case.
Degree counts, alias resolution, suggestion ranking and any cap selection are
exact; these are not order freedoms.

Inverse-provenance collision ruling: two custom relations can share an inverse
target, and Python's base set iteration chooses between different provenance
strings for the same conclusion based on its hash seed. Rust selects the
lexicographically smallest source relation name. The harness keeps the exact
triple multiset and checks that provenance against the analytically enumerated
set Python can produce, while separately pinning Rust's smallest-name choice.
Every non-collision triple and provenance remains an exact multiset comparison.
This is a declared deterministic divergence; Python is unchanged in this slice.

The differential fixture reuses the existing graph-store executable and
bank harness, compares responses and unchanged all-bank state after each read,
and records oracle replay. Rejecting controls pin path validity/minimality,
asserted-before-derived grouping, provenance, duplicate asserted edges, alias
precedence, degree multiplicity and suggestion tie order. Communities, service
fact projection and graph/review tool registration remain later increments.

NetworkX transitive_closure(reflexive=False) uses edge_bfs. Its first-seen
source-node and adjacency order is retained because new transitive edges can
select subgraph paths. A 100-case diagnostic exposed two changed selections
under sorted derived output; both are covered by the recorded read profile.
The actual GraphStore oracle file is loaded directly to avoid unrelated torch
imports in memory/__init__; no oracle algorithm or Python source is changed.

Independent review correction: preserve ordered registry pairs in both the
fixture and bank API, including the transitive-source inverse phase. The
aa/a-a/z ordered reverse-inverse case and multiple-transitive path/provenance
cases failed before correction. An actual en_US.utf8 disposable-bank aa/a-z/z
case also exposed a changed inverse triple. The expanded 164-case row includes
these controls and bank reads under the real loaded order.

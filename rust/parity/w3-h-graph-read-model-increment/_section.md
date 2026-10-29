## W3-H graph read-model increment

`daemon/src/graph_read.rs` adds pure alias, relation-suggestion, degree,
transitive/inverse, neighborhood and path helpers. `storage::graph::subgraph`
loads the full graph and registry under the shared writer guard, then returns
the GraphStore view including full entity and alias maps. It does not register
Console GET or MCP handlers.

The `graph_store.py --row read` profile reuses the store fixture and disposable
bank harness, with all-bank comparison after every operation and oracle replay.
Derived triples/provenance compare as multisets; asserted order, subgraph path
choice and returned node/edge selection remain exact. Standalone paths may be
any valid minimum-hop path. The declared inverse-provenance collision rule
chooses the smallest source relation and checks membership in the oracle's
possible provenance set. Other inference provenance stays exact.

GRAPH-REVIEW remains deferred for service/MCP wiring, fact projection, whole
graph caps, communities, proposals, review judgments/audit and deep dream.

## W3-H graph-store increment

`daemon/src/storage/graph.rs` provides native entity, alias, relation, edge,
merge/delete and orphan-repair APIs, exercised through the feature-gated
`graph-contract` binary. `daemon/harness/graph_store.py` compares responses
and all bank state after every operation, with recorded oracle replay and
source-mutant controls. PostgreSQL REAL response values follow Psycopg's
text-format decoding exactly (binary widening differs).

The GRAPH-REVIEW row remains deferred: traversal/order, communities, graph
services/MCP wiring, proposals, judges/audit and deep dream are not closed by
this storage increment. Console GET routes retain their HTTP-READS ownership.

### memory_dream run: the graph-insight refresh (2026-10-25)

Accepted by the maintainer's Option B decision (2026-10-19, `rust/CUTOVER.md`):
the communities and the betweenness come from a deterministic algorithm. The
comparison is structural where the partition depends on the visiting order:
`src/graph_insight_tests.rs` runs 16 recorded graphs (`harness/goldens/
graph-insight.json`, from `gen_graph_insight_goldens.py`) and requires, on
each, byte equality with NetworkX run without its shuffle, and against
NetworkX with its seed an adjusted Rand index, community count, size
distribution, god-node set and surprise set each inside the envelope Python
itself spans across seeds 1..20 against seed 42 (and an adjusted Rand index
of at least 0.95 on the planted-structure graphs). The `dream-insight-*`
w2e scenarios compare whole banks on graphs whose partition is the same in
both orders. The oracle is NetworkX 3.6.1, the version
`ops/requirements.lock.txt` pins for the daemon image (`pyproject.toml`
allows any 3.x); a NetworkX that changes Louvain or Brandes moves the
goldens.

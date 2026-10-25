### Changed (2026-10-25 — the Rust daemon's dream refreshes graph insight instead of refusing)
- `memory_dream(action="run")` on the Rust daemon refused with
  `not_implemented` under the default configuration, because its last stage,
  the graph-insight refresh (communities, god nodes, surprising connections
  and suggested questions), was not ported. The Rust daemon now runs it, so a
  default-config dream completes and `memory_graph` and `memory_stats` show
  the communities it writes.
- Under the maintainer's Option B decision the Rust side uses NetworkX's
  Louvain without its seeded shuffle and exact betweenness. On graphs with
  clear structure it writes the same communities as Python; elsewhere its
  partition differs from Python's seed-42 one no more than another seed of
  Python's would, measured on 16 recorded graphs (declared in
  `rust/daemon/divergences.md`). The new `memory.graph_insight` knobs are
  typed, and a wrong type is refused at startup like every other key.

| Item | Python | Rust | Owner / why |
|---|---|---|---|
| Louvain's visiting order | `seed.shuffle` (`random.Random(42)`) at every level | graph order (first appearance), ties to the first community in adjacency order | Option B; on a graph whose structure fixes the partition the two agree, otherwise the partition can differ within Louvain's own seed-to-seed spread (measured above) |

| Item | Python | Rust | Owner / why |
|---|---|---|---|
| The order of the inverse-derived edges that mirror asserted edges, in `edges` | the graph read's hash order (the memory_graph row above) | the read model's order | inherited from `memory_graph`; recall's edges carry no provenance, so when the 15-edge cap binds, which of them survive can differ too. The harness's recall templates hold no asserted inverse pair |

| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A wrong-type `memory.recall` setting (`max_searches_per_hop: 2.5`, `hub_gate: "false"`, `driver: null`) | taken untyped (`int()` truncates, a non-empty string is truthy, `None` falls to mechanical) | refused at config load, like every other typed key | the existing config-type substitution |

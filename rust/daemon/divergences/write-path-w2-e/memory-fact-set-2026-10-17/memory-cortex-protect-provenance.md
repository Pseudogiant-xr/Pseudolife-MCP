| Item | Python | Rust | Owner / why |
|---|---|---|---|
| `memory.cortex.protect_provenance`, `supersede_confidence_margin`, `reinforce_rate`, `memory.lessons.enabled`, `memory.lessons.min_confidence` of the wrong type | `bool()` / `float()` at store build or per call (a string `"false"` is truthy) | refused at config load, like every other typed key (`wrong_types_python_accepts_refuse_here`) | the existing config-type substitution |

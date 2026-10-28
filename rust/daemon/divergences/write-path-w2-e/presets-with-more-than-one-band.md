| Item | Python | Rust | Owner / why |
|---|---|---|---|
| Presets with more than one band (`continuum` and aliases, multi-band `custom`) | store lands in `bands[0]`, the promotion walk moves entries down the chain, and eviction relocates to the next band | store lands in `bands[0]`; no promotion walk; every eviction is a true drop | the opt-in presets; flat is the default since 2026-08-15 |

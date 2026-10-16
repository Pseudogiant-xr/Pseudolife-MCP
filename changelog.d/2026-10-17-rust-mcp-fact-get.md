### Added (2026-10-17 — the Rust daemon serves memory_fact_get)

- The Rust daemon's MCP endpoint now answers `memory_fact_get` as the Python daemon does: the current value at an (entity, attribute) slot, found through the entity's graph aliases when the name misses, set-valued slots with their removed members, contenders, nearby slots for an empty one, the correction hint and the re-verify warning, compact unless `verbose`.

### Fixed (2026-11-17 — Keep batch members with generated census conflicts)
- Integration batches retain members whose only conflict is the generated producer census, regenerate it on the batch head, and report the member; failed regeneration rebuilds without the census-input members.

### Deprecated (2026-11-01 — retire unused Console REST routes)
- Under D9 / Proposal A / B2r (decided 2026-10-23), `POST /api/lessons/restore` and `POST /api/world/restore` are deprecated for removal in a later release; use `memory_graph_review(action="restore_slot")` with the matching store instead.
- `GET /api/curation/retired`, `GET /api/graph/communities` and `GET /api/graph/proposal-evidence` are deprecated for removal in a later release (no replacement; no caller). Python handlers remain unchanged; Rust keeps its declared `501` responses for all five routes.

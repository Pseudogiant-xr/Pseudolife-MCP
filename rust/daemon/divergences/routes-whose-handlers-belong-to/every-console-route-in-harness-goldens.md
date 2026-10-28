| Surface | Python behaviour | Owner |
|---|---|---|
| Every Console route in `harness/goldens/routes.json` other than `GET /api/search`, the coordination view of `GET /api/agents`, `POST /api/daemon-notice`, `POST /api/episodes/rename`, `/merge` and `/prune`, `POST /api/episode/start` and `/end`, the reads `read/console.rs` serves (the `w2d-console-projections` row below) and `/api/agents`' default view, after all gates | the handler's answer | W2-D (reads), W2-E (writes), W2-F (board, maintainer) |

### Changed (2026-10-20 — `/health` says when the daemon is ready, and every harness waits for it)
- `/health` gains two keys in both the Python and the Rust daemon: `ready`
  (true once `status` is `ok` and nothing is pending) and `pending`, the
  startup stages still unfinished: `init` (embedder loaded, stores hydrated,
  clock reseeded) and `principals` (the stored-principal view's first read).
  Neither changes `status` or the HTTP code, so the Docker healthcheck and
  `pseudolife-mcp update` behave as before.
- The test helpers, both parity harnesses and the evals' daemon starters
  now share one wait (`pseudolife_memory/readiness.py`). Any answer before
  `ready: true` (a 503, `ready: false`, an older daemon without the key)
  counts as "not started". A harness whose daemon cannot finish `init` by
  design (no embedding model in CI, an unreachable or leased bank) waits
  for the stages it can still finish. This replaces the per-scenario
  stored-principal polls (#826, #827), which only covered the places that
  had already flaked.
- A bearer the environment does not match no longer meets a 503
  `principals_unavailable` right after a harness starts a daemon.

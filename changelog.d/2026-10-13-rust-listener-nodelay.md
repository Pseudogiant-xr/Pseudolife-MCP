### Fixed (2026-10-13 — Rust keep-alive response latency)

- The Rust daemon disables Nagle's algorithm on accepted TCP connections to
  avoid delayed-acknowledgment stalls for small streamed responses over
  keep-alive connections.

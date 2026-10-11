### Fixed (2026-10-11 — Console SPA fallback containment)
- The Console's SPA fallback now applies the same resolved-path containment
  check as a direct request in Python and Rust. Missing indexes still return
  `404 not found`, and indexes resolving within the static root remain served.

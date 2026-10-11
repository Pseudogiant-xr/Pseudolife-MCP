### Fixed (2026-10-11 — Console index containment)
- The Console's directory indexes and SPA fallback now apply the same resolved-path
  containment check as a direct request in Python and Rust. Missing indexes still return
  `404 not found`, and indexes resolving within the static root remain served.

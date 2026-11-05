### Performance (2026-11-04 — Avoid repeating successful master push coverage nightly)
- Scheduled CI and Rust workflows skip their ordinary lanes after a successful push run of the same workflow on the exact master commit. Nightly-only evidence and its prerequisite builds still run; missing evidence or an API failure runs all lanes.

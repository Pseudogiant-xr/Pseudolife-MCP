### Added (2026-11-02 — Nightly Rust daemon body evidence)
- Run the full write, search and board differentials on self-hosted Linux nightly and through an isolated manual dispatch, publishing compared heads, scenario counts and differences.
- Respect the registered nightly runners and yield between differentials when the machine's full-suite lease becomes held, preserving an incomplete report.
- Compare the served tokenless hook `woke` response with Python instead of expecting its retired 501 stub.

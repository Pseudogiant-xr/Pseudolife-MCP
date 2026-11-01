### Added (2026-11-02 — Compare Rust reads beside the active daemon)
- The Rust daemon accepts `PSEUDOLIFE_READ_ONLY=1` or `read_only: true` for a shadow that takes no writer lease, skips bank mutations and reports `read_only: true` in health; write requests name the active writer as the destination.
- Add an idempotent PostgreSQL shadow-reader role script and a read-only side-by-side comparator with explicit normalization, JSON results and a readable digest.
- Shadow readers reuse resident state until compact row-version probes change, refuse writer credentials and unclassified GET routes, and report effective disabled settings; comparator replays are excluded from later retrieval-log corpora.
- Shadow request bursts share a probe for one second, and valid bank-bound MCP reads use a read-only coordination identity lookup.

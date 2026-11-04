### Fixed (2026-11-04 — use board session ids as episode anchors)
- Memory tools accept the board's exact host session id for an open session. Session-id prefixes, unknown sessions and closed-session aliases remain refused; daemon-minted episode handles retain their existing resume behavior.

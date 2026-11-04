### Fixed (2026-11-04 — use board session ids as episode anchors)
- Memory tools accept the board's exact host session id for an open session, including a hookless client's first write. Session-id prefixes, unknown foreign sessions and closed-session aliases remain refused; daemon-minted episode handles retain their existing resume behavior.

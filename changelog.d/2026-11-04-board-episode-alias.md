### Fixed (2026-11-04 — use board session ids as episode anchors)
- Memory tools accept the board's exact host session id for an open session. Session-id prefixes, unknown sessions and closed-session aliases remain refused; daemon-minted episode handles retain their existing resume behavior.
- A hookless session passing its board id before it has an episode gets the exact bootstrap command; closed aliases have a separate refusal. The Rust daemon mirrors the alias and warning behavior.

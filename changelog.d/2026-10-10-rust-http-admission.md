### Fixed (2026-10-10 — preserve HTTP admission responses in the Rust daemon)

- Return the SessionEnd hook's no-op response for empty, malformed or non-object bodies, preserving its byte limit, authentication order and UTF-8 error response.
- Verify per-route wire limits, chunked requests, pairing-budget consumption and coordination-view admission with the daemon differential harness; scenario crashes cannot count as successful source controls.
- Preserve readable early-refusal responses, including always-200 hook replies, by retaining unread request bodies for bounded cleanup; keep headers-only and Expect refusals ahead of body parsing.

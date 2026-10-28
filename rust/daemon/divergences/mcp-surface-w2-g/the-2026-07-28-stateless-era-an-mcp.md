| Item | Python | Rust | Owner |
|---|---|---|---|
| The 2026-07-28 stateless era (an `MCP-Protocol-Version` header outside the handshake versions, `_meta` envelopes, `subscriptions/listen`) | served | the transport checks (spec.md M4), then only the no-envelope refusal (400, -32602) | W2-G (follow-up); the shims and doctor use the handshake era, but README documents direct `--transport http` clients |

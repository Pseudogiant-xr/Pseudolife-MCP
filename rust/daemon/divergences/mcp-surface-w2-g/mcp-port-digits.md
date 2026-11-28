**MCP-PORT-DIGITS** (declared 2026-11-27): tokenless `/mcp` accepts a Host or
Origin only as an allowlisted host, a colon and digits (spec.md M4), where the
SDK's `_validate_host` / `_validate_origin` accept any text after the colon,
so `localhost:1.evil.example` passed it. `mcp_ci.py` declares exactly the four
`rebinding_edges` responses this changes (`TOKENLESS_DECLARED`: Host
`127.0.0.1:notaport`, Origin `http://localhost:x`), and the `routes/mcp.rs`
tests pin the Rust answers (421, 403). D7 bucket: behaviour with no caller. No browser or client sends a non-numeric port, so no caller sees it; it is kept under the maintainer's ruling of 14 November 2026 (the bar is working behaviour, not parity). Code verification: `rust/daemon/src/mcp/mod.rs::security` (`with_port`), versus `transport_security.py` reached by `pseudolife_memory/mcp_server.py::transport_security_for`. Caller: none; B14b owner: none.

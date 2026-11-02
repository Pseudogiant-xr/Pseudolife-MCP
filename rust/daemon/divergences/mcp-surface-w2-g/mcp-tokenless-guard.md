**MCP-TOKENLESS-GUARD** is closed (B6, under D3): tokenless `/mcp` is
served behind the SDK's loopback Host/Origin allowlist (spec.md M4), checked
on every request, the stateless era's included. `mcp_ci.py` compares the
tokenless corpus (`rebinding`, `rebinding_edges`, `open_lists`) with Python,
legitimate near misses beside each refusal, with no declared difference,
and catches `mcp-bare-host`, `mcp-any-origin` and `mcp-era-skips-security`.
The corpus runs on Linux; on both OSes the `routes/mcp.rs` tests run the
same Host and Origin cases in-process against the Python answers they
pin (the near misses, duplicate headers, the prefix rule, the stateless era,
bound identity, router order and a live session's POST, GET and DELETE), and
the W1-A `tokenless` golden compares `GET /mcp` with Python's recorded 406.
**MCP-BOUND-IDENTITY** is closed: an authenticated `/mcp` request carrying
`X-PL-Bank` / `X-PL-Principal` is checked against the bearer's W2-F board
context before the MCP layer (spec.md M18); the live `bound_identity` case
compares 19 requests with Python and the `mcp-binding-open` mutant is caught. D7 bucket: closed. Code verification: `rust/daemon/src/mcp/mod.rs::security` applies Host/Origin allowlists and `rust/daemon/src/routes/mcp.rs::route` calls `board::http::mcp_binding`, matching `pseudolife_memory/mcp_server.py::transport_security_for` and Python bound identity; existing differential: `rust/daemon/harness/mcp_ci.py` rebinding/open_lists/bound_identity. B14b owner: none (closed).

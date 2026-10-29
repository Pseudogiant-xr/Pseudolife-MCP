| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| MCP-WIRE | 1 | initialize instructions/capabilities, tool annotations/schema/help, supported revision negotiation, JSON-RPC errors, streams, cancellation, notifications. `mcp_server.py`, `shim.py` | `test_shim.py` P/I; `test_shim_transport_recovery.py` mixed P/I; `test_mcp_server.py`, `test_mcp_string_arguments.py`, `test_mcp_client_neutrality.py`, `test_mcp_stdio_errlog.py` I/A; help fixture A | ported |

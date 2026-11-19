### Added (2026-11-20 — Native foreground tunnel)
- Run a prepared Secure MCP Tunnel with `pseudolife-mcp tunnel run`. Use `tunnel stop` to stop a tunnel started by this native client. The supervisor checks local readiness and owns the runtime and its launch files. Use the Python client for key renewal, background start and refresh.
- Native verification binds receipts to the saved credential, runtime and active launch. A later run removes private launch files only when it can confirm the old runtime has stopped. Recognized vendor startup errors appear in a bounded diagnostic summary.

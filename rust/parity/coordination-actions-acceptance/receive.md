| Item | Phase | Status | Rust | Evidence |
|---|---|---|---|---|
| receive | 1/4 | ported | served | Board harness scenarios: mail, console, mixed; includes `wait_seconds` (W2-F). The `ack` flag (#754) is compared over MCP by #777's `mcp-tools` run; its REST steps await a live run. W2F-MAIL-PACING applies |

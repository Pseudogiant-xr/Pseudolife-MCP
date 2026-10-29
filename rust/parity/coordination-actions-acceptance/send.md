| Item | Phase | Status | Rust | Evidence |
|---|---|---|---|---|
| send | 1/4 | ported | served | Board harness scenarios: mail, wake, prune, console, mixed, mcp-tools; declared: W2F-CLOCK-RETRY (cutover blocker) and W2F-MAIL-PACING. Sends tick the service clock (spec FS6; `board::clock_tests`, run by `board.boundary --shared-clock`) |

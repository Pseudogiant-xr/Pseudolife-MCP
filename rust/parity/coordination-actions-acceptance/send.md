| Item | Phase | Status | Rust | Evidence |
|---|---|---|---|---|
| send | 1/4 | ported | served | Board harness scenarios: mail, wake, prune, console, mixed, mcp-tools; B5 closed W2F-CLOCK-RETRY as a cutover blocker; its captured-epoch difference and W2F-MAIL-PACING remain declared (`daemon/divergences/coordination-board-completion-w2/`). Sends tick the service clock (spec FS6; `board::clock_tests`, run by `board.boundary --shared-clock`) |

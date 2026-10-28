| Item | Python | Rust | Owner |
|---|---|---|---|
| MCP-REQUEST-CANCELLATION | `notifications/cancelled` cancels the matching in-flight response; a legacy response stream settles with -32800 | cancellation notifications receive 202 but are discarded; any matching request still in flight later emits its ordinary response. Currently `memory_toolset` blocked by a backpressured GET notification stream exposes this window | W2-G follow-up: add per-session request tracking and response cancellation separately from disconnect-surviving write bodies; no bank tool body is enabled here |

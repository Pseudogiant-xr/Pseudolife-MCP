| Item | Python | Rust | Owner |
|---|---|---|---|
| MCP-SEALED-REQUEST-STATE | `tools/call`, `prompts/get` and `resources/read` unseal a non-null `requestState` before validation/dispatch; a valid server-minted token may resume a multi-round exchange | this slice never mints round-trip state and refuses every non-null token with -32602 `Invalid or expired requestState`, before readiness or tier changes; null remains absent | multi-round state issuance and authenticated unsealing are deferred with the unserved tool bodies; untrusted tokens cannot invoke a body |

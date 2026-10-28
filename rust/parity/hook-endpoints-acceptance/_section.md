## Hook endpoints acceptance checklist

Source: `pseudolife_memory/web/api.py`; cross-reference HOOKS and the Console checklist hook summary. The gate checks Host and Origin in tokenless mode only and is skipped with configured authentication. Each item requires normal, invalid-input, authentication and durable-effect evidence as applicable. The Rust and Status columns follow the Console checklist's rule. Audited at `98c5d6bf` (2026-10-19): the four board hooks are served; session-start and memory-policy answer 501; memory-changes and session-end serve their gates and refusals but not the authorized body. The board hooks inherit W2F-SHARED-HLC through the board's writes.

| Item | Phase | Status | Rust | Evidence |
|---|---|---|---|---|

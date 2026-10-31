## Hook endpoints acceptance checklist

Source: `pseudolife_memory/web/api.py`; cross-reference HOOKS and the Console checklist hook summary. The gate checks Host and Origin in tokenless mode only and is skipped with configured authentication. Each item requires normal, invalid-input, authentication and durable-effect evidence as applicable. The Rust and Status columns follow the Console checklist's rule. Audited at `75a8015e` (2026-11-01): all eight paths declared by `rust/daemon/src/routes/hook.rs` have Rust bodies; none answers the declared 501 refusal for its supported method. The four board hooks retain their recorded differential status; the four B3 hooks remain evidence-deferred as their rows explain. The maintainer's conductor reminder on `memory-changes` (`?agent=`) remains a declared gap, and the session-start update-offer and dream-stall variants have unit-test evidence only. The board hooks' writes are stamped by the service clock (spec FS6).

| Item | Phase | Status | Rust | Evidence |
|---|---|---|---|---|

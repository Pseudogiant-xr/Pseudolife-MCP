//! Slice W2-D: the search and read routes (spec.md, section "W2-D").

pub mod chronicle;
pub mod console;
pub mod cortex_search;
pub mod embed_cache;
pub mod fact_get;
pub mod history;
pub mod identity;
pub mod maintainer;
pub mod mcp_reads;
pub mod mcp_search;
pub mod rerank;
pub mod search_route;
pub mod stats;
pub mod trace;

/// Keeps resident access accrual and its response telemetry in search order.
/// Database operations use `txn::with_client`; this mutex protects only the
/// search ordering across CPU ranking and the cortex block.
pub fn search_guard() -> &'static tokio::sync::Mutex<()> {
    static GUARD: std::sync::OnceLock<tokio::sync::Mutex<()>> = std::sync::OnceLock::new();
    GUARD.get_or_init(|| tokio::sync::Mutex::new(()))
}

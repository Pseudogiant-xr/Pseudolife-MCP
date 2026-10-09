//! `memory_toolset` (`mcp_server.memory_toolset`): the one tool body W2-G owns.

use super::McpState;
use super::catalogue;
use super::tiers::{self, LADDER, Tier};
use serde_json::{Value, json};

/// What each step of the ladder adds (`_TIER_ADDS`).
const ADDS_CORE: &str = "board (memory_agents/memory_message), graph + recall, world facts, documents, stats, episodes, memory_get/fact_resolve";
const ADDS_FULL: &str = "supersede/reinstate/forget/history/reinforce, recent, episode_summary, dream + graph-review, graph_unrelate, aliases, consolidation, relation-define";

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Expand,
    Collapse,
    Status,
}

/// Returns the result dict, and whether the visible list changed (so the
/// caller can send `notifications/tools/list_changed`).
pub fn run(
    state: &McpState,
    store: &crate::auth::PrincipalStore,
    action: Action,
    principal: &str,
    key: Option<&str>,
) -> (Value, bool) {
    let norm_key = tiers::norm(key.unwrap_or(""));
    let default_tier = state
        .tier_map
        .get(&norm_key)
        .copied()
        .or_else(|| state.stored_tier_for(store, principal, &norm_key))
        .unwrap_or(state.default_tier);
    let current = state.resolve_tier(store, principal, key);

    if action == Action::Status {
        let ladder: Vec<&str> = LADDER.iter().map(|t| t.name()).collect();
        return (
            json!({"current": current.name(), "default": default_tier.name(),
                   "ladder": ladder, "adds": {"core": ADDS_CORE, "full": ADDS_FULL}}),
            false,
        );
    }
    let new = match action {
        Action::Expand => current.step(1, Tier::Minimal),
        _ if crate::mutants::active("mcp-toolset-floor") => current.step(-1, Tier::Minimal),
        _ => current.step(-1, default_tier),
    };
    if new == current {
        let reason = if action == Action::Expand {
            "already at full".to_string()
        } else {
            format!("already at your floor ({})", default_tier.name())
        };
        return (
            json!({"changed": false, "current": current.name(), "reason": reason}),
            false,
        );
    }
    state.overrides.set(key, new);
    let (before, after) = (catalogue::visible(current), catalogue::visible(new));
    let added: Vec<&str> = after.difference(&before).copied().collect();
    let removed: Vec<&str> = before.difference(&after).copied().collect();
    // Python publishes on the 2026-07-28 subscription bus, which succeeds
    // with no subscribers, so `list_changed_sent` is always true.
    (
        json!({"changed": true, "current": new.name(), "previous": current.name(),
               "visible_tools_added": added, "visible_tools_removed": removed,
               "list_changed_sent": true}),
        true,
    )
}

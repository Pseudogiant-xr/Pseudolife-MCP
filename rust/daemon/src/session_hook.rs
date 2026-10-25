//! The plugin's SessionStart hook texts (`web/session_hook.py`). So far:
//! the separate memory-policy hook (`hook_memory_policy`).

use crate::config::MemoryPolicyConfig;
use sha2::{Digest, Sha256};

/// `HOOK_CONTEXT_MAX_CHARS`: one hook output's budget, in UTF-8 bytes.
const HOOK_CONTEXT_MAX_CHARS: usize = 9_500;

/// `MEMORY_POLICY_VARIANTS`.
const MEMORY_POLICY_VARIANTS: [&str; 3] = ["none", "compact", "full_separate_hook"];

/// `CONTINUED_SOURCES`: SessionStart sources that continue a conversation.
const CONTINUED_SOURCES: [&str; 2] = ["resume", "compact"];

/// `MEMORY_LOOP_BLOCK`, recorded byte for byte from Python
/// (`harness/record_routes.py`).
const MEMORY_LOOP_BLOCK: &str = include_str!("../harness/goldens/memory-loop-block.txt");

/// `ab_arm_index`: SHA-256 of the session id, its first 8 bytes big-endian,
/// modulo the arm count.
fn ab_arm_index(session_id: &str, arms: usize) -> usize {
    let digest = Sha256::digest(session_id.as_bytes());
    let head = u64::from_be_bytes(digest[..8].try_into().expect("8 bytes"));
    if crate::mutants::active("policy-arm-shifted") {
        return ((head + 1) % arms as u64) as usize;
    }
    (head % arms as u64) as usize
}

/// `memory_policy_variant`: the session's A/B arm when two or more arms are
/// set and it has an id, else the configured variant; `compact` for
/// anything unknown.
pub fn memory_policy_variant(cfg: &MemoryPolicyConfig, session_id: Option<&str>) -> String {
    let variant = match session_id {
        Some(id) if !id.is_empty() && cfg.ab_arms.len() >= 2 => {
            cfg.ab_arms[ab_arm_index(id, cfg.ab_arms.len())].clone()
        }
        _ => cfg.variant.clone(),
    };
    if MEMORY_POLICY_VARIANTS.contains(&variant.as_str()) {
        variant
    } else {
        "compact".into()
    }
}

/// `hook_memory_policy`: the full memory-loop block for a session whose
/// variant is `full_separate_hook`, else empty; nothing on a resume or
/// compaction.
pub fn hook_memory_policy(
    cfg: &MemoryPolicyConfig,
    session_id: Option<&str>,
    source: Option<&str>,
) -> &'static str {
    if source.is_some_and(|s| CONTINUED_SOURCES.contains(&s)) {
        return "";
    }
    if memory_policy_variant(cfg, session_id) != "full_separate_hook" {
        return "";
    }
    if MEMORY_LOOP_BLOCK.len() <= HOOK_CONTEXT_MAX_CHARS {
        MEMORY_LOOP_BLOCK
    } else {
        ""
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg(variant: &str, arms: &[&str]) -> MemoryPolicyConfig {
        MemoryPolicyConfig {
            variant: variant.into(),
            ab_arms: arms.iter().map(|a| a.to_string()).collect(),
        }
    }

    #[test]
    fn arms_follow_the_python_hash() {
        // `int.from_bytes(sha256(id)[:8], "big") % 3`, computed in Python.
        assert_eq!(ab_arm_index("sess-a", 3), 1);
        assert_eq!(ab_arm_index("sess-b", 3), 2);
        assert_eq!(ab_arm_index("sess-c", 3), 0);
        assert_eq!(ab_arm_index("sess-c", 2), 1);
    }

    #[test]
    fn the_block_serves_only_the_separate_hook_variant() {
        let full = cfg("full_separate_hook", &[]);
        assert_eq!(hook_memory_policy(&full, None, None), MEMORY_LOOP_BLOCK);
        assert_eq!(hook_memory_policy(&full, Some("x"), Some("resume")), "");
        assert_eq!(hook_memory_policy(&full, Some("x"), Some("compact")), "");
        assert_eq!(
            hook_memory_policy(&full, Some("x"), Some("startup")),
            MEMORY_LOOP_BLOCK
        );
        assert_eq!(hook_memory_policy(&cfg("compact", &[]), None, None), "");
        assert!(MEMORY_LOOP_BLOCK.len() < HOOK_CONTEXT_MAX_CHARS);
        assert!(!MEMORY_LOOP_BLOCK.contains('\r'));
    }

    #[test]
    fn a_session_id_picks_its_arm_only_with_two_or_more_arms() {
        let arms = cfg("compact", &["none", "compact", "full_separate_hook"]);
        assert_eq!(
            memory_policy_variant(&arms, Some("sess-b")),
            "full_separate_hook"
        );
        assert_eq!(memory_policy_variant(&arms, Some("sess-c")), "none");
        assert_eq!(memory_policy_variant(&arms, None), "compact");
        assert_eq!(memory_policy_variant(&arms, Some("")), "compact");
        assert_eq!(memory_policy_variant(&cfg("bogus", &[]), None), "compact");
    }
}

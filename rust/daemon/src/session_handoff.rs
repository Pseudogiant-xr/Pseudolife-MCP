//! The session handoff: one reserved cortex slot per session, keyed to the
//! session's root episode, that a long session keeps current with
//! `memory_fact_set` and the SessionStart hook prints back after a
//! compaction or resume.
//!
//! The slot is entity `session:<root episode id>` (the full 32-character
//! hex id), attribute `handoff`. The root, not the advertised handle,
//! because the handle names the open leaf, which changes when the session
//! opens a sub-episode; the full id, because a 12-character prefix could
//! name another session's slot. `norm_key` keeps the colon and lowercase
//! hex unchanged, so the key is one string in every normalised form.
//!
//! A handoff is served only by key (`memory_fact_get`, `memory_history`)
//! and through the hook. Every read path that ranks, lists or consumes
//! cortex facts without naming the slot skips it ([`is_handoff_key`]),
//! no graph node is minted for the entity, and a non-dream write always
//! supersedes the previous value. A Rust-only behaviour: the Python daemon
//! treats the slot as an ordinary fact
//! (`divergences/write-path-w2-e/hooks-and-the-cli-s-episode/the-session-handoff-slot-entity-session.md`).

use crate::stores::cortex::{CortexStore, SlotKey, norm_key};

/// The handoff slot's attribute.
pub const ATTRIBUTE: &str = "handoff";

/// The handoff entity's prefix, before the root episode id.
pub const ENTITY_PREFIX: &str = "session:";

/// The most of a handoff's value the hook prints, in UTF-8 bytes; the rest
/// is one `memory_fact_get` away.
pub const HOOK_VALUE_MAX_BYTES: usize = 1_500;

/// The reserved entity for a root episode id.
pub fn entity_for(root_id: &str) -> String {
    format!("{ENTITY_PREFIX}{root_id}")
}

/// A normalised entity of the exact reserved shape: `session:` and 32
/// lowercase hex digits, nothing else.
pub fn is_handoff_entity_norm(entity_norm: &str) -> bool {
    entity_norm.strip_prefix(ENTITY_PREFIX).is_some_and(|id| {
        id.len() == 32 && id.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
    })
}

/// An entity, as written, that normalises to the reserved shape.
pub fn is_handoff_entity(entity: &str) -> bool {
    is_handoff_entity_norm(&norm_key(entity))
}

/// A slot key that is a handoff slot.
pub fn is_handoff_key(key: &SlotKey) -> bool {
    key.1 == ATTRIBUTE && is_handoff_entity_norm(&key.0)
}

/// `value` cut to at most `max` bytes on a character boundary, and whether
/// it was cut.
fn cut(value: &str, max: usize) -> (&str, bool) {
    if value.len() <= max {
        return (value, false);
    }
    let mut end = max;
    while !value.is_char_boundary(end) {
        end -= 1;
    }
    (&value[..end], true)
}

fn get_call(entity: &str) -> String {
    format!("memory_fact_get(entity=\"{entity}\", attribute=\"{ATTRIBUTE}\")")
}

fn set_call(entity: &str) -> String {
    format!("memory_fact_set(entity=\"{entity}\", attribute=\"{ATTRIBUTE}\", value=...)")
}

/// The handoff the hook serves: the first of `roots` (the session's
/// current root first, then its earlier roots, newest first) whose slot
/// holds a current value, as `(that root, the value)`. Read from the
/// snapshot only: no telemetry, no stale demotion, nothing written.
pub fn find<'a>(store: &'a CortexStore, roots: &[String]) -> Option<(String, &'a str)> {
    roots.iter().find_map(|root| {
        store
            .lookup(&entity_for(root), ATTRIBUTE)
            .map(|r| (root.clone(), r.value.as_str()))
    })
}

/// The pointer paragraph of a compacted or resumed session's hook answer
/// whose root is known: the handoff when one exists (cut at
/// [`HOOK_VALUE_MAX_BYTES`] with the call that returns the rest), else how
/// to write one; either way it names the exact slot. `kind` is "compacted"
/// or "resumed", `handle` the sentence on the episode handle (or empty),
/// `tail` the recall pointer. The forms are tried longest first; the first
/// that fits `max_bytes` is returned, or None when none does.
pub fn hook_note(
    kind: &str,
    handle: &str,
    tail: &str,
    current_root: &str,
    found: Option<(&str, &str)>,
    max_bytes: i64,
) -> Option<String> {
    let current = entity_for(current_root);
    let fits = |s: &String| s.len() as i64 <= max_bytes;
    let Some((root, value)) = found else {
        let note = format!(
            "Pseudolife-MCP: {kind} session. This session has no handoff yet: keep what it is \
             doing and what it still owes in its handoff slot with {}; each write replaces the \
             last, and the next compaction or resume prints it here. {handle}{tail}",
            set_call(&current)
        );
        return Some(note).filter(fits);
    };
    let entity = entity_for(root);
    let elsewhere = if root == current_root {
        String::new()
    } else {
        format!(
            " It was written under this session's earlier episode; write the next one to \
             entity=\"{current}\"."
        )
    };
    let head = format!(
        "Pseudolife-MCP: {kind} session. This session's handoff (entity=\"{entity}\", \
         attribute=\"{ATTRIBUTE}\"; keep it current with memory_fact_set at that slot):{elsewhere}"
    );
    let (shown, was_cut) = cut(value, HOOK_VALUE_MAX_BYTES);
    let marker = if was_cut {
        format!(
            "\n[handoff cut at {} of {} bytes; the rest: {}]",
            shown.len(),
            value.len(),
            get_call(&entity)
        )
    } else {
        String::new()
    };
    let full = format!("{head}\n\n{shown}{marker}\n\n{handle}{tail}");
    if fits(&full) {
        return Some(full);
    }
    let pointer = format!(
        "Pseudolife-MCP: {kind} session. This session has a handoff ({} bytes) that does not \
         fit this hook's room: read it with {}.{elsewhere} {handle}{tail}",
        value.len(),
        get_call(&entity)
    );
    Some(pointer).filter(fits)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stores::cortex::tests::{rec, store};

    const ROOT: &str = "0123456789abcdef0123456789abcdef";
    const OTHER: &str = "fedcba9876543210fedcba9876543210";

    #[test]
    fn the_reserved_shape_is_exact() {
        assert!(is_handoff_entity(&entity_for(ROOT)));
        // Case and surrounding separators fold away, as every slot key does.
        assert!(is_handoff_entity(&format!(
            " Session:{} ",
            ROOT.to_uppercase()
        )));
        // Near misses stay ordinary entities.
        for near in [
            "session",
            "session:",
            "session hook",
            "session:abc",
            "session-0123456789abcdef0123456789abcdef",
            "session: 0123456789abcdef0123456789abcdef",
            "session:0123456789abcdef0123456789abcdeg",
            "session:0123456789abcdef0123456789abcdef0",
            "my session:0123456789abcdef0123456789abcdef",
        ] {
            assert!(!is_handoff_entity(near), "{near}");
        }
        let key = (norm_key(&entity_for(ROOT)), norm_key("Handoff"));
        assert!(is_handoff_key(&key));
        assert!(!is_handoff_key(&(
            norm_key(&entity_for(ROOT)),
            "status".into()
        )));
        assert!(!is_handoff_key(&("session hook".into(), "handoff".into())));
    }

    #[test]
    fn find_reads_the_first_root_with_a_value() {
        let s = store(vec![
            rec(&entity_for(OTHER), ATTRIBUTE, "older root's state"),
            rec("session hook", ATTRIBUTE, "an ordinary fact"),
        ]);
        assert_eq!(find(&s, &[ROOT.into()]), None);
        assert_eq!(
            find(&s, &[ROOT.into(), OTHER.into()]),
            Some((OTHER.to_string(), "older root's state"))
        );
    }

    #[test]
    fn the_cut_lands_on_a_character_boundary() {
        let v = "é".repeat(1000); // 2000 bytes
        let (shown, was_cut) = cut(&v, HOOK_VALUE_MAX_BYTES);
        assert!(was_cut);
        assert_eq!(shown.len(), 1500);
        let v = format!("a{}", "é".repeat(1000)); // odd offset: 1500 is mid-character
        let (shown, _) = cut(&v, HOOK_VALUE_MAX_BYTES);
        assert_eq!(shown.len(), 1499);
        assert_eq!(cut("short", 1500), ("short", false));
    }

    #[test]
    fn the_note_names_the_slot_with_and_without_a_handoff() {
        let none = hook_note("compacted", "H. ", "T.", ROOT, None, 9_000).unwrap();
        assert!(none.starts_with("Pseudolife-MCP: compacted session. This session has no handoff"));
        assert!(none.contains(&format!(
            "memory_fact_set(entity=\"session:{ROOT}\", attribute=\"handoff\", value=...)"
        )));
        assert!(none.ends_with("H. T."));
        let some = hook_note(
            "resumed",
            "",
            "T.",
            ROOT,
            Some((ROOT, "Doing X; owe Y.")),
            9_000,
        )
        .unwrap();
        assert!(some.contains(&format!("entity=\"session:{ROOT}\", attribute=\"handoff\"")));
        assert!(some.contains("\n\nDoing X; owe Y.\n\nT."));
        assert!(!some.contains("cut at"));
        assert!(!some.contains("earlier episode"));
    }

    #[test]
    fn a_long_handoff_is_cut_with_the_call_for_the_rest() {
        let value = "x".repeat(4_000);
        let note = hook_note("compacted", "", "T.", ROOT, Some((ROOT, &value)), 9_000).unwrap();
        assert!(note.contains(&format!(
            "\n\n{}\n[handoff cut at 1500 of 4000 bytes",
            "x".repeat(1500)
        )));
        assert!(note.contains(&format!(
            "the rest: memory_fact_get(entity=\"session:{ROOT}\", attribute=\"handoff\")]"
        )));
        assert!(!note.contains(&"x".repeat(1501)));
    }

    #[test]
    fn a_handoff_under_an_earlier_root_says_where_to_write_next() {
        let note = hook_note("resumed", "", "T.", ROOT, Some((OTHER, "state")), 9_000).unwrap();
        assert!(note.contains(&format!("entity=\"session:{OTHER}\"")));
        assert!(note.contains(&format!("write the next one to entity=\"session:{ROOT}\"")));
    }

    #[test]
    fn a_tight_budget_falls_back_to_the_pointer_then_to_nothing() {
        let value = "y".repeat(1_400);
        let full = hook_note("compacted", "", "T.", ROOT, Some((ROOT, &value)), 9_000).unwrap();
        let pointer = hook_note(
            "compacted",
            "",
            "T.",
            ROOT,
            Some((ROOT, &value)),
            full.len() as i64 - 1,
        )
        .unwrap();
        assert!(pointer.contains("does not fit this hook's room"));
        assert!(pointer.contains("memory_fact_get(entity=\""));
        assert!(!pointer.contains(&value));
        assert_eq!(
            hook_note("compacted", "", "T.", ROOT, Some((ROOT, &value)), 40),
            None
        );
        assert_eq!(hook_note("compacted", "", "T.", ROOT, None, 40), None);
    }
}

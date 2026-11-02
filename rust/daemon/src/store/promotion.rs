//! `MemoryService._promote_slots`: deterministic slot writes under the caller's
//! existing service write lock. These deliberately bypass `cortex_write`'s
//! inferred freshness, slot embedding and correction-signal side effects.

use crate::config::MemoryConfig;
use crate::labels::Resolved;
use crate::service::{Ready, Service};
use crate::stores::cortex_write::{FactWrite, norm_support};

pub(super) struct Input<'a> {
    pub text: &'a str,
    pub source: &'a str,
    pub origin: Option<&'a str>,
    pub writer: &'a str,
    pub session: Option<String>,
    pub authority: Option<String>,
    pub distortion: Option<String>,
}

fn source_origin(source: &str) -> Option<&'static str> {
    match super::py_strip(source).to_lowercase().as_str() {
        "conversation" | "user" => Some("user"),
        "claude" | "assistant" | "agent" => Some("agent"),
        "tool" | "action" => Some("action"),
        _ => None,
    }
}

pub(super) async fn promote(
    svc: &Service,
    ready: &Ready,
    cfg: &MemoryConfig,
    a: Input<'_>,
) -> usize {
    let support = norm_support(a.origin.or_else(|| source_origin(a.source)));
    let provenance = if a.source.is_empty() {
        Vec::new()
    } else {
        vec![a.source.to_string()]
    };
    let mut written = 0;
    for slot in crate::slots::extract_slots(a.text, None) {
        let value = if slot.polarity == "-" {
            format!("NOT {}", slot.value)
        } else {
            slot.value
        };
        let claim = format!("{} {} {value}", slot.entity, slot.attribute);
        let Ok(embedding) =
            crate::fact_set::embed(ready, super::py_strip(&claim).to_string()).await
        else {
            continue;
        };
        let w = FactWrite {
            entity: &slot.entity,
            attribute: &slot.attribute,
            value: &value,
            raw: Default::default(),
            embedding,
            slot_embedding: None,
            confidence: cfg.cortex_promote_confidence,
            support,
            now: crate::episodes::now(),
            hlc: ready.tick_hlc(),
            writer_id: Some(a.writer.to_string()),
            session_id: a.session.clone(),
            freshness_class: "evergreen".into(),
            authority: a
                .authority
                .clone()
                .map_or(Resolved::Inherit, |s| Resolved::Set(Some(s))),
            distortion_tolerance: a
                .distortion
                .clone()
                .map_or(Resolved::Inherit, |s| Resolved::Set(Some(s))),
            provenance: provenance.clone(),
            stance: None,
            force_contend: false,
            dream: false,
        };
        if ready.cortex.write(|store| store.write_fact(&w)).is_err() {
            eprintln!(
                "scalar auto-promote skipped: slot holds a set: {}.{}",
                slot.entity, slot.attribute
            );
            continue;
        }
        match crate::fact_set::ensure_subject_entity(ready.storage.client(), &slot.entity).await {
            Ok(Some(id)) => {
                crate::dream::relations::propose_write_dedup(
                    svc,
                    ready.storage.client(),
                    id,
                    &slot.entity,
                )
                .await
            }
            Ok(None) => {}
            Err(_) => continue,
        }
        written += 1;
    }
    written
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_defaults_and_explicit_origin_follow_python() {
        for (source, expected) in [
            (" Conversation ", Some("user")),
            ("user", Some("user")),
            ("claude", Some("agent")),
            ("assistant", Some("agent")),
            ("agent", Some("agent")),
            (" TOOL ", Some("action")),
            ("action", Some("action")),
            ("notes", None),
            ("", None),
        ] {
            assert_eq!(source_origin(source), expected);
        }
        // An explicitly empty/unknown origin stays unknown, rather than falling back.
        assert_eq!(
            norm_support(Some("").or_else(|| source_origin("conversation"))),
            None
        );
    }
}

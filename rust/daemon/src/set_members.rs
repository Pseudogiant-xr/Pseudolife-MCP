//! `memory_set_add` and `memory_set_remove` over MCP (spec
//! "memory_set_add and memory_set_remove over MCP", SA1-SA11).
//! Python: `mcp_server.py:1750-1782` with `_noted` / `_SET_NOTES`
//! (`:679`, `:721-733`); `service.py` `set_add` (4042-4098) and
//! `set_remove` (4100-4133). The engine is `stores/cortex_members.rs`; the
//! embedding, entity mint and persistence are `fact_set.rs`'s.

use std::sync::Arc;

use serde_json::{Map, Value, json};

use crate::episodes::{self, Caller};
use crate::fact_set::{embed, ensure_subject_entity, internal, save_cortex};
use crate::mcp::dispatch::{ToolCall, ToolError, ToolFn, ToolFuture};
use crate::service::{Ready, Service};
use crate::store::py_strip;
use crate::stores::cortex_members::{MAX_CURRENT_MEMBERS, MemberWrite};

/// `set_add`'s default confidence (`service.py:4047`).
const SET_ADD_CONFIDENCE: f64 = 0.7;

pub fn mcp_tools() -> Vec<(&'static str, ToolFn)> {
    vec![
        ("memory_set_add", mcp_memory_set_add),
        ("memory_set_remove", mcp_memory_set_remove),
    ]
}

/// `_SET_NOTES` (`mcp_server.py:721-733`): the note a refusal or no-op
/// carries beside its action.
fn set_note(action: &str) -> Option<String> {
    Some(match action {
        "member_invalid" => "Not added: the member is blank; pass a non-empty value.".into(),
        "member_capped" => format!(
            "Not added: the slot already holds {MAX_CURRENT_MEMBERS} current members, the \
             cap; retract one with memory_set_remove first."
        ),
        "member_not_found" => "Nothing removed: no current member matches (case-insensitive); \
                               read the slot with memory_fact_get."
            .into(),
        "member_remove_refused" => "Not removed: an assistant-origin retraction cannot remove \
                                    a member another source added; ask the human."
            .into(),
        _ => return None,
    })
}

/// The three arguments both MCP tools bind, as plain strings.
struct Args {
    entity: String,
    attribute: String,
    member: String,
}

impl Args {
    fn bind(a: &Map<String, Value>) -> Args {
        let text = |k: &str| a.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        Args {
            entity: text("entity"),
            attribute: text("attribute"),
            member: text("member"),
        }
    }

    /// `memory_set_add`'s service call: `service.set_add(entity,
    /// attribute, member)` with every other input at its default.
    fn add(self) -> SetAddArgs {
        SetAddArgs::new(&self.entity, &self.attribute, &self.member)
    }

    /// `memory_set_remove`'s service call, likewise.
    fn remove(self) -> SetRemoveArgs {
        SetRemoveArgs::new(&self.entity, &self.attribute, &self.member)
    }
}

/// `{action, entity, attribute, member, members_count}` echoing the raw
/// arguments: the service answer of `set_add` and `set_remove`.
fn answer(entity: &str, attribute: &str, member: &str, action: &str, count: usize) -> Value {
    let mut out = Map::new();
    out.insert("action".into(), json!(action));
    out.insert("entity".into(), json!(entity));
    out.insert("attribute".into(), json!(attribute));
    out.insert("member".into(), json!(member));
    out.insert("members_count".into(), json!(count));
    Value::Object(out)
}

/// `_noted(out, _SET_NOTES, key="action")`: the MCP tools' note.
fn noted(mut out: Value) -> Value {
    let note = out["action"].as_str().and_then(set_note);
    if let (Some(note), Value::Object(map)) = (note, &mut out) {
        map.entry("note").or_insert(json!(note));
    }
    out
}

/// The arguments of `service.set_add` (`service.py:4042-4050`):
/// `memory_set_add` passes the slot and member; the dream's member add
/// passes `confidence`, `origin` (the claim's tier) and `dream=True`
/// (`service_dream.py:2056`), the rollback replay `confidence` and `origin`.
pub(crate) struct SetAddArgs {
    pub entity: String,
    pub attribute: String,
    pub member: String,
    pub provenance: Vec<String>,
    /// As passed; the store normalises it for its guards.
    pub origin: Option<String>,
    pub confidence: f64,
    pub dream: bool,
}

impl SetAddArgs {
    /// The Python defaults: no provenance or origin, confidence 0.7, not a
    /// dream op.
    pub(crate) fn new(entity: &str, attribute: &str, member: &str) -> SetAddArgs {
        SetAddArgs {
            entity: entity.into(),
            attribute: attribute.into(),
            member: member.into(),
            provenance: Vec::new(),
            origin: None,
            confidence: SET_ADD_CONFIDENCE,
            dream: false,
        }
    }
}

/// The arguments of `service.set_remove` (`service.py:4100-4102`):
/// `memory_set_remove` passes the slot and member; the dream's retraction
/// passes `origin` and `dream=True` (`service_dream.py:2075`).
pub(crate) struct SetRemoveArgs {
    pub entity: String,
    pub attribute: String,
    pub member: String,
    pub origin: Option<String>,
    pub dream: bool,
}

impl SetRemoveArgs {
    pub(crate) fn new(entity: &str, attribute: &str, member: &str) -> SetRemoveArgs {
        SetRemoveArgs {
            entity: entity.into(),
            attribute: attribute.into(),
            member: member.into(),
            origin: None,
            dream: false,
        }
    }
}

fn mcp_memory_set_add<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let svc = app.service.clone();
        let ready = svc.ensure_init().await.map_err(internal)?;
        let caller = Caller {
            header_session: call.ident.header_session.clone(),
            transport_session: call.ident.transport_session.clone(),
        };
        let writer = call.ident.writer.clone();
        let args = Args::bind(call.args).add();
        // An admitted write runs to completion in its own task, as
        // `memory_fact_set` does.
        let r2 = ready.clone();
        ready
            .writes
            .spawn(async move { set_add(&svc, &r2, &caller, writer, args).await.map(noted) })
            .await
            .map_err(internal)?
    })
}

fn mcp_memory_set_remove<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let svc = app.service.clone();
        let ready = svc.ensure_init().await.map_err(internal)?;
        let args = Args::bind(call.args).remove();
        let r2 = ready.clone();
        ready
            .writes
            .spawn(async move { set_remove(&svc, &r2, args).await.map(noted) })
            .await
            .map_err(internal)?
    })
}

/// `service.set_add` (`service.py:4042-4098`), under the write lock from
/// the embedding to the answer: the embedding, the writer stamps, an HLC
/// tick, the store add, the entity mint and the per-slot save. Answers the
/// service dict (no note; the MCP tool adds it).
pub(crate) async fn set_add(
    svc: &Service,
    ready: &Arc<Ready>,
    caller: &Caller,
    writer: String,
    a: SetAddArgs,
) -> Result<Value, ToolError> {
    let db = ready.storage.client();
    let ws = ready.write.lock().await;
    // Embedded even for a blank member, as Python does.
    let claim = py_strip(&format!("{} {} {}", a.entity, a.attribute, a.member)).to_string();
    let embedding = embed(ready, claim).await?;
    let session = ws.resolve_session(caller).map_err(ToolError::Invalid)?;
    // Ticked before the write, so an invalid or capped add still advances
    // the clock.
    let hlc = ready.tick_hlc();
    let w = MemberWrite {
        entity: &a.entity,
        attribute: &a.attribute,
        member: &a.member,
        embedding,
        confidence: a.confidence,
        now: episodes::now(),
        hlc,
        writer_id: Some(writer),
        session_id: session,
        support: a.origin.clone(),
        provenance: a.provenance.clone(),
        dream: a.dream,
    };
    let outcome = ready.cortex.write(|s| s.add_member(&w));
    // Every outcome, invalid and capped included, may mint the subject.
    ensure_subject_entity(db, &a.entity)
        .await
        .map_err(internal)?;
    save_cortex(svc, ready).await?;
    let count = ready
        .cortex
        .snapshot()
        .members(&a.entity, &a.attribute)
        .len();
    drop(ws);
    Ok(answer(
        &a.entity,
        &a.attribute,
        &a.member,
        outcome.action,
        count,
    ))
}

/// `service.set_remove` (`service.py:4100-4133`), under the write lock: no
/// embedding, no clock tick, no writer, no entity mint; the per-slot save
/// runs on every outcome, a refusal included. Answers the service dict.
pub(crate) async fn set_remove(
    svc: &Service,
    ready: &Arc<Ready>,
    a: SetRemoveArgs,
) -> Result<Value, ToolError> {
    let ws = ready.write.lock().await;
    let now = episodes::now();
    let outcome = ready.cortex.write(|s| {
        s.remove_member(
            &a.entity,
            &a.attribute,
            &a.member,
            now,
            a.origin.as_deref(),
            a.dream,
        )
    });
    save_cortex(svc, ready).await?;
    let count = ready
        .cortex
        .snapshot()
        .members(&a.entity, &a.attribute)
        .len();
    drop(ws);
    Ok(answer(
        &a.entity,
        &a.attribute,
        &a.member,
        outcome.action,
        count,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_mcp_binding_passes_the_neutral_dream_inputs() {
        let bound = || {
            Args::bind(
                json!({"entity": "Box", "attribute": "tags", "member": "x"})
                    .as_object()
                    .unwrap(),
            )
        };
        let add = bound().add();
        assert_eq!(add.confidence, SET_ADD_CONFIDENCE);
        assert!(add.origin.is_none() && add.provenance.is_empty() && !add.dream);
        let remove = bound().remove();
        assert!(remove.origin.is_none() && !remove.dream);
        // The service answer carries no note; the MCP tool adds it, as
        // `_noted` does, without replacing one already there.
        let out = answer("Box", "tags", "x", "member_remove_refused", 1);
        assert!(out.get("note").is_none());
        assert_eq!(
            noted(out)["note"],
            json!(set_note("member_remove_refused").unwrap())
        );
        let mut kept = answer("Box", "tags", "x", "member_invalid", 0);
        kept["note"] = json!("kept");
        assert_eq!(noted(kept)["note"], json!("kept"));
    }

    #[test]
    fn notes_follow_python_set_notes() {
        struct A;
        impl A {
            fn answer(&self, action: &str, n: usize) -> Value {
                noted(answer("Box", "tags", " ", action, n))
            }
        }
        let a = A;
        assert_eq!(
            a.answer("member_invalid", 0),
            json!({"action": "member_invalid", "entity": "Box", "attribute": "tags",
                   "member": " ", "members_count": 0,
                   "note": "Not added: the member is blank; pass a non-empty value."})
        );
        assert_eq!(
            a.answer("member_capped", 100)["note"],
            json!(
                "Not added: the slot already holds 100 current members, the cap; retract one \
                 with memory_set_remove first."
            )
        );
        assert_eq!(
            a.answer("member_not_found", 2)["note"],
            json!(
                "Nothing removed: no current member matches (case-insensitive); read the slot \
                 with memory_fact_get."
            )
        );
        for quiet in [
            "member_added",
            "member_confirmed",
            "member_removed",
            "confirmed",
            "contested",
        ] {
            assert!(a.answer(quiet, 1).get("note").is_none(), "{quiet}");
        }
        // Key order is Python's dict order.
        let keys: Vec<String> = a
            .answer("member_not_found", 0)
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect();
        assert_eq!(
            keys,
            [
                "action",
                "entity",
                "attribute",
                "member",
                "members_count",
                "note"
            ]
        );
    }
}

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

/// The three bound arguments, as plain strings.
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

    /// `{action, entity, attribute, member, members_count}` echoing the raw
    /// arguments, then `_noted(..., key="action")`.
    fn answer(&self, action: &str, members_count: usize) -> Value {
        let mut out = Map::new();
        out.insert("action".into(), json!(action));
        out.insert("entity".into(), json!(self.entity));
        out.insert("attribute".into(), json!(self.attribute));
        out.insert("member".into(), json!(self.member));
        out.insert("members_count".into(), json!(members_count));
        if let Some(note) = set_note(action) {
            out.insert("note".into(), json!(note));
        }
        Value::Object(out)
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
        let args = Args::bind(call.args);
        // An admitted write runs to completion in its own task, as
        // `memory_fact_set` does.
        let r2 = ready.clone();
        ready
            .writes
            .spawn(async move { set_add(&svc, &r2, &caller, writer, args).await })
            .await
            .map_err(internal)?
    })
}

fn mcp_memory_set_remove<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let svc = app.service.clone();
        let ready = svc.ensure_init().await.map_err(internal)?;
        let args = Args::bind(call.args);
        let r2 = ready.clone();
        ready
            .writes
            .spawn(async move { set_remove(&svc, &r2, args).await })
            .await
            .map_err(internal)?
    })
}

/// `service.set_add` with the MCP arguments (`origin=None`, no provenance,
/// `dream=False`), under the write lock from the embedding to the answer.
async fn set_add(
    svc: &Service,
    ready: &Arc<Ready>,
    caller: &Caller,
    writer: String,
    a: Args,
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
        confidence: SET_ADD_CONFIDENCE,
        now: episodes::now(),
        hlc,
        writer_id: Some(writer),
        session_id: session,
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
    Ok(a.answer(outcome.action, count))
}

/// `service.set_remove` with the MCP arguments (`origin=None`,
/// `dream=False`): no embedding, no clock tick, no writer, no entity mint.
async fn set_remove(svc: &Service, ready: &Arc<Ready>, a: Args) -> Result<Value, ToolError> {
    let ws = ready.write.lock().await;
    let now = episodes::now();
    let outcome = ready
        .cortex
        .write(|s| s.remove_member(&a.entity, &a.attribute, &a.member, now));
    save_cortex(svc, ready).await?;
    let count = ready
        .cortex
        .snapshot()
        .members(&a.entity, &a.attribute)
        .len();
    drop(ws);
    Ok(a.answer(outcome.action, count))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn notes_follow_python_set_notes() {
        let a = Args {
            entity: "Box".into(),
            attribute: "tags".into(),
            member: " ".into(),
        };
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

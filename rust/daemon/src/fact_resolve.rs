//! `memory_fact_resolve` over MCP (spec "memory_fact_resolve over MCP",
//! FR1-FR8). Python: `mcp_server.py:1785-1801`; `service.py`
//! `cortex_resolve` (4465-4528). The engine is `stores/cortex_resolve.rs`;
//! the persistence is `fact_set.rs`'s `save_cortex`.

use std::sync::Arc;

use serde_json::{Map, Value, json};

use crate::episodes;
use crate::fact_set::{internal, save_cortex};
use crate::mcp::dispatch::{ToolCall, ToolError, ToolFn, ToolFuture};
use crate::read::cortex_search::record_to_dict;
use crate::service::{Ready, Service};

/// The tier `cortex_resolve` stamps on an accepted contender: its
/// `support="user"` default, which the MCP tool never overrides.
pub(crate) const RESOLVE_SUPPORT: &str = "user";

pub fn mcp_tools() -> Vec<(&'static str, ToolFn)> {
    vec![("memory_fact_resolve", mcp_memory_fact_resolve)]
}

/// The arguments of `service.cortex_resolve` (`service.py:4465-4467`).
/// `memory_fact_resolve` passes the slot and `accept` (`Self::bind`); the
/// consolidation quarantine's promotion passes `support` (the promoting
/// claim's tier), `dream=True` and `expect_value` (`service_dream.py:2134`).
pub(crate) struct ResolveArgs {
    pub entity: String,
    pub attribute: String,
    pub accept: bool,
    pub support: String,
    pub dream: bool,
    pub expect_value: Option<String>,
}

impl ResolveArgs {
    /// The Python defaults: `support="user"`, `dream=False`, no
    /// `expect_value`.
    pub(crate) fn new(entity: &str, attribute: &str, accept: bool) -> ResolveArgs {
        ResolveArgs {
            entity: entity.into(),
            attribute: attribute.into(),
            accept,
            support: RESOLVE_SUPPORT.into(),
            dream: false,
            expect_value: None,
        }
    }

    fn bind(a: &Map<String, Value>) -> ResolveArgs {
        let text = |k: &str| a.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        ResolveArgs::new(
            &text("entity"),
            &text("attribute"),
            a.get("accept").and_then(Value::as_bool).unwrap_or(false),
        )
    }

    /// `{"resolved": False, "reason", "entity", "attribute"}` with the raw
    /// arguments.
    fn unresolved(&self, reason: &str) -> Value {
        json!({"resolved": false, "reason": reason, "entity": self.entity,
               "attribute": self.attribute})
    }
}

fn mcp_memory_fact_resolve<'a>(app: &'a crate::http::App, call: ToolCall<'a>) -> ToolFuture<'a> {
    Box::pin(async move {
        let svc = app.service.clone();
        let ready = svc.ensure_init().await.map_err(internal)?;
        let args = ResolveArgs::bind(call.args);
        // An admitted write runs to completion in its own task, as
        // `memory_fact_set` does.
        let r2 = ready.clone();
        ready
            .writes
            .spawn(async move { cortex_resolve(&svc, &r2, args).await })
            .await
            .map_err(internal)?
    })
}

/// `service.cortex_resolve` (`service.py:4465-4528`), under the write
/// lock: no embedding, no writer or session, no entity mint, no correction
/// signal. A refusal answers `resolved: false` with its reason
/// (`slot_holds_set`, `direct_write`, `contender_changed`) and saves
/// nothing. `memory_fact_resolve` calls it with the neutral inputs; the
/// quarantine's promotion calls it with its own.
pub(crate) async fn cortex_resolve(
    svc: &Service,
    ready: &Arc<Ready>,
    a: ResolveArgs,
) -> Result<Value, ToolError> {
    let ws = ready.write.lock().await;
    // The clock ticks only on accept, before the store is consulted, so an
    // accept that finds no contender or is refused still advances it.
    let hlc = a.accept.then(|| ready.tick_hlc());
    let now = episodes::now();
    let res = ready.cortex.write(|s| {
        s.resolve(
            &a.entity,
            &a.attribute,
            a.accept,
            now,
            &a.support,
            hlc,
            a.dream,
            a.expect_value.as_deref(),
        )
    });
    let Some(res) = res else {
        return Ok(a.unresolved("no_contender"));
    };
    match res.action {
        "refused" => return Ok(a.unresolved("slot_holds_set")),
        "refused_direct" => return Ok(a.unresolved("direct_write")),
        "refused_changed" => return Ok(a.unresolved("contender_changed")),
        _ => {}
    }
    save_cortex(svc, ready).await?;
    let snap = ready.cortex.snapshot();
    let policy = &svc.config.memory.search.stale_policy;
    let now = episodes::now();
    let current = snap
        .lookup(&a.entity, &a.attribute)
        .map_or(Value::Null, |c| {
            Value::Object(record_to_dict(c, policy, now))
        });
    let record = Value::Object(record_to_dict(&snap.records[res.record], policy, now));
    drop(ws);
    Ok(
        json!({"resolved": true, "accepted": a.accept, "action": res.action,
              "current": current, "record": record}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unresolved_answers_echo_the_raw_arguments_in_python_order() {
        let a = ResolveArgs::bind(
            json!({"entity": " Dev Box ", "attribute": "OS", "accept": true})
                .as_object()
                .unwrap(),
        );
        assert!(a.accept);
        assert_eq!(a.support, "user");
        assert!(!a.dream && a.expect_value.is_none());
        let v = a.unresolved("no_contender");
        let keys: Vec<&String> = v.as_object().unwrap().keys().collect();
        assert_eq!(keys, ["resolved", "reason", "entity", "attribute"]);
        assert_eq!(
            v,
            json!({"resolved": false, "reason": "no_contender", "entity": " Dev Box ",
                   "attribute": "OS"})
        );
    }
}

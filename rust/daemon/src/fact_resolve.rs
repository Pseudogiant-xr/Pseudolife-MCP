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
const RESOLVE_SUPPORT: &str = "user";

pub fn mcp_tools() -> Vec<(&'static str, ToolFn)> {
    vec![("memory_fact_resolve", mcp_memory_fact_resolve)]
}

/// The bound arguments `mcp_server.memory_fact_resolve` passes on.
struct Args {
    entity: String,
    attribute: String,
    accept: bool,
}

impl Args {
    fn bind(a: &Map<String, Value>) -> Args {
        let text = |k: &str| a.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        Args {
            entity: text("entity"),
            attribute: text("attribute"),
            accept: a.get("accept").and_then(Value::as_bool).unwrap_or(false),
        }
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
        let args = Args::bind(call.args);
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

/// `service.cortex_resolve` with the MCP arguments (`support="user"`,
/// `dream=False`, `expect_value=None`), under the write lock: no
/// embedding, no writer or session, no entity mint, no correction signal.
async fn cortex_resolve(svc: &Service, ready: &Arc<Ready>, a: Args) -> Result<Value, ToolError> {
    let ws = ready.write.lock().await;
    // The clock ticks only on accept, before the store is consulted, so an
    // accept that finds no contender or a set slot still advances it.
    let hlc = a.accept.then(|| ready.tick_hlc());
    let now = episodes::now();
    let res = ready
        .cortex
        .write(|s| s.resolve(&a.entity, &a.attribute, a.accept, now, RESOLVE_SUPPORT, hlc));
    let Some(res) = res else {
        return Ok(a.unresolved("no_contender"));
    };
    if res.action == "refused" {
        return Ok(a.unresolved("slot_holds_set"));
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
        let a = Args::bind(
            json!({"entity": " Dev Box ", "attribute": "OS", "accept": true})
                .as_object()
                .unwrap(),
        );
        assert!(a.accept);
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

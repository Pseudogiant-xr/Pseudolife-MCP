//! `GET /api/agents?view=coordination` (`coordination.console_snapshot`,
//! `storage/coordination.py:1831-1899`): one read-only, repeatable-read
//! snapshot of board metadata for the Console. No registration, mailbox
//! read, prune or lease settlement; mail counts and events are limited to
//! the caller's scope (v59: the owner set for an owner principal, else the
//! caller alone); no payload, body, salt or credential leaves it.

use super::Board;
use super::canon::float_value;
use super::db::{obj, opt_str};
use super::error::{BoardError, Result};
use super::store::{Store, check_limit, now};
use crate::auth::{EnvTokens, PrincipalStore};
use crate::config::CoordinationConfig;
use serde_json::{Value, json};

/// `CONSOLE_TIMELINE_SQL` (storage/coordination.py), the same text with
/// `$1` the scope and `$2` the cap; tests/test_coordination_console.py pins
/// it equal to Python's.
const TIMELINE_SQL: &str = include_str!("console_timeline.sql");
/// `CONSOLE_EVENTS_CAP`: 100 events shown and one to say the list was cut.
const EVENTS_CAP: i64 = 101;

/// `principals.owner_principals` (v59): the environment principals (the
/// token map's names, and `default` with a singular token) not listed in
/// `coordination.not_owner_principals`, the owned stored principals that
/// are not revoked, and `maintainer`; never `daemon`. Sorted, as Python's
/// `sorted(owners)` passes them.
pub(crate) fn owner_principals(
    config: &CoordinationConfig,
    env: &EnvTokens,
    store: &PrincipalStore,
) -> Vec<String> {
    let mut owners: Vec<String> = env.map.iter().map(|(_, p)| p.clone()).collect();
    if env.single.is_some() {
        owners.push("default".to_string());
    }
    owners.retain(|p| !config.not_owner_principals.contains(p));
    owners.extend(store.owned_principals());
    owners.retain(|p| p != super::store::DAEMON_PRINCIPAL);
    owners.push(super::store::MAINTAINER_PRINCIPAL.to_string());
    owners.sort();
    owners.dedup();
    owners
}

/// The scope a caller's snapshot covers: the owner set when the caller is
/// the owner's, else the caller alone.
pub(crate) fn console_scope(principal: &str, owners: Vec<String>) -> Vec<String> {
    if owners.iter().any(|p| p == principal) {
        owners
    } else {
        vec![principal.to_string()]
    }
}

const DETAILS: [&str; 9] = [
    "hinted",
    "not_needed",
    "rung",
    "withheld",
    "nudged",
    "no_path",
    "capped",
    "pull",
    "delivery",
];

impl Store {
    pub async fn console_snapshot(
        &self,
        client: &mut tokio_postgres::Client,
        scope: &[String],
        limit: i64,
    ) -> Result<Value> {
        let tx = client.transaction().await?;
        // No JIT for this read, as Python's `console_snapshot`: measured
        // 2026-11-02 on a live-size board (470 agents, 77,767 events,
        // PostgreSQL 18.6; evals/results/console-snapshot-bench-20261102.json),
        // turning JIT off took the owner-scope timeline's median from 627 to
        // 130 ms and one principal's from 532 to 54 ms.
        tx.batch_execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY; \
             SET LOCAL statement_timeout = '5s'; SET LOCAL jit = off",
        )
        .await?;
        let limit = check_limit(&json!(limit))?;
        let mut result = self.roster_read(&tx, "", None, None, limit).await?;
        let now = now();
        let ids: Vec<String> = result["agents"]
            .as_array()
            .map(|a| {
                a.iter()
                    .map(|x| x["agent_id"].as_str().unwrap_or("").to_string())
                    .collect()
            })
            .unwrap_or_default();
        let counts: std::collections::HashMap<String, i64> = tx
            .query(
                "SELECT m.recipient_agent_id,count(*) AS n FROM coordination_messages m \
                 JOIN coordination_agents a ON a.agent_id=m.recipient_agent_id \
                 WHERE a.principal=ANY($1) AND m.acknowledged_at IS NULL AND m.expires_at>$2 \
                 AND m.recipient_agent_id=ANY($3) GROUP BY m.recipient_agent_id",
                &[&scope, &now, &ids],
            )
            .await?
            .iter()
            .map(|r| (r.get(0), r.get(1)))
            .collect();
        if let Some(agents) = result["agents"].as_array_mut() {
            for agent in agents {
                let mine = agent["principal"]
                    .as_str()
                    .is_some_and(|p| scope.iter().any(|s| s == p));
                let id = agent["agent_id"].as_str().unwrap_or("").to_string();
                agent["pending_count"] = if mine {
                    json!(counts.get(&id).copied().unwrap_or(0))
                } else {
                    Value::Null
                };
            }
        }
        if let Some(leases) = result["leases"].as_array_mut() {
            for lease in leases {
                let expired = lease["expires_at"].as_f64().is_some_and(|e| e <= now);
                lease["expired"] = json!(expired);
            }
        }
        let rows = tx.query(TIMELINE_SQL, &[&scope, &EVENTS_CAP]).await?;
        super::db::commit(tx).await?;
        let events: Vec<Value> = rows
            .iter()
            .take(100)
            .map(|r| {
                let detail: Option<String> = r.get("detail");
                obj(vec![
                    ("seq", json!(r.get::<_, i64>("seq"))),
                    ("event", json!(r.get::<_, String>("event"))),
                    ("created_at", float_value(r.get("created_at"))),
                    ("agent_id", json!(r.get::<_, String>("agent_id"))),
                    (
                        "recipient_agent_id",
                        opt_str(r.get::<_, Option<String>>("recipient_agent_id").as_deref()),
                    ),
                    (
                        "message_id",
                        opt_str(r.get::<_, Option<String>>("message_id").as_deref()),
                    ),
                    ("project", json!(r.get::<_, String>("project"))),
                    ("task", json!(r.get::<_, String>("task"))),
                    (
                        "detail",
                        opt_str(detail.as_deref().filter(|d| DETAILS.contains(d))),
                    ),
                    ("expired_count", json!(r.get::<_, i64>("expired_count"))),
                ])
            })
            .collect();
        let mut out = json!({"enabled": true, "available": true, "snapshot_at": float_value(now)});
        if let (Value::Object(o), Value::Object(r)) = (&mut out, result) {
            for (k, v) in r {
                o.insert(k, v);
            }
        }
        out["events"] = Value::Array(events);
        out["events_truncated"] = json!(rows.len() > 100);
        Ok(out)
    }
}

/// Python `int(v)` on a query value: surrounding whitespace, a sign,
/// single underscores between digits; anything else falls back.
fn py_int_param(raw: Option<&String>, default: i64) -> i64 {
    let Some(raw) = raw.filter(|s| !s.is_empty()) else {
        return default;
    };
    let t = super::text::py_strip(raw);
    let chars: Vec<char> = t.chars().collect();
    for (i, c) in chars.iter().enumerate() {
        if *c == '_'
            && !(i > 0
                && i + 1 < chars.len()
                && chars[i - 1].is_ascii_digit()
                && chars[i + 1].is_ascii_digit())
        {
            return default;
        }
    }
    let digits = t.replace('_', "");
    let unsigned = digits.strip_prefix(['+', '-']).unwrap_or(&digits);
    if unsigned.is_empty() || !unsigned.bytes().all(|b| b.is_ascii_digit()) {
        return default;
    }
    // A Python int too large for i64 still reaches `_limit` and fails it.
    digits.parse::<i64>().unwrap_or(i64::MAX)
}

impl Board {
    /// `coordination.console_snapshot` for the route.
    pub async fn console(
        &self,
        principal: &str,
        params: &std::collections::HashMap<String, String>,
    ) -> Result<Value> {
        if !self.config.enabled {
            return Ok(json!({"enabled": false, "available": false, "reason": "disabled"}));
        }
        let limit = py_int_param(params.get("limit"), 50);
        if !self.admitted(principal) {
            return Err(BoardError::code("principal_not_allowed"));
        }
        if self.service.snapshot().storage.is_none() {
            return Ok(json!({"enabled": true, "available": false, "reason": "not_initialized"}));
        }
        let owners = owner_principals(&self.config, &EnvTokens::from_env(), &self.principals);
        let scope = console_scope(principal, owners);
        let mut conn = self.conn.lock().await;
        let client = conn.client().await?;
        self.store.console_snapshot(client, &scope, limit).await
    }
}

#[cfg(test)]
mod owner_tests {
    use super::*;
    use crate::auth::StoredPrincipal;

    fn stored(name: &str, revoked: bool, owned: bool) -> StoredPrincipal {
        StoredPrincipal {
            principal: name.into(),
            token_hash: Some(format!("{name:0>64}")),
            tier: None,
            board: true,
            revoked,
            owned,
        }
    }

    fn snapshot() -> PrincipalStore {
        let store = PrincipalStore::new();
        let rows = vec![
            stored("laptop", false, true),
            stored("guest", false, false),
            stored("old", true, true),
        ];
        store.finish_refresh(store.begin_refresh(), rows, None);
        store
    }

    fn env(map: &[(&str, &str)], single: bool) -> EnvTokens {
        EnvTokens {
            map: map.iter().map(|(t, p)| (t.to_string(), p.to_string())).collect(),
            single: single.then(|| "t0".to_string()),
        }
    }

    fn names(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| s.to_string()).collect()
    }

    /// tests/test_principals.py's owner-set cases, mirrored.
    #[test]
    fn the_owner_set_matches_python() {
        let config = CoordinationConfig::default();
        let owners = owner_principals(
            &config,
            &env(&[("t1", "desk"), ("t2", "codex")], true),
            &snapshot(),
        );
        assert_eq!(
            owners,
            names(&["codex", "default", "desk", "laptop", "maintainer"])
        );
        let open = owner_principals(&config, &env(&[], false), &PrincipalStore::new());
        assert_eq!(open, names(&["maintainer"]));
        let config = CoordinationConfig {
            not_owner_principals: names(&["friend", "default"]),
            ..CoordinationConfig::default()
        };
        let owners = owner_principals(
            &config,
            &env(&[("t1", "friend"), ("t2", "codex"), ("t3", "daemon")], true),
            &snapshot(),
        );
        assert_eq!(owners, names(&["codex", "laptop", "maintainer"]));
    }

    #[test]
    fn only_an_owner_caller_gets_the_owner_scope() {
        let owners = names(&["alice", "bob", "maintainer"]);
        assert_eq!(console_scope("bob", owners.clone()), owners);
        assert_eq!(console_scope("guest", owners), names(&["guest"]));
    }

    #[test]
    fn the_timeline_sql_is_the_shared_text() {
        assert!(TIMELINE_SQL.starts_with("WITH scope AS (SELECT DISTINCT unnest($1::text[])"));
        assert!(!TIMELINE_SQL.contains("%("));
        assert_eq!(TIMELINE_SQL.matches("LIMIT $2").count(), 7);
    }
}

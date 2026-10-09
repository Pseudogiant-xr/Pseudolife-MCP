//! Who a read is attributed to (spec S8c): `MemoryService._resolve_writer`
//! tiers 1 and 3 (`service.py:970-1011`) and `_caller_episode_id`
//! (`service.py:4915-4930`, `memory/episodes.py:187-202`).

use serde_json::Value;
use tokio_postgres::Client;

const DEFAULT_TTL_S: f64 = 21600.0;

/// The `X-PL-Session` header when present, else the active-session pointer
/// (`meta.active_session_pointer`) while younger than
/// `PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS` (0 disables the TTL).
pub async fn session_id(db: &Client, header: Option<&str>, now: f64) -> Option<String> {
    if let Some(h) = header.filter(|h| !h.is_empty()) {
        return Some(h.to_string());
    }
    let row = db
        .query_opt(
            "SELECT value FROM meta WHERE key = 'active_session_pointer'",
            &[],
        )
        .await
        .ok()??;
    let v: Option<Value> = row.get(0);
    let v = v?;
    let sid = v
        .get("session_id")?
        .as_str()
        .filter(|s| !s.is_empty())?
        .to_string();
    let ts = v.get("ts").and_then(Value::as_f64).unwrap_or(0.0);
    let ttl = std::env::var("PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS")
        .ok()
        .and_then(|s| s.trim().parse::<f64>().ok())
        .unwrap_or(DEFAULT_TTL_S);
    if ttl > 0.0 && now - ts > ttl {
        return None;
    }
    Some(sid)
}

/// `EpisodeManager.open_leaf_for`: the deepest open episode of the session
/// (the open episode no other open one names as parent), latest start first.
pub async fn open_leaf_episode(db: &Client, session: &str) -> Option<String> {
    let rows = db
        .query(
            "SELECT id, parent_id, started_at FROM episodes \
             WHERE ended_at IS NULL AND session_key = $1 ORDER BY started_at",
            &[&session],
        )
        .await
        .ok()?;
    let open: Vec<(String, Option<String>, f64)> = rows
        .iter()
        .map(|r| (r.get(0), r.get(1), r.get(2)))
        .collect();
    if open.is_empty() {
        return None;
    }
    let parents: Vec<&str> = open.iter().filter_map(|e| e.1.as_deref()).collect();
    let leaves: Vec<&(String, Option<String>, f64)> = open
        .iter()
        .filter(|e| !parents.contains(&e.0.as_str()))
        .collect();
    let pool: Vec<&(String, Option<String>, f64)> = if leaves.is_empty() {
        open.iter().collect()
    } else {
        leaves
    };
    // `max(key=started_at)`: the first maximum wins.
    let mut best = pool[0];
    for e in &pool[1..] {
        if e.2 > best.2 {
            best = e;
        }
    }
    Some(best.0.clone())
}

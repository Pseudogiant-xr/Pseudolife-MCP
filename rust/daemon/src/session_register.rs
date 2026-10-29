//! A keyed session start (`service.py` `episode_start_session`,
//! `_register_client_session_locked`, `storage/postgres.py`
//! `register_client_session`) and the active-session pointer
//! (`set_active_session`): what the SessionStart hook runs, and what
//! `POST /api/episode/start` will.

use crate::episodes::WriteState;
use crate::service::Ready;
use serde_json::{Map, Value, json};
use std::sync::Arc;

/// `_ACTIVE_SESSION_META_KEY`.
const ACTIVE_SESSION_META_KEY: &str = "active_session_pointer";

/// How the session registered (`client_sessions.registered_via`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RegisteredVia {
    /// The SessionStart hook.
    Hook,
    /// `POST /api/episode/start` and in-process callers.
    #[allow(dead_code)] // B10f's `/api/episode/start`
    Api,
}

impl RegisteredVia {
    fn as_str(self) -> &'static str {
        match self {
            RegisteredVia::Hook => "hook",
            RegisteredVia::Api => "api",
        }
    }
}

/// What a keyed session start registers beside the episode.
pub struct Registration<'a> {
    pub via: RegisteredVia,
    /// `request_principal()`: the caller's principal name inside a request.
    pub principal: Option<&'a str>,
    pub policy_variant: Option<&'a str>,
}

/// `episode_start_session`: the key's open leaf when one is open (a resume
/// or compaction re-fire), else its recently reaped root reopened, else a
/// new root, never closing another session's episode. A keyed start then
/// upserts the session's `client_sessions` row. The episode's dict, or the
/// error Python raises (an unparsable resume window).
pub async fn episode_start_session(
    ready: &Arc<Ready>,
    key: Option<&str>,
    title: String,
    hint: Option<String>,
    reg: Registration<'_>,
) -> Result<Map<String, Value>, String> {
    let mut ws = ready.write.lock().await;
    let client = ready.storage.client();
    let existing = key.and_then(|k| ws.log.open_leaf_for(Some(k)));
    let i = match existing {
        Some(i) => i,
        None => {
            let resumed = match key.filter(|k| !k.is_empty()) {
                Some(k) => ws.resume_closed_session(client, k).await?,
                None => None,
            };
            match resumed {
                Some(i) => i,
                None => {
                    let i = ws.log.start_session(title, key.map(str::to_string));
                    ws.log.episodes[i].hint = hint;
                    ws.persist_episodes(client).await;
                    i
                }
            }
        }
    };
    register_client_session(ready, &ws, key, i, &reg).await;
    Ok(crate::episode_tools::episode_dict(&ws.log.episodes[i]))
}

/// `_register_client_session_locked`: the row names the session's root (a
/// re-fire hands back an open sub-episode). The first registration sets
/// `started_at`; a later one keeps it, clears the last close, appends the
/// root when it is new and its own time to `start_times`, and fills
/// `principal` and `policy_variant` only where they are still null. Best
/// effort; nothing for a keyless start.
async fn register_client_session(
    ready: &Ready,
    ws: &WriteState,
    key: Option<&str>,
    leaf: usize,
    reg: &Registration<'_>,
) {
    let Some(key) = key.filter(|k| !k.is_empty()) else {
        return;
    };
    if crate::mutants::active("session-start-no-registration") {
        return;
    }
    let log = &ws.log;
    let mut root = leaf;
    let mut seen = vec![log.episodes[leaf].id.clone()];
    while let Some(parent) = log.episodes[root].parent_id.as_deref()
        && let Some(p) = log.index(parent)
        && !seen.iter().any(|s| s == parent)
    {
        root = p;
        seen.push(log.episodes[p].id.clone());
    }
    let now = crate::episodes::now();
    let ids = json!([log.episodes[root].id]);
    let times = json!([now]);
    let via = reg.via.as_str();
    let (principal, variant) = (reg.principal, reg.policy_variant);
    let res = crate::txn::run(ready.storage.client(), |c| async move {
        c.execute(
            "INSERT INTO client_sessions (session_key, registered_via, principal, started_at, \
             policy_variant, episode_ids, start_times) VALUES ($1, $2, $3, $4, $5, $6, $7) \
             ON CONFLICT (session_key) DO UPDATE SET \
               ended_at = NULL, \
               end_reason = NULL, \
               principal = COALESCE(client_sessions.principal, EXCLUDED.principal), \
               policy_variant = COALESCE(client_sessions.policy_variant, EXCLUDED.policy_variant), \
               episode_ids = CASE \
                 WHEN client_sessions.episode_ids @> EXCLUDED.episode_ids \
                   THEN client_sessions.episode_ids \
                 ELSE client_sessions.episode_ids || EXCLUDED.episode_ids \
               END, \
               start_times = client_sessions.start_times || EXCLUDED.start_times",
            &[&key, &via, &principal, &now, &variant, &ids, &times],
        )
        .await
        .map(|_| ())
    })
    .await;
    if let Err(e) = res {
        eprintln!("client-session registration failed: {e}");
    }
}

/// `set_active_session(session_id)`: the machine-scoped active-session
/// pointer, last start wins; persisted to meta. A failed write is Python's
/// raised `set_meta`.
pub async fn set_active_session(ready: &Arc<Ready>, session_id: &str) -> Result<(), String> {
    let mut ws = ready.write.lock().await;
    let ts = crate::episodes::now();
    ws.active_session = Some((session_id.to_string(), ts));
    if crate::mutants::active("session-start-no-pointer") {
        return Ok(());
    }
    crate::txn::set_meta(
        ready.storage.client(),
        ACTIVE_SESSION_META_KEY,
        &json!({"session_id": session_id, "ts": ts}),
    )
    .await
    .map_err(|e| e.to_string())
}

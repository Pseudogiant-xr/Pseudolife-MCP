//! An explicit session end (`service.py` `episode_end_session`,
//! `_close_session_locked` with `prune_empty=True`, `_end_client_session_locked`
//! and `clear_active_session`): what the SessionEnd hook runs, and what
//! `POST /api/episode/end` will.

use crate::episodes::{EpisodeLog, WriteState};
use crate::service::Ready;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;

/// `_ACTIVE_SESSION_META_KEY`.
const ACTIVE_SESSION_META_KEY: &str = "active_session_pointer";

impl EpisodeLog {
    /// `EpisodeManager.end_session`: close the first open root with the key
    /// and every still-open episode below it at one instant; current
    /// clears. The closed root's index.
    fn end_session_root(&mut self, key: &str) -> Option<usize> {
        let root = self.episodes.iter().position(|e| {
            e.ended_at.is_none() && e.parent_id.is_none() && e.session_key.as_deref() == Some(key)
        })?;
        let root_id = self.episodes[root].id.clone();
        let now = crate::episodes::now();
        for i in 0..self.episodes.len() {
            if self.episodes[i].ended_at.is_none() && self.descends_from(i, &root_id) {
                self.episodes[i].ended_at = Some(now);
            }
        }
        self.current_id = None;
        Some(root)
    }
}

/// `_episode_entry_counts`: resident entries per episode, digests excluded.
fn entry_counts(ready: &Ready) -> HashMap<String, usize> {
    let bank = ready.bank.read().expect("bank lock");
    let mut counts = HashMap::new();
    for e in &bank.entries {
        if let Some(id) = e.episode_id.as_deref().filter(|id| !id.is_empty())
            && e.source != "digest"
        {
            *counts.entry(id.to_string()).or_insert(0) += 1;
        }
    }
    counts
}

/// `_end_client_session_locked`: stamp the close on the key's registration
/// row (no row, no write), naming the closed root when there is one.
async fn end_client_session(ready: &Ready, key: &str, root: Option<(&str, Option<f64>)>) {
    if crate::mutants::active("session-end-no-client-stamp") {
        return;
    }
    let ids = json!(root.map(|(id, _)| vec![id]).unwrap_or_default());
    let ended = root
        .and_then(|(_, ended)| ended)
        .filter(|&t| t != 0.0)
        .unwrap_or_else(crate::episodes::now);
    let reason = "end";
    let res = crate::txn::run(ready.storage.client(), |c| async move {
        c.execute(
            "UPDATE client_sessions SET ended_at = $1, end_reason = $2, \
             episode_ids = CASE WHEN episode_ids @> $3 THEN episode_ids \
             ELSE episode_ids || $3 END WHERE session_key = $4",
            &[&ended, &reason, &ids, &key],
        )
        .await
        .map(|_| ())
    })
    .await;
    if let Err(e) = res {
        eprintln!("client-session end stamp failed: {e}");
    }
}

/// `_auto_title_locked`: a root still carrying the generic lazy-open title
/// takes a content title from its subtree's entries.
async fn auto_title(
    ready: &Arc<Ready>,
    ws: &mut WriteState,
    root: usize,
    subtree: &HashSet<String>,
    band_order: &[String],
) {
    if !crate::episodes::is_generic_title(&ws.log.episodes[root].title) {
        return;
    }
    let title = {
        let bank = ready.bank.read().expect("bank lock");
        // Python walks band by band, so the title's first-seen and
        // first-minimum tie-breaks follow the configured band order.
        let rank = |band: &str| {
            band_order
                .iter()
                .position(|b| b == band)
                .unwrap_or(usize::MAX)
        };
        let mut held: Vec<&crate::bank::Entry> = bank
            .entries
            .iter()
            .filter(|e| e.episode_id.as_ref().is_some_and(|id| subtree.contains(id)))
            .collect();
        held.sort_by_key(|e| rank(&e.band));
        let entries: Vec<(f64, &str, &str)> = held
            .iter()
            .map(|e| (e.ts, e.source.as_str(), e.text.as_str()))
            .collect();
        crate::session_title::derive_session_title(ws.log.episodes[root].started_at, &entries)
    };
    if let Some(title) = title {
        crate::episode_tools::retitle(ready, ws, root, &title).await;
    }
}

/// `episode_end_session(key)` for an explicit key: close the key's open
/// root and its subtree. An empty subtree is deleted, answering `{}`; else
/// the closed root's dict, and whether the session-end dream should fire.
/// A key with nothing open still stamps its registration row.
pub async fn episode_end_session(
    ready: &Arc<Ready>,
    key: &str,
    run_dream: bool,
    band_order: &[String],
) -> (Value, bool) {
    let mut ws = ready.write.lock().await;
    let Some(root) = ws.log.end_session_root(key) else {
        end_client_session(ready, key, None).await;
        return (json!({}), false);
    };
    let (root_id, root_key, root_ended) = {
        let ep = &ws.log.episodes[root];
        (ep.id.clone(), ep.session_key.clone(), ep.ended_at)
    };
    if let Some(k) = root_key.as_deref().filter(|k| !k.is_empty()) {
        end_client_session(ready, k, Some((&root_id, root_ended))).await;
    }
    let subtree: HashSet<String> = (0..ws.log.episodes.len())
        .filter(|&i| ws.log.descends_from(i, &root_id))
        .map(|i| ws.log.episodes[i].id.clone())
        .chain(std::iter::once(root_id.clone()))
        .collect();
    let counts = entry_counts(ready);
    let empty = subtree
        .iter()
        .map(|id| counts.get(id).copied().unwrap_or(0))
        .sum::<usize>()
        == 0;
    if empty && !crate::mutants::active("session-end-keeps-empty") {
        for id in &subtree {
            if let Some(i) = ws.log.index(id) {
                ws.log.episodes.remove(i);
            }
            if ws.log.current_id.as_deref() == Some(id.as_str()) {
                ws.log.current_id = None;
            }
            let res = crate::txn::run(ready.storage.client(), |c| async move {
                c.execute("DELETE FROM episodes WHERE id = $1", &[id])
                    .await
                    .map(|_| ())
            })
            .await;
            if let Err(e) = res {
                eprintln!("episode delete failed: {e}");
            }
        }
        return (json!({}), false);
    }
    if !empty {
        auto_title(ready, &mut ws, root, &subtree, band_order).await;
    }
    ws.persist_episodes(ready.storage.client()).await;
    let result = Value::Object(crate::episode_tools::episode_dict(&ws.log.episodes[root]));
    (result, fires(run_dream, true, empty))
}

/// `_close_session_locked`'s `fire`: a found root whose subtree captured
/// entries fires the session-end dream (an empty one was pruned instead).
fn fires(run_dream: bool, found: bool, empty: bool) -> bool {
    run_dream && found && !empty
}

/// `clear_active_session`: clear the active-session pointer only while it
/// still names `session_id`, so an old SessionEnd never erases a newer
/// SessionStart.
pub async fn clear_active_session(ready: &Arc<Ready>, session_id: &str) -> bool {
    let mut ws = ready.write.lock().await;
    let owned = ws.active_session.as_ref().is_some_and(|(id, _)| {
        id == session_id || crate::mutants::active("session-end-clears-foreign")
    });
    if !owned {
        return false;
    }
    ws.active_session = None;
    if let Err(e) = crate::txn::set_meta(
        ready.storage.client(),
        ACTIVE_SESSION_META_KEY,
        &Value::Null,
    )
    .await
    {
        eprintln!("session-end pointer clear failed: {e}");
    }
    true
}

/// `hook_session_end`: close the session's episode, fire the session-end
/// dream when it should, then clear the pointer it owns. Fail-open: the
/// caller always answers `{"ok": true}`.
pub async fn hook_session_end(app: &crate::http::App, session_id: &Value) {
    let ready = match app.service.ensure_init().await {
        Ok(ready) => ready,
        Err(e) => {
            eprintln!("session-end episode close failed: {e}");
            return;
        }
    };
    // Only a string can name a session; Python's lookups miss on anything
    // else and its registration stamp fails on the type, so no row changes.
    let Some(key) = session_id.as_str().map(str::to_string) else {
        return;
    };
    let service = app.service.clone();
    let band_order: Vec<String> = app
        .service
        .config
        .memory
        .band_specs
        .iter()
        .map(|b| b.name.clone())
        .collect();
    let r2 = ready.clone();
    let task = ready.writes.spawn(async move {
        // Python clears the pointer even when the close raised.
        let (r3, k2) = (r2.clone(), key.clone());
        let close =
            tokio::spawn(async move { episode_end_session(&r3, &k2, true, &band_order).await });
        match close.await {
            Ok((_, true)) => service.fire_session_dream(),
            Ok(_) => {}
            Err(e) => eprintln!("session-end episode close failed: {e}"),
        }
        clear_active_session(&r2, &key).await;
    });
    if let Err(e) = task.await {
        eprintln!("session-end task failed: {e}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_a_found_root_with_entries_fires_the_dream() {
        assert!(fires(true, true, false));
        assert!(!fires(true, true, true));
        assert!(!fires(true, false, false));
        assert!(!fires(false, true, false));
    }
}

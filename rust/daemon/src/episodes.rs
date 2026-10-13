//! Episodes on the write path (spec W2) and the caller's session (W0.3).
//!
//! The episode log is resident like Python's `EpisodeManager`
//! (`memory/episodes.py`) and written through on every change with the full
//! upsert sweep (`service._persist_episodes`). `WriteState` holds every other
//! piece of resident state the write path reads: the active-session pointer,
//! tombstones, deferred-empty roots, handle touches and the store's
//! coreference anchor. It lives behind `Ready.write`, which plays the part of
//! the Python service lock for writes.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};
use tokio_postgres::Client;

use crate::txn;

static SEQ: AtomicU64 = AtomicU64::new(1);

/// `MemoryEntry.seq`: `itertools.count(1)` shared by every entry the process
/// builds (`memory/titans_memory.py:23`).
pub fn next_seq() -> u64 {
    SEQ.fetch_add(1, Ordering::Relaxed)
}

/// `time.time()`.
pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// `time.strftime("session - %Y-%m-%d %H:%M")`, daemon-local time.
pub fn generic_title() -> String {
    chrono::Local::now()
        .format("session - %Y-%m-%d %H:%M")
        .to_string()
}

/// `GENERIC_TITLE_RE` (`session_title.py:23`): `^session - \d{4}-\d{2}-\d{2}
/// \d{2}:\d{2}$`, where Python's `$` also matches before one final `\n`.
/// Python's `\d` also takes non-ASCII decimal digits; producers write titles
/// with `strftime`, so only ASCII digits are matched here (declared).
pub fn is_generic_title(title: &str) -> bool {
    let t = title.strip_suffix('\n').unwrap_or(title);
    let Some(rest) = t.strip_prefix("session - ") else {
        return false;
    };
    let shape = "dddd-dd-dd dd:dd";
    rest.len() == shape.len()
        && shape.bytes().zip(rest.bytes()).all(|(s, c)| match s {
            b'd' => c.is_ascii_digit(),
            other => other == c,
        })
}

#[derive(Debug, Clone, PartialEq, serde::Deserialize)]
pub struct Episode {
    pub id: String,
    pub title: String,
    pub started_at: f64,
    pub ended_at: Option<f64>,
    pub hint: Option<String>,
    pub closed_by_new_start: bool,
    pub session_key: Option<String>,
    pub parent_id: Option<String>,
}

/// One episode owner shared by stores and background reaping.
pub use crate::background_sessions::Sessions as EpisodeLog;

impl EpisodeLog {
    pub fn index(&self, id: &str) -> Option<usize> {
        self.episodes.iter().position(|e| e.id == id)
    }

    pub fn get(&self, id: &str) -> Option<&Episode> {
        self.episodes.iter().find(|e| e.id == id)
    }

    /// `open_leaf_for`: the deepest open episode for the key. Python's
    /// `max(..., key=started_at)` keeps the first of equal maxima.
    pub fn open_leaf_for(&self, key: Option<&str>) -> Option<usize> {
        let key = key?;
        let open: Vec<usize> = (0..self.episodes.len())
            .filter(|&i| {
                let e = &self.episodes[i];
                e.ended_at.is_none() && e.session_key.as_deref() == Some(key)
            })
            .collect();
        if open.is_empty() {
            return None;
        }
        let parents: Vec<&str> = open
            .iter()
            .filter_map(|&i| self.episodes[i].parent_id.as_deref())
            .collect();
        let leaves: Vec<usize> = open
            .iter()
            .copied()
            .filter(|&i| !parents.contains(&self.episodes[i].id.as_str()))
            .collect();
        let candidates = if leaves.is_empty() { &open } else { &leaves };
        let mut best = candidates[0];
        for &i in &candidates[1..] {
            if self.episodes[i].started_at > self.episodes[best].started_at {
                best = i;
            }
        }
        Some(best)
    }

    /// `open_episode`: the global current leaf when it is still open.
    pub fn open_episode(&self) -> Option<usize> {
        let i = self.index(self.current_id.as_deref()?)?;
        self.episodes[i].ended_at.is_none().then_some(i)
    }

    /// `start_session` with a key whose open leaf is known absent.
    pub fn start_session(&mut self, title: String, key: Option<String>) -> usize {
        if key.is_some()
            && let Some(i) = self.open_leaf_for(key.as_deref())
        {
            return i;
        }
        let id = uuid::Uuid::new_v4().simple().to_string();
        self.episodes.push(Episode {
            id: id.clone(),
            title,
            started_at: now(),
            ended_at: None,
            hint: None,
            closed_by_new_start: false,
            session_key: key,
            parent_id: None,
        });
        self.current_id = Some(id);
        self.episodes.len() - 1
    }

    /// `stamp`: the episode an entry stored under `key` lands on.
    pub fn stamp_target(&self, key: Option<&str>) -> Option<usize> {
        match key {
            Some(k) => self.open_leaf_for(Some(k)),
            None => self.open_episode(),
        }
    }

    /// `_session_root_locked`: the first open root with the key.
    pub fn session_root(&self, key: Option<&str>) -> Option<usize> {
        let key = key.filter(|k| !k.is_empty())?;
        self.episodes.iter().position(|e| {
            e.session_key.as_deref() == Some(key) && e.parent_id.is_none() && e.ended_at.is_none()
        })
    }
}

/// Who is calling: the headers the shim and hooks send (`writer_context.py`).
#[derive(Debug, Clone, Default)]
pub struct Caller {
    /// `X-PL-Session`, raw (tier 1).
    pub header_session: Option<String>,
    /// `mcp-session-id` when `PSEUDOLIFE_LEGACY_TRANSPORT_SESSION` allows it.
    pub transport_session: Option<String>,
}

/// The write path's resident state (Python keeps it on `MemoryService`).
#[derive(Debug, Default)]
pub struct WriteState {
    pub log: EpisodeLog,
    /// `_active_session`: `(session_id, ts)`.
    pub active_session: Option<(String, f64)>,
    /// `ContinuumMemorySystem._last_entity_seen` (never persisted).
    pub last_entity_seen: Option<String>,
    /// `_interaction_count` (persisted only in `weights.pt`).
    pub interaction_count: u64,
    /// `_surprise_history` per band, last 100 (persisted only in `weights.pt`).
    pub surprise_history: HashMap<String, Vec<f64>>,
}

fn env_f64(name: &str, default: f64) -> f64 {
    std::env::var(name)
        .ok()
        .and_then(|v| v.trim().parse::<f64>().ok())
        .unwrap_or(default)
}

fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

fn py_float(v: Option<&Value>) -> f64 {
    v.and_then(Value::as_f64).unwrap_or(0.0)
}

/// Build write state from the complete startup candidate before publishing Ready.
pub fn from_startup(startup: &crate::startup::StartupState) -> anyhow::Result<WriteState> {
    let mut ws = WriteState {
        log: EpisodeLog {
            episodes: startup
                .episodes
                .iter()
                .cloned()
                .map(serde_json::from_value)
                .collect::<Result<_, _>>()?,
            current_id: startup.current_episode.clone(),
            ..Default::default()
        },
        ..Default::default()
    };
    if let Some(raw) = startup.metadata["active_session"].as_array() {
        ws.active_session = Some((py_str(&raw[0]), py_float(raw.get(1))));
    }
    if let Some(raw) = startup.metadata["episode_tombstones"].as_object() {
        for (id, value) in raw {
            ws.log.tombstones.insert(
                id.clone(),
                (py_str(&value[0]), py_float(value.get(1)), py_str(&value[2])),
            );
        }
    }
    if let Some(raw) = startup.metadata["deferred_empty_roots"].as_object() {
        ws.log.deferred = raw
            .iter()
            .map(|(id, ts)| (id.clone(), py_float(Some(ts))))
            .collect();
    }
    Ok(ws)
}

impl WriteState {
    /// `_active_session_id` (`service.py:983-1011`).
    pub fn active_session_id(&self) -> Option<String> {
        let (id, ts) = self.active_session.as_ref()?;
        let ttl = env_f64("PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS", 21600.0);
        if ttl > 0.0 && now() - ts > ttl {
            return None;
        }
        Some(id.clone())
    }

    /// `_resolve_writer`'s session (spec W0.3).
    pub fn resolve_session(&self, caller: &Caller) -> Option<String> {
        if let Some(s) = caller.header_session.as_ref().filter(|s| !s.is_empty()) {
            return Some(s.clone());
        }
        if let Some(a) = self.active_session_id() {
            return Some(a);
        }
        caller.transport_session.clone().filter(|s| !s.is_empty())
    }

    /// `_persist_episodes` (spec W2.4): every episode, one transaction each;
    /// a failure is logged and ends the sweep.
    pub async fn persist_episodes(&self, client: &Client) {
        for ep in &self.log.episodes {
            let res = txn::run(client, |c| async move {
                c.execute(
                    "INSERT INTO episodes (id, title, hint, started_at, ended_at, \
                     closed_by_new_start, session_key, parent_id) \
                     VALUES ($1, $2, $3, $4, $5, $6, $7, $8) \
                     ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, \
                     hint = EXCLUDED.hint, started_at = EXCLUDED.started_at, \
                     ended_at = EXCLUDED.ended_at, \
                     closed_by_new_start = EXCLUDED.closed_by_new_start, \
                     session_key = EXCLUDED.session_key, parent_id = EXCLUDED.parent_id",
                    &[
                        &ep.id,
                        &ep.title,
                        &ep.hint,
                        &ep.started_at,
                        &ep.ended_at,
                        &ep.closed_by_new_start,
                        &ep.session_key,
                        &ep.parent_id,
                    ],
                )
                .await
                .map(|_| ())
            })
            .await;
            if let Err(e) = res {
                eprintln!("episode write-through failed: {e}");
                return;
            }
        }
    }

    /// `_reopen_client_session_locked`: best effort.
    async fn reopen_client_session(client: &Client, key: Option<&str>) {
        let Some(key) = key.filter(|k| !k.is_empty()) else {
            return;
        };
        let res = txn::run(client, |c| async move {
            c.execute(
                "UPDATE client_sessions SET ended_at = NULL, end_reason = NULL \
                 WHERE session_key = $1 AND ended_at IS NOT NULL",
                &[&key],
            )
            .await
            .map(|_| ())
        })
        .await;
        if let Err(e) = res {
            eprintln!("client-session reopen failed: {e}");
        }
    }

    /// `_persist_tombstones`: best effort.
    async fn persist_tombstones(&self, client: &Client) {
        let mut map = serde_json::Map::new();
        for (tid, (key, ended, title)) in &self.log.tombstones {
            map.insert(
                tid.clone(),
                json!({"session_key": key, "ended_at": ended, "title": title}),
            );
        }
        if let Err(e) = txn::set_meta(client, "episode_tombstones", &Value::Object(map)).await {
            eprintln!("episode tombstone write-through failed: {e}");
        }
    }

    /// `_resume_closed_session_locked` (spec W2.2).
    async fn resume_closed_session(&mut self, client: &Client, key: &str) -> Option<usize> {
        let window = env_f64("PSEUDOLIFE_SESSION_RESUME_SECONDS", 21600.0);
        if window <= 0.0 {
            return None;
        }
        // `max(closed, key=ended_at)`: the first of equal maxima.
        let mut last: Option<usize> = None;
        for (i, e) in self.log.episodes.iter().enumerate() {
            if e.session_key.as_deref() == Some(key)
                && e.parent_id.is_none()
                && let Some(ended) = e.ended_at
                && last.is_none_or(|l| ended > self.log.episodes[l].ended_at.unwrap_or(0.0))
            {
                last = Some(i);
            }
        }
        let i = last?;
        if now() - self.log.episodes[i].ended_at.unwrap_or(0.0) > window {
            return None;
        }
        let ep = &mut self.log.episodes[i];
        ep.ended_at = None;
        ep.closed_by_new_start = false;
        let id = ep.id.clone();
        self.log.current_id = Some(id.clone());
        self.log.touches.insert(id, now());
        self.persist_episodes(client).await;
        let key = self.log.episodes[i].session_key.clone();
        Self::reopen_client_session(client, key.as_deref()).await;
        Some(i)
    }

    /// `_ensure_session_episode` (spec W2.2).
    pub async fn ensure_session_episode(&mut self, client: &Client, key: Option<&str>) {
        let Some(key) = key.filter(|k| !k.is_empty()) else {
            return;
        };
        if self.log.open_leaf_for(Some(key)).is_some() {
            return;
        }
        if self.resume_closed_session(client, key).await.is_some() {
            return;
        }
        self.log
            .start_session(generic_title(), Some(key.to_string()));
        self.persist_episodes(client).await;
    }

    /// `_handle_resume_window`: a non-number falls back to 30 days.
    fn handle_resume_window() -> f64 {
        env_f64("PSEUDOLIFE_HANDLE_RESUME_SECONDS", 2_592_000.0)
    }

    /// `_resolve_episode_handle` with `resume=True` (spec W2.3): the root id
    /// and its session key, or None on any miss.
    pub async fn resolve_handle(
        &mut self,
        client: &Client,
        handle: Option<&str>,
    ) -> Option<(String, Option<String>)> {
        let handle = handle.filter(|h| h.chars().count() >= 8)?;
        let open: Vec<usize> = (0..self.log.episodes.len())
            .filter(|&i| {
                let e = &self.log.episodes[i];
                e.parent_id.is_none() && e.ended_at.is_none() && e.id.starts_with(handle)
            })
            .collect();
        if open.len() == 1 {
            let e = &self.log.episodes[open[0]];
            e.session_key.as_ref()?;
            let found = (e.id.clone(), e.session_key.clone());
            self.log.touches.insert(found.0.clone(), now());
            return Some(found);
        }
        if open.len() > 1 {
            return None;
        }
        let window = Self::handle_resume_window();
        if window <= 0.0 {
            return None;
        }
        let t = now();
        let closed: Vec<usize> = (0..self.log.episodes.len())
            .filter(|&i| {
                let e = &self.log.episodes[i];
                e.parent_id.is_none()
                    && e.ended_at.is_some()
                    && e.session_key.as_deref().is_some_and(|k| !k.is_empty())
                    && e.id.starts_with(handle)
            })
            .collect();
        if closed.len() > 1 {
            return None;
        }
        if let [i] = closed[..] {
            if t - self.log.episodes[i].ended_at.unwrap_or(0.0) > window {
                return None;
            }
            let ep = &mut self.log.episodes[i];
            ep.ended_at = None;
            ep.closed_by_new_start = false;
            let found = (ep.id.clone(), ep.session_key.clone());
            self.log.touches.insert(found.0.clone(), t);
            self.persist_episodes(client).await;
            Self::reopen_client_session(client, found.1.as_deref()).await;
            return Some(found);
        }
        let tomb: Vec<usize> = (0..self.log.tombstones.len())
            .filter(|&i| {
                self.log
                    .tombstones
                    .get_index(i)
                    .unwrap()
                    .0
                    .starts_with(handle)
            })
            .collect();
        let [ti] = tomb[..] else {
            return None;
        };
        let (tid, (key, ended_at, title)) = self.log.tombstones.get_index(ti)?;
        let (tid, key, ended_at, title) = (tid.clone(), key.clone(), *ended_at, title.clone());
        if t - ended_at > window {
            return None;
        }
        self.log.episodes.push(Episode {
            id: tid.clone(),
            title: if title.is_empty() {
                generic_title()
            } else {
                title
            },
            started_at: t,
            ended_at: None,
            hint: None,
            closed_by_new_start: false,
            session_key: Some(key.clone()),
            parent_id: None,
        });
        self.log.tombstones.shift_remove_index(ti);
        self.persist_tombstones(client).await;
        self.log.touches.insert(tid.clone(), t);
        self.persist_episodes(client).await;
        Self::reopen_client_session(client, Some(&key)).await;
        Some((tid, Some(key)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ep(
        id: &str,
        key: Option<&str>,
        parent: Option<&str>,
        started: f64,
        ended: Option<f64>,
    ) -> Episode {
        Episode {
            id: id.into(),
            title: "t".into(),
            started_at: started,
            ended_at: ended,
            hint: None,
            closed_by_new_start: false,
            session_key: key.map(String::from),
            parent_id: parent.map(String::from),
        }
    }

    #[test]
    fn open_leaf_prefers_the_deepest_then_the_first_latest() {
        let mut log = EpisodeLog {
            episodes: vec![
                ep("root", Some("k"), None, 1.0, None),
                ep("child", Some("k"), Some("root"), 2.0, None),
                ep("other", Some("j"), None, 3.0, None),
            ],
            current_id: None,
            ..Default::default()
        };
        assert_eq!(log.open_leaf_for(Some("k")), Some(1));
        log.episodes[1].ended_at = Some(4.0);
        assert_eq!(log.open_leaf_for(Some("k")), Some(0));
        // Two open leaves with one start time: the first wins, as max() does.
        log.episodes = vec![
            ep("a", Some("k"), None, 5.0, None),
            ep("b", Some("k"), None, 5.0, None),
        ];
        assert_eq!(log.open_leaf_for(Some("k")), Some(0));
        assert_eq!(log.open_leaf_for(None), None);
    }

    #[test]
    fn store_and_reaper_share_roots_touches_and_tombstones() {
        let mut ws = WriteState::default();
        let i = ws.log.start_session("fixture".into(), Some("key".into()));
        let id = ws.log.episodes[i].id.clone();
        ws.log.episodes[i].started_at = 1.0;
        ws.log.touches.insert(id.clone(), 95.0);
        assert!(
            ws.log
                .reap(10.0, 100.0, 100.0, 20.0, 1000.0, &[])
                .closed
                .is_empty()
        );
        let closed = ws.log.reap(10.0, 105.0, 105.0, 20.0, 1000.0, &[]);
        assert_eq!(closed.closed, vec![id.clone()]);
        assert_eq!(ws.log.stamp_target(Some("key")), None);
        assert_eq!(ws.log.current_id, None);
        assert_eq!(ws.log.deferred[&id], 105.0);
        let swept = ws.log.reap(10.0, 126.0, 126.0, 20.0, 1000.0, &[]);
        assert_eq!(swept.deleted, vec![id.clone()]);
        assert_eq!(ws.log.get(&id), None);
        assert_eq!(
            ws.log.tombstones[&id],
            ("key".into(), 105.0, "fixture".into())
        );
    }

    #[test]
    fn generic_title_shape() {
        assert!(is_generic_title("session - 2026-10-09 15:04"));
        assert!(is_generic_title("session - 2026-10-09 15:04\n"));
        assert!(!is_generic_title("session - 2026-10-09 15:04\n\n"));
        assert!(!is_generic_title("session"));
        assert!(!is_generic_title("Session - 2026-10-09 15:04"));
        assert!(!is_generic_title("session - 2026-10-09 15:4"));
        assert!(is_generic_title(&generic_title()));
    }
}

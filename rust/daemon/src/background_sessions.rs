//! Session reaping and deferred-empty retention (`service.py:6469-6714`).

use indexmap::IndexMap;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use tokio_postgres::Client;

/// The dream slice owns fire-and-forget execution; reaping never holds
/// the service lock while asking it to launch a cycle.
pub trait SessionDream: Send + Sync {
    fn fire(&self);
}

pub struct DeferredSessionDream;
impl SessionDream for DeferredSessionDream {
    fn fire(&self) {
        eprintln!("session-end dream trigger: deferred (W3-H)");
    }
}

#[derive(Clone, Debug)]
pub struct Episode {
    pub id: String,
    pub title: String,
    pub hint: Option<String>,
    pub started_at: f64,
    pub ended_at: Option<f64>,
    pub closed_by_new_start: bool,
    pub session_key: Option<String>,
    pub parent_id: Option<String>,
}

#[derive(Default)]
pub struct Sessions {
    pub episodes: Vec<Episode>,
    pub touches: HashMap<String, f64>,
    pub deferred: IndexMap<String, f64>,
    pub tombstones: IndexMap<String, (String, f64, String)>,
}

#[derive(Default, Debug)]
pub struct Reaped {
    pub failure: Option<String>,
    pub session_keys: Vec<String>,
    pub swept: usize,
    pub fire_dream: bool,
    pub closed: Vec<String>,
    pub ended_sessions: Vec<(String, String, f64)>,
    pub deleted: Vec<String>,
    pub deferred_changed: bool,
    pub tombstones_changed: bool,
}

impl Sessions {
    pub async fn hydrate(client: &Client) -> Result<Self, String> {
        let mut sessions = Self::default();
        for r in client.query("SELECT id,title,hint,started_at,ended_at,closed_by_new_start,session_key,parent_id FROM episodes ORDER BY started_at", &[]).await.map_err(|e| e.to_string())? {
            sessions.episodes.push(Episode { id: r.get(0), title: r.get(1), hint: r.get(2),
                started_at: r.get(3), ended_at: r.get(4), closed_by_new_start: r.get(5),
                session_key: r.get(6), parent_id: r.get(7) });
        }
        for r in client
            .query(
                "SELECT key,value FROM meta WHERE key = ANY($1)",
                &[&vec!["episode_tombstones", "deferred_empty_roots"]],
            )
            .await
            .map_err(|e| e.to_string())?
        {
            let key: String = r.get(0);
            let value: Value = r.get(1);
            if let Some(map) = value.as_object() {
                for (id, v) in map {
                    if key == "episode_tombstones" {
                        if let Some(key) = v["session_key"].as_str().filter(|k| !k.is_empty()) {
                            sessions.tombstones.insert(
                                id.clone(),
                                (
                                    key.into(),
                                    v["ended_at"].as_f64().unwrap_or(0.0),
                                    v["title"].as_str().unwrap_or("").into(),
                                ),
                            );
                        }
                    } else {
                        sessions
                            .deferred
                            .insert(id.clone(), v.as_f64().unwrap_or(0.0));
                    }
                }
            }
        }
        Ok(sessions)
    }

    /// Best-effort write-through, like Python's episode helpers. Each
    /// statement commits separately; callers hold the session state lock.
    pub async fn persist_reap(&self, client: &Client, result: &Reaped) {
        for (key, id, ended) in &result.ended_sessions {
            let ids = json!([id]);
            if let Err(e) = client.execute("UPDATE client_sessions SET ended_at=$1,end_reason='idle',episode_ids=CASE WHEN episode_ids @> $2 THEN episode_ids ELSE episode_ids || $2 END WHERE session_key=$3", &[&ended, &ids, key]).await {
                    eprintln!("client-session end stamp failed: {e}");
                }
        }
        if !result.session_keys.is_empty() {
            for ep in &self.episodes {
                if let Err(e) = client.execute("INSERT INTO episodes (id,title,hint,started_at,ended_at,closed_by_new_start,session_key,parent_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title,hint=EXCLUDED.hint,started_at=EXCLUDED.started_at,ended_at=EXCLUDED.ended_at,closed_by_new_start=EXCLUDED.closed_by_new_start,session_key=EXCLUDED.session_key,parent_id=EXCLUDED.parent_id",
                    &[&ep.id,&ep.title,&ep.hint,&ep.started_at,&ep.ended_at,&ep.closed_by_new_start,&ep.session_key,&ep.parent_id]).await {
                    eprintln!("episode write-through failed: {e}"); break;
                }
            }
        }
        for id in &result.deleted {
            if let Err(e) = client
                .execute("DELETE FROM episodes WHERE id=$1", &[id])
                .await
            {
                eprintln!("episode delete failed: {e}");
            }
        }
        if result.deferred_changed {
            let mut map = serde_json::Map::new();
            for (id, ended) in &self.deferred {
                map.insert(id.clone(), json!(ended));
            }
            Self::persist_meta(client, "deferred_empty_roots", Value::Object(map)).await;
        }
        if result.tombstones_changed {
            let mut map = serde_json::Map::new();
            for (id, (key, ended, title)) in &self.tombstones {
                map.insert(
                    id.clone(),
                    json!({"session_key":key,"ended_at":ended,"title":title}),
                );
            }
            Self::persist_meta(client, "episode_tombstones", Value::Object(map)).await;
        }
    }

    async fn persist_meta(client: &Client, key: &str, value: Value) {
        if let Err(e) = client.execute("INSERT INTO meta(key,value) VALUES ($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", &[&key,&value]).await { eprintln!("{key} write-through failed: {e}"); }
    }
    fn subtree(parents: &HashMap<String, Option<String>>, root: &str) -> HashSet<String> {
        parents
            .keys()
            .filter(|id| {
                let mut current = Some(id.as_str());
                let mut seen = HashSet::new();
                while let Some(id) = current {
                    if id == root {
                        return true;
                    }
                    if !seen.insert(id) {
                        break;
                    }
                    current = parents.get(id).and_then(|parent| parent.as_deref());
                }
                false
            })
            .cloned()
            .collect()
    }

    #[cfg(test)]
    pub fn reap(
        &mut self,
        idle: f64,
        now: f64,
        close_at: f64,
        resume: f64,
        handle_resume: f64,
        entries: &[(String, f64, String)],
    ) -> Reaped {
        self.reap_with_clock(idle, now, resume, handle_resume, entries, || close_at)
    }

    #[cfg(test)]
    pub fn reap_with_clock(
        &mut self,
        idle: f64,
        now: f64,
        resume: f64,
        handle_resume: f64,
        entries: &[(String, f64, String)],
        close_clock: impl FnMut() -> f64,
    ) -> Reaped {
        self.reap_with_settings(
            idle,
            now,
            entries,
            || Ok((resume, handle_resume)),
            close_clock,
        )
    }

    pub fn reap_with_settings(
        &mut self,
        idle: f64,
        now: f64,
        entries: &[(String, f64, String)],
        settings: impl FnOnce() -> Result<(f64, f64), String>,
        mut close_clock: impl FnMut() -> f64,
    ) -> Reaped {
        let mut out = Reaped::default();
        let parents: HashMap<_, _> = self
            .episodes
            .iter()
            .map(|episode| (episode.id.clone(), episode.parent_id.clone()))
            .collect();
        let targets: Vec<_> = self
            .episodes
            .iter()
            .filter(|root| {
                if root.ended_at.is_some()
                    || root.parent_id.is_some()
                    || root.session_key.as_ref().is_none_or(|k| k.is_empty())
                {
                    return false;
                }
                let subtree = Self::subtree(&parents, &root.id);
                let root_ts = entries
                    .iter()
                    .filter(|(id, _, _)| id == &root.id)
                    .map(|(_, ts, _)| *ts)
                    .filter(|ts| *ts > 0.0)
                    .reduce(f64::max)
                    .unwrap_or(root.started_at);
                let mut activity = root_ts.max(*self.touches.get(&root.id).unwrap_or(&0.0));
                for (id, ts, _) in entries {
                    if subtree.contains(id) && *ts > 0.0 {
                        activity = activity.max(*ts);
                    }
                }
                if crate::mutants::active("session-idle-exclusive") {
                    now - activity > idle
                } else {
                    now - activity >= idle
                }
            })
            .map(|root| root.session_key.clone().expect("key checked"))
            .collect();
        for key in targets {
            // Resolve by key again: Python end_session closes the first open
            // root, even when two roots have the same connection key.
            let Some(root_id) = self
                .episodes
                .iter()
                .find(|e| {
                    e.parent_id.is_none()
                        && e.ended_at.is_none()
                        && e.session_key.as_ref() == Some(&key)
                })
                .map(|e| e.id.clone())
            else {
                out.session_keys.push(key);
                continue;
            };
            let close_at = close_clock();
            let subtree = Self::subtree(&parents, &root_id);
            out.ended_sessions
                .push((key.clone(), root_id.clone(), close_at));
            for ep in &mut self.episodes {
                if subtree.contains(&ep.id) && ep.ended_at.is_none() {
                    ep.ended_at = Some(close_at);
                    out.closed.push(ep.id.clone());
                }
            }
            if entries
                .iter()
                .any(|(id, _, source)| subtree.contains(id) && source != "digest")
            {
                out.fire_dream = true;
            } else {
                self.deferred.insert(root_id, close_at);
                out.deferred_changed = true;
            }
            out.session_keys.push(key);
        }
        // Python parses the resume window after closing and persisting roots.
        // A malformed value must leave those closes intact but skip the sweep
        // and the batch dream trigger.
        let (resume, handle_resume) = match settings() {
            Ok(settings) => settings,
            Err(error) => {
                out.failure = Some(error);
                out.fire_dream = false;
                return out;
            }
        };
        let deferred: Vec<_> = self.deferred.keys().cloned().collect();
        for id in deferred {
            let Some(root) = self.episodes.iter().find(|e| e.id == id) else {
                self.deferred.shift_remove(&id);
                out.deferred_changed = true;
                continue;
            };
            let Some(ended) = root.ended_at else {
                self.deferred.shift_remove(&id);
                out.deferred_changed = true;
                continue;
            };
            if now - ended <= resume {
                continue;
            }
            let subtree = Self::subtree(&parents, &id);
            if entries
                .iter()
                .any(|(id, _, source)| subtree.contains(id) && source != "digest")
            {
                self.deferred.shift_remove(&id);
                out.deferred_changed = true;
                continue;
            }
            if let Some(key) = root.session_key.as_ref().filter(|k| !k.is_empty()) {
                self.tombstones
                    .insert(id.clone(), (key.clone(), ended, root.title.clone()));
            }
            self.episodes.retain(|ep| {
                if subtree.contains(&ep.id) {
                    out.deleted.push(ep.id.clone());
                    false
                } else {
                    true
                }
            });
            self.deferred.shift_remove(&id);
            out.deferred_changed = true;
            out.swept += 1;
        }
        if out.swept > 0 {
            self.tombstones.retain(|_, (_, ended, _)| {
                !(handle_resume <= 0.0
                    || now - *ended > handle_resume
                    || (crate::mutants::active("session-handle-exclusive")
                        && now - *ended == handle_resume))
            });
            if self.tombstones.len() > 200 {
                let mut newest: Vec<_> = self
                    .tombstones
                    .iter()
                    .map(|(id, (_, ended, _))| (id.clone(), *ended))
                    .collect();
                newest.sort_by(|a, b| b.1.total_cmp(&a.1));
                let keep: HashSet<_> = newest.into_iter().take(200).map(|(id, _)| id).collect();
                self.tombstones.retain(|id, _| keep.contains(id));
            }
            out.tombstones_changed = true;
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn malformed_resume_keeps_closes_but_skips_sweep_and_dream() {
        let mut s = Sessions {
            episodes: vec![
                ep("empty", "a", None, None),
                ep("populated", "b", None, None),
            ],
            ..Default::default()
        };
        let r = s.reap_with_settings(
            0.0,
            100.0,
            &[("populated".into(), 20.0, "project".into())],
            || crate::background::seconds(Some("bad"), 21600.0).map(|v| (v, 1000.0)),
            || 100.0,
        );
        assert!(r.failure.is_some());
        assert_eq!(r.session_keys, ["a", "b"]);
        assert_eq!(r.ended_sessions.len(), 2);
        assert!(r.deferred_changed);
        assert_eq!(s.deferred["empty"], 100.0);
        assert!(s.episodes.iter().all(|e| e.ended_at == Some(100.0)));
        assert_eq!(r.swept, 0);
        assert!(!r.fire_dream);
    }

    #[test]
    fn idle_and_handle_expiry_include_exact_threshold() {
        let mut s = Sessions {
            episodes: vec![ep("root", "a", None, None)],
            ..Default::default()
        };
        assert!(
            s.reap(30.0, 39.0, 39.0, 60.0, 1000.0, &[])
                .session_keys
                .is_empty()
        );
        assert_eq!(
            s.reap(30.0, 40.0, 40.0, 60.0, 1000.0, &[]).session_keys,
            ["a"]
        );
        s.tombstones
            .insert("old".into(), ("old-key".into(), 11.0, "old-title".into()));
        assert_eq!(s.reap(30.0, 101.0, 101.0, 60.0, 90.0, &[]).swept, 1);
        assert!(s.tombstones.contains_key("old"));
        s.episodes.push(ep("new", "b", None, Some(41.0)));
        s.deferred.insert("new".into(), 41.0);
        assert_eq!(s.reap(30.0, 102.0, 102.0, 60.0, 90.0, &[]).swept, 1);
        assert!(!s.tombstones.contains_key("old"));
    }

    #[test]
    fn resume_settings_accept_numeric_underscores_and_nan() {
        assert_eq!(
            crate::background::seconds(Some(" 1_0 "), 0.0).unwrap(),
            10.0
        );
        let nan = crate::background::seconds(Some("nan"), 0.0).unwrap();
        assert!(nan.is_nan());
        let mut s = Sessions {
            episodes: vec![ep("root", "a", None, Some(10.0))],
            ..Default::default()
        };
        s.deferred.insert("root".into(), 10.0);
        s.tombstones
            .insert("old".into(), ("key".into(), 0.0, "title".into()));
        assert_eq!(s.reap(30.0, 100.0, 100.0, nan, nan, &[]).swept, 1);
        assert!(s.tombstones.contains_key("old"));
    }

    #[test]
    fn cap_retains_newest_closes_in_one_large_tick() {
        let mut s = Sessions {
            episodes: (0..250)
                .map(|i| ep(&format!("{i:08}"), &format!("key{i}"), None, None))
                .collect(),
            ..Default::default()
        };
        let mut clock = 0.0;
        let result = s.reap_with_clock(0.0, 1000.0, 0.0, 10000.0, &[], || {
            clock += 1.0;
            clock
        });
        assert_eq!(result.swept, 250);
        assert_eq!(s.tombstones.len(), 200);
        assert!(s.tombstones.contains_key("00000249"));
        assert!(!s.tombstones.contains_key("00000049"));
    }

    fn ep(id: &str, key: &str, parent: Option<&str>, ended: Option<f64>) -> Episode {
        Episode {
            id: id.into(),
            title: "named session".into(),
            hint: None,
            started_at: 10.0,
            ended_at: ended,
            closed_by_new_start: false,
            session_key: Some(key.into()),
            parent_id: parent.map(String::from),
        }
    }

    #[test]
    fn one_dream_per_tick_and_cascade_close() {
        let mut s = Sessions {
            episodes: vec![
                ep("root", "a", None, None),
                ep("child", "a", Some("root"), None),
                ep("other", "b", None, None),
            ],
            ..Default::default()
        };
        let r = s.reap(
            30.0,
            100.0,
            101.0,
            60.0,
            1000.0,
            &[
                ("child".into(), 20.0, "project".into()),
                ("other".into(), 20.0, "project".into()),
            ],
        );
        assert_eq!(r.session_keys, vec!["a", "b"]);
        assert!(r.fire_dream);
        assert!(s.episodes.iter().all(|e| e.ended_at == Some(101.0)));
        assert!(!s.reap(30.0, 110.0, 110.0, 60.0, 1000.0, &[]).fire_dream);
    }

    #[test]
    fn descendant_activity_and_handle_touch_keep_roots_open() {
        let mut s = Sessions {
            episodes: vec![
                ep("root", "a", None, None),
                ep("child", "a", Some("root"), None),
                ep("touched", "b", None, None),
            ],
            ..Default::default()
        };
        s.touches.insert("touched".into(), 95.0);
        let r = s.reap(
            30.0,
            100.0,
            100.0,
            60.0,
            1000.0,
            &[("child".into(), 90.0, "project".into())],
        );
        assert!(r.session_keys.is_empty());
        assert!(s.episodes.iter().all(|e| e.ended_at.is_none()));
    }

    #[test]
    fn empty_close_retains_then_sweeps_but_never_deletes_evicted_history() {
        let mut s = Sessions {
            episodes: vec![
                ep("empty", "a", None, None),
                ep("history", "b", None, Some(10.0)),
            ],
            ..Default::default()
        };
        let r = s.reap(30.0, 100.0, 100.0, 60.0, 1000.0, &[]);
        assert!(!r.fire_dream);
        assert_eq!(s.deferred.get("empty"), Some(&100.0));
        assert_eq!(s.reap(30.0, 160.0, 160.0, 60.0, 1000.0, &[]).swept, 0);
        assert_eq!(s.reap(30.0, 161.0, 161.0, 60.0, 1000.0, &[]).swept, 1);
        assert_eq!(
            s.tombstones["empty"],
            ("a".into(), 100.0, "named session".into())
        );
        assert_eq!(s.episodes.len(), 1);
        assert_eq!(s.episodes[0].id, "history");
    }

    #[test]
    fn resumed_or_populated_deferred_roots_are_preserved_and_digest_does_not_count() {
        let mut s = Sessions {
            episodes: vec![
                ep("resumed", "a", None, None),
                ep("populated", "b", None, Some(10.0)),
                ep("digest", "c", None, Some(10.0)),
            ],
            ..Default::default()
        };
        for id in ["resumed", "populated", "digest"] {
            s.deferred.insert(id.into(), 10.0);
        }
        s.touches.insert("resumed".into(), 100.0);
        let r = s.reap(
            30.0,
            100.0,
            100.0,
            60.0,
            1000.0,
            &[
                ("populated".into(), 20.0, "project".into()),
                ("digest".into(), 20.0, "digest".into()),
            ],
        );
        assert_eq!(r.deleted, vec!["digest"]);
        assert!(s.deferred.is_empty());
        assert_eq!(s.episodes.len(), 2);
    }
}

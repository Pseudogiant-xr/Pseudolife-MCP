//! The optional per-prompt conductor reminder (`coordination.py`).
//! A lookup may outlive the request, but neither its wait nor its cache is unbounded.

use super::{Board, error::Result, http};
use crate::http::App;
use axum::http::HeaderMap;
use indexmap::IndexMap;
use std::collections::HashMap;
use std::future::Future;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::sync::watch;

const REMINDER: &str = "Conductor: raw output goes to a subagent; a quiet wake gets one line.\n";
const TTL: Duration = Duration::from_secs(60);
const BUDGET: Duration = Duration::from_millis(500);
const CACHE_MAX: usize = 1024;
const INFLIGHT_MAX: usize = 256;
type Key = (String, String);
type Answer = (Instant, bool);

#[derive(Default)]
struct State {
    hits: IndexMap<Key, Answer>,
    inflight: HashMap<Key, watch::Receiver<Option<Answer>>>,
}

impl State {
    fn finish(
        &mut self,
        key: Key,
        result: Result<Option<f64>>,
        started: Instant,
        now: Instant,
    ) -> Answer {
        self.inflight.remove(&key);
        let Ok(remaining) = result else {
            return (now, false);
        };
        let ttl = remaining.map_or(TTL, |r| {
            Duration::from_secs_f64(r.clamp(0.0, TTL.as_secs_f64()))
        });
        let until = started + ttl;
        // A positive answer arriving after its lease expired must not reach
        // either the waiting requests or the cache.
        if remaining.is_some() && until <= now {
            return (now, false);
        }
        if self.hits.len() >= CACHE_MAX {
            self.hits.retain(|_, (until, _)| *until > started);
            while self.hits.len() >= CACHE_MAX {
                self.hits.shift_remove_index(0);
            }
        }
        let answer = (until, remaining.is_some());
        self.hits.insert(key, answer);
        answer
    }
}

#[derive(Default)]
pub(crate) struct Cache(Mutex<State>);

impl Cache {
    async fn lookup<F>(self: &Arc<Self>, key: Key, fetch: F) -> bool
    where
        F: Future<Output = Result<Option<f64>>> + Send + 'static,
    {
        let mut rx = {
            let mut state = self.0.lock().unwrap();
            if let Some(&(until, answer)) = state.hits.get(&key)
                && until > Instant::now()
            {
                return answer;
            }
            if let Some(rx) = state.inflight.get(&key) {
                rx.clone()
            } else {
                if state.inflight.len() >= INFLIGHT_MAX {
                    return false;
                }
                let (tx, rx) = watch::channel(None);
                state.inflight.insert(key.clone(), rx.clone());
                let cache = self.clone();
                tokio::spawn(async move {
                    // Count from before the query, including any wait for the board lock.
                    let started = Instant::now();
                    let result = fetch.await;
                    let mut state = cache.0.lock().unwrap();
                    let answer = state.finish(key, result, started, Instant::now());
                    tx.send_replace(Some(answer));
                });
                rx
            }
        };
        tokio::time::timeout(BUDGET, async {
            rx.wait_for(|answer| answer.is_some())
                .await
                .ok()
                .and_then(|answer| *answer)
                // The worker may publish before expiry but this waiter can
                // resume after it. Check the proof again on delivery.
                .is_some_and(|(until, granted)| granted && until > Instant::now())
        })
        .await
        .unwrap_or(false)
    }
}

/// No board I/O on a cold daemon, for malformed addresses, or for a caller
/// to whom the board is not served. A missing reminder never fails the hook.
pub(crate) async fn reminder(app: &App, h: &HeaderMap, agent: Option<&String>) -> &'static str {
    let headers = http::header_dict(h);
    if http::unavailable_reason(app, &headers).is_some() || app.service.snapshot().storage.is_none()
    {
        return "";
    }
    let Some(agent) = agent.filter(|a| {
        a.len() == 32
            && a.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    }) else {
        return "";
    };
    let Ok(principal) = http::authenticated_principal(app, &headers) else {
        return "";
    };
    let key = (principal, agent.clone());
    let lookup_key = key.clone();
    let board = app.board.clone();
    if app
        .board
        .conductor
        .lookup(key, async move {
            board
                .conductor_remaining(&lookup_key.0, &lookup_key.1)
                .await
        })
        .await
    {
        REMINDER
    } else {
        ""
    }
}

impl Board {
    /// `conductor_of`: only an owned credentialed agent with an unexpired
    /// operator/maintainer grant at the hold's current fence qualifies.
    async fn conductor_remaining(&self, principal: &str, agent: &str) -> Result<Option<f64>> {
        self.service
            .storage_tier()
            .await
            .map_err(super::error::BoardError::Internal)?;
        let mut conn = self.conn.lock().await;
        let c = conn.client().await?;
        if c.query_opt(
            "SELECT 1 FROM coordination_agents WHERE agent_id=$1 AND principal=$2 \
             AND credential_hash IS NOT NULL",
            &[&agent, &principal],
        )
        .await?
        .is_none()
        {
            return Ok(None);
        }
        let now = super::store::now();
        let events = vec!["lease_conductor".to_string(), "lease_delegate".to_string()];
        let actors = vec!["operator".to_string(), "maintainer".to_string()];
        let rows = c
            .query(
                "SELECT l.expires_at FROM coordination_leases l \
             WHERE (left(l.name,10)='conductor:' OR left(l.name,9)='delegate:') \
             AND l.holder_agent_id=$1 AND l.expires_at>$2 AND EXISTS \
             (SELECT 1 FROM coordination_events e WHERE e.event=ANY($3) \
             AND e.actor=ANY($4) AND e.agent_id=l.holder_agent_id \
             AND CASE WHEN e.event=ANY($3) THEN e.payload::jsonb->>'name'=l.name \
             AND (e.payload::jsonb->>'fence')::bigint=l.fence ELSE false END)",
                &[&agent, &now, &events, &actors],
            )
            .await?;
        Ok(rows
            .iter()
            .map(|r| r.get::<_, f64>(0) - now)
            .reduce(f64::min))
    }
}

#[cfg(test)]
mod tests {
    use super::super::error::BoardError;
    use super::*;
    use tokio::sync::oneshot;

    fn key(n: usize) -> Key {
        ("fixture".into(), format!("{n:032x}"))
    }

    #[test]
    fn cache_expiry_uses_query_start_and_earliest_lease() {
        let mut state = State::default();
        let start = Instant::now();
        assert!(state.finish(key(1), Ok(Some(3.0)), start, start).1);
        assert_eq!(state.hits[&key(1)], (start + Duration::from_secs(3), true));
        assert!(state.finish(key(2), Ok(Some(90.0)), start, start).1);
        assert_eq!(state.hits[&key(2)], (start + TTL, true));
        assert!(
            !state
                .finish(key(3), Ok(Some(3.0)), start, start + Duration::from_secs(3))
                .1
        );
        assert!(!state.hits.contains_key(&key(3)));
        assert!(!state.finish(key(4), Ok(None), start, start).1);
        assert_eq!(state.hits[&key(4)], (start + TTL, false));
    }

    #[test]
    fn cache_is_bounded_and_discards_expired_entries_before_oldest() {
        let mut state = State::default();
        let start = Instant::now();
        for n in 0..CACHE_MAX {
            state.finish(
                key(n),
                Ok(Some(if n == 5 { 1.0 } else { 60.0 })),
                start,
                start,
            );
        }
        let later = start + Duration::from_secs(2);
        state.finish(key(CACHE_MAX), Ok(None), later, later);
        assert_eq!(state.hits.len(), CACHE_MAX);
        assert!(state.hits.contains_key(&key(0)));
        assert!(!state.hits.contains_key(&key(5)));
        state.finish(key(CACHE_MAX + 1), Ok(None), later, later);
        assert_eq!(state.hits.len(), CACHE_MAX);
        assert!(!state.hits.contains_key(&key(0)));
    }

    #[tokio::test]
    async fn failures_retry_and_negative_answers_cache() {
        let cache = Arc::new(Cache::default());
        assert!(
            !cache
                .lookup(key(0), async {
                    Err(BoardError::Internal("fixture".into()))
                })
                .await
        );
        assert!(cache.lookup(key(0), async { Ok(Some(30.0)) }).await);
        assert!(
            cache
                .lookup(key(0), async { panic!("positive cache missed") })
                .await
        );
        assert!(!cache.lookup(key(1), async { Ok(None) }).await);
        assert!(
            !cache
                .lookup(key(1), async { panic!("negative cache missed") })
                .await
        );
        assert!(
            !cache
                .lookup(("other".into(), key(0).1), async { Ok(None) })
                .await
        );
    }

    #[tokio::test]
    async fn descheduled_waiter_rechecks_expiry_after_result_publication() {
        let cache = Arc::new(Cache::default());
        let (published_tx, published_rx) = oneshot::channel();
        let mut waiting = Box::pin(cache.lookup(key(0), async move {
            published_tx.send(()).unwrap();
            Ok(Some(0.05))
        }));
        // Admit the lookup and suspend this waiter before the worker publishes.
        std::future::poll_fn(|cx| {
            assert!(waiting.as_mut().poll(cx).is_pending());
            std::task::Poll::Ready(())
        })
        .await;
        published_rx.await.unwrap();
        let until = cache.0.lock().unwrap().hits[&key(0)].0;
        // The result has been published; model a descheduled hook without
        // allowing its future to consume the result until after expiry.
        std::thread::sleep(
            until.saturating_duration_since(Instant::now()) + Duration::from_millis(10),
        );
        assert!(!waiting.await);
    }

    #[tokio::test]
    async fn bounded_wait_keeps_one_lookup_for_concurrent_and_later_requests() {
        let cache = Arc::new(Cache::default());
        let (entered_tx, entered_rx) = oneshot::channel();
        let (release_tx, release_rx) = oneshot::channel();
        let worker_cache = cache.clone();
        let first = tokio::spawn(async move {
            worker_cache
                .lookup(key(0), async move {
                    entered_tx.send(()).unwrap();
                    release_rx.await.unwrap();
                    Ok(Some(30.0))
                })
                .await
        });
        entered_rx.await.unwrap();
        assert!(
            !cache
                .lookup(key(0), async { panic!("duplicated in-flight lookup") })
                .await
        );
        assert!(!first.await.unwrap());
        assert_eq!(cache.0.lock().unwrap().inflight.len(), 1);
        release_tx.send(()).unwrap();
        assert!(
            cache
                .lookup(key(0), async { panic!("timed-out lookup was lost") })
                .await
        );
        assert!(cache.0.lock().unwrap().inflight.is_empty());
    }

    #[tokio::test]
    async fn cancellation_keeps_lookup_and_capacity_declines_new_work() {
        let cache = Arc::new(Cache::default());
        let (entered_tx, entered_rx) = oneshot::channel();
        let (release_tx, release_rx) = oneshot::channel();
        let worker_cache = cache.clone();
        let waiter = tokio::spawn(async move {
            worker_cache
                .lookup(key(0), async move {
                    entered_tx.send(()).unwrap();
                    release_rx.await.unwrap();
                    Ok(Some(30.0))
                })
                .await
        });
        entered_rx.await.unwrap();
        waiter.abort();
        assert!(waiter.await.unwrap_err().is_cancelled());
        release_tx.send(()).unwrap();
        assert!(
            cache
                .lookup(key(0), async {
                    panic!("cancelled waiter cancelled lookup")
                })
                .await
        );
        let (tx, rx) = watch::channel(None);
        for n in 0..INFLIGHT_MAX {
            cache
                .0
                .lock()
                .unwrap()
                .inflight
                .insert(key(n + 1), rx.clone());
        }
        assert!(
            !cache
                .lookup(key(INFLIGHT_MAX + 1), async { panic!("capacity ignored") })
                .await
        );
        drop(tx);
    }
}

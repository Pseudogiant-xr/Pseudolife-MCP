//! `CoordinationHub` (spec BH1-BH3, `web/coordination.py`): bounded
//! receive waits. Every call runs as its own task, which owns its capacity
//! until the database work ends, so a disconnecting client never cancels a
//! write half-way and never frees capacity early. A wait subscribes before
//! each read, so a commit during the read still wakes it.

use super::Board;
use super::error::{BoardError, Result};
use serde_json::{Map, Value};
use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use tokio::sync::{Notify, Semaphore};

/// `MAX_WAITERS`, `MAX_WAIT_SECONDS`.
pub const MAX_WAITERS: usize = 64;
pub const MAX_WAIT_SECONDS: f64 = 30.0;
const WORKERS: usize = 16;

/// An `asyncio.Event`: set until cleared.
pub struct Event {
    flag: AtomicBool,
    notify: Notify,
}

impl Event {
    fn new() -> Event {
        Event {
            flag: AtomicBool::new(false),
            notify: Notify::new(),
        }
    }
    fn set(&self) {
        self.flag.store(true, Ordering::SeqCst);
        self.notify.notify_waiters();
    }
    fn clear(&self) {
        self.flag.store(false, Ordering::SeqCst);
    }
    async fn wait(&self) {
        loop {
            let notified = self.notify.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if self.flag.load(Ordering::SeqCst) {
                return;
            }
            notified.await;
        }
    }
}

#[derive(Default)]
struct Waiters {
    by_recipient: HashMap<String, Vec<Arc<Event>>>,
    attachments: HashSet<(String, String, i64)>,
}

pub struct Hub {
    waiters: Mutex<Waiters>,
    pending_calls: Arc<AtomicUsize>,
    workers: Arc<Semaphore>,
}

/// The spawned job owns admission until completion, cancellation, or panic.
struct PendingCall(Arc<AtomicUsize>);

impl Drop for PendingCall {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
    }
}

impl Default for Hub {
    fn default() -> Self {
        Hub {
            waiters: Mutex::new(Waiters::default()),
            pending_calls: Arc::new(AtomicUsize::new(0)),
            workers: Arc::new(Semaphore::new(WORKERS)),
        }
    }
}

/// Removes a wait's subscription however the wait ends (including a
/// dropped future when the client disconnects).
struct WaitGuard<'a> {
    hub: &'a Hub,
    recipient: String,
    attachment: (String, String, i64),
    event: Arc<Event>,
}

impl Drop for WaitGuard<'_> {
    fn drop(&mut self) {
        let mut w = self.hub.waiters.lock().expect("hub lock");
        w.attachments.remove(&self.attachment);
        if let Some(list) = w.by_recipient.get_mut(&self.recipient) {
            list.retain(|e| !Arc::ptr_eq(e, &self.event));
            if list.is_empty() {
                w.by_recipient.remove(&self.recipient);
            }
        }
    }
}

impl Hub {
    /// `notify`: wake every wait for `recipient`.
    pub fn notify(&self, recipient: &str) {
        let w = self.waiters.lock().expect("hub lock");
        if let Some(list) = w.by_recipient.get(recipient) {
            for event in list {
                event.set();
            }
        }
    }
}

/// `wait_seconds` as the hub admits it.
fn wait_value(v: Option<&Value>) -> Result<f64> {
    let Some(v) = v else {
        return Ok(0.0);
    };
    let ok = matches!(v, Value::Number(_))
        && super::canon::as_f64(v)
            .is_some_and(|w| w.is_finite() && (0.0..=MAX_WAIT_SECONDS).contains(&w));
    if !ok {
        return Err(BoardError::code("invalid_wait_seconds"));
    }
    Ok(super::canon::as_f64(v).expect("number"))
}

impl Board {
    /// One call through its own job (`invoke`).
    async fn invoke(
        self: &Arc<Self>,
        action: &str,
        params: Map<String, Value>,
        headers: &HashMap<String, String>,
        principal: &str,
    ) -> Result<Value> {
        let hub = &self.hub;
        // Check and reserve in one step: concurrent calls cannot both take
        // the last slot (Codex review finding 2).
        if hub
            .pending_calls
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| {
                (n < MAX_WAITERS).then_some(n + 1)
            })
            .is_err()
        {
            return Err(BoardError::code("wait_capacity_exceeded"));
        }
        let board = self.clone();
        let workers = hub.workers.clone();
        let pending = PendingCall(hub.pending_calls.clone());
        let (action, headers, principal) =
            (action.to_string(), headers.clone(), principal.to_string());
        let job = tokio::spawn(async move {
            let _pending = pending;
            let _permit = workers.acquire().await.expect("semaphore open");
            board.dispatch(&action, params, &headers, &principal).await
        });
        job.await
            .unwrap_or_else(|e| Err(BoardError::Internal(e.to_string())))
    }

    /// The context lookup of the `/mcp` binding gate. Python calls
    /// `coordination.dispatch` on an executor thread there (`web/api.py`),
    /// outside the hub, so no hub admission limit applies; the spawned task,
    /// like that thread, finishes even when the caller disconnects.
    pub async fn binding_context(
        self: &Arc<Self>,
        headers: &HashMap<String, String>,
        principal: &str,
    ) -> Result<Value> {
        if crate::mutants::active("mcp-binding-hub") {
            return self.invoke("context", Map::new(), headers, principal).await;
        }
        let board = self.clone();
        let (headers, principal) = (headers.clone(), principal.to_string());
        tokio::spawn(async move {
            board
                .dispatch("context", Map::new(), &headers, &principal)
                .await
        })
        .await
        .unwrap_or_else(|e| Err(BoardError::Internal(e.to_string())))
    }

    /// `CoordinationHub.handle`.
    pub async fn handle(
        self: &Arc<Self>,
        action: &str,
        mut params: Map<String, Value>,
        headers: &HashMap<String, String>,
        principal: &str,
    ) -> Result<Value> {
        let wait = if action == "receive" {
            let w = params.remove("wait_seconds");
            wait_value(w.as_ref())?
        } else {
            0.0
        };
        if wait == 0.0 {
            return self.invoke(action, params, headers, principal).await;
        }
        let recipient = headers.get("x-pl-agent").cloned().unwrap_or_default();
        if recipient.is_empty() {
            return Err(BoardError::code("instance_authentication_required"));
        }
        let attachment_id = params.get("attachment_id");
        let generation = params.get("generation");
        let truthy = attachment_id.is_some_and(super::store::py_truthy);
        if !truthy || !generation.is_some_and(super::canon::is_py_int) {
            return Err(BoardError::code("attachment_required"));
        }
        if attachment_id.is_some_and(|a| a.is_array() || a.is_object()) {
            // An unhashable value in Python's tuple key: TypeError.
            return Err(BoardError::code("invalid_request"));
        }
        let key = (
            recipient.clone(),
            super::canon::canonical(attachment_id.expect("checked")),
            super::canon::py_int_i64(generation.expect("checked")).unwrap_or(i64::MAX),
        );
        let event = Arc::new(Event::new());
        {
            let mut w = self.hub.waiters.lock().expect("hub lock");
            if w.attachments.contains(&key) {
                return Err(BoardError::code("attachment_already_waiting"));
            }
            let total: usize = w.by_recipient.values().map(Vec::len).sum();
            if total >= MAX_WAITERS {
                return Err(BoardError::code("wait_capacity_exceeded"));
            }
            w.attachments.insert(key.clone());
            w.by_recipient
                .entry(recipient.clone())
                .or_default()
                .push(event.clone());
        }
        let _guard = WaitGuard {
            hub: &self.hub,
            recipient,
            attachment: key,
            event: event.clone(),
        };
        let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs_f64(wait);
        loop {
            event.clear();
            let result = self
                .invoke(action, params.clone(), headers, principal)
                .await?;
            let has_messages = result
                .get("messages")
                .and_then(Value::as_array)
                .is_some_and(|m| !m.is_empty());
            if has_messages || result.get("enabled") == Some(&Value::Bool(false)) {
                return Ok(result);
            }
            if tokio::time::Instant::now() >= deadline {
                return Ok(result);
            }
            if tokio::time::timeout_at(deadline, event.wait())
                .await
                .is_err()
            {
                return self
                    .invoke(action, params.clone(), headers, principal)
                    .await;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{Config, DaemonEnv};
    use crate::service::Service;
    use std::path::PathBuf;
    use std::time::Duration;

    fn board() -> Arc<Board> {
        let config = Config::default();
        let service = Arc::new(Service::new(
            config.clone(),
            DaemonEnv::from_env(&|_| None).unwrap(),
            PathBuf::new(),
            String::new(),
            true,
        ));
        Arc::new(Board::new(
            config.coordination,
            String::new(),
            service,
            Arc::new(crate::auth::PrincipalStore::new()),
        ))
    }

    #[tokio::test]
    async fn binding_context_bypasses_hub_admission() {
        let board = board();
        board.hub.pending_calls.store(MAX_WAITERS, Ordering::SeqCst);
        let saturated = |r: &Result<Value>| matches!(r, Err(BoardError::Refused(x)) if x.code == "wait_capacity_exceeded");
        let through_hub = board
            .invoke("context", Map::new(), &HashMap::new(), "default")
            .await;
        assert!(saturated(&through_hub));
        let direct = board.binding_context(&HashMap::new(), "default").await;
        assert!(!saturated(&direct), "{direct:?}");
        assert_eq!(board.hub.pending_calls.load(Ordering::SeqCst), MAX_WAITERS);
    }

    #[tokio::test]
    async fn panicking_job_releases_its_pending_slot() {
        let board = board();
        board.hub.workers.close();
        let result = board
            .invoke("unknown", Map::new(), &HashMap::new(), "default")
            .await;
        assert!(result.is_err());
        assert_eq!(
            board.hub.pending_calls.load(Ordering::SeqCst),
            0,
            "a worker panic leaked an admission slot"
        );
    }

    #[tokio::test]
    async fn caller_cancellation_retains_slot_until_queued_job_finishes() {
        let board = board();
        let permits = board
            .hub
            .workers
            .clone()
            .acquire_many_owned(WORKERS as u32)
            .await
            .unwrap();
        let caller_board = board.clone();
        let caller = tokio::spawn(async move {
            caller_board
                .invoke("unknown", Map::new(), &HashMap::new(), "default")
                .await
        });
        tokio::time::timeout(Duration::from_secs(2), async {
            while board.hub.pending_calls.load(Ordering::SeqCst) == 0 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        caller.abort();
        assert!(caller.await.unwrap_err().is_cancelled());
        assert_eq!(board.hub.pending_calls.load(Ordering::SeqCst), 1);
        drop(permits);
        tokio::time::timeout(Duration::from_secs(2), async {
            while board.hub.pending_calls.load(Ordering::SeqCst) != 0 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("finished worker retained its admission slot");
    }
}

use crate::{
    Ownership,
    lifecycle::{self, Runtime},
    subscriptions::Subscriptions,
};
use rmcp::{
    Peer, RoleServer,
    model::{ListToolsResult, Tool, ToolListChangedNotification},
};
use std::sync::{Arc, Mutex};
use tokio_util::sync::CancellationToken;

#[derive(Clone)]
pub(crate) struct Link {
    state: Arc<Mutex<State>>,
    runtime: Arc<Runtime>,
    ownership: Ownership,
    subscriptions: Subscriptions,
}
struct State {
    up: bool,
    backoff: usize,
    watcher: Option<CancellationToken>,
    session: Option<Peer<RoleServer>>,
}
impl Link {
    pub(crate) fn new(
        runtime: Arc<Runtime>,
        ownership: Ownership,
        subscriptions: Subscriptions,
    ) -> Self {
        Self {
            state: Arc::new(Mutex::new(State {
                up: !runtime.daemon_unreachable(),
                backoff: 0,
                watcher: None,
                session: None,
            })),
            runtime,
            ownership,
            subscriptions,
        }
    }
    pub(crate) fn remember(&self, session: Peer<RoleServer>, modern: bool) {
        if !modern {
            self.state.lock().expect("recovery lock poisoned").session = Some(session);
        }
    }
    pub(crate) fn up(&self) -> bool {
        self.state.lock().expect("recovery lock poisoned").up
    }
    pub(crate) fn cached(&self) -> (ListToolsResult, Option<serde_json::Value>) {
        let cache = self
            .runtime
            .cache
            .as_ref()
            .map(|cache| cache.load())
            .unwrap_or_default();
        let tools = cache
            .get("tools")
            .and_then(serde_json::Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|raw| {
                // Decode directly from bytes; serde's buffered untagged visitors
                // reject some arbitrary-precision integer tokens in a Value.
                serde_json::to_string(raw)
                    .ok()
                    .and_then(|raw| serde_json::from_str::<Tool>(&raw).ok())
            })
            .collect();
        (
            ListToolsResult::with_all_items(tools),
            Some(
                serde_json::json!({"tools":cache.get("tools").cloned().unwrap_or_else(||serde_json::json!([]))}),
            ),
        )
    }
    pub(crate) fn lost(&self) {
        let token = {
            let mut state = self.state.lock().expect("recovery lock poisoned");
            if state.up {
                state.up = false;
                crate::stderrln!("{}", lifecycle::DAEMON_LOST_NOTE);
            }
            if state.watcher.is_some() {
                return;
            }
            let token = self.ownership.token();
            state.watcher = Some(token.clone());
            token
        };
        let link = self.clone();
        self.ownership.spawn(async move {
            while !link.up() {
                let delay = { let state = link.state.lock().expect("recovery lock poisoned"); lifecycle::RETRY_DELAYS[state.backoff.min(lifecycle::RETRY_DELAYS.len()-1)] };
                tokio::select! { biased; _ = token.cancelled() => return, _ = tokio::time::sleep(std::time::Duration::from_secs(delay)) => {} }
                { let mut state = link.state.lock().expect("recovery lock poisoned"); state.backoff = state.backoff.saturating_add(1); }
                let timeout = if crate::daemon_url::is_loopback(&link.runtime.url) { std::time::Duration::from_millis(500) } else { lifecycle::REMOTE_PROBE_TIMEOUT };
                let answered = tokio::select! { biased; _ = token.cancelled() => return, result = lifecycle::probe_health(&link.runtime.url,timeout) => result.is_some() };
                if answered { link.answered().await; }
            }
        });
    }
    pub(crate) async fn request_succeeded(&self) {
        self.state.lock().expect("recovery lock poisoned").backoff = 0;
        self.answered().await;
    }
    async fn answered(&self) {
        {
            let mut state = self.state.lock().expect("recovery lock poisoned");
            if state.up {
                return;
            }
            state.up = true;
            if let Some(watcher) = state.watcher.take() {
                watcher.cancel();
            }
        }
        crate::stderrln!("{}", lifecycle::DAEMON_RECOVERED_NOTE);
        self.notify(None).await;
    }
    pub(crate) async fn notify(&self, session: Option<Peer<RoleServer>>) {
        self.subscriptions.publish_tools_changed().await;
        let session = session.or_else(|| {
            self.state
                .lock()
                .expect("recovery lock poisoned")
                .session
                .clone()
        });
        if let Some(session) = session {
            let _ = session
                .send_notification(ToolListChangedNotification::default().into())
                .await;
        }
    }
}

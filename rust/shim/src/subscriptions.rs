use std::sync::{Arc, Mutex};

use rmcp::{ErrorData, RoleServer, model::*, service::RequestContext};
use serde_json::json;
use tokio::sync::mpsc;

#[derive(Clone, Default)]
pub struct Subscriptions(Arc<Mutex<Vec<Listener>>>);

struct Listener {
    token: Arc<()>,
    tools: bool,
    events: mpsc::Sender<()>,
}

struct Registration {
    subscriptions: Subscriptions,
    token: Arc<()>,
}

impl Drop for Registration {
    fn drop(&mut self) {
        self.subscriptions
            .0
            .lock()
            .expect("subscription lock poisoned")
            .retain(|listener| !Arc::ptr_eq(&listener.token, &self.token));
    }
}

impl Subscriptions {
    pub async fn publish_tools_changed(&self) {
        self.0
            .lock()
            .expect("subscription lock poisoned")
            .retain(|listener| !listener.tools || listener.events.try_send(()).is_ok());
        tokio::task::yield_now().await;
    }

    pub async fn listen(
        &self,
        mut filter: SubscriptionFilter,
        context: RequestContext<RoleServer>,
        ownership: crate::Ownership,
    ) -> Result<CustomResult, ErrorData> {
        filter.tools_list_changed = filter.tools_list_changed.filter(|value| *value);
        filter.prompts_list_changed = filter.prompts_list_changed.filter(|value| *value);
        filter.resources_list_changed = filter.resources_list_changed.filter(|value| *value);
        filter.resource_subscriptions = filter
            .resource_subscriptions
            .filter(|uris| !uris.is_empty());
        let (events, mut receiver) = mpsc::channel(1024);
        let token = Arc::new(());
        {
            let mut listeners = self.0.lock().expect("subscription lock poisoned");
            if listeners.len() >= 1024 {
                return Err(ErrorData::internal_error(
                    "Subscription limit reached",
                    None,
                ));
            }
            listeners.push(Listener {
                token: token.clone(),
                tools: filter.tools_list_changed == Some(true),
                events,
            });
        }
        // Registration precedes the acknowledgement; this task alone drains
        // the buffer so the acknowledgement is always the first frame.
        let _registration = Registration {
            subscriptions: self.clone(),
            token,
        };
        let meta = json!({"io.modelcontextprotocol/subscriptionId": context.id});
        let acknowledged = CustomNotification::new(
            "notifications/subscriptions/acknowledged",
            Some(json!({"notifications": filter, "_meta": meta})),
        );
        // EOF can close the SDK notification dispatcher before this future is
        // polled. The transport retains the accepted acknowledgement until it
        // is written, or hands it off ahead of this listener's final error.
        ownership.queue_ack(context.id.clone(), acknowledged.clone());
        tokio::select! {
            biased;
            _ = context.ct.cancelled() => {
                ownership.take_ack(&context.id);
                return Err(cancelled());
            },
            _ = ownership.cancelled() => return Err(crate::Ownership::closed_error()),
            result = context.peer.send_notification(acknowledged.into()) => result.map_err(|_| cancelled())?,
        }
        loop {
            tokio::select! {
                biased;
                _ = context.ct.cancelled() => return Err(cancelled()),
                _ = ownership.cancelled() => return Err(crate::Ownership::closed_error()),
                event = receiver.recv() => {
                    if event.is_none() { break; }
                    let notification = CustomNotification::new("notifications/tools/list_changed", Some(json!({"_meta": meta})));
                    tokio::select! {
                        biased;
                        _ = context.ct.cancelled() => return Err(cancelled()),
                        _ = ownership.cancelled() => return Err(crate::Ownership::closed_error()),
                        result = context.peer.send_notification(notification.into()) => result.map_err(|_| cancelled())?,
                    }
                }
            }
        }
        Ok(CustomResult(
            json!({"resultType": "complete", "_meta": meta}),
        ))
    }
}

fn cancelled() -> ErrorData {
    // RMCP removes the inbound request on peer cancellation and suppresses
    // this handler's final reply; returning releases its registration.
    ErrorData::internal_error("subscription ended", None)
}

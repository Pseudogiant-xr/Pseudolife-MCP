use rmcp::{
    RoleServer,
    model::{JsonRpcMessage, RequestId, ServerNotification},
    service::{RxJsonRpcMessage, TxJsonRpcMessage},
    transport::Transport,
};

use crate::Ownership;

type PendingSend<E> = std::pin::Pin<Box<dyn Future<Output = Result<(), E>> + Send>>;

/// Observe SDK EOF before its request drain, without replacing wire parsing.
pub struct OwnedTransport<T> {
    pub inner: T,
    pub ownership: Ownership,
}

impl<T: Transport<RoleServer>> Transport<RoleServer> for OwnedTransport<T> {
    type Error = T::Error;

    fn send(
        &mut self,
        item: TxJsonRpcMessage<RoleServer>,
    ) -> impl Future<Output = Result<(), Self::Error>> + Send + 'static {
        let acknowledgement_id = match &item {
            JsonRpcMessage::Notification(notification) => match &notification.notification {
                ServerNotification::CustomNotification(notification)
                    if notification.method == "notifications/subscriptions/acknowledged" =>
                {
                    notification
                        .params
                        .as_ref()
                        .and_then(|params| params.get("_meta"))
                        .and_then(|meta| meta.get("io.modelcontextprotocol/subscriptionId"))
                        .and_then(|value| serde_json::from_value::<RequestId>(value.clone()).ok())
                }
                _ => None,
            },
            _ => None,
        };
        let error_id = match &item {
            JsonRpcMessage::Error(error) => error.id.clone(),
            _ => None,
        };
        let acknowledgement = error_id
            .as_ref()
            .and_then(|id| self.ownership.pending_ack(id));
        let pending: Option<PendingSend<Self::Error>> =
            if let Some(acknowledgement) = acknowledgement {
                Some(Box::pin(self.inner.send(acknowledgement)))
            } else {
                None
            };
        let response = self.inner.send(item);
        let ownership = self.ownership.clone();
        async move {
            // Claim delivery only while holding the writer. The SDK's queued
            // notification and its EOF error may race; exactly one path emits
            // the acknowledgement, always before the error for that ID.
            let _writer = ownership.writes.lock().await;
            if let Some(id) = acknowledgement_id {
                if ownership.take_ack(&id).is_none() {
                    return Ok(());
                }
            } else if let Some(id) = error_id
                && ownership.take_ack(&id).is_some()
                && let Some(pending) = pending
            {
                pending.await?;
            }
            response.await
        }
    }

    async fn receive(&mut self) -> Option<RxJsonRpcMessage<RoleServer>> {
        let message = self.inner.receive().await;
        if message.is_none() {
            self.ownership.cancel();
        }
        message
    }

    async fn close(&mut self) -> Result<(), Self::Error> {
        self.ownership.cancel();
        self.inner.close().await
    }
}

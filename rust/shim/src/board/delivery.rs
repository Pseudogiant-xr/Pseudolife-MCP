//! The authenticated host bridge only emits attributed tool output.
use futures::{SinkExt, StreamExt, stream::SplitSink};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};
use tokio::{
    net::TcpStream,
    sync::{Mutex as AsyncMutex, oneshot},
};
use tokio_tungstenite::{
    MaybeTlsStream, WebSocketStream,
    tungstenite::{Message, client::IntoClientRequest, protocol::WebSocketConfig},
};
use tokio_util::{sync::CancellationToken, task::TaskTracker};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DeliveryError {
    pub uncertain: bool,
}
impl DeliveryError {
    fn certain() -> Self {
        Self { uncertain: false }
    }
}
type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;
struct SetupGuard(Option<CancellationToken>);
impl Drop for SetupGuard {
    fn drop(&mut self) {
        if let Some(cancel) = self.0.take() {
            cancel.cancel();
        }
    }
}
pub struct Delivery {
    thread: String,
    sink: AsyncMutex<SplitSink<Socket, Message>>,
    pending: Mutex<HashMap<u64, oneshot::Sender<Result<Value, DeliveryError>>>>,
    next: AtomicU64,
    cancel: CancellationToken,
    tasks: TaskTracker,
    serial: AsyncMutex<()>,
}
pub fn endpoint(url: &str) -> bool {
    reqwest::Url::parse(url).is_ok_and(|url| {
        url.scheme() == "ws"
            && matches!(url.host_str(), Some("127.0.0.1" | "[::1]"))
            && url.port().is_some_and(|port| port != 0)
            && url.username().is_empty()
            && url.password().is_none()
            && url.query().is_none()
            && url.fragment().is_none()
            && matches!(url.path(), "" | "/")
    })
}
impl Delivery {
    pub async fn connect(url: &str, token: &str, thread: &str) -> Result<Arc<Self>, DeliveryError> {
        if !endpoint(url)
            || token.is_empty()
            || token.chars().any(char::is_whitespace)
            || super::policy::canonical_uuid(thread).is_none()
        {
            return Err(DeliveryError::certain());
        }
        let mut request = url
            .into_client_request()
            .map_err(|_| DeliveryError::certain())?;
        let mut bearer =
            tokio_tungstenite::tungstenite::http::HeaderValue::from_str(&format!("Bearer {token}"))
                .map_err(|_| DeliveryError::certain())?;
        bearer.set_sensitive(true);
        request.headers_mut().insert("authorization", bearer);
        let config = WebSocketConfig::default().max_message_size(Some(1_048_576));
        let (socket, _) = tokio::time::timeout(
            Duration::from_secs(3),
            tokio_tungstenite::connect_async_with_config(request, Some(config), false),
        )
        .await
        .map_err(|_| DeliveryError::certain())?
        .map_err(|_| DeliveryError::certain())?;
        let (sink, mut stream) = socket.split();
        let delivery = Arc::new(Self {
            thread: thread.into(),
            sink: AsyncMutex::new(sink),
            pending: Mutex::new(HashMap::new()),
            next: AtomicU64::new(1),
            cancel: CancellationToken::new(),
            tasks: TaskTracker::new(),
            serial: AsyncMutex::new(()),
        });
        let mut setup_guard = SetupGuard(Some(delivery.cancel.clone()));
        let reader = delivery.clone();
        delivery.tasks.spawn(async move {
            loop {
                let message=tokio::select! {_=reader.cancel.cancelled()=>break,value=stream.next()=>value};
                let Some(Ok(message))=message else {break};
                let text=match message {Message::Text(text)=>text,Message::Ping(bytes)=>{let _=reader.sink.lock().await.send(Message::Pong(bytes)).await;continue;},Message::Close(_)=>break,_=>continue};
                let Ok(value)=serde_json::from_str::<Value>(&text) else {break};
                if !value.is_object() {break;}
                if let Some(id)=value.get("id").and_then(Value::as_u64)
                    && value.get("method").is_none() {
                        if let Some(sender)=reader.pending.lock().expect("delivery pending lock").remove(&id) {let _=sender.send(Ok(value));}
                        continue;
                    }
                if value.get("id").is_some_and(|id| !id.is_null()) && value.get("method").and_then(Value::as_str).is_some() {
                    let reply=json!({"jsonrpc":"2.0","id":value["id"],"error":{"code":-32001,"message":"delivery bridge does not handle server requests"}});
                    let _=reader.send(&reply).await;break;
                }
            }
            reader.cancel.cancel();
            for (_,sender) in reader.pending.lock().expect("delivery pending lock").drain() {let _=sender.send(Err(DeliveryError {uncertain:true}));}
            let _=tokio::time::timeout(Duration::from_secs(1),reader.sink.lock().await.close()).await;
        });
        let setup=async {
            delivery.rpc("initialize",json!({"clientInfo":{"name":"pseudolife-codex-delivery","version":"1.0.0"},"capabilities":{"experimentalApi":true}}),false).await?;
            delivery.send(&json!({"jsonrpc":"2.0","method":"initialized","params":{}})).await?;
            Ok(())
        }.await;
        if let Err(error) = setup {
            delivery.close().await;
            return Err(error);
        }
        setup_guard.0 = None;
        Ok(delivery)
    }
    async fn send(&self, value: &Value) -> Result<(), DeliveryError> {
        if self.cancel.is_cancelled() {
            return Err(DeliveryError::certain());
        }
        self.sink
            .lock()
            .await
            .send(Message::Text(value.to_string().into()))
            .await
            .map_err(|_| DeliveryError::certain())
    }
    async fn rpc(
        &self,
        method: &str,
        params: Value,
        uncertain: bool,
    ) -> Result<Value, DeliveryError> {
        let id = self.next.fetch_add(1, Ordering::Relaxed);
        let (sender, receiver) = oneshot::channel();
        self.pending
            .lock()
            .expect("delivery pending lock")
            .insert(id, sender);
        let result = async {
            self.send(&json!({"jsonrpc":"2.0","id":id,"method":method,"params":params}))
                .await
                .map_err(|_| DeliveryError { uncertain })?;
            let value = tokio::time::timeout(Duration::from_secs(5), receiver)
                .await
                .map_err(|_| DeliveryError { uncertain })?
                .map_err(|_| DeliveryError { uncertain })??;
            if value.get("error").is_some() {
                return Err(DeliveryError::certain());
            }
            value
                .get("result")
                .cloned()
                .ok_or(DeliveryError { uncertain })
        }
        .await;
        self.pending
            .lock()
            .expect("delivery pending lock")
            .remove(&id);
        result
    }
    pub async fn verify(&self) -> Result<(), DeliveryError> {
        let mut cursor = None;
        let mut seen = std::collections::HashSet::new();
        for _ in 0..32 {
            let mut params = json!({"limit":100});
            if let Some(cursor) = &cursor {
                params["cursor"] = json!(cursor);
            }
            let result = self.rpc("thread/loaded/list", params, false).await?;
            let data = result
                .get("data")
                .and_then(Value::as_array)
                .ok_or(DeliveryError::certain())?;
            if data
                .iter()
                .any(|value| value.as_str() == Some(&self.thread))
            {
                return Ok(());
            }
            let next = result
                .get("nextCursor")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .ok_or(DeliveryError::certain())?;
            if !seen.insert(next.to_owned()) {
                return Err(DeliveryError::certain());
            }
            cursor = Some(next.to_owned());
        }
        Err(DeliveryError::certain())
    }
    pub async fn deliver(&self, content: &str) -> Result<(), DeliveryError> {
        let _guard = self.serial.lock().await;
        self.verify().await?;
        let result=self.rpc("turn/start",json!({"threadId":self.thread,"input":[],"toolOutput":{"name":"pseudolife_message","namespace":"pseudolife","output":content}}),true).await?;
        if result
            .get("turn")
            .and_then(|turn| turn.get("id"))
            .and_then(Value::as_str)
            .is_none()
        {
            return Err(DeliveryError { uncertain: true });
        }
        Ok(())
    }
    pub async fn close(&self) {
        self.cancel.cancel();
        self.tasks.close();
        self.tasks.wait().await;
    }
}

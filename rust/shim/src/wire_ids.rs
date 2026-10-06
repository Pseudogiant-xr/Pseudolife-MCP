//! Preserve Python's arbitrary integer correlation identifiers at the SDK seam.
use hmac::{Hmac, Mac};
use serde_json::Value;
use sha2::Sha256;
use std::{
    pin::Pin,
    sync::{Arc, Mutex},
    task::{Context, Poll},
};
use tokio::io::{AsyncBufRead, AsyncRead, BufReader, ReadBuf};
#[derive(Clone)]
pub(crate) struct Ids(Arc<Mutex<State>>);
// No per-request registry: cancelled SDK requests can suppress their final reply.
// Deterministic authenticated aliases preserve duplicate-ID and cancellation
// semantics without retaining completed/cancelled correlation entries.
struct State {
    prefix: String,
    key: [u8; 32],
}
impl Default for Ids {
    fn default() -> Self {
        let mut key = [0; 32];
        key[..16].copy_from_slice(uuid::Uuid::new_v4().as_bytes());
        key[16..].copy_from_slice(uuid::Uuid::new_v4().as_bytes());
        Self(Arc::new(Mutex::new(State {
            prefix: format!("pseudolife-rpc-{}-", uuid::Uuid::new_v4()),
            key,
        })))
    }
}
impl Ids {
    fn input(&self, value: &mut Value) {
        let state = self.0.lock().expect("ID lock poisoned");
        if value.get("method").is_some()
            && let Some(id) = value.get_mut("id")
            && state.needs_alias(id)
        {
            *id = Value::String(state.alias(id));
        }
        if value.get("method").and_then(Value::as_str) == Some("notifications/cancelled")
            && let Some(id) = value.pointer_mut("/params/requestId")
            && state.needs_alias(id)
        {
            // A caller's literal alias-shaped string is itself wrapped, so even
            // a previously issued alias cannot cancel a different original ID.
            *id = Value::String(state.alias(id));
        }
    }
    pub(crate) fn output(&self, value: &mut Value) {
        let state = self.0.lock().expect("ID lock poisoned");
        for path in [
            "/params/_meta/io.modelcontextprotocol~1subscriptionId",
            "/result/_meta/io.modelcontextprotocol~1subscriptionId",
            "/id",
        ] {
            if let Some(id) = value.pointer_mut(path)
                && let Some(original) = id.as_str().and_then(|alias| state.original(alias))
            {
                *id = original;
            }
        }
    }
    #[cfg(test)]
    fn retained_bytes(&self) -> usize {
        let state = self.0.lock().unwrap();
        std::mem::size_of::<State>() + state.prefix.capacity()
    }
}
impl State {
    fn needs_alias(&self, id: &Value) -> bool {
        id.is_number() && id.as_i64().is_none() && !id.to_string().contains(['.', 'e', 'E'])
            || id.as_str().is_some_and(|id| id.starts_with(&self.prefix))
    }
    fn alias(&self, original: &Value) -> String {
        let payload = original.to_string();
        let tag = self.mac(&payload).finalize().into_bytes();
        let tag: String = tag.iter().map(|byte| format!("{byte:02x}")).collect();
        format!("{}{tag}:{payload}", self.prefix)
    }
    fn mac(&self, payload: &str) -> Hmac<Sha256> {
        let mut mac = Hmac::<Sha256>::new_from_slice(&self.key).expect("fixed HMAC key length");
        mac.update(payload.as_bytes());
        mac
    }
    fn original(&self, alias: &str) -> Option<Value> {
        let (tag, payload) = alias.strip_prefix(&self.prefix)?.split_once(':')?;
        if tag.len() != 64 {
            return None;
        }
        let mut decoded = [0; 32];
        for (byte, pair) in decoded.iter_mut().zip(tag.as_bytes().chunks_exact(2)) {
            let digit = |byte: u8| (byte as char).to_digit(16).map(|value| value as u8);
            *byte = digit(pair[0])? * 16 + digit(pair[1])?;
        }
        // Prefix/syntax alone never establish that an alias was issued here.
        self.mac(payload).verify_slice(&decoded).ok()?;
        serde_json::from_str(payload).ok()
    }
}
pub(crate) struct Input<R> {
    reader: BufReader<R>,
    ids: Ids,
    line: Vec<u8>,
    ready: Vec<u8>,
    offset: usize,
    eof: bool,
}
impl<R: AsyncRead> Input<R> {
    pub(crate) fn new(read: R, ids: Ids) -> Self {
        Self {
            reader: BufReader::new(read),
            ids,
            line: Vec::new(),
            ready: Vec::new(),
            offset: 0,
            eof: false,
        }
    }
}
impl<R: AsyncRead + Unpin> AsyncRead for Input<R> {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        out: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        let this = self.get_mut();
        if out.remaining() == 0 {
            return Poll::Ready(Ok(()));
        }
        loop {
            if this.offset < this.ready.len() {
                let count = out.remaining().min(this.ready.len() - this.offset);
                out.put_slice(&this.ready[this.offset..this.offset + count]);
                this.offset += count;
                return Poll::Ready(Ok(()));
            }
            this.ready.clear();
            this.offset = 0;
            if this.eof {
                return Poll::Ready(Ok(()));
            }
            let (chunk, complete) = match Pin::new(&mut this.reader).poll_fill_buf(cx) {
                Poll::Pending => return Poll::Pending,
                Poll::Ready(Err(error)) => return Poll::Ready(Err(error)),
                Poll::Ready(Ok(bytes)) => {
                    if bytes.is_empty() {
                        this.eof = true;
                        (Vec::new(), true)
                    } else {
                        let end = bytes
                            .iter()
                            .position(|byte| *byte == b'\n')
                            .map(|index| index + 1);
                        (bytes[..end.unwrap_or(bytes.len())].to_vec(), end.is_some())
                    }
                }
            };
            Pin::new(&mut this.reader).consume(chunk.len());
            this.line.extend_from_slice(&chunk);
            if complete {
                this.ready = std::mem::take(&mut this.line);
                // Invalid envelopes still reach the SDK unchanged for its own validation.
                if let Ok(mut value) = serde_json::from_slice::<Value>(&this.ready) {
                    // The oracle's strict request ID union ignores these
                    // invalid envelopes and continues reading later requests.
                    if value.get("method").is_some()
                        && value.get("id").is_some_and(|id| {
                            !id.is_string()
                                && (!id.is_number() || id.to_string().contains(['.', 'e', 'E']))
                        })
                    {
                        this.ready.clear();
                        continue;
                    }
                    this.ids.input(&mut value);
                    this.ready = serde_json::to_vec(&value).map_err(std::io::Error::other)?;
                    this.ready.push(b'\n');
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn cancelled_aliases_release_without_losing_queued_frames_or_reused_ids() {
        let ids = Ids::default();
        let retained = ids.retained_bytes();
        let big: Value = serde_json::from_str("184467440737095516160").unwrap();
        for _ in 0..32 {
            let mut request = json!({"method":"subscriptions/listen","id":big});
            ids.input(&mut request);
            let alias = request["id"].clone();
            let mut cancel = json!({"method":"notifications/cancelled","params":{"requestId":big}});
            ids.input(&mut cancel);
            assert_eq!(cancel["params"]["requestId"], alias);
            assert_eq!(
                ids.retained_bytes(),
                retained,
                "cancelled request retains correlation state"
            );

            // A notification already queued by the SDK must still restore.
            let mut ack =
                json!({"params":{"_meta":{"io.modelcontextprotocol/subscriptionId":alias}}});
            ids.output(&mut ack);
            assert_eq!(
                ack["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"],
                big
            );
            // Even a literal, authenticated old alias is an ordinary client ID.
            let mut literal = json!({"method":"tools/list","id":alias});
            ids.input(&mut literal);
            assert_ne!(literal["id"], alias);
            let mut response = json!({"id":literal["id"],"result":{}});
            ids.output(&mut response);
            assert_eq!(response["id"], alias);
            assert_eq!(ids.retained_bytes(), retained);
        }
    }
    #[test]
    fn cancellation_retains_active_duplicate_and_rejects_untrusted_alias_syntax() {
        let ids = Ids::default();
        let retained = ids.retained_bytes();
        let big: Value = serde_json::from_str("184467440737095516160").unwrap();
        let mut first = json!({"method":"subscriptions/listen","id":big});
        let mut second = first.clone();
        ids.input(&mut first);
        ids.input(&mut second);
        assert_eq!(first["id"], second["id"]);
        let mut cancel = json!({"method":"notifications/cancelled","params":{"requestId":big}});
        ids.input(&mut cancel);
        assert_eq!(cancel["params"]["requestId"], first["id"]);
        assert_eq!(ids.retained_bytes(), retained);
        let mut ack =
            json!({"params":{"_meta":{"io.modelcontextprotocol/subscriptionId":second["id"]}}});
        ids.output(&mut ack);
        assert_eq!(
            ack["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"],
            big
        );

        let mut eof =
            json!({"id":second["id"],"error":{"code":-32000,"message":"Connection closed"}});
        ids.output(&mut eof);
        assert_eq!(eof["id"], big);
        assert_eq!(ids.retained_bytes(), retained);
        let prefix = ids.0.lock().unwrap().prefix.clone();
        let forged = format!("{prefix}{}:184467440737095516160", "0".repeat(64));
        let mut untrusted = json!({"id":forged,"result":{}});
        ids.output(&mut untrusted);
        assert_eq!(untrusted["id"], forged);
        let mut literal = json!({"method":"tools/list","id":forged});
        ids.input(&mut literal);
        let mut response = json!({"id":literal["id"],"result":{}});
        ids.output(&mut response);
        assert_eq!(response["id"], forged);
        assert_eq!(ids.retained_bytes(), retained);
    }
    #[tokio::test]
    async fn sdk_cancellation_reclaims_handler_aliases_in_a_live_session() {
        use rmcp::{
            ErrorData, RoleServer, ServerHandler, ServiceExt, model::*, service::RequestContext,
        };
        use tokio::io::{AsyncBufReadExt, AsyncWriteExt};
        #[derive(Clone)]
        struct Waiting(Arc<tokio::sync::Notify>, Arc<tokio::sync::Notify>);
        impl ServerHandler for Waiting {
            async fn on_custom_request(
                &self,
                _: CustomRequest,
                context: RequestContext<RoleServer>,
            ) -> Result<CustomResult, ErrorData> {
                self.0.notify_one();
                context.ct.cancelled().await;
                self.1.notify_one();
                Err(ErrorData::internal_error("cancelled", None))
            }
        }
        let ids = Ids::default();
        let retained = ids.retained_bytes();
        let started = Arc::new(tokio::sync::Notify::new());
        let finished = Arc::new(tokio::sync::Notify::new());
        let (mut send, input) = tokio::io::duplex(64 * 1024);
        let (raw, write) = tokio::io::duplex(64 * 1024);
        let (read, output) = tokio::io::duplex(64 * 1024);
        let writer = tokio::spawn(crate::wire_json::output(raw, output, ids.clone()));
        let handler = crate::wire_compatibility::WireCompatibility(Waiting(
            started.clone(),
            finished.clone(),
        ));
        let transport = rmcp::transport::async_rw::AsyncRwTransport::new_server(
            Input::new(input, ids.clone()),
            write,
        );
        let server = tokio::spawn(async move {
            handler
                .serve(transport)
                .await
                .unwrap()
                .waiting()
                .await
                .unwrap();
        });
        let mut read = tokio::io::BufReader::new(read);
        let mut line = String::new();
        send.write_all(b"{\"jsonrpc\":\"2.0\",\"id\":0,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-11-25\",\"capabilities\":{},\"clientInfo\":{\"name\":\"retention\",\"version\":\"1\"}}}\n").await.unwrap();
        tokio::time::timeout(std::time::Duration::from_secs(1), read.read_line(&mut line))
            .await
            .unwrap()
            .unwrap();
        assert!(
            serde_json::from_str::<Value>(&line)
                .unwrap()
                .get("result")
                .is_some()
        );
        send.write_all(b"{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}\n")
            .await
            .unwrap();
        for _ in 0..16 {
            send.write_all(
                b"{\"jsonrpc\":\"2.0\",\"id\":184467440737095516160,\"method\":\"hold\"}\n",
            )
            .await
            .unwrap();
            tokio::time::timeout(std::time::Duration::from_secs(1), started.notified())
                .await
                .unwrap();
            assert_eq!(ids.retained_bytes(), retained);
            send.write_all(b"{\"jsonrpc\":\"2.0\",\"method\":\"notifications/cancelled\",\"params\":{\"requestId\":184467440737095516160}}\n").await.unwrap();
            tokio::time::timeout(std::time::Duration::from_secs(1), finished.notified())
                .await
                .unwrap();
            assert_eq!(ids.retained_bytes(), retained);
            send.write_all(b"{\"jsonrpc\":\"2.0\",\"id\":\"barrier\",\"method\":\"ping\"}\n")
                .await
                .unwrap();
            line.clear();
            tokio::time::timeout(std::time::Duration::from_secs(1), read.read_line(&mut line))
                .await
                .unwrap()
                .unwrap();
            assert_eq!(
                serde_json::from_str::<Value>(&line).unwrap()["id"],
                "barrier",
                "cancelled response escaped SDK suppression"
            );
        }
        send.shutdown().await.unwrap();
        drop(send);
        tokio::time::timeout(std::time::Duration::from_secs(1), server)
            .await
            .unwrap()
            .unwrap();
        writer.await.unwrap().unwrap();
        assert_eq!(ids.retained_bytes(), retained);
    }
    #[test]
    fn private_alias_collision_does_not_steal_a_legitimate_client_id() {
        let ids = Ids::default();
        ids.0.lock().unwrap().prefix = "pseudolife-rpc-fixed-".into();
        let retained = ids.retained_bytes();
        let big: Value = serde_json::from_str("184467440737095516160").unwrap();
        let mut request = json!({"method":"tools/list","id":big});
        ids.input(&mut request);
        let alias = request["id"].clone();
        let mut colliding = json!({"method":"tools/list","id":alias});
        ids.input(&mut colliding);
        assert_ne!(request["id"], colliding["id"]);
        let mut cancel = json!({"method":"notifications/cancelled","params":{"requestId":alias}});
        ids.input(&mut cancel);
        assert_eq!(cancel["params"]["requestId"], colliding["id"]);
        let mut ack = json!({"method":"notifications/subscriptions/acknowledged","params":{"_meta":{"io.modelcontextprotocol/subscriptionId":request["id"]}}});
        ids.output(&mut ack);
        assert_eq!(
            ack["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"],
            big
        );
        let mut first = json!({"id":request["id"],"error":{"code":-32000}});
        ids.output(&mut first);
        assert_eq!(first["id"], big);
        let mut second = json!({"id":colliding["id"],"result":{}});
        ids.output(&mut second);
        assert_eq!(second["id"], alias);
        assert_eq!(ids.retained_bytes(), retained);
    }
    #[test]
    fn ordinary_ids_retain_the_sdk_representation() {
        let ids = Ids::default();
        let retained = ids.retained_bytes();
        for id in [
            json!(i64::MIN),
            json!(i64::MAX),
            json!(0),
            json!("ordinary"),
        ] {
            let mut request = json!({"method":"tools/list","id":id});
            ids.input(&mut request);
            assert_eq!(request["id"], id);
            ids.output(&mut request);
            assert_eq!(request["id"], id);
        }
        assert_eq!(ids.retained_bytes(), retained);
    }
}

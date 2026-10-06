use futures::{StreamExt, stream::BoxStream};
use http::{HeaderName, HeaderValue};
use rmcp::{
    model::*,
    transport::streamable_http_client::{
        SseError, StreamableHttpClient, StreamableHttpError, StreamableHttpPostResponse,
    },
};
use serde_json::Value;
use sse_stream::{Sse, SseStream};
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
};

#[derive(Default)]
pub struct Observation {
    pub phase: &'static str,
    pub requested: &'static str,
    pub refused_phase: Option<&'static str>,
    pub credential_failed: bool,
    pub dispatched: bool,
    pub response_phase: Option<&'static str>,
    pub status: Option<u16>,
    pub failure: Option<&'static str>,
    pub expected_id: Option<Value>,
    pub forwarded_meta: Option<RequestMetaObject>,
    pub raw_result: Option<Value>,
    pub session: Option<String>,
    pub deleted: bool,
    pub initialized: bool,
}

#[derive(Clone)]
pub struct ObservedHttp {
    pub client: reqwest::Client,
    pub observation: Arc<Mutex<Observation>>,
}

impl ObservedHttp {
    pub fn note_error(&self, error: &reqwest::Error) {
        let mut observation = self.observation.lock().expect("observation lock poisoned");
        if let Some(status) = error.status() {
            observation.status = Some(status.as_u16());
        }
        let failure = if error.is_connect() {
            "connection_failure"
        } else if error.is_timeout() {
            "timeout"
        } else if error.is_body() || error.is_request() {
            "connection_failure"
        } else {
            "protocol"
        };
        let rank = |kind: Option<&str>| match kind {
            Some("connection_failure") => 3,
            Some("timeout") => 2,
            Some("protocol") => 1,
            _ => 0,
        };
        if rank(Some(failure)) > rank(observation.failure) {
            observation.failure = Some(failure);
        }
    }

    fn observe_message(observation: &Arc<Mutex<Observation>>, value: &Value) {
        let mut observation = observation.lock().expect("observation lock poisoned");
        if value.get("id") == observation.expected_id.as_ref()
            && let Some(result) = value.get("result")
        {
            observation.raw_result = Some(result.clone());
        }
    }

    async fn post(
        &self,
        uri: Arc<str>,
        mut message: ClientJsonRpcMessage,
        session_id: Option<Arc<str>>,
        auth: Option<String>,
        headers: HashMap<HeaderName, HeaderValue>,
        max_size: usize,
    ) -> Result<StreamableHttpPostResponse, StreamableHttpError<reqwest::Error>> {
        let before = serde_json::to_value(&message)?;
        let method = before.get("method").and_then(Value::as_str);
        if matches!(method, Some("tools/list" | "tools/call"))
            && let ClientJsonRpcMessage::Request(request) = &mut message
        {
            *request.request.get_meta_mut() = self
                .observation
                .lock()
                .expect("observation lock poisoned")
                .forwarded_meta
                .clone()
                .unwrap_or_default();
        }
        let value = serde_json::to_value(&message)?;
        if matches!(method, Some("tools/list" | "tools/call")) {
            self.observation
                .lock()
                .expect("observation lock poisoned")
                .expected_id = value.get("id").cloned();
        }
        let mut request = self
            .client
            .post(uri.as_ref())
            .header("Accept", "application/json, text/event-stream")
            .headers(headers.into_iter().collect());
        if let Some(session) = session_id.as_ref() {
            request = request.header("Mcp-Session-Id", session.as_ref());
        }
        if let Some(auth) = auth {
            request = request.bearer_auth(auth);
        }
        let response = request.json(&message).send().await.map_err(|error| {
            self.note_error(&error);
            StreamableHttpError::Client(error)
        })?;
        let status = response.status();
        if status.as_u16() >= 400 {
            let mut observation = self.observation.lock().expect("observation lock poisoned");
            observation.status = Some(status.as_u16());
            observation.response_phase = Some(observation.phase);
        }
        let session = response
            .headers()
            .get("Mcp-Session-Id")
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned);
        if method == Some("initialize")
            && let Some(session) = &session
        {
            self.observation
                .lock()
                .expect("observation lock poisoned")
                .session = Some(session.clone());
        }
        if method == Some("notifications/initialized") && status.is_success() {
            self.observation
                .lock()
                .expect("observation lock poisoned")
                .initialized = true;
        }
        if matches!(status.as_u16(), 202 | 204) {
            return Ok(StreamableHttpPostResponse::Accepted);
        }
        if !status.is_success() {
            let content_type = response
                .headers()
                .get("Content-Type")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("")
                .to_owned();
            let length = response
                .headers()
                .get("Content-Length")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("")
                .to_owned();
            // Read only the small body that can establish the daemon gate's
            // exact refusal. Everything else is sanitized status evidence.
            if status.as_u16() == 503 && bounded_length(&length) {
                let payload = response.bytes().await.map_err(|error| {
                    self.note_error(&error);
                    StreamableHttpError::Client(error)
                })?;
                let refused =
                    refusal_phase(status.as_u16(), &content_type, &length, &payload, &value);
                self.observation
                    .lock()
                    .expect("observation lock poisoned")
                    .refused_phase = refused;
            }
            if status.as_u16() == 404 && session_id.is_some() {
                return Err(StreamableHttpError::SessionExpired);
            }
            return Err(StreamableHttpError::UnexpectedServerResponse(
                "HTTP operation refused".into(),
            ));
        }
        let content_type = response
            .headers()
            .get("Content-Type")
            .and_then(|value| value.to_str().ok())
            .unwrap_or("")
            .to_owned();
        if content_type.starts_with("text/event-stream") {
            Ok(StreamableHttpPostResponse::Sse(
                observed_stream(response, self.observation.clone(), max_size),
                session,
            ))
        } else if content_type.starts_with("application/json") {
            let body = response.bytes().await.map_err(|error| {
                self.note_error(&error);
                StreamableHttpError::Client(error)
            })?;
            if value.get("id").is_none() {
                return Ok(StreamableHttpPostResponse::Accepted);
            }
            let raw = serde_json::from_slice::<Value>(&body)?;
            let normalized = legacy_result(raw.clone(), &self.observation);
            let body = serde_json::to_vec(&normalized)?;
            // Decode the SDK envelope directly from JSON bytes. With arbitrary
            // precision, Value's u128 fast path is incompatible with serde's
            // buffered untagged/flattened visitors and rejects accepted integers.
            let typed = serde_json::from_slice::<ServerJsonRpcMessage>(&body)?;
            Self::observe_message(&self.observation, &raw);
            Ok(StreamableHttpPostResponse::Json(typed, session))
        } else {
            Err(StreamableHttpError::UnexpectedContentType(Some(
                content_type,
            )))
        }
    }
}

fn bounded_length(length: &str) -> bool {
    !length.is_empty()
        && length.bytes().all(|byte| byte.is_ascii_digit())
        && length.parse::<usize>().is_ok_and(|length| length <= 256)
}
struct GateBody;
impl<'de> serde::Deserialize<'de> for GateBody {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct OneMember;
        impl<'de> serde::de::Visitor<'de> for OneMember {
            type Value = GateBody;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("one refusal member")
            }
            fn visit_map<M: serde::de::MapAccess<'de>>(
                self,
                mut map: M,
            ) -> Result<GateBody, M::Error> {
                let first = map.next_entry::<String, Value>()?;
                if first
                    != Some((
                        "error".to_owned(),
                        Value::String("principals_unavailable".to_owned()),
                    ))
                    || map.next_entry::<String, Value>()?.is_some()
                {
                    return Err(serde::de::Error::custom("inexact refusal"));
                }
                Ok(GateBody)
            }
        }
        deserializer.deserialize_map(OneMember)
    }
}
fn refusal_phase(
    status: u16,
    content_type: &str,
    length: &str,
    body: &[u8],
    sent: &Value,
) -> Option<&'static str> {
    if status != 503
        || !content_type
            .split(';')
            .next()?
            .trim()
            .eq_ignore_ascii_case("application/json")
        || !bounded_length(length)
        || body.len() > 256
        || serde_json::from_slice::<GateBody>(body).is_err()
    {
        return None;
    }
    let id = sent.get("id")?;
    if !(id.is_string() || id.is_number() && !id.to_string().contains(['.', 'e', 'E'])) {
        return None;
    }
    match sent.get("method")?.as_str()? {
        "initialize" => Some("initialize"),
        "tools/list" => Some("list"),
        "tools/call" => Some("call"),
        _ => None,
    }
}
fn legacy_result(mut value: Value, observation: &Arc<Mutex<Observation>>) -> Value {
    let observed = observation.lock().expect("observation lock poisoned");
    if matches!(observed.phase, "list" | "call")
        && value.get("id") == observed.expected_id.as_ref()
        && let Some(result) = value.get_mut("result").and_then(Value::as_object_mut)
    {
        // Python's version-free model drops fields that exist only in a later
        // protocol revision before parsing constraints for that revision.
        for key in ["ttlMs", "cacheScope", "resultType"] {
            result.shift_remove(key);
        }
    }
    value
}
fn observed_stream(
    response: reqwest::Response,
    observation: Arc<Mutex<Observation>>,
    maximum: usize,
) -> BoxStream<'static, Result<Sse, SseError>> {
    let mut size = EventSize::default();
    let bytes = response.bytes_stream().map(move |chunk| {
        chunk.map_err(std::io::Error::other).and_then(|chunk| {
            size.observe(&chunk, maximum)?;
            Ok(chunk)
        })
    });
    SseStream::from_bytes_stream(bytes)
        .map(move |mut event| {
            if let Ok(event) = &mut event
                && let Some(data) = &event.data
                && let Ok(value) = serde_json::from_str::<Value>(data)
            {
                ObservedHttp::observe_message(&observation, &value);
                event.data = Some(legacy_result(value, &observation).to_string());
            }
            event
        })
        .boxed()
}

// Matches RMCP's retained-event accounting, including CR/LF, comments and
// split chunks; unlike a parsed-event check this bounds unterminated input.
#[derive(Default)]
struct EventSize {
    retained: usize,
    line: usize,
    comment: bool,
    cr: bool,
}
impl EventSize {
    fn observe(&mut self, bytes: &[u8], maximum: usize) -> Result<(), std::io::Error> {
        for &byte in bytes {
            if self.cr {
                self.cr = false;
                if byte == b'\n' {
                    continue;
                }
            }
            if matches!(byte, b'\r' | b'\n') {
                if self.line == 0 {
                    self.retained = 0;
                } else if !self.comment {
                    self.retained = self.retained.saturating_add(self.line).saturating_add(1);
                }
                self.line = 0;
                self.comment = false;
                self.cr = byte == b'\r';
            } else {
                if self.line == 0 {
                    self.comment = byte == b':';
                }
                self.line = self.line.saturating_add(1);
            }
            if self.retained.saturating_add(self.line) > maximum {
                return Err(std::io::Error::other("SSE event size limit exceeded"));
            }
        }
        Ok(())
    }
}

impl StreamableHttpClient for ObservedHttp {
    type Error = reqwest::Error;
    async fn post_message(
        &self,
        uri: Arc<str>,
        message: ClientJsonRpcMessage,
        session: Option<Arc<str>>,
        auth: Option<String>,
        headers: HashMap<HeaderName, HeaderValue>,
    ) -> Result<StreamableHttpPostResponse, StreamableHttpError<Self::Error>> {
        self.post(uri, message, session, auth, headers, 16 * 1024 * 1024)
            .await
    }
    async fn post_message_with_max_sse_event_size(
        &self,
        uri: Arc<str>,
        message: ClientJsonRpcMessage,
        session: Option<Arc<str>>,
        auth: Option<String>,
        headers: HashMap<HeaderName, HeaderValue>,
        maximum: usize,
    ) -> Result<StreamableHttpPostResponse, StreamableHttpError<Self::Error>> {
        self.post(uri, message, session, auth, headers, maximum)
            .await
    }
    async fn delete_session(
        &self,
        uri: Arc<str>,
        session: Arc<str>,
        auth: Option<String>,
        headers: HashMap<HeaderName, HeaderValue>,
    ) -> Result<(), StreamableHttpError<Self::Error>> {
        let result = self
            .client
            .delete_session(uri, session, auth, headers)
            .await;
        self.observation
            .lock()
            .expect("observation lock poisoned")
            .deleted = result.is_ok();
        result
    }
    async fn get_stream(
        &self,
        uri: Arc<str>,
        session: Option<Arc<str>>,
        last: Option<String>,
        auth: Option<String>,
        headers: HashMap<HeaderName, HeaderValue>,
    ) -> Result<BoxStream<'static, Result<Sse, SseError>>, StreamableHttpError<Self::Error>> {
        self.get_stream_with_max_sse_event_size(uri, session, last, auth, headers, 16 * 1024 * 1024)
            .await
    }
    async fn get_stream_with_max_sse_event_size(
        &self,
        uri: Arc<str>,
        session: Option<Arc<str>>,
        last: Option<String>,
        auth: Option<String>,
        headers: HashMap<HeaderName, HeaderValue>,
        maximum: usize,
    ) -> Result<BoxStream<'static, Result<Sse, SseError>>, StreamableHttpError<Self::Error>> {
        let mut request = self
            .client
            .get(uri.as_ref())
            .header("Accept", "text/event-stream")
            .headers(headers.into_iter().collect());
        if let Some(session) = session {
            request = request.header("Mcp-Session-Id", session.as_ref());
        }
        if let Some(last) = last {
            request = request.header("Last-Event-ID", last);
        }
        if let Some(auth) = auth {
            request = request.bearer_auth(auth);
        }
        let response = request.send().await.map_err(|error| {
            self.note_error(&error);
            StreamableHttpError::Client(error)
        })?;
        let status = response.status();
        if status.as_u16() == 405 {
            return Err(StreamableHttpError::ServerDoesNotSupportSse);
        }
        if !status.is_success() {
            let mut observation = self.observation.lock().expect("observation lock poisoned");
            observation.status = Some(status.as_u16());
            observation.response_phase = Some(observation.phase);
            return Err(StreamableHttpError::UnexpectedServerResponse(
                "HTTP listen refused".into(),
            ));
        }
        let content_type = response
            .headers()
            .get("Content-Type")
            .and_then(|value| value.to_str().ok())
            .unwrap_or("");
        if !content_type.starts_with("text/event-stream") {
            return Err(StreamableHttpError::UnexpectedContentType(Some(
                content_type.to_owned(),
            )));
        }
        Ok(observed_stream(response, self.observation.clone(), maximum))
    }
}
#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test(flavor = "current_thread")]
    async fn test_listen_and_resume_streams_get_the_wider_event_limit() {
        use std::io::{BufRead, Write};
        for (last, maximum, accepted) in [
            (None, 1024 * 1024, false),
            (None, 16 * 1024 * 1024, true),
            (Some("fixture-event".to_owned()), 16 * 1024 * 1024, true),
        ] {
            let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            let uri: Arc<str> = format!("http://{}/mcp", listener.local_addr().unwrap()).into();
            let expected_last = last.clone();
            let server = std::thread::spawn(move || {
                let (mut socket, _) = listener.accept().unwrap();
                socket
                    .set_read_timeout(Some(std::time::Duration::from_secs(5)))
                    .unwrap();
                let mut reader = std::io::BufReader::new(socket.try_clone().unwrap());
                let mut lines = String::new();
                loop {
                    let mut line = String::new();
                    reader.read_line(&mut line).unwrap();
                    if line == "\r\n" {
                        break;
                    }
                    lines.push_str(&line);
                }
                assert!(lines.starts_with("GET /mcp "));
                assert!(
                    lines
                        .to_ascii_lowercase()
                        .contains("mcp-session-id: fixture-session")
                );
                if let Some(last) = expected_last {
                    assert!(
                        lines
                            .to_ascii_lowercase()
                            .contains(&format!("last-event-id: {last}"))
                    );
                }
                let body = format!(
                    "data: {}\n\n",
                    serde_json::json!({"jsonrpc":"2.0","method":"notifications/message","params":{"data":"x".repeat(1_400_000)}})
                );
                write!(socket,"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len()).unwrap();
            });
            let client = ObservedHttp {
                client: reqwest::Client::builder()
                    .redirect(reqwest::redirect::Policy::none())
                    .timeout(std::time::Duration::from_secs(5))
                    .build()
                    .unwrap(),
                observation: Default::default(),
            };
            let mut stream = client
                .get_stream_with_max_sse_event_size(
                    uri,
                    Some("fixture-session".into()),
                    last,
                    None,
                    Default::default(),
                    maximum,
                )
                .await
                .unwrap();
            let event = stream.next().await.unwrap();
            if accepted {
                let event = event.unwrap();
                let data: Value = serde_json::from_str(event.data.as_ref().unwrap()).unwrap();
                assert_eq!(data["params"]["data"].as_str().unwrap().len(), 1_400_000);
            } else {
                assert!(
                    event.is_err(),
                    "baseline event cap accepted the oversized event"
                );
            }
            drop(stream);
            server.join().unwrap();
        }
    }
    #[test]
    fn exact_http_gate_refusal_requires_one_small_json_member_and_request_id() {
        assert_eq!(
            refusal_phase(
                503,
                "application/json; charset=utf-8",
                "35",
                br#"{"error":"principals_unavailable"}"#,
                &serde_json::json!({"id":1,"method":"tools/call"})
            ),
            Some("call")
        );
        for body in [
            br#"{"error":"principals_unavailable","error":"principals_unavailable"}"#.as_slice(),
            br#"{"error":"principals_unavailable","detail":"secret.invalid"}"#.as_slice(),
        ] {
            assert_eq!(
                refusal_phase(
                    503,
                    "application/json",
                    &body.len().to_string(),
                    body,
                    &serde_json::json!({"id":1,"method":"tools/call"})
                ),
                None
            );
        }
        assert_eq!(
            refusal_phase(
                503,
                "application/json",
                "35",
                br#"{"error":"principals_unavailable"}"#,
                &serde_json::json!({"method":"notifications/initialized"})
            ),
            None
        );
        assert_eq!(
            refusal_phase(
                503,
                "application/json",
                "35",
                br#"{"error":"principals_unavailable"}"#,
                &serde_json::json!({"id":true,"method":"tools/call"})
            ),
            None
        );
        assert_eq!(
            refusal_phase(
                503,
                "application/json",
                "257",
                br#"{"error":"principals_unavailable"}"#,
                &serde_json::json!({"id":1,"method":"tools/call"})
            ),
            None
        );
    }
    #[test]
    fn wider_sse_limit_bounds_split_unterminated_input_and_resets_at_event() {
        let mut size = EventSize::default();
        assert!(
            size.observe(&vec![b'x'; 1_124_250], 16 * 1024 * 1024)
                .is_ok()
        );
        assert!(
            size.observe(&vec![b'x'; 16 * 1024 * 1024], 16 * 1024 * 1024)
                .is_err()
        );
        let mut size = EventSize::default();
        size.observe(b"data: x\r", 9).unwrap();
        size.observe(b"\n\r\n", 9).unwrap();
        size.observe(b"data: y\n\n", 9).unwrap();
    }
}

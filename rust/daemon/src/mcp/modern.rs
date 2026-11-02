//! MCP 2026-07-28: one request envelope per POST, without a transport session.
//! Oracle: mcp 2.2 `streamable_http_modern`, `inbound` and `ListenHandler`.

use super::*;
use base64::{Engine, engine::general_purpose::STANDARD};
use std::collections::BTreeMap;
use std::fmt::Write;
use std::sync::OnceLock;

const VERSION: &str = "2026-07-28";
const VERSION_KEY: &str = "io.modelcontextprotocol/protocolVersion";
const CAPS_KEY: &str = "io.modelcontextprotocol/clientCapabilities";
const SERVER_KEY: &str = "io.modelcontextprotocol/serverInfo";
const SUBSCRIPTION_KEY: &str = "io.modelcontextprotocol/subscriptionId";
const HEADER_MISMATCH: i64 = -32020;
const UNSUPPORTED_VERSION: i64 = -32022;
const INVALID_BODY: &str = "Body must be a single JSON-RPC request or notification object";

fn schema() -> &'static Value {
    static SCHEMA: OnceLock<Value> = OnceLock::new();
    SCHEMA.get_or_init(|| {
        serde_json::from_str(include_str!("modern-schema.json")).expect("SDK schema")
    })
}

fn error(id: &Value, code: i64, message: &str, data: Option<Value>) -> Value {
    let mut detail = json!({"code": code, "message": message});
    if let Some(data) = data {
        detail["data"] = data;
    }
    if id.is_null() {
        json!({"jsonrpc": "2.0", "error": detail, "id": null})
    } else {
        json!({"jsonrpc": "2.0", "id": id, "error": detail})
    }
}

fn response(value: Value) -> Response {
    let status = match value
        .get("error")
        .and_then(|e| e.get("code"))
        .and_then(Value::as_i64)
    {
        Some(
            PARSE_ERROR | INVALID_REQUEST | INVALID_PARAMS | HEADER_MISMATCH | -32021
            | UNSUPPORTED_VERSION,
        ) => 400,
        Some(METHOD_NOT_FOUND) => 404,
        _ => 200,
    };
    json_bytes(status, wire(&value).into_bytes(), None)
}

/// Modern HTTP uses json.dumps(ensure_ascii=True), including SSE messages.
fn wire(value: &Value) -> String {
    let mut out = String::new();
    for c in value.to_string().chars() {
        if c < '\u{7f}' {
            out.push(c);
        } else {
            for unit in c.encode_utf16(&mut [0; 2]) {
                write!(out, "\\u{unit:04x}").unwrap();
            }
        }
    }
    out
}

/// Legacy schema models move their named fields first. Restore the generated
/// schema's order for modern InputSchema (`$schema`, `type`, then extras)
/// and OutputSchema (`$schema`, then extras).
fn tool_schema_order(result: &mut Value) {
    for tool in result["tools"].as_array_mut().expect("tool catalogue") {
        for name in ["inputSchema", "outputSchema"] {
            let Some(schema) = tool.get_mut(name).and_then(Value::as_object_mut) else {
                continue;
            };
            let mut fields: BTreeMap<_, _> = std::mem::take(schema).into_iter().collect();
            for key in ["$schema", "type"]
                .into_iter()
                .take(if name == "inputSchema" { 2 } else { 1 })
            {
                if let Some(value) = fields.remove(key) {
                    schema.insert(key.into(), value);
                }
            }
            schema.extend(fields);
        }
    }
}

/// json.loads(bytes) detects UTF-8/16/32, with or without a byte-order mark.
fn decode_body(body: &[u8]) -> Option<String> {
    let (body, width, little) = if let Some(body) = body.strip_prefix(b"\xff\xfe\0\0") {
        (body, 4, true)
    } else if let Some(body) = body.strip_prefix(b"\0\0\xfe\xff") {
        (body, 4, false)
    } else if let Some(body) = body.strip_prefix(b"\xff\xfe") {
        (body, 2, true)
    } else if let Some(body) = body.strip_prefix(b"\xfe\xff") {
        (body, 2, false)
    } else if let Some(body) = body.strip_prefix(b"\xef\xbb\xbf") {
        (body, 1, false)
    } else if body.len() >= 4 && body[0] == 0 && body[1] == 0 {
        (body, 4, false)
    } else if body.len() >= 4 && body[1..4] == [0, 0, 0] {
        (body, 4, true)
    } else if body.len() >= 2 && body[0] == 0 {
        (body, 2, false)
    } else if body.len() >= 2 && body[1] == 0 {
        (body, 2, true)
    } else {
        (body, 1, false)
    };
    if body.len() % width != 0 {
        return None;
    }
    match width {
        2 => String::from_utf16(
            &body
                .chunks_exact(2)
                .map(|v| {
                    if little {
                        u16::from_le_bytes([v[0], v[1]])
                    } else {
                        u16::from_be_bytes([v[0], v[1]])
                    }
                })
                .collect::<Vec<_>>(),
        )
        .ok(),
        4 => body
            .chunks_exact(4)
            .map(|v| {
                char::from_u32(if little {
                    u32::from_le_bytes(v.try_into().unwrap())
                } else {
                    u32::from_be_bytes(v.try_into().unwrap())
                })
            })
            .collect(),
        _ => String::from_utf8(body.to_vec()).ok(),
    }
}

fn unsupported(id: &Value, requested: &str) -> Value {
    error(
        id,
        UNSUPPORTED_VERSION,
        "Unsupported protocol version",
        Some(json!({"supported": [VERSION], "requested": requested})),
    )
}

fn decode_name(raw: Option<String>) -> Option<String> {
    let raw = raw?;
    let Some(payload) = raw
        .strip_prefix("=?base64?")
        .and_then(|s| s.strip_suffix("?="))
    else {
        return Some(raw);
    };
    let bytes = STANDARD.decode(payload).ok()?;
    if STANDARD.encode(&bytes) != payload {
        return None;
    }
    String::from_utf8(bytes).ok()
}

fn envelope(
    id: &Value,
    method: &str,
    params: &Map<String, Value>,
    headers: &HeaderMap,
) -> Result<(), Value> {
    for key in ["mcp-protocol-version", "mcp-method", "mcp-name"] {
        if headers.get_all(key).iter().count() > 1 {
            return Err(error(
                id,
                HEADER_MISMATCH,
                &format!("{key} header appears more than once"),
                None,
            ));
        }
    }
    let Some(meta) = params.get("_meta").and_then(Value::as_object) else {
        return Err(error(
            id,
            INVALID_PARAMS,
            "params._meta must be an object carrying the required 'io.modelcontextprotocol/protocolVersion' and 'io.modelcontextprotocol/clientCapabilities' envelope keys",
            None,
        ));
    };
    let missing: Vec<_> = [VERSION_KEY, CAPS_KEY]
        .into_iter()
        .filter(|k| !meta.contains_key(*k))
        .collect();
    if !missing.is_empty() {
        return Err(error(
            id,
            INVALID_PARAMS,
            &format!(
                "params._meta is missing the required envelope key(s): {}",
                missing.join(", ")
            ),
            None,
        ));
    }
    let version = meta[VERSION_KEY].as_str();
    if version.is_none() || latin1(headers, "mcp-protocol-version").as_deref() != version {
        return Err(error(
            id,
            HEADER_MISMATCH,
            "mcp-protocol-version header does not match the request envelope's protocol version",
            None,
        ));
    }
    if latin1(headers, "mcp-method").as_deref() != Some(method) {
        return Err(error(
            id,
            HEADER_MISMATCH,
            "mcp-method header does not match the request body's method",
            None,
        ));
    }
    let name = match method {
        "tools/call" | "prompts/get" => Some("name"),
        "resources/read" => Some("uri"),
        _ => None,
    };
    if let Some(key) = name
        && let Some(value) = params.get(key).filter(|v| !v.is_null())
        && decode_name(latin1(headers, "mcp-name"))
            .map(Value::String)
            .as_ref()
            != Some(value)
    {
        return Err(error(
            id,
            HEADER_MISMATCH,
            &format!("mcp-name header does not match the request body's '{key}' parameter"),
            None,
        ));
    }
    if version != Some(VERSION) {
        return Err(unsupported(id, version.unwrap()));
    }
    Ok(())
}

fn validate(id: &Value, method: &str, params: &Map<String, Value>) -> Result<(), Value> {
    let schema = schema();
    let spec_method = schema["spec_methods"]
        .as_array()
        .unwrap()
        .iter()
        .any(|m| m == method);
    if spec_method && schema["requests"]["params"].get(method).is_none() {
        return Err(error(
            id,
            METHOD_NOT_FOUND,
            "Method not found",
            Some(json!(method)),
        ));
    }
    if validation::has_untrusted_state(method, params) {
        return Err(error(
            id,
            INVALID_PARAMS,
            "Invalid or expired requestState",
            Some(json!({"reason":"invalid_request_state"})),
        ));
    }
    for layer in ["requests", "handlers"] {
        if let Some(shape) = schema[layer]["params"].get(method)
            && !validation::valid(
                shape,
                &Value::Object(params.clone()),
                &schema[layer]["definitions"],
            )
        {
            return Err(error(
                id,
                INVALID_PARAMS,
                "Invalid request parameters",
                Some(json!("")),
            ));
        }
    }
    Ok(())
}

/// The per-version result sieve orders core fields, supplies cache defaults
/// for cacheable methods, and appends the server identity stamp.
fn stamp(method: &str, mut result: Value) -> Value {
    result["resultType"] = json!("complete");
    if matches!(
        method,
        "server/discover"
            | "tools/list"
            | "prompts/list"
            | "prompts/get"
            | "resources/list"
            | "resources/templates/list"
            | "resources/read"
    ) {
        result["cacheScope"] = json!("private");
        result["ttlMs"] = json!(0);
    }
    let fields: BTreeMap<_, _> = result
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    let mut result = Value::Object(fields.into_iter().collect());
    let meta = result
        .as_object_mut()
        .unwrap()
        .entry("_meta")
        .or_insert_with(|| json!({}));
    meta[SERVER_KEY] = json!({"name": "Pseudolife Memory", "version": ""});
    result
}

async fn dispatch(
    app: Arc<crate::http::App>,
    principal: String,
    headers: HeaderMap,
    id: Value,
    method: String,
    params: Map<String, Value>,
) -> Value {
    let mut out = if method == "server/discover" {
        json!({"jsonrpc":"2.0", "id":id, "result": {
            "capabilities": {"prompts":{"listChanged":true}, "resources":{"listChanged":true,"subscribe":true}, "tools":{"listChanged":true}},
            "instructions":INSTRUCTIONS, "supportedVersions":[VERSION]}})
    } else if method == "prompts/get" {
        // Python's handler raises ValueError; modern runner sanitizes it.
        error(&id, -32603, "Internal server error", None)
    } else {
        let data = method_response(&app, &principal, "", &headers, &id, &method, &params).await;
        serde_json::from_str(&data).expect("handler JSON-RPC response")
    };
    if let Some(result) = out.get_mut("result") {
        if method == "tools/list" {
            tool_schema_order(result);
        }
        *result = stamp(&method, result.take());
    }
    out
}

pub(super) async fn handle(
    app: &Arc<crate::http::App>,
    principal: &str,
    method: Method,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method != Method::POST {
        let mut r = plain(405, "");
        r.headers_mut()
            .insert(header::ALLOW, HeaderValue::from_static("POST"));
        return r;
    }
    if accepts(&headers) != (true, true) {
        return plain(406, "");
    }
    let raw = match decode_body(&body).and_then(|s| py_loads::loads(&s)) {
        Some(value) => value,
        None => return response(error(&Value::Null, PARSE_ERROR, "Parse error", None)),
    };
    if raw.as_object().is_some_and(|o| !o.contains_key("id")) {
        if !matches!(classify(&raw), Some(Message::Notification { .. })) {
            return response(error(&Value::Null, INVALID_REQUEST, INVALID_BODY, None));
        }
        let version = latin1(&headers, "mcp-protocol-version").unwrap_or_default();
        return if version == VERSION {
            plain(202, "")
        } else {
            response(unsupported(&Value::Null, &version))
        };
    }
    let Some(Message::Request { id, method, params }) = classify(&raw) else {
        return response(error(&Value::Null, INVALID_REQUEST, INVALID_BODY, None));
    };
    if let Err(error) =
        envelope(&id, &method, &params, &headers).and_then(|_| validate(&id, &method, &params))
    {
        return response(error);
    }
    // None of this daemon's input schemas declares x-mcp-header: unbound
    // Mcp-Param-* headers are ignored by the SDK as well.
    if method == "subscriptions/listen" {
        return app.mcp.modern.listen(id, params).await;
    }
    // Shield synchronous tool work from a disconnect, just like Python's
    // anyio worker threads; only the HTTP response stops being delivered.
    let mut work = tokio::spawn(dispatch(
        app.clone(),
        principal.to_string(),
        headers,
        id,
        method,
        params,
    ));
    tokio::select! {
        done = &mut work => response(done.expect("request task")),
        _ = tokio::time::sleep(PING_EVERY) => {
            let stream = async_stream(move |tx| async move {
                let _ = tx.send(Ok(Bytes::from_static(b": ping\r\n\r\n"))).await;
                loop {
                    tokio::select! {
                        done = &mut work => {
                            if let Ok(value) = done { let _ = tx.send(Ok(sse_event(&wire(&value)))).await; }
                            break;
                        },
                        _ = tx.closed() => break,
                        _ = tokio::time::sleep(PING_EVERY) => {
                            if tx.send(Ok(Bytes::from_static(b": ping\r\n\r\n"))).await.is_err() { break; }
                        }
                    }
                }
            });
            stream_response(Body::from_stream(stream))
        }
    }
}

fn stream_response(body: Body) -> Response {
    let mut r = Response::new(body);
    sse_headers(&mut r, "");
    r.headers_mut().remove("mcp-session-id");
    r
}

#[derive(Default)]
pub(super) struct Subscriptions {
    slots: Mutex<(u64, BTreeMap<u64, Listener>)>,
}

struct Listener {
    tools: bool,
    tx: mpsc::Sender<()>,
}

fn enabled(v: &Value) -> bool {
    v.as_bool() == Some(true)
        || v.as_f64() == Some(1.0)
        || v.as_str().is_some_and(|s| {
            matches!(
                s.to_ascii_lowercase().as_str(),
                "1" | "on" | "t" | "true" | "y" | "yes"
            )
        })
}

impl Subscriptions {
    pub(super) fn tools_changed(&self) {
        let mut slots = self.slots.lock().expect("subscription lock");
        slots.1.retain(|_, listener| {
            !listener.tx.is_closed() && (!listener.tools || listener.tx.try_send(()).is_ok())
        });
    }

    async fn listen(self: &Arc<Self>, id: Value, params: Map<String, Value>) -> Response {
        let requested = params["notifications"]
            .as_object()
            .expect("validated filter");
        let mut honored = Map::new();
        for key in [
            "promptsListChanged",
            "resourceSubscriptions",
            "resourcesListChanged",
            "toolsListChanged",
        ] {
            if let Some(v) = requested.get(key)
                && (enabled(v) || v.as_array().is_some_and(|a| !a.is_empty()))
            {
                honored.insert(
                    key.to_string(),
                    if v.is_array() { v.clone() } else { json!(true) },
                );
            }
        }
        let tools = honored.get("toolsListChanged") == Some(&json!(true));
        let (tx, mut rx) = mpsc::channel(1024);
        let token = {
            let mut slots = self.slots.lock().expect("subscription lock");
            slots.1.retain(|_, listener| !listener.tx.is_closed());
            if slots.1.len() >= 1024 {
                return response(error(&id, -32603, "Subscription limit reached", None));
            }
            slots.0 += 1;
            let token = slots.0;
            slots.1.insert(token, Listener { tools, tx });
            token
        };
        let guard = SubscriptionGuard(self.clone(), token);
        let meta = json!({SUBSCRIPTION_KEY: id});
        let ack = json!({"jsonrpc":"2.0", "method":"notifications/subscriptions/acknowledged",
                         "params":{"_meta":meta, "notifications":honored}});
        let stream = async_stream(move |tx| async move {
            let _guard = guard;
            if tx.send(Ok(sse_event(&wire(&ack)))).await.is_err() {
                return;
            }
            loop {
                tokio::select! {
                    event = rx.recv() => {
                        if event.is_none() {
                            let result = stamp("subscriptions/listen", json!({"_meta":meta}));
                            let value = json!({"jsonrpc":"2.0", "id":id, "result":result});
                            let _ = tx.send(Ok(sse_event(&wire(&value)))).await;
                            break;
                        }
                        if tools {
                            let message = json!({"jsonrpc":"2.0", "method":"notifications/tools/list_changed", "params":{"_meta":meta}});
                            if tx.send(Ok(sse_event(&wire(&message)))).await.is_err() { break; }
                        }
                    },
                    _ = tx.closed() => break,
                    _ = tokio::time::sleep(PING_EVERY) => {
                        if tx.send(Ok(Bytes::from_static(b": ping\r\n\r\n"))).await.is_err() { break; }
                    }
                }
            }
        });
        stream_response(Body::from_stream(stream))
    }
}

struct SubscriptionGuard(Arc<Subscriptions>, u64);
impl Drop for SubscriptionGuard {
    fn drop(&mut self) {
        self.0
            .slots
            .lock()
            .expect("subscription lock")
            .1
            .remove(&self.1);
    }
}

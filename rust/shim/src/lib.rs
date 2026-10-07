#![deny(unsafe_code)]

pub mod board;
pub mod cache;
pub mod cli;
pub mod credentials;
pub mod daemon_url;
mod errors;
pub mod lifecycle;
pub mod maintainer_sent;
mod owned_transport;
mod raw_http;
mod recovery;
pub mod sent_json;
mod subscriptions;
mod upstream;
mod wire_compatibility;
mod wire_ids;
mod wire_json;

use rmcp::{
    ErrorData, RoleServer, ServerHandler, ServiceExt, model::*, service::RequestContext,
    transport::async_rw::AsyncRwTransport,
};
pub use upstream::Ownership;
use upstream::Upstream;

#[derive(Clone)]
pub struct Proxy {
    upstream: Upstream,
    instructions: Option<String>,
    subscriptions: subscriptions::Subscriptions,
    link: recovery::Link,
    channel: bool,
    board: std::sync::Arc<board::Board>,
}

impl Proxy {
    async fn prepared_call(
        &self,
        mut params: CallToolRequestParams,
        context: &RequestContext<RoleServer>,
    ) -> Result<(CallToolResult, Option<serde_json::Value>), ErrorData> {
        let deadline = tokio::time::Instant::now() + upstream::operation_timeout();
        let meta = serde_json::to_value(&context.meta).map_err(|_| {
            ErrorData::internal_error("request metadata serialization failed", None)
        })?;
        let prepared = preparation_before(
            deadline,
            &self.upstream.ownership,
            &context.ct,
            self.board
                .prepare_call(&params.name, params.arguments.take(), &meta),
        )
        .await?;
        let prepared = match prepared {
            board::Preparation::Result(result) => return Ok((result, None)),
            board::Preparation::Forward(prepared) => prepared,
        };
        params.arguments = Some(prepared.arguments.clone());
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        if remaining.is_zero() {
            return Err(errors::failure(
                "timeout",
                "initialize",
                false,
                ErrorCode::INTERNAL_ERROR,
            ));
        }
        let (mut result, mut raw) = self
            .upstream
            .call_tool_with_timeout(
                params,
                context.ct.clone(),
                Some(prepared.operation.clone()),
                remaining,
            )
            .await?;
        let hint = finish_owned(
            &self.upstream.ownership,
            &context.ct,
            self.board.finish_call(&prepared, &mut result),
        )
        .await?;
        if let Some(hint) = hint
            && let Some(raw) = raw.as_mut()
        {
            if let Some(content) = raw
                .get_mut("content")
                .and_then(serde_json::Value::as_array_mut)
            {
                content.push(serde_json::json!({"type":"text","text":hint}));
            }
            if let Some(structured) = raw
                .get_mut("structuredContent")
                .and_then(serde_json::Value::as_object_mut)
            {
                structured
                    .entry("coordination_hint".to_owned())
                    .or_insert(serde_json::Value::String(hint));
            }
        }
        Ok((result, raw))
    }
    pub fn ownership(&self) -> Ownership {
        self.upstream.ownership.clone()
    }

    pub fn runtime(&self) -> std::sync::Arc<lifecycle::Runtime> {
        self.upstream.runtime.clone()
    }
    pub fn board(&self) -> std::sync::Arc<board::Board> {
        self.board.clone()
    }
    pub async fn attach() -> Result<Self, lifecycle::StartupError> {
        Self::attach_mode(false).await
    }
    pub async fn attach_mode(channel: bool) -> Result<Self, lifecycle::StartupError> {
        let runtime = std::sync::Arc::new(lifecycle::Runtime::from_environment().await?);
        let upstream = Upstream::new(runtime.clone());
        let (board, fetched) = tokio::join!(
            board::Board::attach(runtime.clone(), channel),
            async {
                if runtime.daemon_unreachable() {
                    None
                } else {
                    match upstream.instructions().await {
                        Ok(instructions) => {
                            if let Some(cache) = &runtime.cache {
                                cache.remember_instructions(instructions.as_deref());
                            }
                            instructions
                        }
                        Err(_error) => {
                            crate::stderrln!(
                                "pseudolife-mcp: instructions unavailable (MCPError); check daemon MCP access and reconnect for startup guidance."
                            );
                            None
                        }
                    }
                }
            }
        );
        let instructions = proxy_instructions(
            &runtime,
            fetched,
            board.board_checkin().await,
            board.instructions_note(),
            channel,
        );
        let subscriptions = subscriptions::Subscriptions::default();
        let link = recovery::Link::new(runtime, upstream.ownership.clone(), subscriptions.clone());
        if !link.up() {
            link.lost();
        }
        Ok(Self {
            upstream,
            instructions,
            subscriptions,
            link,
            channel,
            board,
        })
    }
}

fn proxy_instructions(
    runtime: &lifecycle::Runtime,
    fetched: Option<String>,
    board_ready: bool,
    board_note: &str,
    channel: bool,
) -> Option<String> {
    let instructions_note = [runtime.instructions_note.as_str(), board_note]
        .into_iter()
        .filter(|note| !note.is_empty())
        .collect::<Vec<_>>()
        .join("\n\n");
    let instructions = lifecycle::Runtime {
        url: runtime.url.clone(),
        provider: runtime.provider.clone(),
        session: runtime.session.clone(),
        health: runtime.health.clone(),
        cache: runtime.cache.clone(),
        instructions_note,
    };
    instructions.instructions(fetched, board_ready, board::policy::CHECKIN, channel)
}

async fn preparation_before<T>(
    deadline: tokio::time::Instant,
    ownership: &Ownership,
    cancelled: &tokio_util::sync::CancellationToken,
    preparation: impl Future<Output = Result<T, ErrorData>>,
) -> Result<T, ErrorData> {
    tokio::select! {
        biased;
        _ = ownership.cancelled() => Err(Ownership::closed_error()),
        _ = cancelled.cancelled() => Err(ErrorData::internal_error("request cancelled", None)),
        result = tokio::time::timeout_at(deadline, preparation) => result
            .map_err(|_| errors::failure("timeout", "initialize", false, ErrorCode::INTERNAL_ERROR))?
            .map_err(|mut error| {
                if let Some(data) = error.data.as_mut()
                    && data["classification"] == "credential_unavailable"
                    && data["operation_outcome"] == "not_dispatched"
                {
                    data["phase"] = serde_json::json!("initialize");
                }
                error
            }),
    }
}

async fn finish_owned<T>(
    ownership: &Ownership,
    cancelled: &tokio_util::sync::CancellationToken,
    finish: impl Future<Output = T>,
) -> Result<T, ErrorData> {
    // Python appends the post-response hint after leaving fail_after; it is
    // still owned by the downstream request and EOF cancellation.
    tokio::select! {
        biased;
        _ = ownership.cancelled() => Err(Ownership::closed_error()),
        _ = cancelled.cancelled() => Err(ErrorData::internal_error("request cancelled", None)),
        result = finish => Ok(result),
    }
}

impl ServerHandler for Proxy {
    fn get_info(&self) -> ServerConfig {
        let mut capabilities = ServerCapabilities::builder()
            .enable_tools()
            .enable_tool_list_changed()
            .build();
        capabilities.experimental = Some(if self.channel {
            std::collections::BTreeMap::from([(
                "claude/channel".to_owned(),
                serde_json::Map::new(),
            )])
        } else {
            Default::default()
        });
        let mut info = ServerConfig::new(capabilities)
            .with_protocol_version(ProtocolVersion::V_2025_11_25)
            .with_server_info(Implementation::new("pseudolife-memory", ""));
        info.instructions = self.instructions.clone();
        info
    }

    async fn discover(&self, _: RequestContext<RoleServer>) -> Result<DiscoverResult, ErrorData> {
        let mut info = self.get_info();
        info.capabilities.experimental = if self.channel {
            Some(std::collections::BTreeMap::from([(
                "claude/channel".to_owned(),
                serde_json::Map::new(),
            )]))
        } else {
            None
        };
        Ok(DiscoverResult::from_server_info(
            vec![ProtocolVersion::V_2026_07_28],
            info,
        ))
    }

    async fn list_tools(
        &self,
        params: Option<PaginatedRequestParams>,
        context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, ErrorData> {
        let modern = context.protocol_version() == Some(ProtocolVersion::V_2026_07_28);
        self.link.remember(context.peer.clone(), modern);
        let mut params = params.unwrap_or_default();
        // rmcp separates request metadata into the context while decoding.
        // Python forwards that metadata alongside the pagination cursor.
        params.meta = Some(context.meta.clone());
        let cursorless = params
            .cursor
            .as_ref()
            .is_none_or(|cursor| cursor.is_empty());
        let (mut result, raw) = if !self.link.up() {
            self.link.lost();
            self.link.cached()
        } else {
            match self
                .upstream
                .list_tools(
                    Some(params),
                    context.ct,
                    self.board.operation_context().await.ok(),
                )
                .await
            {
                Ok((result, raw)) => {
                    // Only a successful MCP operation resets recovery backoff.
                    self.link.request_succeeded().await;
                    if cursorless && let Some(cache) = &self.upstream.runtime.cache {
                        cache.remember_tools(
                            raw.as_ref()
                                .and_then(|raw| raw.get("tools"))
                                .and_then(serde_json::Value::as_array)
                                .cloned()
                                .unwrap_or_default(),
                        );
                    }
                    (result, raw)
                }
                Err(error) if errors::never_reached(&error) => {
                    self.link.lost();
                    self.link.cached()
                }
                Err(error) => return Err(error),
            }
        };
        if let Some(schemas) = context
            .extensions
            .get::<wire_compatibility::RawToolSchemas>()
        {
            *schemas.0.lock().expect("schema lock poisoned") = raw
                .as_ref()
                .and_then(|raw| raw.get("tools"))
                .and_then(serde_json::Value::as_array)
                .cloned()
                .unwrap_or_default();
        }
        if modern {
            if let Some(shape) = context.extensions.get::<wire_compatibility::ResultShape>() {
                *shape.0.lock().expect("result shape lock poisoned") = result.meta.is_none();
            }
            result.result_type = Some(ResultType::COMPLETE);
            result.ttl_ms.get_or_insert(0);
            result.cache_scope.get_or_insert(CacheScope::Private);
            let mut meta = result.meta.unwrap_or_default();
            let key = "io.modelcontextprotocol/serverInfo".to_owned();
            if meta.get(&key).is_none_or(serde_json::Value::is_null) {
                meta.insert(
                    key,
                    serde_json::to_value(self.get_info().server_info).map_err(|_| {
                        ErrorData::internal_error("server metadata serialization failed", None)
                    })?,
                );
            }
            result.meta = Some(meta);
        } else {
            result.ttl_ms = None;
            result.cache_scope = None;
        }
        Ok(result)
    }

    async fn call_tool(
        &self,
        params: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {
        let toolset = params.name == "memory_toolset";
        let modern = context.protocol_version() == Some(ProtocolVersion::V_2026_07_28);
        self.link.remember(context.peer.clone(), modern);
        let (mut result, raw) = match self.prepared_call(params, &context).await {
            Ok(result) => {
                self.link.request_succeeded().await;
                result
            }
            Err(mut error) if errors::never_reached(&error) => {
                self.link.lost();
                error.message = lifecycle::DAEMON_UNREACHABLE_MESSAGE.into();
                return Err(error);
            }
            Err(error) => return Err(error),
        };
        if let Some(priorities) = context
            .extensions
            .get::<wire_compatibility::ContentPriorities>()
        {
            *priorities.0.lock().expect("priority lock poisoned") = raw
                .as_ref()
                .and_then(|value| value.get("content"))
                .and_then(serde_json::Value::as_array)
                .map(|content| {
                    content
                        .iter()
                        .map(|block| {
                            block
                                .get("annotations")?
                                .get("priority")?
                                .as_f64()
                                .map(|value| serde_json::json!(value))
                        })
                        .collect()
                })
                .unwrap_or_default();
        }
        if toolset && result.is_error != Some(true) && toolset_changed(&result) {
            self.link
                .notify((!modern).then_some(context.peer.clone()))
                .await;
        }
        if modern {
            if let Some(shape) = context.extensions.get::<wire_compatibility::ResultShape>() {
                *shape.0.lock().expect("result shape lock poisoned") = result.meta.is_none();
            }
            result.result_type = Some(ResultType::COMPLETE);
            let mut meta = result.meta.unwrap_or_default();
            let key = "io.modelcontextprotocol/serverInfo".to_owned();
            if meta.get(&key).is_none_or(serde_json::Value::is_null) {
                meta.insert(
                    key,
                    serde_json::to_value(self.get_info().server_info).map_err(|_| {
                        ErrorData::internal_error("server metadata serialization failed", None)
                    })?,
                );
            }
            result.meta = Some(meta);
        }
        Ok(result.into())
    }

    async fn on_custom_request(
        &self,
        request: CustomRequest,
        context: RequestContext<RoleServer>,
    ) -> Result<CustomResult, ErrorData> {
        if request.method != "subscriptions/listen"
            || context.protocol_version() != Some(ProtocolVersion::V_2026_07_28)
        {
            return Err(ErrorData::new(
                ErrorCode::METHOD_NOT_FOUND,
                "Method not found",
                Some(serde_json::json!(request.method)),
            ));
        }
        let params = request
            .params_as::<SubscriptionsListenRequestParams>()
            .map_err(|_| ErrorData::invalid_params("invalid subscription filter", None))?
            .ok_or_else(|| ErrorData::invalid_params("missing subscription filter", None))?;
        let mut result = self
            .subscriptions
            .listen(params.notifications, context, self.ownership())
            .await?;
        result.0["_meta"]["io.modelcontextprotocol/serverInfo"] =
            serde_json::to_value(self.get_info().server_info).map_err(|_| {
                ErrorData::internal_error("server metadata serialization failed", None)
            })?;
        Ok(result)
    }
}

fn toolset_changed(result: &CallToolResult) -> bool {
    if let Some(structured) = result
        .structured_content
        .as_ref()
        .and_then(serde_json::Value::as_object)
        .filter(|object| !object.is_empty())
    {
        let target = structured
            .get("result")
            .and_then(serde_json::Value::as_object)
            .unwrap_or(structured);
        return target.get("changed").and_then(serde_json::Value::as_bool) == Some(true);
    }
    for block in &result.content {
        if let ContentBlock::Text(text) = block
            && let Ok(object) = serde_json::from_str::<serde_json::Value>(&text.text)
            && let Some(object) = object.as_object()
        {
            return object.get("changed").and_then(serde_json::Value::as_bool) == Some(true);
        }
    }
    false
}

pub async fn serve(handler: impl ServerHandler, ownership: Ownership) -> Result<(), &'static str> {
    serve_after_first_frame(handler, ownership, || {}).await
}

pub async fn serve_after_first_frame(
    handler: impl ServerHandler,
    ownership: Ownership,
    first_frame: impl FnOnce() + Send + 'static,
) -> Result<(), &'static str> {
    let (output_read, output_write) = tokio::io::duplex(64 * 1024);
    let ids = wire_ids::Ids::default();
    let output = tokio::spawn(wire_json::output_after_first_frame(
        output_read,
        tokio::io::stdout(),
        ids.clone(),
        first_frame,
    ));
    let transport = owned_transport::OwnedTransport {
        inner: AsyncRwTransport::new_server(
            wire_ids::Input::new(tokio::io::stdin(), ids.clone()),
            output_write,
        ),
        ownership: ownership.clone(),
    };
    let result = match wire_compatibility::WireCompatibility(handler)
        .serve(transport)
        .await
    {
        Ok(service) => service
            .waiting()
            .await
            .map(|_| ())
            .map_err(|_| "downstream MCP shutdown failed"),
        Err(rmcp::service::ServerInitializeError::ConnectionClosed(_)) if ownership.is_closed() => {
            Ok(())
        }
        Err(_) => Err("downstream MCP startup failed"),
    };
    ownership.shutdown().await;
    output
        .await
        .map_err(|_| "downstream MCP output failed")?
        .map_err(|_| "downstream MCP output failed")?;
    result
}

/// Match Python text stderr framing while leaving diagnostic content unchanged.
pub fn stderr_line(message: std::fmt::Arguments<'_>) {
    use std::io::Write;
    let message = message.to_string();
    let message = if cfg!(windows) {
        message.replace("\r\n", "\n").replace('\n', "\r\n")
    } else {
        message
    };
    let mut stderr = std::io::stderr().lock();
    let _ = stderr.write_all(message.as_bytes());
    let _ = stderr.write_all(if cfg!(windows) { b"\r\n" } else { b"\n" });
}
#[macro_export]
macro_rules! stderrln {
    ($($arg:tt)*) => { $crate::stderr_line(format_args!($($arg)*)) };
}

#[cfg(test)]
mod integration_tests {
    use super::*;
    use serde_json::json;
    use std::sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    };
    use std::time::Duration;
    use tokio_util::sync::CancellationToken;
    #[test]
    fn board_checkin_and_shared_note_are_assembled_before_channel_safety() {
        let runtime = lifecycle::Runtime {
            url: "http://fixture".into(),
            provider: credentials::CredentialProvider::new(None, None).unwrap(),
            session: lifecycle::SessionIdentity::new(None, None),
            health: Some(json!({"status":"ok"})),
            cache: None,
            instructions_note: "version note".into(),
        };
        assert_eq!(
            proxy_instructions(&runtime, Some("workflow".into()), true, "", false),
            Some(format!(
                "version note\n\nworkflow {}",
                board::policy::CHECKIN
            ))
        );
        assert_eq!(
            proxy_instructions(
                &runtime,
                Some("workflow".into()),
                false,
                board::policy::SHARED_NOTE,
                true
            ),
            Some(format!(
                "version note\n\n{}\n\nworkflow{}",
                board::policy::SHARED_NOTE,
                lifecycle::CHANNEL_SAFETY
            ))
        );
        assert_eq!(
            proxy_instructions(
                &runtime,
                Some("memory_agents already".into()),
                true,
                "",
                false
            ),
            Some("version note\n\nmemory_agents already".into())
        );
    }
    #[tokio::test]
    async fn preparation_timeout_cannot_finish_or_dispatch_after_the_deadline() {
        let prepared = Arc::new(AtomicBool::new(false));
        let observed = prepared.clone();
        let result = preparation_before(
            tokio::time::Instant::now() + Duration::from_millis(15),
            &Ownership::default(),
            &CancellationToken::new(),
            async move {
                tokio::time::sleep(Duration::from_millis(60)).await;
                observed.store(true, Ordering::SeqCst);
                Ok(())
            },
        )
        .await;
        assert_eq!(
            result.unwrap_err().data,
            Some(
                json!({"classification":"timeout","phase":"initialize","operation_outcome":"not_dispatched"})
            )
        );
        tokio::time::sleep(Duration::from_millis(70)).await;
        assert!(!prepared.load(Ordering::SeqCst));
    }
    #[tokio::test]
    async fn post_response_hint_is_outside_prepare_deadline_but_cancellation_owned() {
        let owner = Ownership::default();
        let cancelled = CancellationToken::new();
        let deadline = tokio::time::Instant::now() + Duration::from_millis(5);
        preparation_before(deadline, &owner, &cancelled, async { Ok(()) })
            .await
            .unwrap();
        let hint = finish_owned(&owner, &cancelled, async {
            tokio::time::sleep(Duration::from_millis(20)).await;
            "hint"
        })
        .await
        .unwrap();
        assert_eq!(hint, "hint");
        assert!(tokio::time::Instant::now() > deadline);
        owner.cancel();
        let completed = AtomicBool::new(false);
        let result = finish_owned(&owner, &cancelled, async {
            completed.store(true, Ordering::SeqCst)
        })
        .await;
        assert_eq!(result.unwrap_err(), Ownership::closed_error());
        assert!(!completed.load(Ordering::SeqCst));
    }
    #[tokio::test]
    async fn only_undispatched_credential_preparation_errors_use_initialize_phase() {
        for (classification, outcome, expected) in [
            ("credential_unavailable", "not_dispatched", "initialize"),
            ("credential_unavailable", "unknown", "call"),
            ("coordination_unavailable", "not_dispatched", "call"),
        ] {
            let result: Result<(),ErrorData> = preparation_before(tokio::time::Instant::now() + Duration::from_secs(1), &Ownership::default(), &CancellationToken::new(), async {Err(ErrorData::internal_error("fixture",Some(json!({"classification":classification,"phase":"call","operation_outcome":outcome}))))}).await;
            assert_eq!(
                result.unwrap_err().data.unwrap(),
                json!({"classification":classification,"phase":expected,"operation_outcome":outcome})
            );
        }
    }
}
